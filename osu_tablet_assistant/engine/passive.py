"""Passive cursor statistics while the user plays (pure functions on ``PenSample``).

The companion records the OS cursor (``gui.pen_sampler``) during normal play
and, per time window (one window per live setting), estimates:

* **rest jitter**: detrended, time-weighted RMS of the cursor inside "rest"
  segments (the cursor stays within ``REST_RADIUS_MM`` for ``REST_MIN_S``);
* **path roughness** at medium speed: how far each point sits from the
  straight line between its neighbours, relative to that chord (0 = locally
  straight/smooth curve, noise raises it); summarised per ``CHUNK_S`` chunk;
* **cursor update rate** while moving.

Windows before and after a change are compared with Cohen's d on the
per-segment / per-chunk values (a verdict also needs a >= 10 % change of the
mean, so a tiny but consistent shift reads "about the same"), and only
reported as *hints*: the program
sees the cursor, not the game, and play differs from one map section to the
next. Short windows yield "not enough data" instead of a verdict. Integer
cursor pixels set a floor: jitter below half a pixel is reported as such.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from statistics import mean, median
from typing import Iterable, Sequence

from ..models import PenSample, Reason

REST_RADIUS_MM = 0.5
REST_MIN_S = 0.25
REST_MIN_SAMPLES = 4
MEDIUM_SPEED_MM_S = (30.0, 600.0)
MAX_GAP_S = 0.05             # neighbours farther apart in time are not "one path"
CHUNK_S = 0.25
CHUNK_MIN_POINTS = 5
MIN_REST_UNITS = 3
MIN_PATH_UNITS = 8
MIN_RATE_INTERVALS = 50
SIMILAR_D = 0.3              # |d| below this is reported as "about the same"
MIN_RELATIVE_CHANGE = 0.10   # ...and so is a change under 10 % (too small to feel, however consistent)


@dataclass(frozen=True)
class WindowStats:
    sample_count: int
    duration_s: float
    mm_per_px: float
    rate_hz: float | None = None
    rest_jitter_mm: float | None = None
    rest_units: tuple[float, ...] = ()       # RMS per rest segment (mm)
    rest_time_s: float = 0.0
    roughness: float | None = None
    path_units: tuple[float, ...] = ()       # median roughness per chunk
    below_resolution: bool = False           # rest jitter < half a cursor pixel


@dataclass(frozen=True)
class Comparison:
    metric: str                              # "jitter" | "path" | "rate"
    verdict: str                             # "better" | "worse" | "similar" | "not_enough_data" | "info"
    before: float | None
    after: float | None
    effect: float | None = None              # Cohen's d (after - before); positive = higher value

    @property
    def strength(self) -> str:
        d = abs(self.effect or 0.0)
        return "large" if d >= 0.8 else "medium" if d >= 0.5 else "small"


# --------------------------------------------------------------------------
# Building blocks
# --------------------------------------------------------------------------
def dedupe(samples: Sequence[PenSample]) -> list[PenSample]:
    """Drop samples that repeat the previous position (sampler heartbeats)."""
    out: list[PenSample] = []
    for s in samples:
        if not out or (s.x, s.y) != (out[-1].x, out[-1].y):
            out.append(s)
    return out


def _dist(a: PenSample, b: PenSample) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def rest_segments(samples: Sequence[PenSample], mm_per_px: float) -> list[list[PenSample]]:
    """Runs where the cursor stays within ``REST_RADIUS_MM`` of the run's first sample."""
    radius_px = REST_RADIUS_MM / mm_per_px if mm_per_px > 0 else 0.0
    out: list[list[PenSample]] = []
    n, i = len(samples), 0
    while i < n:
        anchor, j = samples[i], i + 1
        while j < n and _dist(samples[j], anchor) <= radius_px:
            j += 1
        seg = list(samples[i:j])
        if len(seg) >= REST_MIN_SAMPLES and seg[-1].t - seg[0].t >= REST_MIN_S:
            out.append(seg)
            i = j
        else:
            i = max(i + 1, (i + j) // 2)
    return out


def detrended_rms_px(seg: Sequence[PenSample]) -> float:
    """Time-weighted RMS distance to a linear drift fit (each sample holds until the next)."""
    if len(seg) < 2:
        return 0.0
    dts = [b.t - a.t for a, b in zip(seg, seg[1:])]
    positive = [d for d in dts if d > 0]
    tail = mean(positive) if positive else 1.0
    w = [max(d, 0.0) for d in dts] + [tail]
    if sum(w) <= 0:
        w = [1.0] * len(seg)
    sw = sum(w)
    t0 = seg[0].t
    ts = [s.t - t0 for s in seg]
    mt = sum(wi * ti for wi, ti in zip(w, ts)) / sw
    var_t = sum(wi * (ti - mt) ** 2 for wi, ti in zip(w, ts)) / sw
    total = 0.0
    for coord in ("x", "y"):
        vals = [getattr(s, coord) for s in seg]
        mv = sum(wi * v for wi, v in zip(w, vals)) / sw
        slope = (sum(wi * (ti - mt) * (v - mv) for wi, ti, v in zip(w, ts, vals)) / sw / var_t) if var_t > 0 else 0.0
        total += sum(wi * (v - (mv + slope * (ti - mt))) ** 2 for wi, ti, v in zip(w, ts, vals)) / sw
    return math.sqrt(total)


def roughness_units(points: Sequence[PenSample], mm_per_px: float) -> list[float]:
    """Median chord-relative deviation per ``CHUNK_S`` chunk of medium-speed movement."""
    lo, hi = MEDIUM_SPEED_MM_S
    chunks: dict[int, list[float]] = {}
    if len(points) < 3 or mm_per_px <= 0:
        return []
    t0 = points[0].t
    for a, p, b in zip(points, points[1:], points[2:]):
        dt = b.t - a.t
        if dt <= 0 or dt > MAX_GAP_S:
            continue
        chord = _dist(a, b)
        if chord <= 0:
            continue
        speed = chord * mm_per_px / dt
        if not lo <= speed <= hi:
            continue
        frac = (p.t - a.t) / dt
        ex, ey = a.x + (b.x - a.x) * frac, a.y + (b.y - a.y) * frac
        chunks.setdefault(int((p.t - t0) / CHUNK_S), []).append(math.hypot(p.x - ex, p.y - ey) / chord)
    return [median(v) for _, v in sorted(chunks.items()) if len(v) >= CHUNK_MIN_POINTS]


def moving_rate_hz(points: Sequence[PenSample], mm_per_px: float) -> float | None:
    """1 / median interval between position changes while the cursor moves."""
    lo = MEDIUM_SPEED_MM_S[0]
    intervals = []
    for a, b in zip(points, points[1:]):
        dt = b.t - a.t
        if 0 < dt <= MAX_GAP_S and _dist(a, b) * mm_per_px / dt >= lo:
            intervals.append(dt)
    if len(intervals) < MIN_RATE_INTERVALS:
        return None
    return 1.0 / median(intervals)


def window_stats(samples: Sequence[PenSample], mm_per_px: float) -> WindowStats:
    """All passive metrics of one window of samples."""
    samples = sorted(samples, key=lambda s: s.t)
    if not samples:
        return WindowStats(0, 0.0, mm_per_px)
    duration = samples[-1].t - samples[0].t
    segs = rest_segments(samples, mm_per_px)
    rest_units = tuple(detrended_rms_px(s) * mm_per_px for s in segs)
    rest_time = sum(s[-1].t - s[0].t for s in segs)
    points = dedupe(samples)
    path_units = tuple(roughness_units(points, mm_per_px))
    jitter = mean(rest_units) if rest_units else None
    return WindowStats(
        sample_count=len(samples), duration_s=duration, mm_per_px=mm_per_px,
        rate_hz=moving_rate_hz(points, mm_per_px), rest_jitter_mm=jitter, rest_units=rest_units,
        rest_time_s=rest_time, roughness=median(path_units) if path_units else None, path_units=path_units,
        below_resolution=jitter is not None and jitter < 0.5 * mm_per_px)


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------
def cohens_d(before: Sequence[float], after: Sequence[float]) -> float | None:
    """(mean(after) - mean(before)) / pooled SD; None with fewer than 2 values a side."""
    na, nb = len(before), len(after)
    if na < 2 or nb < 2:
        return None
    ma, mb = mean(before), mean(after)
    va = sum((x - ma) ** 2 for x in before) / (na - 1)
    vb = sum((x - mb) ** 2 for x in after) / (nb - 1)
    sd = math.sqrt(((na - 1) * va + (nb - 1) * vb) / (na + nb - 2))
    if sd == 0:
        return 0.0 if ma == mb else math.copysign(10.0, mb - ma)
    return (mb - ma) / sd


def _compare(metric: str, before: Sequence[float], after: Sequence[float], min_units: int) -> Comparison:
    b_val = mean(before) if before else None
    a_val = mean(after) if after else None
    if len(before) < min_units or len(after) < min_units:
        return Comparison(metric, "not_enough_data", b_val, a_val)
    d = cohens_d(before, after)
    rel = abs(a_val - b_val) / abs(b_val) if b_val else (0.0 if a_val == b_val else math.inf)
    if d is None or abs(d) < SIMILAR_D or rel < MIN_RELATIVE_CHANGE:
        return Comparison(metric, "similar", b_val, a_val, d)
    return Comparison(metric, "worse" if d > 0 else "better", b_val, a_val, d)   # lower is better


def compare_windows(before: WindowStats, after: WindowStats) -> tuple[Comparison, ...]:
    """Jitter, path roughness and update rate before vs after a change."""
    out = [_compare("jitter", before.rest_units, after.rest_units, MIN_REST_UNITS),
           _compare("path", before.path_units, after.path_units, MIN_PATH_UNITS)]
    if before.rate_hz and after.rate_hz:
        out.append(Comparison("rate", "info", before.rate_hz, after.rate_hz))
    return tuple(out)


def hints(comparisons: Iterable[Comparison]) -> tuple[Reason, ...]:
    """Translatable hint lines (``passive.<metric>.<verdict>``)."""
    out = []
    for c in comparisons:
        params: dict[str, object] = {}
        if c.before is not None:
            params["before"] = _short(c.metric, c.before)
        if c.after is not None:
            params["after"] = _short(c.metric, c.after)
        if c.effect is not None:
            params["d"] = f"{c.effect:+.2f}"
            params["strength"] = c.strength
        out.append(Reason(f"passive.{c.metric}.{c.verdict}", params))
    return tuple(out)


def _short(metric: str, value: float) -> str:
    if metric == "jitter":
        return f"{value:.3f}"
    if metric == "rate":
        return f"{value:.0f}"
    return f"{value:.3f}"


class PassiveMonitor:
    """Keeps the samples of the current setting and the stats of the previous one."""

    def __init__(self, mm_per_px: float, max_window_s: float = 90.0) -> None:
        self.mm_per_px = float(mm_per_px)
        self.max_window_s = max_window_s
        self._samples: deque[PenSample] = deque()
        self.before: WindowStats | None = None

    def feed(self, samples: Iterable[PenSample]) -> None:
        """Append samples (time order); anything older than ``max_window_s`` is dropped."""
        self._samples.extend(samples)
        if self._samples:
            limit = self._samples[-1].t - self.max_window_s
            while self._samples and self._samples[0].t < limit:
                self._samples.popleft()

    def set_scale(self, mm_per_px: float) -> None:
        """The area changed: later samples use the new scale (call after :meth:`mark_change`)."""
        self.mm_per_px = float(mm_per_px)

    def mark_change(self) -> WindowStats:
        """Close the current window (it becomes ``before``) and start a new one."""
        self.before = window_stats(list(self._samples), self.mm_per_px)
        self._samples.clear()
        return self.before

    def current(self) -> WindowStats:
        return window_stats(list(self._samples), self.mm_per_px)

    def compare(self) -> tuple[Comparison, ...]:
        """Comparisons of the previous window against the current one (empty before any change)."""
        if self.before is None:
            return ()
        return compare_windows(self.before, self.current())

    def hints(self) -> tuple[Reason, ...]:
        return hints(self.compare())
