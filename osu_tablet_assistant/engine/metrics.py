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
# Aim
# --------------------------------------------------------------------------
def aim_report(clicks: Sequence[tuple[PenSample, tuple[float, float]]], mm_per_px: float,
               *, appear_times: Sequence[float] | None = None) -> MetricsReport:
    """Accuracy and speed of clicking circles one at a time.

    ``clicks[i]`` is the click sample and the center ``(x, y)`` (desktop px)
    of the circle it was aimed at. ``aim_error_mean_mm`` is the mean distance
    from click to circle center.

    Time: the contract carries no target-appearance timestamps, so the
    optional keyword ``appear_times`` (seconds, same clock, parallel to
    ``clicks``) supplies them. With it, ``aim_time_ms_mean`` is the mean of
    ``click.t - appear`` (``extras["aim_time_source"] == "appear"``). Without
    it, the mean interval between consecutive clicks is used instead
    (``"inter_click"``; needs two clicks). Targets are assumed to appear as
    soon as the previous one is clicked.

    Extras: ``click_count``, ``aim_error_median_mm``, ``aim_error_p95_mm``,
    ``aim_time_ms_median``, ``aim_time_source``, ``bias_x_mm`` / ``bias_y_mm``
    (mean click offset, a systematic shift hints at an area misalignment).
    """
    appears: list[float | None] = [None] * len(clicks)
    if appear_times is not None and len(appear_times) == len(clicks):
        appears = [a if math.isfinite(a) else None for a in appear_times]
    rows = []
    for (click, (tx, ty)), appear in zip(clicks, appears):
        if all(math.isfinite(v) for v in (click.t, click.x, click.y, tx, ty)):
            rows.append((click, tx, ty, appear))
    rows.sort(key=lambda r: r[0].t)
    if not rows or not _valid_scale(mm_per_px):
        return MetricsReport(test_kind="aim", sample_count=0, duration_s=0.0)

    dx = [(c.x - tx) * mm_per_px for c, tx, _, _ in rows]
    dy = [(c.y - ty) * mm_per_px for c, _, ty, _ in rows]
    miss = [math.hypot(a, b) for a, b in zip(dx, dy)]
    extras: dict[str, Any] = {
        "click_count": len(rows),
        "aim_error_median_mm": statistics.median(miss),
        "aim_error_p95_mm": _percentile(miss, 0.95),
        "bias_x_mm": statistics.fmean(dx),
        "bias_y_mm": statistics.fmean(dy),
    }

    appeared = [(c.t, a) for c, _, _, a in rows if a is not None]
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

    return MetricsReport(
        test_kind="aim", sample_count=len(rows), duration_s=max(rows[-1][0].t - start, 0.0),
        aim_error_mean_mm=statistics.fmean(miss),
        aim_time_ms_mean=statistics.fmean(times_ms) if times_ms else None,
        extras=extras)
