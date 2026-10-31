"""Fine analysis grid: pooled cell estimates and small "hotspots" (pure, deterministic, fast).

The playfield is cut into ``rows x cols`` cells (``playtypes.ANALYSIS_DETAILS``). Fine cells hold few objects
each, so a raw per-cell mean would mostly show noise. Instead:

1. **Baseline**: what the whole play already explains is removed first: the reach fit (``cursor = gain *
   object + bias``; the gain only where its interval excludes 1, else the median error). What is left per
   object is its *residual*: the local part of the error. Global shifts and size problems are the
   optimizer's shift/scale job; this module only looks for what they cannot explain.
2. **Pooling** (empirical Bayes): each cell's mean residual is blended with its 3x3 neighbourhood (edge
   neighbours weight 1, diagonal 0.5; the neighbourhood itself is pulled towards 0 when it is sparse). The
   blend weight is ``noise / (noise + spread)``, where ``noise = sigma^2 / n`` (sigma: robust scatter of all
   residuals) and ``spread`` is how much cells really differ (method of moments). Cells with fewer than
   ``MIN_CELL_N`` clicked objects are marked ``enough=False`` and are never spoken about.
3. **Hotspots**: adjacent cells whose pooled residual is at least ``CELL_MM`` and that point the same way are
   merged; the region is kept only when all its objects together are clearly off (``n >= MIN_SPOT_N``, the
   mean residual at least ``SPOT_MM`` and ``SPOT_Z`` standard errors). Each region gets a centroid, a size in
   tablet millimetres and a kind: ``short`` (the cursor stays nearer the centre), ``over`` or ``off``.

Thresholds are estimates (not calibrated on real players). See docs/PLAY_COACH.md for how many objects a
region needs.
"""
from __future__ import annotations

import math
import statistics
from typing import Sequence

from .playtypes import NO_CLICK, TOO_FAR, CellStat, Hotspot, ObjectResult, ReachFit

MIN_CELL_N = 3            # fewer clicked objects in a cell: "no data" (no arrow, never in a finding)
NEIGHBOUR_PRIOR_N = 3.0   # pseudo-objects pulling a sparse neighbourhood towards "no local error"
MISS_PRIOR_N = 3.0
MIN_SPREAD_MM = 0.15      # floor of the between-cell spread (keeps a quiet play from over-trusting one cell)
MIN_SIGMA_MM = 0.1
CELL_MM = 0.35            # pooled residual of a cell that may belong to a hotspot
SAME_WAY = 0.3            # neighbouring cells join only when their errors point the same way (cosine)
MIN_SPOT_N = 5
SPOT_MM = 0.8             # ... or 20 % of the circle radius, whichever is larger (as playobserve.SHORT_MM)
SPOT_RADIUS_FRACTION = 0.2
SPOT_Z = 4.0
MAX_SPOTS = 4
CENTRE_FRACTION = 0.12    # a region this close to the playfield centre has no "inward": kind "off"


def _significant(gain: float, ci: tuple[float, float]) -> bool:
    return ci[1] < 1.0 or ci[0] > 1.0


def baseline(results: Sequence[ObjectResult], reach: ReachFit | None) -> tuple[float, float, float, float]:
    """``(gain_x - 1, gain_y - 1, bias_x, bias_y)``: the whole-play error at an object is
    ``(g - 1) * (u - 0.5) * width + bias`` per axis (gain only where it is significant)."""
    clicked = [r for r in results if r.err_x_mm is not None]
    med_x = statistics.median(r.err_x_mm for r in clicked) if clicked else 0.0
    med_y = statistics.median(r.err_y_mm for r in clicked) if clicked else 0.0
    if reach is None:
        return 0.0, 0.0, med_x, med_y
    use_x, use_y = _significant(reach.gain_x, reach.ci_x), _significant(reach.gain_y, reach.ci_y)
    return (reach.gain_x - 1.0 if use_x else 0.0, reach.gain_y - 1.0 if use_y else 0.0,
            reach.bias_x_mm if use_x else med_x, reach.bias_y_mm if use_y else med_y)


def _robust_sigma(values: Sequence[float]) -> float:
    if len(values) < 4:
        return 1.0
    med = statistics.median(values)
    return max(1.4826 * statistics.median(abs(v - med) for v in values), MIN_SIGMA_MM)


def place_id(fx: float, fy: float) -> str:
    """Plain place of a playfield point: ``"<third>.<quarter>[.<near>]"``, e.g. ``"bottom.center_right.corner"``.

    Thirds: top / middle / bottom; quarters: left / center_left / center_right / right; ``near``: corner or
    edge when the point is close to one. The GUI turns it into words.
    """
    v = "top" if fy < 1 / 3 else "middle" if fy < 2 / 3 else "bottom"
    h = ("left", "center_left", "center_right", "right")[min(int(fx * 4), 3)] if 0 <= fx <= 1 else \
        ("left" if fx < 0 else "right")
    near_x, near_y = min(fx, 1 - fx), min(fy, 1 - fy)
    near = "corner" if near_x < 0.2 and near_y < 0.2 else "edge" if min(near_x, near_y) < 0.12 else ""
    return f"{v}.{h}.{near}" if near else f"{v}.{h}"


def direction_id(dx: float, dy: float) -> str:
    """Where an error points in the player's view: up / down / left / right / up_left ... (screen y grows down)."""
    angle = math.degrees(math.atan2(-dy, dx)) % 360.0
    names = ("right", "up_right", "up", "up_left", "left", "down_left", "down", "down_right")
    return names[int((angle + 22.5) // 45) % 8]


def analyze_grid(results: Sequence[ObjectResult], rows: int, cols: int, reach: ReachFit | None,
                 view_mm: tuple[float, float], playfield_uv: tuple[float, float, float, float],
                 radius_mm: float) -> tuple[tuple[CellStat, ...], tuple[Hotspot, ...]]:
    """Cells (raw and pooled) and hotspots of a judged play. ``playfield_uv``: ``(u_left, v_top, u_right, v_bottom)``."""
    rows, cols = max(1, int(rows)), max(1, int(cols))
    vw, vh = view_mm
    ul, vt, ur, vb = playfield_uv
    pf_w, pf_h = (ur - ul) * vw, (vb - vt) * vh
    gx1, gy1, bx, by = baseline(results, reach)

    def expected(u: float, v: float) -> tuple[float, float]:
        return gx1 * (u - 0.5) * vw + bx, gy1 * (v - 0.5) * vh + by

    count = rows * cols
    n_all = [0] * count
    misses = [0] * count
    n = [0] * count
    sx = [0.0] * count
    sy = [0.0] * count
    raw = [[0.0, 0.0, 0.0, 0.0, 0] for _ in range(count)]    # sums of err_x, err_y, err, along (and its count)
    members: list[list[tuple[ObjectResult, float, float]]] = [[] for _ in range(count)]
    residuals: list[float] = []
    for r in results:
        k = min(int(r.fy * rows), rows - 1) * cols + min(int(r.fx * cols), cols - 1)
        n_all[k] += 1
        if r.verdict in (NO_CLICK, TOO_FAR):
            misses[k] += 1
        if r.err_x_mm is None or r.err_y_mm is None:
            continue
        ex, ey = expected(r.u, r.v)
        rx, ry = r.err_x_mm - ex, r.err_y_mm - ey
        n[k] += 1
        sx[k] += rx
        sy[k] += ry
        acc = raw[k]
        acc[0] += r.err_x_mm
        acc[1] += r.err_y_mm
        acc[2] += r.err_mm or 0.0
        if r.along_mm is not None:
            acc[3] += r.along_mm
            acc[4] += 1
        members[k].append((r, rx, ry))
        residuals += (rx, ry)
    sigma = _robust_sigma(residuals)
    var = sigma * sigma
    total_clicked = sum(n)
    total_items = sum(n_all)
    global_miss = sum(misses) / total_items if total_items else 0.0

    # between-cell spread of the true local errors (method of moments, both axes pooled)
    obs = [((sx[k] / n[k]) ** 2 + (sy[k] / n[k]) ** 2) / 2.0 - var / n[k] for k in range(count) if n[k] >= 2]
    spread = max(statistics.fmean(obs) if obs else 0.0, MIN_SPREAD_MM ** 2)

    est: list[tuple[float, float]] = []
    est_miss: list[float] = []
    for row in range(rows):
        for col in range(cols):
            k = row * cols + col
            wn = wx = wy = wm = wi = 0.0
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    rr, cc = row + dr, col + dc
                    if (dr or dc) and 0 <= rr < rows and 0 <= cc < cols:
                        w = 0.5 if dr and dc else 1.0
                        j = rr * cols + cc
                        wn += w * n[j]
                        wx += w * sx[j]
                        wy += w * sy[j]
                        wm += w * misses[j]
                        wi += w * n_all[j]
            prior_x, prior_y = wx / (wn + NEIGHBOUR_PRIOR_N), wy / (wn + NEIGHBOUR_PRIOR_N)
            if n[k]:
                noise = var / n[k]
                keep = spread / (spread + noise)
                est.append((keep * sx[k] / n[k] + (1 - keep) * prior_x, keep * sy[k] / n[k] + (1 - keep) * prior_y))
            else:
                est.append((prior_x, prior_y))
            prior_miss = (wm + NEIGHBOUR_PRIOR_N * global_miss) / (wi + NEIGHBOUR_PRIOR_N)
            est_miss.append((misses[k] + MISS_PRIOR_N * prior_miss) / (n_all[k] + MISS_PRIOR_N))

    cells = []
    for row in range(rows):
        for col in range(cols):
            k = row * cols + col
            u = ul + (col + 0.5) / cols * (ur - ul)
            v = vt + (row + 0.5) / rows * (vb - vt)
            ex, ey = expected(u, v)
            acc, m = raw[k], n[k]
            cells.append(CellStat(
                row, col, n_all[k], misses[k], acc[0] / m if m else 0.0, acc[1] / m if m else 0.0,
                acc[2] / m if m else 0.0, acc[3] / acc[4] if acc[4] else 0.0,
                est[k][0] + ex, est[k][1] + ey, est_miss[k], m >= MIN_CELL_N))
    spots = _hotspots(rows, cols, n, est, members, sigma, total_clicked, (pf_w, pf_h), radius_mm)
    return tuple(cells), spots


def _hotspots(rows: int, cols: int, n: Sequence[int], est: Sequence[tuple[float, float]],
              members: Sequence[Sequence[tuple[ObjectResult, float, float]]], sigma: float, total: int,
              playfield_mm: tuple[float, float], radius_mm: float) -> tuple[Hotspot, ...]:
    if total == 0:
        return ()
    candidate = [n[k] > 0 and math.hypot(*est[k]) >= CELL_MM for k in range(rows * cols)]
    seen = [False] * (rows * cols)
    spots: list[Hotspot] = []
    min_mm = max(SPOT_MM, SPOT_RADIUS_FRACTION * (radius_mm or 0.0))
    pf_w, pf_h = playfield_mm
    for start in range(rows * cols):
        if not candidate[start] or seen[start]:
            continue
        seen[start] = True
        group, stack = [start], [start]
        while stack:
            k = stack.pop()
            row, col = divmod(k, cols)
            ax, ay = est[k]
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    rr, cc = row + dr, col + dc
                    if not (dr or dc) or not (0 <= rr < rows and 0 <= cc < cols):
                        continue
                    j = rr * cols + cc
                    if seen[j] or not candidate[j]:
                        continue
                    bx, by = est[j]
                    if ax * bx + ay * by >= SAME_WAY * math.hypot(ax, ay) * math.hypot(bx, by):
                        seen[j] = True
                        group.append(j)
                        stack.append(j)
        items = [item for k in group for item in members[k]]
        count = len(items)
        if count < MIN_SPOT_N:
            continue
        mx = sum(rx for _r, rx, _ry in items) / count
        my = sum(ry for _r, _rx, ry in items) / count
        size = math.hypot(mx, my)
        z = size / (sigma / math.sqrt(count))
        if size < min_mm or z < SPOT_Z:
            continue
        fx = sum(r.fx for r, _x, _y in items) / count
        fy = sum(r.fy for r, _x, _y in items) / count
        u = sum(r.u for r, _x, _y in items) / count
        v = sum(r.v for r, _x, _y in items) / count
        r0 = min(k // cols for k in group)
        r1 = max(k // cols for k in group)
        c0 = min(k % cols for k in group)
        c1 = max(k % cols for k in group)
        wf, hf = (c1 - c0 + 1) / cols, (r1 - r0 + 1) / rows
        ix, iy = (0.5 - fx) * pf_w, (0.5 - fy) * pf_h          # towards the playfield centre, in mm
        length = math.hypot(ix, iy)
        kind = "off"
        if length >= CENTRE_FRACTION * min(pf_w, pf_h):
            inward = (mx * ix + my * iy) / length
            kind = "short" if inward > 0.5 * size else "over" if inward < -0.5 * size else "off"
        spots.append(Hotspot(kind, r0, c0, r1, c1, u, v, fx, fy, wf * pf_w, hf * pf_h, count, mx, my, size, z,
                             count / total, wf, hf))
    spots.sort(key=lambda s: -(s.z * s.err_mm))
    return tuple(spots[:MAX_SPOTS])
