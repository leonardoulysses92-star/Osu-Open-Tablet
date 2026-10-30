"""Pure signal processing: pen samples -> ``MetricsReport``.

No tkinter, no I/O. Every report function accepts degenerate input (empty,
single sample, constant timestamps, non-finite values) and then returns a
report whose metrics are ``None``; none of them raises.

Conventions
-----------
* Samples are ``PenSample`` (seconds on one monotonic clock, desktop pixels).
  They are sorted by time and de-duplicated by timestamp before use.
* Lengths in reports are millimetres (``mm_per_px`` converts from pixels).
* Cursor-derived samples are usually recorded *on change* (plus a slow
  heartbeat), so time-based statistics first resample onto a uniform grid
  instead of averaging raw samples.
"""
from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from typing import Any, Sequence

from osu_tablet_assistant.models import MetricsReport, PenSample

# effective_rate
MAX_GAP_S = 0.1                 # longer gaps are pauses, not report intervals
MIN_RATE_INTERVALS = 3

# stillness
STILL_GRID_HZ = 500.0
MAX_GRID_POINTS = 20000

# tracking
LAG_GRID_S = 0.004              # lag search resolution (refined by a parabola fit)
LAG_MIN_S = -0.1                # pen may lead the target a little (anticipation)
LAG_MAX_S = 0.5
MIN_TRACK_SAMPLES = 8
JERK_RESAMPLE_HZ = 60.0
MIN_JERK_POINTS = 6
MIN_TARGET_JERK = 1e-3          # px/s^3; below this the target is a straight line


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _clean(samples: Sequence[PenSample]) -> list[PenSample]:
    """Finite samples sorted by time; the last one wins on equal timestamps."""
    good = [s for s in samples if math.isfinite(s.t) and math.isfinite(s.x) and math.isfinite(s.y)]
    good.sort(key=lambda s: s.t)
    out: list[PenSample] = []
    for s in good:
        if out and s.t == out[-1].t:
            out[-1] = s
        else:
            out.append(s)
    return out


def _span(samples: Sequence[PenSample]) -> float:
    return samples[-1].t - samples[0].t if samples else 0.0


def _empty(kind: str, samples: Sequence[PenSample]) -> MetricsReport:
    """Report with no metrics, carrying only count and duration."""
    return MetricsReport(test_kind=kind, sample_count=len(samples), duration_s=_span(samples))


def _valid_scale(mm_per_px: float) -> bool:
    return math.isfinite(mm_per_px) and mm_per_px > 0


def _percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile, ``q`` in [0, 1]; ``values`` non-empty."""
    ordered = sorted(values)
    pos = q * (len(ordered) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


class _Path:
    """Piecewise-linear position over time built from clean samples."""

    def __init__(self, samples: Sequence[PenSample]) -> None:
        self.ts = [s.t for s in samples]
        self.xs = [s.x for s in samples]
        self.ys = [s.y for s in samples]

    @property
    def t0(self) -> float:
        return self.ts[0]

    @property
    def t1(self) -> float:
        return self.ts[-1]

    def at(self, t: float) -> tuple[float, float]:
        """Linear interpolation, clamped to the first/last sample."""
        i = bisect_right(self.ts, t)
        if i == 0:
            return self.xs[0], self.ys[0]
        if i == len(self.ts):
            return self.xs[-1], self.ys[-1]
        f = (t - self.ts[i - 1]) / (self.ts[i] - self.ts[i - 1])
        return (self.xs[i - 1] + f * (self.xs[i] - self.xs[i - 1]),
                self.ys[i - 1] + f * (self.ys[i] - self.ys[i - 1]))

    def held(self, t: float) -> tuple[float, float]:
        """Zero-order hold: the last known position at time ``t``."""
        i = max(bisect_right(self.ts, t) - 1, 0)
        return self.xs[i], self.ys[i]


def _detrend(ts: Sequence[float], vs: Sequence[float]) -> tuple[list[float], float]:
    """Remove a least-squares line; return (residuals, slope per second)."""
    n = len(ts)
    mt = sum(ts) / n
    mv = sum(vs) / n
    stt = sum((t - mt) ** 2 for t in ts)
    slope = sum((t - mt) * (v - mv) for t, v in zip(ts, vs)) / stt if stt > 0 else 0.0
    return [v - mv - slope * (t - mt) for t, v in zip(ts, vs)], slope


# --------------------------------------------------------------------------
# Rate
# --------------------------------------------------------------------------
def effective_rate(samples: Sequence[PenSample]) -> float | None:
    """Position-update rate in Hz: ``1 / median`` interval between *changed* positions.

    Samples repeating the previous position (heartbeats) are skipped, and
    intervals longer than ``MAX_GAP_S`` (pauses) are ignored. ``None`` when
    fewer than ``MIN_RATE_INTERVALS`` valid intervals exist. Assumes the pen
    moves fast enough that every report changes the pixel position; very slow
    motion makes the estimate read low.
    """
    intervals: list[float] = []
    prev: PenSample | None = None
    for s in _clean(samples):
        if prev is not None:
            if (s.x, s.y) == (prev.x, prev.y):
                continue
            dt = s.t - prev.t
            if 0 < dt <= MAX_GAP_S:
                intervals.append(dt)
        prev = s
    if len(intervals) < MIN_RATE_INTERVALS:
        return None
    return 1.0 / statistics.median(intervals)


def rate_report(samples: Sequence[PenSample]) -> MetricsReport:
    """``MetricsReport`` of kind ``"rate"`` wrapping :func:`effective_rate`."""
    clean = _clean(samples)
    return MetricsReport(test_kind="rate", sample_count=len(clean), duration_s=_span(clean),
                         effective_rate_hz=effective_rate(clean))


# --------------------------------------------------------------------------
# Stillness
# --------------------------------------------------------------------------
def stillness_report(samples: Sequence[PenSample], mm_per_px: float) -> MetricsReport:
    """RMS wobble of a pen held still.

    The path is resampled to a uniform grid (zero-order hold, so on-change
    sampling does not bias the result), a least-squares line is removed from
    x and y separately (slow drift), and
    ``jitter_rms_mm = sqrt(mean(dx^2 + dy^2))`` of the residuals, i.e. the 2-D
    radial RMS (``sqrt(2) * sigma`` for isotropic noise of std ``sigma``).

    Extras: ``rms_x_mm``, ``rms_y_mm``, ``max_excursion_mm`` (largest residual
    distance), ``drift_mm`` (length of the removed linear drift over the
    window) and ``distinct_pixel_positions`` (hint of quantization: 1-3 means
    the cursor barely left its pixel, so the reading is limited by pixel size).
    """
    clean = _clean(samples)
    if len(clean) < 3 or _span(clean) <= 0 or not _valid_scale(mm_per_px):
        return _empty("stillness", clean)
    path = _Path(clean)
    n = min(MAX_GRID_POINTS, max(int(_span(clean) * STILL_GRID_HZ) + 1, 8))
    step = _span(clean) / (n - 1)
    ts = [path.t0 + i * step for i in range(n)]
    held = [path.held(t) for t in ts]
    rx, slope_x = _detrend(ts, [p[0] for p in held])
    ry, slope_y = _detrend(ts, [p[1] for p in held])
    sq = [a * a + b * b for a, b in zip(rx, ry)]
    rms_px = math.sqrt(sum(sq) / n)
    extras: dict[str, Any] = {
        "rms_x_mm": math.sqrt(sum(a * a for a in rx) / n) * mm_per_px,
        "rms_y_mm": math.sqrt(sum(b * b for b in ry) / n) * mm_per_px,
        "max_excursion_mm": math.sqrt(max(sq)) * mm_per_px,
        "drift_mm": math.hypot(slope_x, slope_y) * _span(clean) * mm_per_px,
        "distinct_pixel_positions": len({(round(s.x), round(s.y)) for s in clean}),
    }
    return MetricsReport(test_kind="stillness", sample_count=len(clean), duration_s=_span(clean),
                         jitter_rms_mm=rms_px * mm_per_px, extras=extras)


# --------------------------------------------------------------------------
# Tracking
# --------------------------------------------------------------------------
def _grid(path: _Path, origin: float) -> tuple[int, list[tuple[float, float]]]:
    """Positions of ``path`` on the global grid ``origin + k * LAG_GRID_S``.

    Returns (index of the first grid point inside the path, positions).
    """
    k0 = math.ceil((path.t0 - origin) / LAG_GRID_S)
    k1 = math.floor((path.t1 - origin) / LAG_GRID_S)
    return k0, [path.at(origin + k * LAG_GRID_S) for k in range(k0, k1 + 1)]


def _estimate_lag(pen: _Path, target: _Path) -> tuple[float, bool] | None:
    """Time shift ``L`` (s) such that ``pen(t) ~ target(t - L)`` and whether it hit a search limit.

    Brute-force minimum of the mean squared distance on a 4 ms grid over
    ``[LAG_MIN_S, LAG_MAX_S]`` (equivalent to a 2-D cross-correlation for
    paths of equal energy), refined to sub-grid resolution by fitting a
    parabola through the three costs around the minimum.
    """
    origin = min(pen.t0, target.t0)
    kp, pp = _grid(pen, origin)
    kg, gp = _grid(target, origin)
    if not pp or not gp:
        return None
    min_overlap = max(10, min(len(pp), len(gp)) // 4)
    m_lo, m_hi = round(LAG_MIN_S / LAG_GRID_S), round(LAG_MAX_S / LAG_GRID_S)
    costs: dict[int, float] = {}
    for m in range(m_lo, m_hi + 1):
        lo = max(kp, kg + m)
        hi = min(kp + len(pp) - 1, kg + len(gp) - 1 + m)
        if hi - lo + 1 < min_overlap:
            continue
        a = pp[lo - kp: hi - kp + 1]
        b = gp[lo - m - kg: hi - m - kg + 1]
        costs[m] = sum((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 for p, q in zip(a, b)) / len(a)
    if not costs:
        return None
    best = min(costs, key=costs.__getitem__)
    frac = 0.0
    if best - 1 in costs and best + 1 in costs:
        left, mid, right = costs[best - 1], costs[best], costs[best + 1]
        denom = left - 2 * mid + right
        if denom > 0:
            frac = max(-1.0, min(1.0, 0.5 * (left - right) / denom))
    return (best + frac) * LAG_GRID_S, best in (m_lo, m_hi)


def _aligned_errors(pen: _Path, target: _Path, lag: float) -> list[float]:
    """Pixel distances of each pen sample to target(t - lag), where the target exists."""
    errors = []
    for t, x, y in zip(pen.ts, pen.xs, pen.ys):
        if target.t0 <= t - lag <= target.t1:
            gx, gy = target.at(t - lag)
            errors.append(math.hypot(x - gx, y - gy))
    return errors


def _rms_jerk(path: _Path, t_start: float, t_end: float) -> float | None:
    """RMS magnitude of the third derivative (px/s^3) of the path resampled at 60 Hz."""
    dt = 1.0 / JERK_RESAMPLE_HZ
    n = int((t_end - t_start) / dt) + 1
    if n < MIN_JERK_POINTS:
        return None
    pts = [path.at(t_start + i * dt) for i in range(n)]
    sq = []
    for i in range(n - 3):
        jx = (pts[i + 3][0] - 3 * pts[i + 2][0] + 3 * pts[i + 1][0] - pts[i][0]) / dt ** 3
        jy = (pts[i + 3][1] - 3 * pts[i + 2][1] + 3 * pts[i + 1][1] - pts[i][1]) / dt ** 3
        sq.append(jx * jx + jy * jy)
    return math.sqrt(sum(sq) / len(sq))


def tracking_report(samples: Sequence[PenSample], target_path: Sequence[PenSample],
                    mm_per_px: float) -> MetricsReport:
    """Quality of following a moving target (pen and target share one clock).

    * ``lag_ms_estimate``: shift ``L`` minimizing the mean squared distance
      between ``pen(t)`` and ``target(t - L)``, searched in [-100, +500] ms
      (positive = pen behind the target). Includes any display latency of the
      test window itself.
    * ``path_error_mean_mm`` / ``path_error_p95_mm``: distance between
      ``pen(t)`` and ``target(t - L)`` at every pen sample inside the target's
      time range (assumes the pen keeps moving, as it does when following).
    * ``smoothness_jerk``: RMS jerk (third derivative) of the pen path divided
      by the RMS jerk of the target over the same (lag-shifted) window, both
      linearly resampled at 60 Hz. Dimensionless and independent of tablet
      report rate, screen size and target speed: 1.0 means as smooth as the
      ideal target, larger is rougher (lower = smoother). ``None`` when the
      target itself has no jerk (straight line).

    Extras: ``path_error_zero_lag_mean_mm`` (error without compensation),
    ``lag_at_search_limit`` (the optimum touched the search border, so the lag
    is unreliable), ``jerk_rms_mm_s3`` and ``target_jerk_rms_mm_s3``.
    """
    pen_s, tgt_s = _clean(samples), _clean(target_path)
    if (len(pen_s) < MIN_TRACK_SAMPLES or len(tgt_s) < 2
            or _span(pen_s) <= 0 or _span(tgt_s) <= 0 or not _valid_scale(mm_per_px)):
        return _empty("tracking", pen_s)
    pen, target = _Path(pen_s), _Path(tgt_s)
    found = _estimate_lag(pen, target)
    if found is None:
        return _empty("tracking", pen_s)
    lag, at_limit = found
    errors = _aligned_errors(pen, target, lag)
    if len(errors) < 2:
        return _empty("tracking", pen_s)

    extras: dict[str, Any] = {"lag_at_search_limit": at_limit}
    zero = _aligned_errors(pen, target, 0.0)
    if zero:
        extras["path_error_zero_lag_mean_mm"] = statistics.fmean(zero) * mm_per_px

    w0, w1 = max(pen.t0, target.t0 + lag), min(pen.t1, target.t1 + lag)
    jerk_pen = _rms_jerk(pen, w0, w1)
    jerk_tgt = _rms_jerk(target, w0 - lag, w1 - lag)
    jerk = None
    if jerk_pen is not None and jerk_tgt is not None and jerk_tgt > MIN_TARGET_JERK:
        jerk = jerk_pen / jerk_tgt
        extras["jerk_rms_mm_s3"] = jerk_pen * mm_per_px
        extras["target_jerk_rms_mm_s3"] = jerk_tgt * mm_per_px

    return MetricsReport(
        test_kind="tracking", sample_count=len(pen_s), duration_s=_span(pen_s),
        path_error_mean_mm=statistics.fmean(errors) * mm_per_px,
        path_error_p95_mm=_percentile(errors, 0.95) * mm_per_px,
        smoothness_jerk=jerk, lag_ms_estimate=lag * 1000.0, extras=extras)


# --------------------------------------------------------------------------
# Aim: circle geometry
# --------------------------------------------------------------------------
Point = tuple[float, float]

AIM_CENTER_FRACTION = 0.35      # center-zone radius as a fraction of the target radius
AIM_SETTLE_S = 0.25             # post-activation precision window (cut when the cursor leaves the circle)
AIM_OVERSHOOT_S = 0.12          # post-activation overshoot window, below a reaction to the next target
_EDGE_TOL = 1e-9                # relative tolerance for "on the circle" after float rounding


def segment_circle_entry(p0: Point, p1: Point, center: Point, radius: float) -> float | None:
    """Fraction ``f`` in [0, 1] at which segment ``p0 -> p1`` first touches the closed disc.

    ``0.0`` when ``p0`` is already inside, ``None`` when the segment never
    reaches the disc. A tangent segment (closest distance exactly ``radius``)
    touches it, so it counts. ``p0 + f * (p1 - p0)`` is the entry point; for a
    segment between two timed samples the same ``f`` interpolates the time.
    """
    fx, fy = p0[0] - center[0], p0[1] - center[1]
    if fx * fx + fy * fy <= radius * radius:
        return 0.0
    line = _line_chord(fx, fy, p1[0] - p0[0], p1[1] - p0[1], radius)
    if line is None:
        return None
    tc, half = line
    if tc <= 0:                     # closest point behind the start: moving away
        return None
    f = tc - half
    return f if f <= 1.0 else None


def segment_circle_exit(p0: Point, p1: Point, center: Point, radius: float) -> float | None:
    """Fraction ``f`` in [0, 1) at which a segment starting inside (or on) the disc leaves it.

    ``None`` when it stays inside up to ``p1`` (inclusive). A start point that
    is only outside by rounding is treated as on the edge.
    """
    fx, fy = p0[0] - center[0], p0[1] - center[1]
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    if dx == 0 and dy == 0:
        return None
    line = _line_chord(fx, fy, dx, dy, radius)
    if line is None:
        return 0.0
    f = line[0] + line[1]
    return None if f >= 1.0 else max(f, 0.0)


def _line_chord(fx: float, fy: float, dx: float, dy: float, radius: float) -> tuple[float, float] | None:
    """For the line ``f -> (fx, fy) + f * (dx, dy)`` (relative to the center): the parameter of
    its closest point to the center and the half-chord length in parameter units.

    ``None`` when the line misses the circle (or does not move). Computed from
    the closest point instead of the quadratic formula, which cancels badly for
    long segments (fast flicks between far-apart samples).
    """
    a = dx * dx + dy * dy
    if a == 0:
        return None
    tc = -(fx * dx + fy * dy) / a
    qx, qy = fx + tc * dx, fy + tc * dy
    h2 = radius * radius - (qx * qx + qy * qy)
    if h2 < 0:
        if h2 < -_EDGE_TOL * radius * radius:
            return None
        h2 = 0.0                    # tangent, lost to rounding
    return tc, math.sqrt(h2 / a)


def _inside(p: Point, center: Point, radius: float) -> bool:
    return math.dist(p, center) <= radius * (1 + _EDGE_TOL) + _EDGE_TOL


# --------------------------------------------------------------------------
# Aim: per-target trajectory statistics
# --------------------------------------------------------------------------
_TPoint = tuple[float, float, float]     # (t, x, y)


def _polyline(path: _Path, t0: float, t1: float) -> list[_TPoint]:
    """The path between ``t0`` and ``t1`` (interpolated ends, samples in between)."""
    pts: list[_TPoint] = [(t0, *path.at(t0))]
    i = bisect_right(path.ts, t0)
    while i < len(path.ts) and path.ts[i] < t1:
        pts.append((path.ts[i], path.xs[i], path.ys[i]))
        i += 1
    if t1 > t0:
        pts.append((t1, *path.at(t1)))
    return pts


def _length(pts: Sequence[_TPoint]) -> float:
    return sum(math.hypot(b[1] - a[1], b[2] - a[2]) for a, b in zip(pts, pts[1:]))


def _until_exit(pts: list[_TPoint], center: Point, radius: float | None) -> list[_TPoint]:
    """``pts`` cut where the path first leaves the circle (just the first point if it starts outside)."""
    if radius is None:
        return pts
    if not _inside(pts[0][1:], center, radius):
        return pts[:1]
    for k, (a, b) in enumerate(zip(pts, pts[1:])):
        f = segment_circle_exit(a[1:], b[1:], center, radius)
        if f is not None:
            cut = (a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1]), a[2] + f * (b[2] - a[2]))
            return pts[:k + 1] + [cut]
    return pts


def _closest(pts: Sequence[_TPoint], center: Point) -> tuple[float, Point]:
    """Smallest distance from ``center`` to the polyline, and the point where it happens."""
    best = (math.dist(pts[0][1:], center), (pts[0][1], pts[0][2]))
    for a, b in zip(pts, pts[1:]):
        dx, dy = b[1] - a[1], b[2] - a[2]
        seg = dx * dx + dy * dy
        f = 0.0 if seg == 0 else max(0.0, min(1.0, ((center[0] - a[1]) * dx + (center[1] - a[2]) * dy) / seg))
        p = (a[1] + f * dx, a[2] + f * dy)
        d = math.dist(p, center)
        if d < best[0]:
            best = (d, p)
    return best


def _rms_distance(pts: Sequence[_TPoint], center: Point) -> float:
    """Exact time-weighted RMS distance to ``center`` along a piecewise-linear path.

    Per segment ``p(s) = p0 + s * d`` the mean of ``|p(s) - c|^2`` over
    ``s`` in [0, 1] is ``|e|^2 + e.d + |d|^2 / 3`` with ``e = p0 - c``.
    """
    total = pts[-1][0] - pts[0][0]
    if total <= 0:
        return math.dist(pts[0][1:], center)
    acc = 0.0
    for a, b in zip(pts, pts[1:]):
        ex, ey = a[1] - center[0], a[2] - center[1]
        dx, dy = b[1] - a[1], b[2] - a[2]
        acc += (b[0] - a[0]) * (ex * ex + ey * ey + ex * dx + ey * dy + (dx * dx + dy * dy) / 3)
    return math.sqrt(max(acc / total, 0.0))


def _first_entry(pts: Sequence[_TPoint], center: Point, radius: float) -> float | None:
    """Time at which the polyline first enters the disc, ``None`` if it never does."""
    if _inside(pts[0][1:], center, radius):
        return pts[0][0]
    for a, b in zip(pts, pts[1:]):
        f = segment_circle_entry(a[1:], b[1:], center, radius)
        if f is not None:
            return a[0] + f * (b[0] - a[0])
    return None


def _target_stats(path: _Path, center: Point, appear: float | None, hit_t: float,
                  radius: float | None, zone: float | None, settle_s: float,
                  overshoot_s: float) -> dict[str, Any]:
    """Trajectory measures of one target (see :func:`aim_report`); px and seconds."""
    hit = path.at(hit_t)
    end = min(hit_t + settle_s, path.t1)
    after = _polyline(path, hit_t, end) if end > hit_t else [(hit_t, *hit)]
    on_target = _until_exit(after, center, radius)
    closest, point = _closest(on_target, center)
    out: dict[str, Any] = {"closest": closest, "point": point, "rms": _rms_distance(on_target, center),
                           "efficiency": None, "overshoot": None, "center_t": None}
    if appear is None:
        return out
    approach = _polyline(path, appear, hit_t) if hit_t > appear else [(hit_t, *hit)]
    traveled = _length(approach)
    start = approach[0][1:]
    if traveled > 0:
        out["efficiency"] = math.dist(start, hit) / traveled
    axis = (center[0] - start[0], center[1] - start[1])
    norm = math.hypot(*axis)
    if norm > 0:
        ux, uy = axis[0] / norm, axis[1] / norm
        o_end = min(hit_t + overshoot_s, path.t1)
        flight = _polyline(path, hit_t, o_end) if o_end > hit_t else [(hit_t, *hit)]
        out["overshoot"] = max(0.0, max((x - center[0]) * ux + (y - center[1]) * uy for _, x, y in flight))
    if zone is not None and end >= appear:
        found = _first_entry(_polyline(path, appear, end), center, zone)
        if found is not None:
            out["center_t"] = found - appear
    return out


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


# --------------------------------------------------------------------------
# Aim: report
# --------------------------------------------------------------------------
def aim_report(activations: Sequence[tuple[PenSample, tuple[float, float]]], mm_per_px: float,
               *, appear_times: Sequence[float] | None = None, samples: Sequence[PenSample] | None = None,
               radius_px: float | None = None, center_fraction: float = AIM_CENTER_FRACTION,
               settle_s: float = AIM_SETTLE_S, overshoot_s: float = AIM_OVERSHOOT_S) -> MetricsReport:
    """Speed and precision of reaching circles shown one at a time.

    ``activations[i]`` is the sample at which target ``i`` fired (the
    interpolated moment the cursor touched the circle / its center zone, or
    the click in the legacy click mode) and the target center ``(x, y)``.
    ``appear_times`` (same clock, parallel) are when each target appeared,
    ``samples`` the cursor trajectory of the run, ``radius_px`` the target
    radius and ``center_fraction`` the center-zone radius relative to it.

    Contract fields (consumed by ``engine.recommend``):

    * ``aim_time_ms_mean``: mean time to activation, ``activation.t - appear``.
      Without ``appear_times`` the mean interval between consecutive
      activations is used (``extras["aim_time_source"] == "inter_click"``).
    * ``aim_error_mean_mm``: mean **closest approach**, the smallest distance
      between the cursor and the target center from the activation until the
      cursor leaves the circle again or ``settle_s`` elapses (lower = more
      precise). With hover activation the distance *at* activation is about
      one radius by construction, so it says nothing; where the cursor goes
      right after does. Targets without trajectory data fall back to the
      distance at activation (identical to the old click error).

    Extras (lengths in mm, times in ms):

    * ``activation_count``, ``trajectory_count`` (targets with trajectory data),
      ``aim_time_source``, ``aim_time_ms_median``, ``aim_time_ms_p95``;
    * ``aim_error_median_mm``, ``aim_error_p95_mm`` (closest approach) and
      ``activation_error_mean_mm`` (center distance at the activation point);
    * ``center_time_ms_mean`` / ``center_time_ms_median``: appear -> first
      entry into the center zone (searched until ``settle_s`` after
      activation), over the targets whose zone was reached;
      ``center_reach_rate`` is that fraction (needs ``radius_px``);
    * ``overshoot_mean_mm`` / ``overshoot_max_mm``: per target, the largest
      excursion past the center along the approach axis (cursor position at
      appearance -> center) in the ``overshoot_s`` after activation, ``>= 0``
      (short so that the move to the next target, which appears at the same
      instant, is not counted);
    * ``settle_rms_mm``: per target, the time-weighted RMS distance to the
      center over the same on-target window as the closest approach, averaged;
    * ``path_efficiency_mean``: straight distance / travelled distance from
      the appearance position to the activation point (1 = perfectly straight);
    * ``bias_x_mm`` / ``bias_y_mm``: mean offset of the closest-approach point
      from the center (a systematic shift hints at an area misalignment).
    """
    appears: list[float | None] = [None] * len(activations)
    if appear_times is not None and len(appear_times) == len(activations):
        appears = [a if math.isfinite(a) else None for a in appear_times]
    rows = []
    for (hit, (tx, ty)), appear in zip(activations, appears):
        if all(math.isfinite(v) for v in (hit.t, hit.x, hit.y, tx, ty)):
            rows.append((hit, (tx, ty), appear))
    rows.sort(key=lambda r: r[0].t)
    if not rows or not _valid_scale(mm_per_px):
        return MetricsReport(test_kind="aim", sample_count=0, duration_s=0.0)

    clean = _clean(samples) if samples else []
    path = _Path(clean) if len(clean) >= 2 and _span(clean) > 0 else None
    radius = radius_px if radius_px is not None and math.isfinite(radius_px) and radius_px > 0 else None
    zone = radius * center_fraction if radius is not None and 0 < center_fraction <= 1 else None
    settle = settle_s if math.isfinite(settle_s) and settle_s > 0 else 0.0
    flight = overshoot_s if math.isfinite(overshoot_s) and overshoot_s > 0 else 0.0

    k = mm_per_px
    errors, offsets, act_err, rms, effs, overs, center_ms = [], [], [], [], [], [], []
    traced = 0
    for hit, center, appear in rows:
        act_err.append(math.dist((hit.x, hit.y), center) * k)
        if path is not None and path.t0 - _EDGE_TOL <= hit.t <= path.t1 + _EDGE_TOL:
            st = _target_stats(path, center, appear, hit.t, radius, zone, settle, flight)
            traced += 1
            errors.append(st["closest"] * k)
            offsets.append(((st["point"][0] - center[0]) * k, (st["point"][1] - center[1]) * k))
            rms.append(st["rms"] * k)
            if st["efficiency"] is not None:
                effs.append(st["efficiency"])
            if st["overshoot"] is not None:
                overs.append(st["overshoot"] * k)
            if st["center_t"] is not None:
                center_ms.append(st["center_t"] * 1000.0)
        else:
            errors.append(act_err[-1])
            offsets.append(((hit.x - center[0]) * k, (hit.y - center[1]) * k))

    extras: dict[str, Any] = {
        "activation_count": len(rows),
        "trajectory_count": traced,
        "aim_error_median_mm": statistics.median(errors),
        "aim_error_p95_mm": _percentile(errors, 0.95),
        "activation_error_mean_mm": statistics.fmean(act_err),
        "bias_x_mm": statistics.fmean(o[0] for o in offsets),
        "bias_y_mm": statistics.fmean(o[1] for o in offsets),
        "settle_rms_mm": _mean(rms),
        "path_efficiency_mean": _mean(effs),
        "overshoot_mean_mm": _mean(overs),
        "overshoot_max_mm": max(overs) if overs else None,
    }
    if zone is not None and traced:
        extras["center_reach_rate"] = len(center_ms) / traced
        extras["center_time_ms_mean"] = _mean(center_ms)
        extras["center_time_ms_median"] = statistics.median(center_ms) if center_ms else None

    appeared = [(h.t, a) for h, _, a in rows if a is not None]
    if len(appeared) == len(rows):
        times_ms = [(t - a) * 1000.0 for t, a in appeared if t >= a]
        extras["aim_time_source"] = "appear"
        start = min(a for _, a in appeared)
    else:
        times_ms = [(b[0].t - a[0].t) * 1000.0 for a, b in zip(rows, rows[1:]) if b[0].t > a[0].t]
        extras["aim_time_source"] = "inter_click"
        start = rows[0][0].t
    if times_ms:
        extras["aim_time_ms_median"] = statistics.median(times_ms)
        extras["aim_time_ms_p95"] = _percentile(times_ms, 0.95)

    return MetricsReport(
        test_kind="aim", sample_count=len(rows), duration_s=max(rows[-1][0].t - start, 0.0),
        aim_error_mean_mm=statistics.fmean(errors), aim_time_ms_mean=_mean(times_ms), extras=extras)
