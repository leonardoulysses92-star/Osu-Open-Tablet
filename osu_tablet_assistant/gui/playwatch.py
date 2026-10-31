"""Records a play for the Play Coach: cursor samples, click times, and osu!'s window title.

What it reads, and nothing else:

* the OS cursor position (``PenSampler``, ~1.5 kHz);
* the pressed/released state of the CONFIGURED click keys only (default Z and X; the player picks them,
  see ``osu_tablet_assistant.keys``), through ``GetAsyncKeyState``. No other key is ever queried, there is
  no global keyboard hook, and only press times (with the key's name) are stored. The pen tip (``mouse1``)
  is polled only when the player said they click with it;
* the title of osu!'s own window (windows of other programs are not read).

Nothing leaves the PC. osu!'s memory is never read. Every OS-facing source is injectable so the
logic runs (and is tested) without Windows.
"""
from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Collection, Mapping, Sequence

from ..engine.playtypes import ClickEvent, PlayCapture
from ..keys import DEFAULT_CLICK_KEYS, key_code
from ..models import DisplaySpec, PenSample, Rect, TabletSpec
from ..otd.livetune import find_osu, list_processes
from .pen_sampler import PenSampler

DEFAULT_POLL_HZ = 500.0
TRAIL_S = 0.6

KeySource = Callable[[], Mapping[str, bool]]
__all__ = ["DEFAULT_CLICK_KEYS", "KeySource", "LiveView", "PlayRecorder", "key_code", "osu_window_title",
           "songs_dir_for", "windows_key_source", "windows_window_lister"]


def windows_key_source(keys: Sequence[str]) -> KeySource | None:
    """``GetAsyncKeyState`` for exactly ``keys``; None when not on Windows or a key is unknown."""
    if os.name != "nt":
        return None
    codes = {k: key_code(k) for k in keys}
    if not codes or any(c is None for c in codes.values()):
        return None
    try:
        import ctypes

        get_state = ctypes.windll.user32.GetAsyncKeyState
        get_state.argtypes = [ctypes.c_int]
        get_state.restype = ctypes.c_short
    except (AttributeError, OSError, ImportError):
        return None

    def read() -> dict[str, bool]:
        return {name: bool(get_state(code) & 0x8000) for name, code in codes.items()}   # type: ignore[arg-type]

    return read


# ------------------------------------------------------------------ osu! window
WindowLister = Callable[[Collection[int]], Sequence["tuple[int, str]"]]   # pids -> (pid, title) of THEIR windows


def windows_window_lister() -> WindowLister | None:
    """Lister of the visible windows that belong to the given process ids, as (pid, title).

    The title of a window is read only after its owner process matched, so the titles of other programs
    are never read. None when not on Windows."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
    except (AttributeError, OSError, ImportError):
        return None

    def read(pids: Collection[int]) -> list[tuple[int, str]]:
        found: list[tuple[int, str]] = []
        wanted = set(pids)

        @callback_type
        def each(hwnd: int, _lparam: int) -> bool:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in wanted and user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buffer = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buffer, length + 1)
                    found.append((int(pid.value), buffer.value))
            return True

        user32.EnumWindows(each, 0)
        return found

    return read


def osu_window_title(pids: Sequence[int] | None = None, windows: WindowLister | None = None) -> str | None:
    """Title of osu!'s window (the one that names the map while playing), or None.

    Only windows that belong to the osu! process are looked at: the lister is given the process ids and
    reads no other window's title. Windows only.
    """
    if pids is None:
        try:
            osu = find_osu(list_processes())
        except Exception:
            osu = None
        pids = [osu.pid] if osu is not None else []
    lister = windows if windows is not None else windows_window_lister()
    if not pids or lister is None:
        return None
    try:
        wanted = set(pids)
        titles = [title for pid, title in lister(wanted) if pid in wanted]
    except Exception:
        return None
    named = [t for t in titles if t.lower().startswith("osu!")]
    playing = [t for t in named if " - " in t]
    return (playing or named or [None])[0]


def songs_dir_for(osu_exe: str | Path | None) -> Path | None:
    """The ``Songs`` folder of an osu!stable install (honours ``BeatmapDirectory`` in ``osu!.<user>.cfg``)."""
    if not osu_exe:
        return None
    root = Path(osu_exe).parent
    try:
        for cfg in sorted(root.glob("osu!.*.cfg")):
            for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
                key, _sep, value = line.partition("=")
                if key.strip() == "BeatmapDirectory" and value.strip():
                    path = Path(value.strip())
                    return path if path.is_absolute() else root / path
    except OSError:
        pass
    songs = root / "Songs"
    return songs if songs.is_dir() else None


# ----------------------------------------------------------------- the recorder
@dataclass(frozen=True)
class LiveView:
    """What the live map needs: the last cursor position, a short trail and the click counters."""
    position: tuple[float, float] | None
    trail: tuple[PenSample, ...]
    clicks: int
    last_click_t: float | None
    now: float
    travel_px: float = 0.0          # how far the cursor moved since the start (to notice "moving but no clicks")
    elapsed_s: float = 0.0          # seconds since the recording started

    @property
    def idle_s(self) -> float | None:
        """Seconds since the last click (None before the first one)."""
        return None if self.last_click_t is None else self.now - self.last_click_t


class PlayRecorder:
    """Collects a :class:`PlayCapture` while the player plays.

    ``sampler`` supplies cursor samples, ``key_source`` the state of the configured keys; both are polled
    on background threads started by :meth:`start` (or by hand with :meth:`poll_keys` in tests). The
    clock is the sampler's, so click times and ``PenSample.t`` share one timeline.
    """

    def __init__(self, sampler: PenSampler, display: DisplaySpec, area: Rect, keys: Sequence[str] = DEFAULT_CLICK_KEYS,
                 key_source: KeySource | None = None, clock: Callable[[], float] | None = None,
                 poll_hz: float = DEFAULT_POLL_HZ, tablet: TabletSpec | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.sampler, self.display, self.area, self.tablet = sampler, display, area, tablet
        self.keys = tuple(keys)
        self._source = key_source if key_source is not None else windows_key_source(self.keys)
        self._clock = clock if clock is not None else sampler.clock
        self._period = 1.0 / poll_hz
        self._sleep = sleep
        self._lock = threading.Lock()
        self._samples: list[PenSample] = []
        self._clicks: list[ClickEvent] = []
        self._down: dict[str, bool] = {k: False for k in self.keys}
        self._travel = 0.0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.started_at = 0.0
        self.ended_at: float | None = None
        self.running = False

    @property
    def keys_available(self) -> bool:
        """False when the click keys cannot be read (not Windows): the coach cannot judge clicks then."""
        return self._source is not None

    def start(self) -> None:
        """Begin recording (clears any previous run)."""
        self.stop()
        with self._lock:
            self._samples, self._clicks, self._travel = [], [], 0.0
            self._down = {k: False for k in self.keys}
        self.sampler.drain()
        self.started_at, self.ended_at, self.running = self._clock(), None, True
        if self.sampler.threaded:
            self.sampler.start()
        if self._source is not None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="PlayRecorderKeys", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Stop the threads; the capture stays available through :meth:`snapshot`."""
        thread, self._thread = self._thread, None
        if self.running:
            self.ended_at = self._clock()
            self.running = False
        self._stop.set()
        if thread is not None:
            thread.join(timeout=1.0)
        self.sampler.stop()
        self._pull_samples()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_keys()
            self._sleep(self._period)

    def poll_keys(self) -> list[ClickEvent]:
        """Read the keys once; returns the presses (rising edges) found. Reads ONLY the configured keys."""
        if self._source is None:
            return []
        try:
            state = self._source()
        except Exception:   # a failing key read must not stop the recording
            return []
        now = self._clock()
        presses = []
        with self._lock:
            for key in self.keys:
                down = bool(state.get(key, False))
                if down and not self._down.get(key, False):
                    presses.append(ClickEvent(now, key))
                self._down[key] = down
            self._clicks.extend(presses)
        return presses

    def _pull_samples(self) -> None:
        fresh = self.sampler.drain()
        if fresh:
            with self._lock:
                prev = self._samples[-1] if self._samples else fresh[0]
                travel = 0.0
                for s in fresh:
                    step = math.hypot(s.x - prev.x, s.y - prev.y)
                    if math.isfinite(step):
                        travel += step
                    prev = s
                self._travel += travel
                self._samples.extend(fresh)

    def snapshot(self) -> PlayCapture:
        """The capture so far (``ended_at`` is None while recording, so analyses know it is partial)."""
        self._pull_samples()
        with self._lock:
            samples, clicks = tuple(self._samples), tuple(self._clicks)
        return PlayCapture(samples, clicks, self.area, self.display, self.tablet, self.started_at,
                           None if self.running else self.ended_at)

    def live(self) -> LiveView:
        """Cheap view for the live map: does not copy the whole capture."""
        self._pull_samples()
        now = self._clock()
        with self._lock:
            trail = tuple(s for s in self._samples[-400:] if now - s.t <= TRAIL_S)
            position = (self._samples[-1].x, self._samples[-1].y) if self._samples else None
            last = self._clicks[-1].t if self._clicks else None
            count = len(self._clicks)
            travel = self._travel
        return LiveView(position, trail, count, last, now, travel, max(0.0, now - self.started_at))
