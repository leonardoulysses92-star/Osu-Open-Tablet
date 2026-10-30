"""Records ``PenSample`` objects from the OS cursor (OpenTabletDriver moves it).

On Windows a background thread polls ``GetCursorPos`` at ~1.5 kHz, well above
any tablet report rate, and stores a sample when the position changed or when
the heartbeat interval elapsed. On other systems there is no thread: the GUI
feeds positions from Tk ``<Motion>`` events through :meth:`PenSampler.record`.

The position source and the clock are injectable, so the logic is testable
without a display. Only the OS cursor is available: no pressure or tilt.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable

from osu_tablet_assistant.models import PenSample

PositionSource = Callable[[], "tuple[float, float] | None"]

DEFAULT_POLL_HZ = 1500.0
DEFAULT_HEARTBEAT_S = 0.05


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


class PenSampler:
    """Thread-safe collector of cursor samples.

    ``position_source`` returns ``(x, y)`` desktop pixels (or ``None``). When it
    is omitted the Windows cursor is used; if that is unavailable too,
    ``threaded`` is False and the owner must call :meth:`record` itself.
    """

    def __init__(self, position_source: PositionSource | None = None,
                 clock: Callable[[], float] = time.perf_counter,
                 poll_hz: float = DEFAULT_POLL_HZ, heartbeat_s: float = DEFAULT_HEARTBEAT_S,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._owns_timer = position_source is None
        self._source = position_source if position_source is not None else windows_cursor_source()
        self._clock = clock
        self._period = 1.0 / poll_hz
        self._heartbeat_s = heartbeat_s
        self._sleep = sleep
        self._lock = threading.Lock()
        self._samples: list[PenSample] = []
        self._last_pos: tuple[float, float] | None = None
        self._last_t = float("-inf")
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

    def start(self) -> None:
        """Begin polling; a no-op when already started or when not ``threaded``."""
        if self._thread is not None or self._source is None:
            return
        self._stop_event.clear()
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
        """Read the position source once; store and return a sample if it is due."""
        if self._source is None:
            return None
        pos = self._source()
        return None if pos is None else self.record(pos[0], pos[1])

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
