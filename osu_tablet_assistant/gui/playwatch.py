"""Records a play for the Play Coach: cursor samples, click times, and osu!'s window title.

What it reads, and nothing else:

* the OS cursor position (``PenSampler``, ~1.5 kHz);
* the pressed/released state of the CONFIGURED click keys only (default Z and X; the player picks them,
  see ``osu_tablet_assistant.keys``), through ``GetAsyncKeyState``. No other key is ever queried, there is
  no global keyboard hook, and only press times (with the key's name) are stored. The pen tip (``mouse1``)
  is polled only when the player said they click with it;
* the title of osu!'s own window (windows of other programs are not read).

Before a recording starts, the recorder can be **armed** (:meth:`PlayRecorder.arm`): it then keeps only the
last ``RING_S`` seconds of cursor samples and key presses, in memory, so a recording started a few seconds into
a play (the title names the map only once the play has begun) can reach back to the moment the title changed.

Nothing leaves the PC. osu!'s memory is never read. Every OS-facing source is injectable so the
logic runs (and is tested) without Windows.
"""
from __future__ import annotations

import bisect
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Collection, Mapping, Sequence

from ..engine import osucfg
from ..engine.beatmap import parse_window_title
from ..engine.playtypes import ClickEvent, PlayCapture, PlayfieldBox
from ..keys import DEFAULT_CLICK_KEYS, key_code
from ..models import DisplaySpec, PenSample, Rect, TabletSpec
from ..otd.livetune import find_osu, list_processes
from .pen_sampler import PenSampler, SamplerStats

DEFAULT_POLL_HZ = 500.0
TRAIL_S = 0.6
RING_S = 10.0                   # an armed recorder keeps this much of the past
TITLE_LEAD_S = 0.25             # a retroactive start reaches this far before the title change (poll period + margin)
_TRIM_EVERY_S = 0.25

KeySource = Callable[[], Mapping[str, bool]]
__all__ = ["DEFAULT_CLICK_KEYS", "KeySource", "LiveView", "PlayRecorder", "key_code", "osu_window_title",
           "songs_dir_for", "title_kind", "windows_key_source", "windows_window_lister"]


def windows_key_source(keys: Sequence[str]) -> KeySource | None:
    """``GetAsyncKeyState`` for exactly ``keys``; None when not on Windows or a key is unknown."""
    if os.name != "nt":
        return None
    codes = {k: key_code(k) for k in keys}
    if not codes or any(c is None for c in codes.values()):
        return None
    try:
        import ctypes

        get_state = ctypes.WinDLL("user32").GetAsyncKeyState    # own handle: the shared windll is never retyped
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

        user32 = ctypes.WinDLL("user32")          # own handle: the shared windll is never retyped
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


class _OsuPid:
    """The osu! process id, listed again at most every ``ttl_s`` (or at once when its window is gone).

    Listing every process (a toolhelp snapshot) is the costly part of reading the title; the window
    enumeration itself is cheap, so the title can be polled ~10 times a second."""

    def __init__(self, lister: Callable[[], Sequence] = list_processes, clock: Callable[[], float] = time.monotonic,
                 ttl_s: float = 3.0) -> None:
        self._lister, self._clock, self.ttl_s = lister, clock, ttl_s
        self._pid: int | None = None
        self._at = float("-inf")

    def get(self, refresh: bool = False) -> int | None:
        now = self._clock()
        if refresh or now - self._at >= self.ttl_s:
            self._at = now
            try:
                osu = find_osu(self._lister())
            except Exception:
                osu = None
            self._pid = osu.pid if osu is not None else None
        return self._pid


_OSU_PID = _OsuPid()


def osu_window_title(pids: Sequence[int] | None = None, windows: WindowLister | None = None) -> str | None:
    """Title of osu!'s window (the one that names the map while playing), or None.

    Only windows that belong to the osu! process are looked at: the lister is given the process ids and
    reads no other window's title. Windows only. Without ``pids`` the osu! process id is cached for a few
    seconds (see :class:`_OsuPid`), so calling this every 100 ms costs one window enumeration.
    """
    cached = pids is None
    if cached:
        pid = _OSU_PID.get()
        pids = [pid] if pid is not None else []
    lister = windows if windows is not None else windows_window_lister()
    if not pids or lister is None:
        return None
    title = _title_of(pids, lister)
    if title is None and cached:                     # osu! restarted (new pid)? ask the process list again now
        pid = _OSU_PID.get(refresh=True)
        title = _title_of([pid], lister) if pid is not None else None
    return title


def _title_of(pids: Sequence[int], lister: WindowLister) -> str | None:
    try:
        wanted = set(pids)
        titles = [title for pid, title in lister(wanted) if pid in wanted]
    except Exception:
        return None
    named = [t for t in titles if t.lower().startswith("osu!")]
    playing = [t for t in named if " - " in t]
    return (playing or named or [None])[0]


# What a window title says osu! is doing. Only "play" titles start a recording by themselves; while watching a
# replay, spectating or editing the title can also name a map (lazer certainly, stable unverified), so those
# need key presses before a recording counts. The words are guesses at what the titles may contain: a title
# that starts with them, or has them after the "[difficulty]" (a map may well be called "Replay").
_WATCH_PREFIXES = ("spectating", "watching")
_WATCH_WORDS = ("spectating", "watching", "replay")


def title_kind(title: str | None) -> str:
    """``menu`` (no map named) | ``play`` | ``watch`` (spectating / a replay) | ``edit`` (a path or ``.osu``)."""
    text = (title or "").strip()
    lowered = text.lower()
    if lowered.endswith(".osu") or "\\" in text:          # the editor shows the file (lazer: its path)
        return "edit"
    rest = lowered[4:].lstrip(" -") if lowered.startswith("osu!") else ""
    after = lowered.rpartition("]")[2] if "]" in lowered else ""
    if rest.startswith(_WATCH_PREFIXES) or any(w in after for w in _WATCH_WORDS):
        return "watch"
    return "play" if parse_window_title(text) is not None else "menu"


def songs_dir_for(osu_exe: str | Path | None) -> Path | None:
    """The ``Songs`` folder of an osu!stable install (honours ``BeatmapDirectory`` in ``osu!.<user>.cfg``)."""
    if not osu_exe:
        return None
    return osucfg.songs_dir(Path(osu_exe).parent)


# ----------------------------------------------------------------- the recorder
def _path_length(samples: Sequence[PenSample]) -> float:
    total = 0.0
    for a, b in zip(samples, samples[1:]):
        step = math.hypot(b.x - a.x, b.y - a.y)
        if math.isfinite(step):
            total += step
    return total


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
                 sleep: Callable[[float], None] = time.sleep, ring_s: float = RING_S,
                 playfield_source: Callable[[], PlayfieldBox | None] | None = None) -> None:
        self.sampler, self.display, self.area, self.tablet = sampler, display, area, tablet
        self.keys = tuple(keys)
        self._source = key_source if key_source is not None else windows_key_source(self.keys)
        self._clock = clock if clock is not None else sampler.clock
        self._period = 1.0 / poll_hz
        self._sleep = sleep
        self.ring_s = ring_s
        self._playfield_source = playfield_source
        self.playfield: PlayfieldBox | None = None
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
        self.armed = False

    @property
    def keys_available(self) -> bool:
        """False when the click keys cannot be read (not Windows): the coach cannot judge clicks then."""
        return self._source is not None

    @property
    def sampler_stats(self) -> SamplerStats | None:
        """How the cursor polling went (largest gap, back-filled points); None for samplers without stats."""
        return getattr(self.sampler, "stats", None)

    def now(self) -> float:
        """The recorder's clock (the one of every sample and click)."""
        return self._clock()

    def _reset(self) -> None:
        with self._lock:
            self._samples, self._clicks, self._travel = [], [], 0.0
            self._down = {k: False for k in self.keys}
        self.sampler.drain()

    def _start_threads(self) -> None:
        if self.sampler.threaded:
            self.sampler.start()
        if self._source is not None and self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="PlayRecorderKeys", daemon=True)
            self._thread.start()

    def _measure_playfield(self) -> None:
        try:
            self.playfield = self._playfield_source() if self._playfield_source is not None else None
        except Exception:     # measuring osu!'s window is a bonus; full screen is assumed without it
            self.playfield = None

    def arm(self) -> bool:
        """Start the threads but keep only the last ``ring_s`` seconds (pre-roll). False when keys cannot be read.

        :meth:`start` with ``since`` then turns what was kept from ``since`` on into the start of the capture."""
        if self.running or self.armed:
            return self.armed
        if self._source is None:
            return False
        self._reset()
        self.armed = True
        self._start_threads()
        return True

    def disarm(self) -> None:
        """Stop a pre-roll and forget what it kept."""
        if not self.armed:
            return
        self.armed = False
        self._stop_threads()
        self._reset()

    def start(self, since: float | None = None) -> None:
        """Begin recording. Armed and ``since`` given: the capture starts at ``since`` with what the pre-roll
        kept from then on (retroactive start). Otherwise any previous run is cleared and it starts now."""
        if self.armed and since is not None:
            self.armed = False
            self._pull_samples()
            start = max(since, self._clock() - self.ring_s)
            self._trim(start)
            self.started_at, self.ended_at, self.running = start, None, True
            self._measure_playfield()
            return
        self.stop()
        self._reset()
        self.started_at, self.ended_at, self.running = self._clock(), None, True
        self._measure_playfield()
        self._start_threads()

    def begin_at(self, t: float) -> bool:
        """While recording: drop what came before ``t`` (e.g. the menu, or the previous map) so the capture
        starts there. True when the capture already reached back to ``t``."""
        if not self.running:
            return False
        self._pull_samples()
        with self._lock:
            covered = bool(self._samples) and self._samples[0].t <= t
        self._trim(t)
        self.started_at = max(self.started_at, t)
        return covered

    def presses_since(self, t: float) -> int:
        """How many presses of the configured keys were recorded from ``t`` on."""
        with self._lock:
            return sum(1 for c in self._clicks if c.t >= t)

    def _trim(self, t0: float) -> None:
        """Forget samples and clicks before ``t0`` (both lists are in time order)."""
        with self._lock:
            cut = bisect.bisect_left(self._samples, t0, key=lambda s: s.t)
            if cut:
                del self._samples[:cut]
            cut = bisect.bisect_left(self._clicks, t0, key=lambda c: c.t)
            if cut:
                del self._clicks[:cut]
            self._travel = _path_length(self._samples)

    def stop(self) -> None:
        """Stop the threads; the capture stays available through :meth:`snapshot`."""
        self.armed = False
        if self.running:
            self.ended_at = self._clock()
            self.running = False
        self._stop_threads()
        self._pull_samples()

    def _stop_threads(self) -> None:
        thread, self._thread = self._thread, None
        self._stop.set()
        if thread is not None:
            thread.join(timeout=1.0)
        self.sampler.stop()

    def _run(self) -> None:
        next_trim = self._clock() + _TRIM_EVERY_S
        while not self._stop.is_set():
            self.poll_keys()
            if self.armed:                       # pre-roll: keep only the last ring_s seconds
                now = self._clock()
                if now >= next_trim:
                    self._pull_samples()
                    self._trim(now - self.ring_s)
                    next_trim = now + _TRIM_EVERY_S
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
                           None if self.running else self.ended_at, self.playfield)

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
