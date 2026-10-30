"""Global hotkeys and audible feedback that work while osu! has the focus.

* :class:`HotkeyManager` registers system-wide hotkeys with Win32
  ``RegisterHotKey`` on a dedicated thread that runs its own message loop
  (``WM_HOTKEY`` is posted to the registering thread). A registration that
  fails (another program owns the combination) is reported as a conflict so
  the user can pick another one. Callbacks run on that thread: the companion
  forwards them through a queue to Tk.
* :class:`Beeper` plays a short, distinct tone pattern per action with
  ``winsound.Beep`` on its own worker thread, because an exclusive-fullscreen
  game hides any overlay window.

The OS layer is a small backend interface so everything is testable with a
fake backend (no real hotkeys are registered in tests).
"""
from __future__ import annotations

import queue
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

MODIFIERS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT,
             "win": MOD_WIN}
VK: dict[str, int] = {
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "pageup": 0x21, "pagedown": 0x22,
    "home": 0x24, "end": 0x23, "insert": 0x2D, "delete": 0x2E, "space": 0x20, "enter": 0x0D,
    "tab": 0x09, "backspace": 0x08, "plus": 0xBB, "minus": 0xBD, "comma": 0xBC, "period": 0xBE,
    **{chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)},
    **{str(d): 0x30 + d for d in range(10)},
    **{f"num{d}": 0x60 + d for d in range(10)},
    **{f"f{n}": 0x6F + n for n in range(1, 25)},
}
ACTIONS = ("smoother", "responsive", "precise", "looser", "area_bigger", "area_smaller", "undo", "keep")
DEFAULT_BINDINGS: dict[str, str] = {
    "smoother": "ctrl+alt+up", "responsive": "ctrl+alt+down",
    "precise": "ctrl+alt+left", "looser": "ctrl+alt+right",
    "area_bigger": "ctrl+alt+pageup", "area_smaller": "ctrl+alt+pagedown",
    "undo": "ctrl+alt+z", "keep": "ctrl+alt+k",
}


class HotkeyError(ValueError):
    """A hotkey string could not be parsed."""


@dataclass(frozen=True)
class Hotkey:
    modifiers: int
    vk: int
    text: str


def parse_hotkey(text: str) -> Hotkey:
    """"ctrl+alt+up" -> Hotkey. Needs at least one modifier and exactly one key."""
    parts = [p.strip().lower() for p in str(text).replace(" ", "").split("+") if p.strip()]
    mods, keys = 0, []
    for part in parts:
        if part in MODIFIERS:
            mods |= MODIFIERS[part]
        elif part in VK:
            keys.append(part)
        else:
            raise HotkeyError(f"unknown key {part!r} in {text!r}")
    if len(keys) != 1 or not mods:
        raise HotkeyError(f"{text!r}: use modifier(s) + one key, e.g. ctrl+alt+up")
    names = [n for n, m in (("ctrl", MOD_CONTROL), ("alt", MOD_ALT), ("shift", MOD_SHIFT), ("win", MOD_WIN))
             if mods & m]
    return Hotkey(mods, VK[keys[0]], "+".join(names + keys))


def parse_bindings(bindings: Mapping[str, str]) -> tuple[dict[str, Hotkey], dict[str, str]]:
    """Valid hotkeys per action plus errors (bad text or a combination used twice)."""
    good: dict[str, Hotkey] = {}
    errors: dict[str, str] = {}
    used: dict[tuple[int, int], str] = {}
    for action, text in bindings.items():
        if not text:
            continue
        try:
            hk = parse_hotkey(text)
        except HotkeyError as exc:
            errors[action] = str(exc)
            continue
        key = (hk.modifiers, hk.vk)
        if key in used:
            errors[action] = f"duplicate of {used[key]}"
            continue
        used[key] = action
        good[action] = hk
    return good, errors


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------
class HotkeyBackend(Protocol):
    def run(self, hotkeys: Mapping[int, Hotkey], on_registered: Callable[[dict[int, int]], None],
            on_hotkey: Callable[[int], None], stop: threading.Event) -> None:
        """Register (report id -> error code, 0 = ok), deliver presses until ``stop``, unregister."""

    def wake(self) -> None:
        """Make a running :meth:`run` return soon."""


class Win32HotkeyBackend:
    """``RegisterHotKey(NULL, ...)`` + ``GetMessageW`` loop on the calling thread."""

    def __init__(self) -> None:
        self._thread_id: int | None = None

    def run(self, hotkeys: Mapping[int, Hotkey], on_registered: Callable[[dict[int, int]], None],
            on_hotkey: Callable[[int], None], stop: threading.Event) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32")
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
                                        wintypes.UINT, wintypes.UINT]
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)   # creates this thread's message queue
        self._thread_id = kernel32.GetCurrentThreadId()
        status: dict[int, int] = {}
        for hid, hk in hotkeys.items():
            ok = user32.RegisterHotKey(None, hid, hk.modifiers | MOD_NOREPEAT, hk.vk)
            status[hid] = 0 if ok else (ctypes.get_last_error() or ERROR_HOTKEY_ALREADY_REGISTERED)
        try:
            on_registered(status)
            while not stop.is_set():
                got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got in (0, -1):   # WM_QUIT or error
                    break
                if msg.message == WM_HOTKEY:
                    on_hotkey(int(msg.wParam))
        finally:
            for hid, code in status.items():
                if code == 0:
                    user32.UnregisterHotKey(None, hid)
            self._thread_id = None

    def wake(self) -> None:
        if self._thread_id is None:
            return
        import ctypes
        ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)


def default_backend() -> HotkeyBackend | None:
    """The Win32 backend on Windows, None elsewhere (hotkeys unavailable)."""
    return Win32HotkeyBackend() if sys.platform == "win32" else None


class HotkeyManager:
    """Owns the hotkey thread; ``on_action(action)`` is called on that thread."""

    def __init__(self, bindings: Mapping[str, str], on_action: Callable[[str], None],
                 backend: HotkeyBackend | None = None, use_default_backend: bool = True) -> None:
        self.bindings = dict(bindings)
        self.on_action = on_action
        self.backend = backend if backend is not None else (default_backend() if use_default_backend else None)
        self.hotkeys, self.errors = parse_bindings(self.bindings)
        self.registered: dict[str, bool] = {}
        self.conflicts: list[str] = []
        self._ids = {i + 1: action for i, action in enumerate(self.hotkeys)}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()

    @property
    def available(self) -> bool:
        return self.backend is not None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, timeout_s: float = 2.0) -> dict[str, bool]:
        """Register everything; returns action -> registered. Conflicts land in ``conflicts``."""
        if self.backend is None or self.running:
            return dict(self.registered)
        self._stop.clear()
        self._ready.clear()
        by_id = {hid: self.hotkeys[action] for hid, action in self._ids.items()}
        self._thread = threading.Thread(target=self._run, args=(by_id,), name="hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(timeout_s)
        return dict(self.registered)

    def _run(self, by_id: Mapping[int, Hotkey]) -> None:
        def registered(status: dict[int, int]) -> None:
            self.registered = {self._ids[h]: code == 0 for h, code in status.items()}
            self.conflicts = [a for a, ok in self.registered.items() if not ok]
            self._ready.set()

        def pressed(hid: int) -> None:
            action = self._ids.get(hid)
            if action is not None:
                try:
                    self.on_action(action)
                except Exception:   # a callback bug must not kill the hotkey thread
                    pass

        try:
            self.backend.run(by_id, registered, pressed, self._stop)   # type: ignore[union-attr]
        finally:
            self._ready.set()

    def stop(self, timeout_s: float = 1.0) -> None:
        """Unregister and end the thread; safe to call repeatedly."""
        thread, self._thread = self._thread, None
        if thread is None:
            return
        self._stop.set()
        if self.backend is not None:
            self.backend.wake()
        thread.join(timeout_s)

    def rebind(self, bindings: Mapping[str, str]) -> dict[str, bool]:
        """Stop, take new bindings, start again."""
        self.stop()
        self.bindings = dict(bindings)
        self.hotkeys, self.errors = parse_bindings(self.bindings)
        self.registered, self.conflicts = {}, []
        self._ids = {i + 1: action for i, action in enumerate(self.hotkeys)}
        return self.start()


# --------------------------------------------------------------------------
# Sounds
# --------------------------------------------------------------------------
PATTERNS: dict[str, tuple[tuple[int, int], ...]] = {   # (frequency Hz, duration ms)
    "smoother": ((520, 70), (390, 90)),
    "responsive": ((660, 60), (880, 80)),
    "precise": ((600, 50), (600, 50)),
    "looser": ((600, 120),),
    "area_bigger": ((500, 50), (650, 50), (800, 60)),
    "area_smaller": ((800, 50), (650, 50), (500, 60)),
    "undo": ((330, 160),),
    "keep": ((523, 70), (659, 70), (784, 120)),
    "limit": ((260, 70), (260, 70)),
    "converged": ((784, 80), (1047, 160)),
    "error": ((200, 300),),
}


def _winsound_beep() -> Callable[[int, int], None] | None:
    try:
        import winsound
    except ImportError:
        return None
    return winsound.Beep


class Beeper:
    """Plays :data:`PATTERNS` without blocking the caller (one worker thread)."""

    def __init__(self, beep: Callable[[int, int], None] | None = None, enabled: bool = True,
                 threaded: bool = True) -> None:
        self._beep = beep if beep is not None else _winsound_beep()
        self.enabled = enabled and self._beep is not None
        self._threaded = threaded
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=8)
        self._thread: threading.Thread | None = None

    def play(self, kind: str) -> None:
        """Queue the pattern of ``kind`` (unknown kinds and a full queue are ignored)."""
        if not self.enabled or kind not in PATTERNS:
            return
        if not self._threaded:
            self._play_now(kind)
            return
        if self._thread is None:
            self._thread = threading.Thread(target=self._worker, name="beeper", daemon=True)
            self._thread.start()
        try:
            self._queue.put_nowait(kind)
        except queue.Full:
            pass

    def _play_now(self, kind: str) -> None:
        for freq, ms in PATTERNS[kind]:
            try:
                self._beep(freq, ms)   # type: ignore[misc]
            except (RuntimeError, OSError):
                return

    def _worker(self) -> None:
        while True:
            kind = self._queue.get()
            if kind is None:
                return
            self._play_now(kind)

    def close(self) -> None:
        if self._thread is not None:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
            self._thread = None
