"""Records ``PenSample`` objects from the OS cursor (OpenTabletDriver moves it).

On Windows a background thread polls ``GetCursorPos`` at ~1.5 kHz, well above
any tablet report rate, and stores a sample when the position changed or when
the heartbeat interval elapsed. On other systems there is no thread: the GUI
feeds positions from Tk ``<Motion>`` events through :meth:`PenSampler.record`.

When the thread stalls (GIL, garbage collection, the Tk thread) for more than
``BACKFILL_GAP_S``, the positions the system saw in between are taken back from
``GetMouseMovePointsEx`` (the last 64 cursor positions with time stamps; the
multi-monitor fix-up of negative coordinates from its documentation is applied,
duplicates are dropped by ``(x, y, time stamp)``). Any error there is ignored.
:attr:`PenSampler.stats` counts the polls, the largest gap and the points back-filled.

:func:`osu_playfield_box` measures where the playfield is from osu!'s own
client rectangle (``GetClientRect`` + ``ClientToScreen`` of its window: the
window's position and size, nothing inside the game).

The position source and the clock are injectable, so the logic is testable
without a display. Only the OS cursor is available: no pressure or tilt.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Sequence

from osu_tablet_assistant.engine.beatmap import box_in_rect
from osu_tablet_assistant.engine.playtypes import PlayfieldBox
from osu_tablet_assistant.models import PenSample

PositionSource = Callable[[], "tuple[float, float] | None"]
# (x, y) of the current position -> the system's recent positions as (x, y, time ms), newest first; None on error
HistorySource = Callable[["tuple[int, int]"], "Sequence[tuple[int, int, int]] | None"]
TickSource = Callable[[], int]

DEFAULT_POLL_HZ = 1500.0
DEFAULT_HEARTBEAT_S = 0.05
BACKFILL_GAP_S = 0.002           # a gap between two polls longer than this is filled from the system's history
_SEEN_KEYS = 256                 # (x, y, time) of recently back-filled points, for de-duplication


def windows_cursor_source() -> PositionSource | None:
    """``GetCursorPos`` wrapper, or ``None`` when not on Windows / ctypes fails."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        get_cursor_pos = ctypes.windll.user32.GetCursorPos
        get_cursor_pos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        get_cursor_pos.restype = wintypes.BOOL
    except (AttributeError, OSError, ImportError):
        return None

    def read() -> tuple[float, float] | None:
        point = wintypes.POINT()
        if not get_cursor_pos(point):
            return None
        return float(point.x), float(point.y)

    return read


def fix_display_point(x: int, y: int) -> tuple[int, int]:
    """``GetMouseMovePointsEx`` display points come back as 16-bit values: >32767 means negative (other monitor)."""
    return (x - 65536 if x > 32767 else x), (y - 65536 if y > 32767 else y)


def windows_move_history() -> tuple[HistorySource, TickSource] | None:
    """``GetMouseMovePointsEx`` (display points) and ``GetTickCount``; None when not on Windows / unavailable."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class MOUSEMOVEPOINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_size_t)]

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        history = user32.GetMouseMovePointsEx
        history.argtypes = [wintypes.UINT, ctypes.POINTER(MOUSEMOVEPOINT), ctypes.POINTER(MOUSEMOVEPOINT),
                            ctypes.c_int, wintypes.DWORD]
        history.restype = ctypes.c_int
        tick = ctypes.windll.kernel32.GetTickCount
        tick.restype = wintypes.DWORD
    except (AttributeError, OSError, ImportError):
        return None
    buffer = (MOUSEMOVEPOINT * 64)()
    size = ctypes.sizeof(MOUSEMOVEPOINT)

    def read(pos: tuple[int, int]) -> list[tuple[int, int, int]] | None:
        query = MOUSEMOVEPOINT(pos[0] & 0xFFFF, pos[1] & 0xFFFF, 0, 0)   # as the documentation's sample does
        try:
            count = history(size, ctypes.byref(query), buffer, 64, 1)     # GMMP_USE_DISPLAY_POINTS
        except OSError:
            return None
        if count <= 0:
            return None
        return [(*fix_display_point(p.x, p.y), int(p.time)) for p in buffer[:count]]

    return read, lambda: int(tick())


def _set_timer_resolution(begin: bool) -> None:
    """Request (or release) 1 ms system timer resolution; best effort, Windows only."""
    if os.name != "nt":
        return
    try:
        import ctypes

        winmm = ctypes.windll.winmm
        (winmm.timeBeginPeriod if begin else winmm.timeEndPeriod)(1)
    except (AttributeError, OSError, ImportError):
        pass


@dataclass(frozen=True)
class SamplerStats:
    """How the polling went during one run (for the report)."""
    polls: int = 0
    max_gap_ms: float = 0.0          # longest time between two polls
    long_gaps: int = 0               # gaps above BACKFILL_GAP_S
    backfilled: int = 0              # points taken back from the system's history
    history_failed: int = 0          # GetMouseMovePointsEx calls that gave nothing


class PenSampler:
    """Thread-safe collector of cursor samples.

    ``position_source`` returns ``(x, y)`` desktop pixels (or ``None``). When it
    is omitted the Windows cursor is used; if that is unavailable too,
    ``threaded`` is False and the owner must call :meth:`record` itself.
    ``history``/``ticks`` are the back-fill source (``GetMouseMovePointsEx`` /
    ``GetTickCount`` by default with the Windows cursor, none otherwise).
    """

    def __init__(self, position_source: PositionSource | None = None,
                 clock: Callable[[], float] = time.perf_counter,
                 poll_hz: float = DEFAULT_POLL_HZ, heartbeat_s: float = DEFAULT_HEARTBEAT_S,
                 sleep: Callable[[float], None] = time.sleep, history: HistorySource | None = None,
                 ticks: TickSource | None = None) -> None:
        self._owns_timer = position_source is None
        self._source = position_source if position_source is not None else windows_cursor_source()
        if history is None and position_source is None and self._source is not None:
            pair = windows_move_history()
            history, ticks = pair if pair is not None else (None, None)
        self._history, self._ticks = history, ticks
        self._clock = clock
        self._period = 1.0 / poll_hz
        self._heartbeat_s = heartbeat_s
        self._sleep = sleep
        self._lock = threading.Lock()
        self._samples: list[PenSample] = []
        self._last_pos: tuple[float, float] | None = None
        self._last_t = float("-inf")
        self._last_poll_t: float | None = None
        self._polled_pos: tuple[float, float] | None = None
        self._seen: deque[tuple[int, int, int]] = deque(maxlen=_SEEN_KEYS)
        self._stats = [0, 0.0, 0, 0, 0]          # polls, max gap s, long gaps, back-filled, history failures
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @property
    def threaded(self) -> bool:
        """True when a polling thread will run (a position source exists)."""
        return self._source is not None

    @property
    def clock(self) -> Callable[[], float]:
        """The clock stamping samples; targets must use the same one."""
        return self._clock

    @property
    def running(self) -> bool:
        return self._thread is not None

    @property
    def stats(self) -> SamplerStats:
        polls, gap, long_gaps, filled, failed = self._stats
        return SamplerStats(polls, gap * 1000.0, long_gaps, filled, failed)

    def reset_stats(self) -> None:
        self._stats = [0, 0.0, 0, 0, 0]
        self._last_poll_t = None

    def start(self) -> None:
        """Begin polling; a no-op when already started or when not ``threaded``."""
        if self._thread is not None or self._source is None:
            return
        self._stop_event.clear()
        self.reset_stats()
        if self._owns_timer:
            _set_timer_resolution(True)
        self._thread = threading.Thread(target=self._run, name="PenSampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop polling and release the timer resolution; safe to call repeatedly."""
        thread, self._thread = self._thread, None
        if thread is None:
            return
        self._stop_event.set()
        thread.join(timeout=1.0)
        if self._owns_timer:
            _set_timer_resolution(False)

    def drain(self) -> list[PenSample]:
        """Return and clear all samples collected so far, in time order."""
        with self._lock:
            out, self._samples = self._samples, []
        return out

    def snapshot(self, start: int = 0) -> list[PenSample]:
        """Copy of the samples from index ``start`` on, without clearing them.

        Lets the GUI consume samples live (e.g. aim activation) while
        :meth:`drain` still returns the whole run at the end. Indices restart
        after a ``drain``.
        """
        with self._lock:
            return self._samples[max(start, 0):]

    def position(self) -> tuple[float, float] | None:
        """Current cursor position: the source if there is one, else the last recorded sample."""
        if self._source is not None:
            pos = self._source()
            if pos is not None:
                return float(pos[0]), float(pos[1])
        with self._lock:
            return (float(self._last_pos[0]), float(self._last_pos[1])) if self._last_pos is not None else None

    def poll(self) -> PenSample | None:
        """Read the position source once; store and return a sample if it is due.

        After a gap of more than ``BACKFILL_GAP_S`` since the previous poll, the positions in between are
        first taken from the system's history (when there is one)."""
        if self._source is None:
            return None
        pos = self._source()
        if pos is None:
            return None
        now = self._clock()
        previous, self._last_poll_t = self._last_poll_t, now
        stats = self._stats
        stats[0] += 1
        if previous is not None:
            gap = now - previous
            if gap > stats[1]:
                stats[1] = gap
            if gap > BACKFILL_GAP_S:
                stats[2] += 1
                if self._history is not None:
                    self._backfill(pos, previous, now)
        self._polled_pos = (float(pos[0]), float(pos[1]))
        return self.record(pos[0], pos[1], now)

    def _backfill(self, pos: tuple[float, float], since: float, now: float) -> None:
        """Insert the system's positions between the previous poll (``since``) and ``now``."""
        try:
            points = self._history((int(pos[0]), int(pos[1])))          # type: ignore[misc]
            tick_now = self._ticks() if self._ticks is not None else None
        except Exception:     # the back-fill is a bonus: never let it break the sampling
            points, tick_now = None, None
        if not points or tick_now is None:
            self._stats[4] += 1
            return
        span = now - since
        before = self._polled_pos            # where the previous poll saw the cursor
        fresh: list[tuple[int, int, int]] = []
        for point in points[1:]:              # [0] is the current position, recorded by the poll itself
            age_s = ((tick_now - point[2]) & 0xFFFFFFFF) / 1000.0
            if point in self._seen or (float(point[0]), float(point[1])) == before or age_s > span + 0.016:
                break                         # reached what the previous poll (or back-fill) already has
            fresh.append(point)
        self._seen.extend(points[:1] + fresh)
        if not fresh:
            return
        fresh.reverse()                       # oldest first
        # The stamps are milliseconds of unverified resolution: keep each inside the gap; when they do not
        # strictly increase there, spread the points evenly over the gap instead (the order is still right).
        times = [min(max(now - ((tick_now - p[2]) & 0xFFFFFFFF) / 1000.0, since), now) for p in fresh]
        if any(b <= a for a, b in zip([since] + times, times + [now])):
            times = [since + span * (i + 1) / (len(fresh) + 1) for i in range(len(fresh))]
        added = 0
        with self._lock:
            for (x, y, _stamp), t in zip(fresh, times):
                if (float(x), float(y)) == self._last_pos or (self._samples and t <= self._samples[-1].t):
                    continue
                self._samples.append(PenSample(t, float(x), float(y)))
                self._last_pos, self._last_t = (float(x), float(y)), t
                added += 1
        self._stats[3] += added

    def record(self, x: float, y: float, t: float | None = None) -> PenSample | None:
        """Offer a position (Tk ``<Motion>`` fallback); kept if changed or heartbeat is due."""
        now = self._clock() if t is None else t
        with self._lock:
            if (x, y) == self._last_pos and now - self._last_t < self._heartbeat_s:
                return None
            sample = PenSample(now, float(x), float(y))
            self._samples.append(sample)
            self._last_pos = (x, y)
            self._last_t = now
            return sample

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.poll()
            self._sleep(self._period)


# ------------------------------------------------------------- osu!'s window
def windows_client_rect(pid: int) -> tuple[int, int, int, int] | None:
    """Client rectangle (desktop pixels) of the visible top-level ``osu!`` window of process ``pid``.

    Only windows of that process are looked at (``GetWindowThreadProcessId`` first); their title is read to
    skip osu!'s tooltips and the like. None off Windows, when minimised, or on any error."""
    if os.name != "nt" or not pid:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        found: list[tuple[int, int, int, int]] = []

        @callback_type
        def each(hwnd: int, _lparam: int) -> bool:
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value != pid or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            buffer = ctypes.create_unicode_buffer(64)
            user32.GetWindowTextW(hwnd, buffer, 64)
            if not buffer.value.lower().startswith("osu!"):
                return True
            rect, origin = wintypes.RECT(), wintypes.POINT(0, 0)
            if user32.GetClientRect(hwnd, ctypes.byref(rect)) and user32.ClientToScreen(hwnd, ctypes.byref(origin)):
                found.append((origin.x, origin.y, rect.right - rect.left, rect.bottom - rect.top))
            return True

        user32.EnumWindows(each, 0)
    except (AttributeError, OSError, ImportError, ValueError):
        return None
    big = [r for r in found if r[2] >= 200 and r[3] >= 150]
    return max(big, key=lambda r: r[2] * r[3]) if big else None


def osu_playfield_box(pid: int | None, client_rect: Callable[[int], "tuple[int, int, int, int] | None"]
                      = windows_client_rect) -> PlayfieldBox | None:
    """The playfield measured from osu!'s client rectangle (the 4:3 game area fitted inside it); None when
    osu!'s window cannot be measured (callers then assume full screen on the configured display)."""
    if not pid:
        return None
    try:
        rect = client_rect(pid)
    except Exception:
        return None
    return box_in_rect(*rect) if rect else None
