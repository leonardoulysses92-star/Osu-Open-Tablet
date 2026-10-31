"""Judge a real play from the cursor and the click times (pure, deterministic, fast).

``analyze_play`` is called every ~1.5 s while the player plays, on a *partial*
capture, so it only judges objects whose time already passed and it never
raises. It does not see osu!: hits and misses are INFERRED by aligning click
times with the beatmap's hit objects and comparing the cursor position at each
click with the object's centre. Only circles and slider heads are judged.

Honest limits (also shown in the UI and in docs/PLAY_COACH.md):

* the playfield transform is the osu!stable full-screen approximation
  (``engine.beatmap``); the alignment quality is the real check;
* the click-time alignment absorbs the player's average lateness, so
  ``time_error_ms`` is relative to the player's own typical timing;
* the area is assumed to map onto the whole display;
* every threshold below is an estimate, not calibrated on real players.
"""
from __future__ import annotations

import bisect
import math
import operator
import random
import statistics
from typing import Sequence

from ..models import PenSample, Reason
from . import areafit
from .beatmap import circle_radius_osu_px, hit_window_ms, playfield_scale, playfield_to_desktop
from .playtypes import (HIT, LATE, NOISY, NO_CLICK, OK, OVER, SHORT, TOO_EARLY, TOO_FAR, TOO_LATE, Alignment,
                        Beatmap, CellStat, EdgeFinding, FeelFit, ObjectResult, PlayCapture, PlayReport, ReachFit)

# ----------------------------------------------------------------- constants
ALIGN_TRUST = 0.6              # below this quality the report should not be trusted
TIME_SCALES = (1.0, 1.5, 0.75)  # NM, DT, HT
OFFSET_BIN_S = 0.012           # width of the offset histogram bins
OFFSET_PROBE_OBJECTS = 16      # objects used to vote for the offset (spread over the map) ...
OFFSET_HEAD_PROBES = 8         # ... plus the first ones
OFFSET_CANDIDATES = 4          # vote peaks that are matched in full and compared by quality
PRECISION_FRACTION = 0.7       # residual spread (MAD) that counts as zero precision, of the 50 window
PLAY_TAIL_S = 3.0              # objects further than this after the last matched click are "not played"
PLAY_LEAD_S = 1.0              # ... and this long before the first matched click
MIN_MATCHED = 8                # fewer matched clicks: the report says "play more"
EDGE_FRACTION = 0.25           # a playfield zone is the outer quarter on that side
MIN_ZONE_N = 6                 # fewer objects in a zone: no statement about it
SHORT_MM = 0.8                 # mean inward error (mm) that counts as "stops short" ...
SHORT_RADIUS_FRACTION = 0.2    # ... or this fraction of the circle radius, whichever is larger
LATE_MS = 20.0                 # lateness of a zone relative to the rest of the play
NOISY_RATIO = 1.6              # scatter of a zone relative to the rest of the play
FAST_SPEED_MM_S = 600.0        # jumps faster than this tell lag / over-smoothing ...
FAST_SPEED_FLOOR_MM_S = 200.0  # ... in a small area few jumps get there: then the fastest quarter, but never below this
MIN_FAST_JUMPS = 8
SLOW_WINDOW_S = 0.12           # chunk length for the rest-jitter estimate
SLOW_NET_MM = 1.0              # a chunk counts as "resting" when the cursor moved less than this
MIN_REACH_POINTS = 12
MIN_REACH_SPREAD_MM = 12.0     # objects must cover at least this much of an axis to fit a gain there
REACH_PAIRS = 400
REACH_BOOTSTRAP = 80
REACH_BOOT_PAIRS = 150
REACH_MIN_SEPARATION_MM = 6.0

_ZONE_TESTS = {
    "left": lambda fx, fy: fx < EDGE_FRACTION,
    "right": lambda fx, fy: fx > 1 - EDGE_FRACTION,
    "top": lambda fx, fy: fy < EDGE_FRACTION,
    "bottom": lambda fx, fy: fy > 1 - EDGE_FRACTION,
    "top_left": lambda fx, fy: fx < EDGE_FRACTION and fy < EDGE_FRACTION,
    "top_right": lambda fx, fy: fx > 1 - EDGE_FRACTION and fy < EDGE_FRACTION,
    "bottom_left": lambda fx, fy: fx < EDGE_FRACTION and fy > 1 - EDGE_FRACTION,
    "bottom_right": lambda fx, fy: fx > 1 - EDGE_FRACTION and fy > 1 - EDGE_FRACTION,
    "center": lambda fx, fy: 0.3 < fx < 0.7 and 0.3 < fy < 0.7,
}


# ------------------------------------------------------------------- helpers
def _mad(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    med = statistics.median(values)
    return statistics.median(abs(v - med) for v in values)


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return min(max(value, lo), hi)


class _Track:
    """Cursor samples as parallel lists with linear interpolation."""

    def __init__(self, samples: Sequence[PenSample]) -> None:
        t = [s.t for s in samples]
        x = [s.x for s in samples]
        y = [s.y for s in samples]
        # Fast path (the recorder's samples are already ordered and finite): two cheap O(n) checks
        # instead of a sort and an isfinite() per sample. Odd data takes the careful path.
        ordered = all(map(operator.le, t, t[1:]))
        if not (ordered and math.isfinite(math.fsum(t) + math.fsum(x) + math.fsum(y))):
            clean = sorted((s for s in samples if math.isfinite(s.t) and math.isfinite(s.x) and math.isfinite(s.y)),
                           key=lambda s: s.t)
            t, x, y = [s.t for s in clean], [s.x for s in clean], [s.y for s in clean]
        self.t, self.x, self.y = t, x, y

    def __bool__(self) -> bool:
        return len(self.t) >= 2

    def at(self, when: float) -> tuple[float, float] | None:
        """Interpolated position; None when ``when`` is outside the recording (by more than 0.1 s)."""
        t = self.t
        if not t or when < t[0] - 0.1 or when > t[-1] + 0.1:
            return None
        i = bisect.bisect_right(t, when)
        if i <= 0:
            return self.x[0], self.y[0]
        if i >= len(t):
            return self.x[-1], self.y[-1]
        span = t[i] - t[i - 1]
        f = (when - t[i - 1]) / span if span > 1e-9 else 0.0
        return self.x[i - 1] + f * (self.x[i] - self.x[i - 1]), self.y[i - 1] + f * (self.y[i] - self.y[i - 1])


def _match(obj_times: Sequence[float], click_times: Sequence[float], offset: float, tol: float
           ) -> list[tuple[int, int, float]]:
    """Greedy one-to-one pairing: smallest residual first. Returns ``(object, click, residual_s)``."""
    pairs = []
    for i, ot in enumerate(obj_times):
        centre = ot + offset
        lo = bisect.bisect_left(click_times, centre - tol)
        hi = bisect.bisect_right(click_times, centre + tol)
        for j in range(lo, hi):
            pairs.append((abs(click_times[j] - centre), i, j))
    pairs.sort()
    used_o, used_c, out = set(), set(), []
    for _r, i, j in pairs:
        if i in used_o or j in used_c:
            continue
        used_o.add(i)
        used_c.add(j)
        out.append((i, j, click_times[j] - obj_times[i] - offset))
    return out


def _vote_offsets(obj_times: Sequence[float], click_times: Sequence[float]) -> list[float]:
    """The ``OFFSET_CANDIDATES`` most voted click-minus-object offsets (seconds), best first.

    Votes come from the first few objects plus a spread-out subset, so a capture that only
    covers the start of the map (it is analysed while the player is still playing) still has
    probes inside it.
    """
    if not obj_times or not click_times:
        return []
    step = max(1, len(obj_times) // OFFSET_PROBE_OBJECTS)
    probes = list(dict.fromkeys(list(range(0, min(OFFSET_HEAD_PROBES, len(obj_times))))
                                + list(range(0, len(obj_times), step))))
    votes: dict[int, int] = {}
    inv = 1.0 / OFFSET_BIN_S
    for index in probes:
        ot = obj_times[index]
        seen = set()
        for ct in click_times:
            b = round((ct - ot) * inv)
            if b not in seen:
                seen.add(b)
                votes[b] = votes.get(b, 0) + 1

    def strength(b: int) -> int:
        return votes.get(b - 1, 0) + votes[b] + votes.get(b + 1, 0)

    ranked = sorted(votes, key=lambda b: (-strength(b), b))
    chosen: list[int] = []
    for b in ranked:
        if all(abs(b - c) > 3 for c in chosen):
            chosen.append(b)
            if len(chosen) >= OFFSET_CANDIDATES:
                break
    return [b * OFFSET_BIN_S for b in chosen]


def _refine(obj_times: Sequence[float], click_times: Sequence[float], offset: float, tol: float
            ) -> tuple[float, list[tuple[int, int, float]]]:
    """Match, move the offset to the median residual, match again."""
    pairs = _match(obj_times, click_times, offset, tol)
    for _ in range(2):
        if not pairs:
            break
        offset += statistics.median(r for _i, _j, r in pairs)
        pairs = _match(obj_times, click_times, offset, tol)
    return offset, pairs


def _quality(pairs: list, obj_times: Sequence[float], offset: float, window_s: float, end: float) -> float:
    """Matched fraction of the objects in the played span, weighted by timing precision."""
    if len(pairs) < 3:
        return 0.0
    first = min(obj_times[i] for i, _j, _r in pairs) - PLAY_LEAD_S
    last = min(max(obj_times[i] for i, _j, _r in pairs) + PLAY_TAIL_S, end - offset)
    expected = sum(1 for ot in obj_times if first <= ot <= last)
    fraction = _clamp(len(pairs) / max(expected, 1))
    spread = _mad([r for _i, _j, r in pairs])
    precision = _clamp(1.0 - spread / (PRECISION_FRACTION * window_s))
    return fraction * precision


def _times(beatmap: Beatmap, scale: float) -> list[float]:
    return [o.time_ms / 1000.0 / scale for o in beatmap.judged_objects]


def _end_time(capture: PlayCapture) -> float:
    ends = [capture.ended_at] if capture.ended_at is not None else []
    if capture.samples:       # recorded in time order: the last one is the latest (a stray NaN is skipped)
        ends += [s.t for s in capture.samples[-3:] if math.isfinite(s.t)]
    if capture.clicks:
        ends.append(max(c.t for c in capture.clicks))
    return max(ends) if ends else 0.0


def _alignment_for(beatmap: Beatmap, clicks: Sequence[float], end: float, scale: float
                   ) -> tuple[Alignment, list[tuple[int, int, float]], list[float]]:
    obj = _times(beatmap, scale)
    window_s = hit_window_ms(beatmap.od, "50") / 1000.0 / scale
    best: tuple[Alignment, list[tuple[int, int, float]]] | None = None
    for guess in _vote_offsets(obj, clicks):
        offset, pairs = _refine(obj, clicks, guess, window_s)
        resid = [r for _i, _j, r in pairs]
        align = Alignment(offset_ms=offset * 1000.0, time_scale=scale,
                          quality=_quality(pairs, obj, offset, window_s, end), matched=len(pairs),
                          spread_ms=_mad(resid) * 1000.0 if resid else 0.0)
        if best is None or align.quality > best[0].quality:
            best = (align, pairs)
    if best is None:
        return Alignment(time_scale=scale), [], obj
    return best[0], best[1], obj


def _best_alignment(beatmap: Beatmap, clicks: Sequence[float], end: float
                    ) -> tuple[Alignment, list[tuple[int, int, float]], list[float]]:
    best = None
    for scale in TIME_SCALES:
        found = _alignment_for(beatmap, clicks, end, scale)
        if best is None or found[0].quality > best[0].quality:
            best = found
        if best[0].quality >= ALIGN_TRUST + 0.15:   # clearly right: do not try the other speeds
            break
    assert best is not None
    return best


def estimate_alignment(beatmap: Beatmap, capture: PlayCapture) -> Alignment:
    """How the clicks line up with the map: global offset, speed (1 / 1.5 / 0.75), quality 0..1.

    ``flip_y`` is decided inside :func:`analyze_play`, which has the cursor positions.
    """
    clicks = sorted(c.t for c in capture.clicks)
    if not beatmap.judged_objects or len(clicks) < 3:
        return Alignment()
    return _best_alignment(beatmap, clicks, _end_time(capture))[0]


# ------------------------------------------------------------------- geometry
class _Geometry:
    """Desktop pixels <-> player's-view area millimetres for one capture."""

    def __init__(self, capture: PlayCapture, beatmap: Beatmap) -> None:
        d = capture.display
        self.display = d
        self.left = d.offset_x - d.width_px / 2
        self.top = d.offset_y - d.height_px / 2
        vw, vh = areafit.view_size(capture.area)
        self.mm_x = vw / d.width_px if d.width_px else 0.0
        self.mm_y = vh / d.height_px if d.height_px else 0.0
        self.scale = playfield_scale(d)
        self.radius_px = circle_radius_osu_px(beatmap.cs) * self.scale
        self.radius_mm = self.radius_px * (self.mm_x + self.mm_y) / 2.0

    def object_px(self, x: float, y: float, flip: bool) -> tuple[float, float]:
        return playfield_to_desktop(x, 384.0 - y if flip else y, self.display)

    def to_mm(self, dx_px: float, dy_px: float) -> tuple[float, float]:
        return dx_px * self.mm_x, dy_px * self.mm_y

    def uv(self, px: float, py: float) -> tuple[float, float]:
        return (px - self.left) / self.display.width_px, (py - self.top) / self.display.height_px


def _choose_flip(beatmap: Beatmap, track: _Track, geo: _Geometry, clicks: Sequence[float],
                 pairs: list[tuple[int, int, float]]) -> bool:
    """HR flips the map vertically: pick the orientation where cursors land closer to the objects."""
    objs = beatmap.judged_objects
    totals = [0.0, 0.0]
    n = 0
    for i, j, _r in pairs:
        pos = track.at(clicks[j])
        if pos is None:
            continue
        n += 1
        for k, flip in enumerate((False, True)):
            ox, oy = geo.object_px(objs[i].x, objs[i].y, flip)
            totals[k] += math.hypot(pos[0] - ox, pos[1] - oy)
    return n >= MIN_MATCHED and totals[1] < 0.9 * totals[0]


# --------------------------------------------------------------------- judging
def _judge(beatmap: Beatmap, capture: PlayCapture, track: _Track, geo: _Geometry, align: Alignment,
           pairs: list[tuple[int, int, float]], obj_times: list[float], clicks: Sequence[float], end: float
           ) -> tuple[list[ObjectResult], list[tuple[float, float]]]:
    objs = beatmap.judged_objects
    offset = align.offset_ms / 1000.0
    scale = align.time_scale
    matched = {i: (j, r) for i, j, r in pairs}
    if matched:
        first = min(obj_times[i] for i in matched) - PLAY_LEAD_S
        last = min(max(obj_times[i] for i in matched) + PLAY_TAIL_S, end - offset)
    else:
        first, last = 0.0, -1.0
    win100 = hit_window_ms(beatmap.od, "100")
    out: list[ObjectResult] = []
    prev_px: tuple[float, float] | None = None
    prev_time: float | None = None
    jumps: list[tuple[float, float]] = []     # (speed mm/s, along-path error mm)
    for i, o in enumerate(objs):
        ox, oy = geo.object_px(o.x, o.y, align.flip_y)
        u, v = geo.uv(ox, oy)
        fx, fy = _clamp(o.x / 512.0), _clamp((384.0 - o.y if align.flip_y else o.y) / 384.0)
        previous, prev_obj_time = prev_px, prev_time
        prev_px, prev_time = (ox, oy), obj_times[i]
        if not (first <= obj_times[i] <= last):
            continue
        base = dict(index=i, time_ms=o.time_ms, u=u, v=v, fx=fx, fy=fy)
        if i not in matched:
            out.append(ObjectResult(verdict=NO_CLICK, **base))
            continue
        j, resid = matched[i]
        click_t = clicks[j]
        pos = track.at(click_t)
        if pos is None:
            out.append(ObjectResult(verdict=NO_CLICK, **base))
            continue
        ex, ey = geo.to_mm(pos[0] - ox, pos[1] - oy)
        dist_mm = math.hypot(ex, ey)
        along = lateral = None
        speed = None
        if previous is not None:
            dx, dy = geo.to_mm(ox - previous[0], oy - previous[1])
            length = math.hypot(dx, dy)
            if length > 2.0:
                along = (ex * dx + ey * dy) / length
                lateral = (-ex * dy + ey * dx) / length
        before = track.at(click_t - 0.03)
        if before is not None:
            bx, by = geo.to_mm(pos[0] - before[0], pos[1] - before[1])
            speed = math.hypot(bx, by) / 0.03
        err_ms = resid * 1000.0
        if dist_mm > geo.radius_mm:
            verdict = TOO_FAR
        elif abs(err_ms) * scale > win100:
            verdict = TOO_EARLY if err_ms < 0 else TOO_LATE
        else:
            verdict = HIT
        if along is not None and previous is not None and prev_obj_time is not None:
            gap = obj_times[i] - prev_obj_time
            jump = math.hypot(*geo.to_mm(ox - previous[0], oy - previous[1]))
            if gap > 0:
                jumps.append((jump / gap, along))
        cu, cv = geo.uv(*pos)
        out.append(ObjectResult(verdict=verdict, click_t=click_t, time_error_ms=err_ms, err_x_mm=ex, err_y_mm=ey,
                                err_mm=dist_mm, along_mm=along, lateral_mm=lateral, cursor_u=cu, cursor_v=cv,
                                speed_mm_s=speed, **base))
    return out, jumps


def _cells(results: Sequence[ObjectResult], rows: int, cols: int) -> tuple[CellStat, ...]:
    acc: dict[tuple[int, int], list[ObjectResult]] = {}
    for r in results:
        key = (min(int(r.fy * rows), rows - 1), min(int(r.fx * cols), cols - 1))
        acc.setdefault(key, []).append(r)
    cells = []
    for row in range(rows):
        for col in range(cols):
            items = acc.get((row, col), [])
            clicked = [r for r in items if r.err_mm is not None]
            along = [r.along_mm for r in clicked if r.along_mm is not None]
            n = len(clicked)
            cells.append(CellStat(
                row, col, len(items), sum(1 for r in items if r.verdict in (NO_CLICK, TOO_FAR)),
                sum(r.err_x_mm for r in clicked) / n if n else 0.0,
                sum(r.err_y_mm for r in clicked) / n if n else 0.0,
                sum(r.err_mm for r in clicked) / n if n else 0.0,
                sum(along) / len(along) if along else 0.0))
    return tuple(cells)


def _slope(xs: Sequence[float], ys: Sequence[float], idx: Sequence[int], rng: random.Random, pairs: int
           ) -> float | None:
    """Median slope over random pairs of the points ``idx`` (Theil-Sen on a sample of the pairs)."""
    draws = rng.choices(idx, k=pairs * 4)
    slopes = []
    for a, b in zip(draws[::2], draws[1::2]):
        dx = xs[b] - xs[a]
        if abs(dx) >= REACH_MIN_SEPARATION_MM:
            slopes.append((ys[b] - ys[a]) / dx)
    return statistics.median(slopes) if len(slopes) >= pairs // 3 else None


def _theil_sen(xs: list[float], ys: list[float], rng: random.Random
               ) -> tuple[float, float, tuple[float, float]] | None:
    """Robust slope/intercept (Theil-Sen on sampled pairs) with a 95 % bootstrap interval over the points."""
    n = len(xs)
    everyone = list(range(n))
    gain = _slope(xs, ys, everyone, rng, REACH_PAIRS)
    if gain is None:
        return None
    bias = statistics.median(y - gain * x for x, y in zip(xs, ys))
    boots = []
    for _ in range(REACH_BOOTSTRAP):
        sample = rng.choices(everyone, k=n)
        value = _slope(xs, ys, sample, rng, REACH_BOOT_PAIRS)
        if value is not None:
            boots.append(value)
    if len(boots) < REACH_BOOTSTRAP // 2:
        return gain, bias, (gain, gain)
    boots.sort()
    lo, hi = boots[int(0.025 * (len(boots) - 1))], boots[int(round(0.975 * (len(boots) - 1)))]
    return gain, bias, (min(lo, gain), max(hi, gain))


def _reach(results: Sequence[ObjectResult], capture: PlayCapture, seed: int) -> ReachFit | None:
    pts = [r for r in results if r.err_mm is not None and r.cursor_u is not None]
    if len(pts) < MIN_REACH_POINTS:
        return None
    vw, vh = areafit.view_size(capture.area)
    rng = random.Random(seed)
    fits = {}
    for axis, size in (("x", vw), ("y", vh)):
        if axis == "x":
            ox = [(r.u - 0.5) * size for r in pts]
            cx = [(r.cursor_u - 0.5) * size for r in pts]
        else:
            ox = [(r.v - 0.5) * size for r in pts]
            cx = [(r.cursor_v - 0.5) * size for r in pts]
        if max(ox) - min(ox) < MIN_REACH_SPREAD_MM:
            fits[axis] = (1.0, 0.0, (1.0, 1.0))
            continue
        fit = _theil_sen(ox, cx, rng)
        fits[axis] = fit if fit is not None else (1.0, 0.0, (1.0, 1.0))
    (gx, bx, cix), (gy, by, ciy) = fits["x"], fits["y"]
    return ReachFit(gx, gy, bx, by, cix, ciy, len(pts))


def _inward(r: ObjectResult) -> float | None:
    """Signed error towards the playfield centre (>0 = the cursor is more central than the object)."""
    if r.err_x_mm is None:
        return None
    dx, dy = 0.5 - r.fx, 0.5 - r.fy
    length = math.hypot(dx, dy)
    if length < 0.12:
        return None
    return (r.err_x_mm * dx + r.err_y_mm * dy) / length


def _edges(results: Sequence[ObjectResult], radius_mm: float) -> tuple[EdgeFinding, ...]:
    findings = []
    # Baseline for "stops short / goes past": the middle of the playfield, where a gain error is ~0.
    middle = [i for i in (_inward(r) for r in results if r.err_mm is not None and _ZONE_TESTS["center"](r.fx, r.fy))
              if i is not None]
    middle_inward = statistics.fmean(middle) if len(middle) >= 4 else 0.0
    for zone, inside in _ZONE_TESTS.items():
        members = [r for r in results if inside(r.fx, r.fy)]
        rest = [r for r in results if not inside(r.fx, r.fy)]
        if len(members) < MIN_ZONE_N or len(rest) < MIN_ZONE_N:
            continue
        clicked = [r for r in members if r.err_mm is not None]
        rest_clicked = [r for r in rest if r.err_mm is not None]
        miss = sum(1 for r in members if r.verdict in (NO_CLICK, TOO_FAR)) / len(members)
        rest_miss = sum(1 for r in rest if r.verdict in (NO_CLICK, TOO_FAR)) / len(rest)
        excess = miss - rest_miss
        dx = statistics.fmean(r.err_x_mm for r in clicked) if clicked else 0.0
        dy = statistics.fmean(r.err_y_mm for r in clicked) if clicked else 0.0
        err = statistics.fmean(r.err_mm for r in clicked) if clicked else 0.0
        inward = [i for i in map(_inward, clicked) if i is not None]
        inward_mean = statistics.fmean(inward) if len(inward) >= MIN_ZONE_N // 2 else 0.0
        inward_gap = inward_mean - middle_inward
        threshold = max(SHORT_MM, SHORT_RADIUS_FRACTION * radius_mm)
        times = [r.time_error_ms for r in clicked if r.time_error_ms is not None]
        rest_times = [r.time_error_ms for r in rest_clicked if r.time_error_ms is not None]
        late_gap = (statistics.fmean(times) - statistics.fmean(rest_times)) if times and rest_times else 0.0
        scatter = statistics.pstdev([r.err_mm for r in clicked]) if len(clicked) > 2 else 0.0
        rest_scatter = statistics.pstdev([r.err_mm for r in rest_clicked]) if len(rest_clicked) > 2 else 0.0

        kind, severity, size = OK, 0.0, err
        if zone != "center" and inward_gap > threshold and excess > -0.05:
            kind, severity, size = SHORT, max(inward_gap / radius_mm, excess * 2), inward_gap
        elif zone != "center" and inward_gap < -threshold and excess > -0.05:
            kind, severity, size = OVER, max(-inward_gap / radius_mm, excess * 2), -inward_gap
        elif late_gap > LATE_MS:
            kind, severity, size = LATE, late_gap / (4 * LATE_MS), late_gap
        elif rest_scatter > 0 and scatter > NOISY_RATIO * rest_scatter and excess > 0.1:
            kind, severity, size = NOISY, excess * 2, scatter
        findings.append(EdgeFinding(zone, kind, _clamp(severity), len(members), miss, size, dx, dy))
    return tuple(findings)


def _fast_jumps(jumps: Sequence[tuple[float, float]]) -> tuple[list[float], float]:
    """Along-path errors of the fast jumps and the speed threshold used (see ``FAST_SPEED_*``)."""
    fast = [a for v, a in jumps if v > FAST_SPEED_MM_S]
    if len(fast) >= MIN_FAST_JUMPS:
        return fast, FAST_SPEED_MM_S
    if len(jumps) < 2 * MIN_FAST_JUMPS:
        return fast, FAST_SPEED_MM_S
    cut = max(sorted(v for v, _a in jumps)[int(0.75 * (len(jumps) - 1))], FAST_SPEED_FLOOR_MM_S)
    return [a for v, a in jumps if v >= cut], cut


def _feel(track: _Track, geo: _Geometry, jumps: Sequence[tuple[float, float]], window: tuple[float, float]
          ) -> FeelFit:
    """Lag on fast jumps and rest-jitter on slow parts, inside the played span."""
    fast_alongs, threshold = _fast_jumps(jumps)
    lag = statistics.fmean(fast_alongs) if len(fast_alongs) >= 4 else None
    lo = bisect.bisect_left(track.t, window[0])
    hi = bisect.bisect_right(track.t, window[1])
    jitters = []
    i = lo
    while i < hi:
        j = bisect.bisect_left(track.t, track.t[i] + SLOW_WINDOW_S, i, hi)
        if j - i >= 8:
            xs = [track.x[k] * geo.mm_x for k in range(i, j)]
            ys = [track.y[k] * geo.mm_y for k in range(i, j)]
            if math.hypot(xs[-1] - xs[0], ys[-1] - ys[0]) < SLOW_NET_MM and \
                    max(xs) - min(xs) < 2 * SLOW_NET_MM and max(ys) - min(ys) < 2 * SLOW_NET_MM:
                ts = track.t[i:j]
                jitters.append(_detrended_rms(ts, xs, ys))
        i = max(j, i + 1)
    return FeelFit(lag, len(fast_alongs), statistics.fmean(jitters) if jitters else None, len(jitters),
                   threshold)


def _detrended_rms(ts: list[float], xs: list[float], ys: list[float]) -> float:
    n = len(ts)
    tm = sum(ts) / n
    var_t = sum((t - tm) ** 2 for t in ts) or 1.0
    total = 0.0
    for vals in (xs, ys):
        vm = sum(vals) / n
        slope = sum((t - tm) * (v - vm) for t, v in zip(ts, vals)) / var_t
        total += sum((v - (vm + slope * (t - tm))) ** 2 for t, v in zip(ts, vals))
    return math.sqrt(total / n)


# ------------------------------------------------------------------ the report
def analyze_play(beatmap: Beatmap, capture: PlayCapture, rows: int = 3, cols: int = 4, seed: int = 0,
                 detail: bool = True) -> PlayReport:
    """Everything the coach learns from a (possibly partial) play. Deterministic for a given ``seed``; never raises.

    ``detail=False`` is the cheap live view: alignment and per-object results only (no reach fit, zones or
    feel), about 3x faster, for the picture that updates while the player is still playing.
    """
    try:
        return _analyze(beatmap, capture, rows, cols, seed, detail)
    except Exception:   # a live display must never die on odd data
        return PlayReport(warnings=(Reason("play.warn.analysis_failed"),), partial=capture.ended_at is None)


def _analyze(beatmap: Beatmap, capture: PlayCapture, rows: int, cols: int, seed: int, detail: bool = True
             ) -> PlayReport:
    objs = beatmap.judged_objects
    clicks = sorted(c.t for c in capture.clicks)
    track = _Track(capture.samples)
    partial = capture.ended_at is None
    if not objs or len(clicks) < 3 or not track or capture.display.width_px <= 0 or capture.display.height_px <= 0 \
            or capture.area.width <= 0 or capture.area.height <= 0:
        return PlayReport(n_objects=len(objs), partial=partial, warnings=(Reason("play.warn.not_enough"),))
    end = _end_time(capture)
    align, pairs, obj_times = _best_alignment(beatmap, clicks, end)
    geo = _Geometry(capture, beatmap)
    if _choose_flip(beatmap, track, geo, clicks, pairs):
        align = Alignment(align.offset_ms, align.time_scale, True, align.quality, align.matched, align.spread_ms)
    results, jumps = _judge(beatmap, capture, track, geo, align, pairs, obj_times, clicks, end)
    warnings: list[Reason] = []
    if align.quality < ALIGN_TRUST:
        warnings.append(Reason("play.warn.align_low", {"quality": f"{align.quality:.2f}"}))
    elif align.matched < MIN_MATCHED:
        warnings.append(Reason("play.warn.few_matched", {"n": align.matched}))
    if not areafit.rotation_verified(capture.area):
        warnings.append(Reason("play.warn.rotation_unverified", {"rotation": f"{capture.area.rotation:g}"}))
    clicked = [r for r in results if r.err_mm is not None]
    misses = sum(1 for r in results if r.verdict != HIT)
    times = [r.time_error_ms for r in clicked if r.time_error_ms is not None]
    span = (min(c.t for c in capture.clicks) - 0.5, end)
    return PlayReport(
        alignment=align, n_objects=len(objs), n_judged=len(results), results=tuple(results),
        cells=_cells(results, rows, cols) if detail else (), reach=_reach(results, capture, seed) if detail else None,
        edges=_edges(results, geo.radius_mm) if detail else (),
        feel=_feel(track, geo, jumps, span) if detail else None,
        miss_rate=misses / len(results) if results else 0.0,
        mean_err_ratio=(statistics.fmean(r.err_mm for r in clicked) / geo.radius_mm) if clicked else None,
        time_spread_ms=_mad(times) * 1.4826 if times else None, radius_mm=geo.radius_mm,
        warnings=tuple(warnings), partial=partial)
