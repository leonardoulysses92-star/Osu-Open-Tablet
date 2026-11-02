"""The player's habits in one observed play (pure, deterministic, no tkinter).

The Play Coach adapts the *setup* to the player, never the other way round: a habit is described so the
area and filters can make the player comfortable and precise *with* it, not so the player changes it. This
module only measures; ``engine.habitstore`` pools plays and ``engine.rulebook`` turns habits into one change.

Inputs are what the coach already has after a round: the aligned clicks (``PlayReport.results`` from
``engine.playobserve``), the cursor trace (``PlayCapture.samples``, ~1.5 kHz live), the beatmap objects and,
for a replay, its mods. Every :class:`Habit` carries a value, a standard error, how many samples it came
from, whether it is significant (``|value| > SIG_Z * SE``) and a confidence level (low / medium / high).

Units. Spatial habits are in **circle radii** (``r``, the radius of this map's circles in area millimetres)
so they compare across maps and area sizes; gains are fractions (+0.03 = the cursor travels 3 % too far),
rotations degrees, times milliseconds. Errors are ``cursor - object`` in the player's view (x right, y down).

Measured (the rule numbers refer to ``engine.rulebook``):

* **one joint fit** of the click errors (``aim_bias_*``, ``gain_*``, ``rotation``, ``edge_stop``)::

      e = a + M p + c * d_hat

  ``p`` = object position from the area centre (mm), ``d_hat`` = unit vector of the jump that led to the object
  (zero for short moves), ``M = G - I`` a 2x2 matrix. ``a`` is the constant aim bias (the error at the centre),
  the diagonal of ``M`` the reach gain per axis (``gain_x`` > 0: overshoot horizontally -> a bigger area),
  ``(M21 - M12) / 2`` the rotation, and ``c`` a constant "stop at the circle edge" term along the jump that
  does NOT grow with distance (rule 2: the area is never changed for it). Weighted least squares,
  streams weighted ``STREAM_WEIGHT`` (players flow through them instead of aiming at centres), one pass of
  outlier removal (``OUTLIER_SIGMAS``). Standard errors from ``sigma^2 (X'WX)^-1``;
* ``overshoot`` (along-the-jump error vs jump distance, descriptive) and ``long_jump.<dir>`` (mean along
  error on long jumps per direction: "you stay a little short on long jumps to the right");
* ``trail``: along-the-jump error vs jump *speed*, distance held fixed. Negative = the cursor arrives late on
  fast jumps only, which points at smoothing before the area (rule 3);
* ``ur`` (osu!'s unstable rate), ``key_gap`` (one key later than the other) and, only when the play is a
  replay (its clock is the map's), ``timing_bias``: informational, they point to osu!'s global offset and
  never change the area (rule 15);
* ``tap_lead``: time from the cursor entering the circle to the key-down (negative = tapped before
  arriving): a style, not a problem;
* ``jitter`` (detrended RMS of the cursor while it is still, minus a normal amount) and ``jitter_hover``
  (still-cursor jitter in breaks minus during play: a pen held high shakes more, rule 8);
* ``drift_*`` (aim bias vs time) and ``fatigue`` (aim error, last third vs first third);
* ``edge_clamp.<side>`` (the cursor pinned at a screen edge while approaching objects: the pen went past the
  area), ``pen_lift`` (cursor frozen while an object far away is due: the pen out of range);
* ``spikes``: EMI-like teleports (impossible speeds that come straight back). Those samples and the clicks
  near them are excluded from everything else (rule 10: hardware, not settings);
* ``zone.<place>``: the small problem regions ``engine.hotspots`` already found.

Spinners, slider bodies and slider tails are never used: ``Beatmap.judged_objects`` only holds circles and
slider heads. Mods: with a replay (``ModInfo``) HR/DT are known and checked against what the alignment
inferred; live, they were inferred, so every confidence is capped at ``medium``. Relax / Autopilot make the
clicks or the aim meaningless: nothing is measured.

Every threshold below is a heuristic, not calibrated on real plays.
"""
from __future__ import annotations

import bisect
import math
import statistics
from dataclasses import dataclass
from typing import Sequence

from ..log import log_exception
from ..models import Reason
from . import areafit
from .hotspots import place_id
from .playtypes import Beatmap, ObjectResult, PlayCapture, PlayReport

# ----------------------------------------------------------------- habit keys
AIM_X, AIM_Y = "aim_bias_x", "aim_bias_y"
GAIN_X, GAIN_Y = "gain_x", "gain_y"
SIZE, ASPECT = "size", "aspect"                   # mean gain / gain_x - gain_y (derived from the two gains)
ROTATION = "rotation"
EDGE_STOP = "edge_stop"
OVERSHOOT = "overshoot"
LONG_JUMP = "long_jump."                          # + right | left | up | down
TRAIL = "trail"
UR, TIMING, KEY_GAP = "ur", "timing_bias", "key_gap"
TAP_LEAD = "tap_lead"
JITTER, JITTER_HOVER = "jitter", "jitter_hover"
DRIFT_X, DRIFT_Y = "drift_x", "drift_y"
FATIGUE = "fatigue"
EDGE_CLAMP = "edge_clamp."                        # + left | right | top | bottom
PEN_LIFT = "pen_lift"
SPIKES = "spikes"
ZONE = "zone."                                    # + hotspots.place_id
SCATTER = "scatter"                               # aim scatter (one SD per axis, radii): what a miss costs
DIRECTIONS = ("right", "left", "up", "down")
SIDES = ("left", "right", "top", "bottom")

# ------------------------------------------------------- significance (heuristics)
SIG_Z = 2.0               # |value| > 2 SE = significant
HIGH_Z = 3.0              # ... and this far for "high" confidence
MIN_N = 8                 # fewer samples: always "low"
MEDIUM_N = 30
HIGH_N = 100
HIGH_PLAYS = 5            # pooled: "high" also needs this many plays (S20: 5 replays)
# ------------------------------------------------------------- aim fit (heuristics)
STREAM_GAP_MS = 180.0     # objects closer in time than this count as a stream ...
STREAM_WEIGHT = 0.3       # ... and weigh this much in the aim fit (players flow through them)
JUMP_MIN_R = 2.0          # a "jump" moves at least this many radii (edge stop, overshoot, trail, tap lead)
LONG_JUMP_R = 4.0         # a "long" jump
OUTLIER_R = 3.0           # clicks further than this from the object are not aim (another object, misalignment)
OUTLIER_SIGMAS = 4.0      # second pass: residuals beyond this many robust SDs are dropped
MIN_AIM = 12              # fewer clicks: no aim fit
MIN_SPREAD_MM = 12.0      # objects must cover this much of both axes for the gain / rotation terms
REACH_QUANTILE = 0.8      # "a far object": this quantile of the distances from the centre
# ---------------------------------------------------------------- feel (heuristics)
STILL_WINDOW_S = 0.12     # chunks for the still-cursor jitter (as playobserve)
STILL_NET_MM = 1.0
JITTER_NORMAL_MM = 0.05   # high-passed RMS that counts as ordinary sensor noise; the habit is the excess
PLAY_NEAR_S = 0.6         # a still chunk is "during play" with an object this close in time ...
BREAK_FAR_S = 1.5         # ... and "in a break" with none this close
MIN_WINDOWS = 5
TAP_BACK_S = 0.3          # how far back the circle entry is looked for (aim-before-tap) ...
TAP_AHEAD_S = 0.15        # ... and ahead (tap-before-arrive)
DRIFT_MIN_S = 90.0        # drift needs a map at least this long ...
FATIGUE_MIN_S = 120.0     # ... and fatigue this long
APPROACH_S = 0.15         # the approach to an object: this long before its time
PEN_LIFT_S = 0.08         # a frozen cursor this long ...
PEN_LIFT_FAR_R = 2.0      # ... while an object at least this far away is due = the pen out of range
# ------------------------------------------------------------ EMI spikes (heuristics)
SPIKE_MIN_MM = 4.0        # an out-and-back spike leaves at least this far ...
SPIKE_SPEED_MM_S = 3000.0  # ... faster than this (a hand does ~1 m/s in osu! area millimetres) ...
SPIKE_RETURN_S = 0.03     # ... and comes back within this time ...
SPIKE_BACK = 0.3          # ... to within this fraction of the way out
TELEPORT_MM = 5.0         # a one-way jump this long ...
TELEPORT_SPEED_MM_S = 6000.0   # ... this fast is a teleport too
SPIKE_GUARD_S = 0.05      # clicks and still chunks this close to a spike are not used
EMI_MIN_SPIKES = 3        # this many spikes in one play = flag the hardware
# osu! mod bits that make the play meaningless for habits (osu! file format docs)
MOD_RELAX, MOD_AUTOPILOT = 128, 8192


# ------------------------------------------------------------------- data
@dataclass(frozen=True)
class Habit:
    """One measured habit. ``value`` is signed (see the module docstring for units); ``raw`` is the plain
    measurement when ``value`` is an excess over a normal amount (jitter, UR); ``detail`` says which key /
    place / side it is about when the key does not."""
    key: str
    value: float
    se: float
    n: int
    unit: str = "r"
    raw: float | None = None
    detail: str = ""
    capped: bool = False          # mods unknown: confidence never above "medium"

    @property
    def z(self) -> float:
        return abs(self.value) / self.se if self.se > 0 else (math.inf if self.value else 0.0)

    @property
    def significant(self) -> bool:
        return self.n >= MIN_N and self.z > SIG_Z

    @property
    def confidence(self) -> str:
        return confidence(self.z, self.n, self.capped)


def confidence(z: float, n: int, capped: bool = False, plays: int | None = None) -> str:
    """``low`` / ``medium`` / ``high`` from the evidence (z) and the sample size; ``plays`` when pooled."""
    if n < MIN_N or not z > SIG_Z:
        return "low"
    level = "high" if z >= HIGH_Z and n >= HIGH_N else "medium" if n >= MEDIUM_N else "low"
    if level == "high" and (capped or (plays is not None and plays < HIGH_PLAYS)):
        level = "medium"
    return level


@dataclass(frozen=True)
class ModInfo:
    """Mods known from a replay (``None`` everywhere else: they were inferred by the alignment)."""
    time_scale: float = 1.0
    hard_rock: bool = False
    meaningless: bool = False     # Relax / Autopilot

    @classmethod
    def from_replay(cls, replay) -> "ModInfo":
        mods = int(getattr(replay, "mods", 0) or 0)
        return cls(float(replay.time_scale), bool(replay.hard_rock), bool(mods & (MOD_RELAX | MOD_AUTOPILOT)))


@dataclass(frozen=True)
class HabitProfile:
    """Every habit of one play, plus what pooling and the rules need to interpret them."""
    habits: tuple[Habit, ...] = ()
    usable: bool = False
    r_mm: float = 0.0             # circle radius in area millimetres
    reach_mm: float = 0.0         # distance of "a far object" from the area centre
    fast_speed: float = 0.0       # jump speed of a fast jump (m/s, area millimetres)
    n_aim: int = 0
    n_jumps: int = 0
    duration_s: float = 0.0
    mods_known: bool = False
    mods_mismatch: bool = False   # the replay's mods disagree with what the alignment inferred
    emi: bool = False
    spike_times: tuple[float, ...] = ()
    warnings: tuple[Reason, ...] = ()
    from_replay: bool = False

    def get(self, key: str) -> Habit | None:
        return next((h for h in self.habits if h.key == key), None)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(h.key for h in self.habits)


@dataclass
class _Aim:
    """One clicked object, in the player's view (mm)."""
    t: float                      # click time, capture clock (s)
    px: float                     # object position from the area centre
    py: float
    ex: float                     # cursor - object
    ey: float
    w: float                      # weight in the aim fit
    dx: float = 0.0               # unit vector of the jump that led here (0, 0 for short moves)
    dy: float = 0.0
    dist: float = 0.0             # jump length (mm), 0 when unknown
    jx: float = 0.0               # the jump vector itself (mm)
    jy: float = 0.0
    gap: float = 0.0              # time since the previous object (s, real time)
    along: float | None = None
    key: str = ""
    time_err: float | None = None
    ox: float = 0.0               # object position from the top-left (mm)
    oy: float = 0.0


# ------------------------------------------------------------------- maths
def _solve(a: list[list[float]], b: list[float]) -> tuple[list[float], list[list[float]]] | None:
    """Gauss-Jordan with partial pivoting: ``(x, a^-1)`` for ``a x = b``; None when singular."""
    n = len(a)
    m = [row[:] + [b[i]] + [1.0 if j == i else 0.0 for j in range(n)] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            return None
        m[col], m[piv] = m[piv], m[col]
        div = m[col][col]
        m[col] = [v / div for v in m[col]]
        for r in range(n):
            if r != col and m[r][col]:
                f = m[r][col]
                m[r] = [v - f * p for v, p in zip(m[r], m[col])]
    return [m[i][n] for i in range(n)], [m[i][n + 1:] for i in range(n)]


def wls(rows: Sequence[tuple[Sequence[float], float, float]]
        ) -> tuple[list[float], list[list[float]], list[float]] | None:
    """Weighted least squares over ``(regressors, target, weight)`` rows.

    Returns ``(beta, covariance, residuals)``. Weights are rescaled to average 1, the residual variance is
    ``sum(w r^2) / (N - p)`` and the covariance ``sigma^2 (X'WX)^-1`` (the usual approximation).
    """
    if not rows:
        return None
    p = len(rows[0][0])
    if len(rows) <= p + 1:
        return None
    mean_w = sum(w for _x, _y, w in rows) / len(rows)
    if mean_w <= 0:
        return None
    xtx = [[0.0] * p for _ in range(p)]
    xty = [0.0] * p
    for x, y, w in rows:
        w /= mean_w
        for i in range(p):
            wx = w * x[i]
            if wx:
                xty[i] += wx * y
                row = xtx[i]
                for j in range(p):
                    row[j] += wx * x[j]
    solved = _solve(xtx, xty)
    if solved is None:
        return None
    beta, inv = solved
    resid = [y - sum(b * v for b, v in zip(beta, x)) for x, y, _w in rows]
    sigma2 = sum(w / mean_w * r * r for (_x, _y, w), r in zip(rows, resid)) / (len(rows) - p)
    return beta, [[sigma2 * v for v in row] for row in inv], resid


def _wmean(values: Sequence[float], weights: Sequence[float] | None = None) -> tuple[float, float] | None:
    """Weighted mean and its (sandwich) standard error; None for fewer than 2 values."""
    if len(values) < 2:
        return None
    ws = list(weights) if weights is not None else [1.0] * len(values)
    total = sum(ws)
    if total <= 0:
        return None
    m = sum(w * v for w, v in zip(ws, values)) / total
    var = sum(w * w * (v - m) ** 2 for w, v in zip(ws, values)) / (total * total)
    n_eff = total * total / sum(w * w for w in ws)
    var *= n_eff / max(n_eff - 1.0, 1.0)
    return m, math.sqrt(var)


def _mad_sigma(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    med = statistics.median(values)
    return 1.4826 * statistics.median(abs(v - med) for v in values)


def _quantile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))]


def _direction(dx: float, dy: float) -> str:
    """right / left / up / down by the larger component (screen y grows down)."""
    if abs(dx) >= abs(dy):
        return "right" if dx > 0 else "left"
    return "down" if dy > 0 else "up"


# ------------------------------------------------------------------- trace
class _Trace:
    """Cursor samples in the player's view, millimetres from the top-left of the screen."""

    def __init__(self, capture: PlayCapture, mm_x: float, mm_y: float) -> None:
        d = capture.display
        left, top = d.offset_x - d.width_px / 2, d.offset_y - d.height_px / 2
        pts = [(s.t, (s.x - left) * mm_x, (s.y - top) * mm_y) for s in capture.samples
               if math.isfinite(s.t) and math.isfinite(s.x) and math.isfinite(s.y)]
        if any(a[0] > b[0] for a, b in zip(pts, pts[1:])):
            pts.sort(key=lambda p: p[0])
        self.t = [p[0] for p in pts]
        self.x = [p[1] for p in pts]
        self.y = [p[2] for p in pts]

    def __len__(self) -> int:
        return len(self.t)

    def at(self, when: float) -> tuple[float, float] | None:
        t = self.t
        if len(t) < 2 or when < t[0] or when > t[-1]:
            return None
        i = bisect.bisect_right(t, when)
        if i >= len(t):
            return self.x[-1], self.y[-1]
        span = t[i] - t[i - 1]
        f = (when - t[i - 1]) / span if span > 1e-9 else 0.0
        return self.x[i - 1] + f * (self.x[i] - self.x[i - 1]), self.y[i - 1] + f * (self.y[i] - self.y[i - 1])

    def runs(self) -> list[tuple[float, float, float]]:
        """The trace as distinct consecutive positions: ``(first time, x, y)``."""
        out: list[tuple[float, float, float]] = []
        for t, x, y in zip(self.t, self.x, self.y):
            if not out or x != out[-1][1] or y != out[-1][2]:
                out.append((t, x, y))
        return out


def find_spikes(runs: Sequence[tuple[float, float, float]]) -> list[float]:
    """Times of EMI-like spikes: a step out faster than a hand can move that comes straight back, or a long
    one-way teleport. ``runs`` = distinct positions with their first time (``_Trace.runs``)."""
    out: list[float] = []
    k = 1
    while k < len(runs):
        t0, x0, y0 = runs[k - 1]
        t1, x1, y1 = runs[k]
        step = math.hypot(x1 - x0, y1 - y0)
        speed = step / max(t1 - t0, 1e-4)
        if step >= SPIKE_MIN_MM and speed >= SPIKE_SPEED_MM_S:
            back = next((m for m in range(k + 1, min(k + 6, len(runs)))
                         if runs[m][0] - t1 <= SPIKE_RETURN_S
                         and math.hypot(runs[m][1] - x0, runs[m][2] - y0) <= SPIKE_BACK * step), None)
            if back is not None:
                out.append(t1)
                k = back + 1
                continue
            if step >= TELEPORT_MM and speed >= TELEPORT_SPEED_MM_S:
                out.append(t1)
        k += 1
    return out


def _near(times: Sequence[float], when: float, within: float) -> bool:
    i = bisect.bisect_left(times, when - within)
    return i < len(times) and times[i] <= when + within


# ------------------------------------------------------------------- the profile
def analyze_habits(beatmap: Beatmap, capture: PlayCapture, report: PlayReport, mods: ModInfo | None = None,
                   from_replay: bool = False) -> HabitProfile:
    """Every habit of one play (see the module docstring). Deterministic; never raises."""
    try:
        return _analyze(beatmap, capture, report, mods, from_replay)
    except Exception:            # a habit that cannot be measured must never break a round
        log_exception("measuring habits")
        return HabitProfile(warnings=(Reason("play.style.warn.failed"),), from_replay=from_replay)


def _analyze(beatmap: Beatmap, capture: PlayCapture, report: PlayReport, mods: ModInfo | None,
             from_replay: bool) -> HabitProfile:
    r = report.radius_mm or 0.0
    if not report.trusted or r <= 0 or not report.results or capture.display.width_px <= 0 \
            or capture.display.height_px <= 0:
        return HabitProfile(warnings=(Reason("play.style.warn.untrusted"),), from_replay=from_replay)
    if mods is not None and mods.meaningless:
        return HabitProfile(warnings=(Reason("play.style.warn.relax"),), mods_known=True, from_replay=from_replay)
    align = report.alignment
    mods_known = mods is not None
    mismatch = mods_known and (mods.hard_rock != align.flip_y or abs(mods.time_scale - align.time_scale) > 0.01)
    capped = not mods_known
    vw, vh = areafit.view_size(capture.area)
    mm_x, mm_y = vw / capture.display.width_px, vh / capture.display.height_px
    trace = _Trace(capture, mm_x, mm_y)
    runs = trace.runs()
    spikes = find_spikes(runs)
    objs = beatmap.judged_objects
    scale = align.time_scale or 1.0
    offset = align.offset_ms / 1000.0

    def obj_t(index: int) -> float:              # an object's time on the capture clock
        return objs[index].time_ms / 1000.0 / scale + offset

    keys = {c.t: c.key for c in capture.clicks}
    by_index = {res.index: res for res in report.results}
    aims = _aims(report.results, by_index, objs, keys, obj_t, vw, vh, r, spikes)
    habits: list[Habit] = []

    def add(key: str, value: float, se: float, n: int, unit: str = "r", raw: float | None = None,
            detail: str = "") -> None:
        if math.isfinite(value) and math.isfinite(se):
            habits.append(Habit(key, value, max(se, 0.0), n, unit, raw, detail, capped))

    jumps = [a for a in aims if a.dist >= JUMP_MIN_R * r and a.along is not None]
    reach = _quantile([math.hypot(a.px, a.py) for a in aims], REACH_QUANTILE)
    speeds = [a.dist / a.gap / 1000.0 for a in jumps if a.gap > 0]
    fit = _aim_fit(aims, r)
    if fit is not None:
        for key, value, se, n, unit in fit:
            add(key, value, se, n, unit)
    _jump_habits(jumps, r, add)
    _timing_habits(report, aims, align.offset_ms, scale, from_replay or mods_known, add)
    _tap_lead(_taps(report.results, by_index, vw, vh, r, spikes), trace, r, add)
    play_span = (min(a.t for a in aims) - 0.5, max(a.t for a in aims) + 0.5) if aims else (0.0, 0.0)
    obj_times = sorted(obj_t(res.index) for res in report.results)
    _stillness(trace, obj_times, spikes, play_span, r, add)
    if fit is not None:
        _time_habits(aims, fit, r, add)
    _edges(trace, report.results, obj_t, vw, vh, mm_x, mm_y, spikes, add)
    _pen_lift(runs, report.results, obj_t, vw, vh, r, add)
    minutes = max((play_span[1] - play_span[0]) / 60.0, 1e-6)
    add(SPIKES, len(spikes) / minutes, math.sqrt(max(len(spikes), 1)) / minutes, max(len(runs), 0), "per_min",
        raw=float(len(spikes)))
    for spot in report.hotspots:
        add(ZONE + place_id(spot.fx, spot.fy), spot.err_mm / r, spot.err_mm / max(spot.z, 1e-6) / r, spot.n,
            detail=f"{spot.kind}:{spot.dx_mm / r:.3f}:{spot.dy_mm / r:.3f}")
    warnings = (Reason("play.style.warn.mods_mismatch"),) if mismatch else ()
    return HabitProfile(tuple(habits), bool(aims), r, reach, _quantile(speeds, REACH_QUANTILE), len(aims),
                        len(jumps), play_span[1] - play_span[0], mods_known, mismatch,
                        len(spikes) >= EMI_MIN_SPIKES, tuple(spikes), warnings, from_replay)


def _aims(results: Sequence[ObjectResult], by_index: dict, objs, keys: dict, obj_t, vw: float, vh: float,
          r: float, spikes: Sequence[float]) -> list[_Aim]:
    out = []
    for res in results:
        if res.err_x_mm is None or res.err_y_mm is None or res.click_t is None \
                or objs[res.index].kind not in ("circle", "slider"):    # slider heads only; never spinners
            continue
        if math.hypot(res.err_x_mm, res.err_y_mm) > OUTLIER_R * r or _near(spikes, res.click_t, SPIKE_GUARD_S):
            continue
        prev = by_index.get(res.index - 1)
        gap = obj_t(res.index) - obj_t(res.index - 1) if res.index > 0 else 0.0
        a = _Aim(res.click_t, (res.u - 0.5) * vw, (res.v - 0.5) * vh, res.err_x_mm, res.err_y_mm, 1.0,
                 along=res.along_mm, key=keys.get(res.click_t, ""), time_err=res.time_error_ms,
                 ox=res.u * vw, oy=res.v * vh, gap=gap)
        if prev is not None:
            a.jx, a.jy = (res.u - prev.u) * vw, (res.v - prev.v) * vh
            a.dist = math.hypot(a.jx, a.jy)
            if a.dist >= JUMP_MIN_R * r:
                a.dx, a.dy = a.jx / a.dist, a.jy / a.dist
        if (0 < gap * 1000.0 < STREAM_GAP_MS) or (prev is not None and a.dist < JUMP_MIN_R * r):
            a.w = STREAM_WEIGHT          # streams and stacks: flowed through, not aimed at
        out.append(a)
    return out


def _columns(aims: Sequence[_Aim]) -> list[str]:
    """Terms the data can support: the gain / rotation matrix needs objects spread over both axes, the edge-stop
    and jump terms need jumps."""
    cols = ["ax", "ay"]
    if (max(a.px for a in aims) - min(a.px for a in aims) >= MIN_SPREAD_MM
            and max(a.py for a in aims) - min(a.py for a in aims) >= MIN_SPREAD_MM):
        cols += ["m11", "m12", "m21", "m22"]
    if sum(1 for a in aims if a.dx or a.dy) >= MIN_N:
        cols += ["c", "kx", "ky"]
    return cols


def _fit_rows(aims: Sequence[_Aim], cols: Sequence[str]) -> list[tuple[list[float], float, float]]:
    """Two rows per click (x and y equations) of ``e = a + M p + c d_hat + K j`` (``j`` = the jump vector)."""
    rows = []
    for a in aims:
        fx = {"ax": 1.0, "m11": a.px, "m12": a.py, "c": a.dx, "kx": a.jx}
        fy = {"ay": 1.0, "m21": a.px, "m22": a.py, "c": a.dy, "ky": a.jy}
        rows.append(([fx.get(c, 0.0) for c in cols], a.ex, a.w))
        rows.append(([fy.get(c, 0.0) for c in cols], a.ey, a.w))
    return rows


def _combo(beta: Sequence[float], cov: Sequence[Sequence[float]], index: dict[str, int],
           weights: dict[str, float]) -> tuple[float, float]:
    """Value and standard error of a linear combination of fitted terms (terms not in the fit count as 0)."""
    use = {k: w for k, w in weights.items() if k in index}
    value = sum(w * beta[index[k]] for k, w in use.items())
    var = sum(wi * wj * cov[index[i]][index[j]] for i, wi in use.items() for j, wj in use.items())
    return value, math.sqrt(max(var, 0.0))


def _aim_fit(aims: list[_Aim], r: float) -> list[tuple[str, float, float, int, str]] | None:
    """The joint fit ``e = a + M p + c d_hat`` (module docstring), with one pass of outlier removal.

    ``aims`` is trimmed in place to the clicks the final fit used.
    """
    if len(aims) < MIN_AIM:
        return None
    cols = _columns(aims)
    solved = wls(_fit_rows(aims, cols))
    if solved is None:
        return None
    sigma = _mad_sigma(solved[2])
    if sigma > 0:
        resid = solved[2]
        kept = [a for i, a in enumerate(aims)
                if abs(resid[2 * i]) <= OUTLIER_SIGMAS * sigma and abs(resid[2 * i + 1]) <= OUTLIER_SIGMAS * sigma]
        if MIN_AIM <= len(kept) < len(aims):
            again = wls(_fit_rows(kept, cols))
            if again is not None:
                aims[:] = kept
                solved = again
    beta, cov, resid = solved
    index = {c: k for k, c in enumerate(cols)}

    def term(weights: dict[str, float]) -> tuple[float, float]:
        return _combo(beta, cov, index, weights)

    n = len(aims)
    n_jumps = sum(1 for a in aims if a.dx or a.dy)
    scatter = math.sqrt(sum(v * v for v in resid) / max(len(resid) - len(beta), 1)) / r
    out = [(AIM_X, *(v / r for v in term({"ax": 1.0})), n, "r"),
           (AIM_Y, *(v / r for v in term({"ay": 1.0})), n, "r"),
           (SCATTER, scatter, scatter / math.sqrt(max(2 * n, 1)), n, "r")]
    if "c" in index:
        out += [(EDGE_STOP, *(v / r for v in term({"c": 1.0})), n_jumps, "r"),
                (OVERSHOOT, *term({"kx": 0.5, "ky": 0.5}), n_jumps, "frac")]
    if "m11" in index:
        # The gain the area should follow: the absolute part (positions) plus the jump part (distance-proportional
        # overshoot). In the usual cases one of them is zero and scaling the area by 1 + gain removes the error.
        gx, gy = {"m11": 1.0, "kx": 1.0}, {"m22": 1.0, "ky": 1.0}
        out += [(GAIN_X, *term(gx), n, "frac"), (GAIN_Y, *term(gy), n, "frac"),
                (SIZE, *term({k: v / 2.0 for k, v in (gx | gy).items()}), n, "frac"),
                (ASPECT, *term(gx | {k: -v for k, v in gy.items()}), n, "frac"),
                (ROTATION, *(math.degrees(v) for v in term({"m21": 0.5, "m12": -0.5})), n, "deg")]
    return out


def residuals(aims: Sequence[_Aim], fit: Sequence[tuple[str, float, float, int, str]], r: float
              ) -> list[tuple[float, float]]:
    """Click errors minus the position part of the fit (the gain / rotation / edge-stop terms), in mm."""
    v = {k: val for k, val, _se, _n, _u in fit}
    k = v.get(OVERSHOOT, 0.0)
    gx, gy = v.get(GAIN_X, k) - k, v.get(GAIN_Y, k) - k       # the absolute (position) part of each gain
    rot, c = math.radians(v.get(ROTATION, 0.0)), v.get(EDGE_STOP, 0.0) * r
    return [(a.ex - gx * a.px + rot * a.py - c * a.dx - k * a.jx, a.ey - gy * a.py - rot * a.px - c * a.dy - k * a.jy)
            for a in aims]


def _jump_habits(jumps: Sequence[_Aim], r: float, add) -> None:
    """Long jumps per direction (descriptive) and the speed-dependent trail (distance held fixed)."""
    plain = [a for a in jumps if a.w >= 1.0]
    for name in DIRECTIONS:
        group = [a.along / r for a in plain if a.dist >= LONG_JUMP_R * r and _direction(a.dx, a.dy) == name]
        mean = _wmean(group)
        if mean is not None:
            add(LONG_JUMP + name, mean[0], mean[1], len(group), "r")
    timed = [a for a in plain if a.gap > 0]
    rows = [([1.0, a.dist, a.dist / a.gap / 1000.0], a.along, 1.0) for a in timed]
    fit = wls(rows) if len(rows) >= MIN_N else None
    if fit is not None:
        beta, cov, _ = fit
        add(TRAIL, beta[2] / r, math.sqrt(max(cov[2][2], 0.0)) / r, len(rows), "r_per_m_s")


def _timing_habits(report: PlayReport, aims: Sequence[_Aim], offset_ms: float, scale: float, clock_known: bool,
                   add) -> None:
    """UR (as osu! shows it), the gap between the two most used keys, and the mean lateness when the clock
    is the map's own (a replay): the alignment absorbs it in a live capture."""
    errs = [a.time_err for a in aims if a.time_err is not None]
    if len(errs) >= MIN_N:
        sd = statistics.pstdev(errs)
        ur = 10.0 * sd * scale
        add(UR, ur, ur / math.sqrt(2.0 * (len(errs) - 1)), len(errs), "ur", raw=ur)
        if clock_known:
            lateness = offset_ms + statistics.fmean(errs)
            add(TIMING, lateness, sd / math.sqrt(len(errs)), len(errs), "ms")
    per_key: dict[str, list[float]] = {}
    for a in aims:
        if a.time_err is not None and a.key:
            per_key.setdefault(a.key, []).append(a.time_err)
    top = sorted(per_key, key=lambda k: -len(per_key[k]))[:2]
    if len(top) == 2 and min(len(per_key[k]) for k in top) >= MIN_N:
        first, second = (per_key[k] for k in top)
        gap = statistics.fmean(first) - statistics.fmean(second)
        se = math.sqrt(statistics.pvariance(first) / len(first) + statistics.pvariance(second) / len(second))
        add(KEY_GAP, gap, se, len(first) + len(second), "ms", detail=f"{top[0]}|{top[1]}")


def _taps(results: Sequence[ObjectResult], by_index: dict, vw: float, vh: float, r: float,
          spikes: Sequence[float]) -> list[_Aim]:
    """Every clicked object reached by a jump, however far the click landed (an early tap lands far: leaving those
    out, as the aim fit does, would hide exactly this habit)."""
    out = []
    for res in results:
        prev = by_index.get(res.index - 1)
        if res.click_t is None or res.err_mm is None or prev is None or _near(spikes, res.click_t, SPIKE_GUARD_S):
            continue
        if math.hypot((res.u - prev.u) * vw, (res.v - prev.v) * vh) >= JUMP_MIN_R * r:
            out.append(_Aim(res.click_t, 0.0, 0.0, 0.0, 0.0, 1.0, ox=res.u * vw, oy=res.v * vh))
    return out


def _tap_lead(jumps: Sequence[_Aim], trace: _Trace, r: float, add) -> None:
    """Time from the cursor entering the circle to the key-down (ms). Negative = tapped before arriving."""
    t = trace.t
    leads = []
    for a in jumps:
        i = bisect.bisect_left(t, a.t)
        if i <= 0 or i >= len(t):
            continue
        def inside(k: int) -> bool:
            return math.hypot(trace.x[k] - a.ox, trace.y[k] - a.oy) <= r
        pos = trace.at(a.t)
        if pos is None:
            continue
        if math.hypot(pos[0] - a.ox, pos[1] - a.oy) <= r:
            k = i - 1
            while k > 0 and inside(k - 1) and a.t - t[k - 1] <= TAP_BACK_S:
                k -= 1
            if a.t - t[k] < TAP_BACK_S:                # a cursor already resting there says nothing
                leads.append((a.t - t[k]) * 1000.0)
        else:
            k = i
            while k < len(t) and t[k] - a.t <= TAP_AHEAD_S and not inside(k):
                k += 1
            if k < len(t) and t[k] - a.t <= TAP_AHEAD_S:
                leads.append((a.t - t[k]) * 1000.0)
    mean = _wmean(leads)
    if mean is not None:
        add(TAP_LEAD, mean[0], mean[1], len(leads), "ms")


def _stillness(trace: _Trace, obj_times: Sequence[float], spikes: Sequence[float], span: tuple[float, float],
               r: float, add) -> None:
    """Jitter of the still cursor during play, and how much more it shakes in breaks (pen held higher)."""
    t = trace.t
    lo, hi = bisect.bisect_left(t, span[0]), bisect.bisect_right(t, span[1])
    play, breaks = [], []
    i = lo
    while i < hi:
        j = bisect.bisect_left(t, t[i] + STILL_WINDOW_S, i, hi)
        if j - i >= 8:
            xs, ys = trace.x[i:j], trace.y[i:j]
            if math.hypot(xs[-1] - xs[0], ys[-1] - ys[0]) < STILL_NET_MM and max(xs) - min(xs) < 2 * STILL_NET_MM \
                    and max(ys) - min(ys) < 2 * STILL_NET_MM and len(set(zip(xs, ys))) > 2 \
                    and not _near(spikes, (t[i] + t[j - 1]) / 2, STILL_WINDOW_S + SPIKE_GUARD_S):
                rms = _highpass_rms(xs, ys)
                mid = (t[i] + t[j - 1]) / 2
                if _near(obj_times, mid, PLAY_NEAR_S):
                    play.append(rms)
                elif not _near(obj_times, mid, BREAK_FAR_S):
                    breaks.append(rms)
        i = max(j, i + 1)
    if len(play) >= MIN_WINDOWS:
        m, se = _wmean(play) or (0.0, 0.0)
        add(JITTER, (m - JITTER_NORMAL_MM) / r, se / r, len(play), "r", raw=m)
        if len(breaks) >= MIN_WINDOWS:
            mb, seb = _wmean(breaks) or (0.0, 0.0)
            add(JITTER_HOVER, (mb - m) / r, math.hypot(se, seb) / r, len(breaks), "r", raw=mb)


def _highpass_rms(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Noise of a still cursor: second differences of its distinct positions (a high-pass filter that removes
    slow, smooth movement; ``var(d2) = 6 var(noise)`` for white noise), both axes together, in mm."""
    pts = [p for k, p in enumerate(zip(xs, ys)) if k == 0 or p != (xs[k - 1], ys[k - 1])]
    if len(pts) < 3:
        return 0.0
    total = sum((a[0] - 2 * b[0] + c[0]) ** 2 + (a[1] - 2 * b[1] + c[1]) ** 2
                for a, b, c in zip(pts, pts[1:], pts[2:]))
    return math.sqrt(total / (6.0 * (len(pts) - 2)))


def _time_habits(aims: list[_Aim], fit, r: float, add) -> None:
    """Drift (aim bias moving during the map, per minute) and fatigue (error, last third vs first third)."""
    if len(aims) < MIN_AIM:
        return
    res = residuals(aims, fit, r)
    t0 = min(a.t for a in aims)
    span = max(a.t for a in aims) - t0
    if span >= DRIFT_MIN_S:
        for key, idx in ((DRIFT_X, 0), (DRIFT_Y, 1)):
            rows = [([1.0, (a.t - t0) / 60.0], e[idx], a.w) for a, e in zip(aims, res)]
            fitted = wls(rows)
            if fitted is not None:
                beta, cov, _ = fitted
                add(key, beta[1] / r, math.sqrt(max(cov[1][1], 0.0)) / r, len(rows), "r_per_min")
    if span >= FATIGUE_MIN_S:
        first = [math.hypot(*e) / r for a, e in zip(aims, res) if a.t - t0 <= span / 3]
        last = [math.hypot(*e) / r for a, e in zip(aims, res) if a.t - t0 >= 2 * span / 3]
        mf, ml = _wmean(first), _wmean(last)
        if mf is not None and ml is not None:
            add(FATIGUE, ml[0] - mf[0], math.hypot(ml[1], mf[1]), len(first) + len(last), "r")


def _edges(trace: _Trace, results: Sequence[ObjectResult], obj_t, vw: float, vh: float, mm_x: float, mm_y: float,
           spikes: Sequence[float], add) -> None:
    """Share of object approaches with the cursor pinned at a screen edge (the pen went past the area)."""
    if not results or len(trace) < 2:
        return
    t = trace.t
    hits = dict.fromkeys(SIDES, 0)
    for res in results:
        end = obj_t(res.index)
        lo, hi = bisect.bisect_left(t, end - APPROACH_S), bisect.bisect_right(t, end)
        if hi - lo < 2 or _near(spikes, end - APPROACH_S / 2, APPROACH_S / 2 + SPIKE_GUARD_S):
            continue
        xs, ys = trace.x[lo:hi], trace.y[lo:hi]
        pinned = {"left": min(xs) <= 0.5 * mm_x, "right": max(xs) >= vw - 1.5 * mm_x,
                  "top": min(ys) <= 0.5 * mm_y, "bottom": max(ys) >= vh - 1.5 * mm_y}
        for side, yes in pinned.items():
            hits[side] += yes
    n = len(results)
    for side, k in hits.items():
        p = k / n
        add(EDGE_CLAMP + side, p, math.sqrt(max(p * (1 - p), 1.0 / n) / n), n, "frac")


def _pen_lift(runs: Sequence[tuple[float, float, float]], results: Sequence[ObjectResult], obj_t, vw: float,
              vh: float, r: float, add) -> None:
    """Frozen cursor (no new position for ``PEN_LIFT_S``) while an object far from it is due."""
    if not results or len(runs) < 2:
        return
    starts = [run[0] for run in runs]
    lifts, frozen_runs = 0, set()
    for res in results:
        when = obj_t(res.index)
        k = bisect.bisect_right(starts, when) - 1
        if k < 0 or k + 1 >= len(runs) or k in frozen_runs:
            continue
        t0, x, y = runs[k]
        if runs[k + 1][0] - t0 >= PEN_LIFT_S and math.hypot(x - res.u * vw, y - res.v * vh) >= PEN_LIFT_FAR_R * r:
            lifts += 1
            frozen_runs.add(k)
    n = len(results)
    p = lifts / n
    add(PEN_LIFT, p, math.sqrt(max(p * (1 - p), 1.0 / n) / n), n, "frac", raw=float(lifts))


# ----------------------------------------------------------------- what a miss costs
def _i0e(x: float) -> float:
    """``I0(x) * exp(-x)`` (modified Bessel function, exponentially scaled), x >= 0."""
    if x < 15.0:
        term, total, k = 1.0, 1.0, 1
        q = x * x / 4.0
        while term > 1e-12 * total:
            term *= q / (k * k)
            total += term
            k += 1
        return total * math.exp(-x)
    return (1.0 + 1.0 / (8.0 * x) + 9.0 / (128.0 * x * x)) / math.sqrt(2.0 * math.pi * x)


def hit_chance(offset: float, scatter: float, steps: int = 64) -> float:
    """Chance that a click lands inside the circle (radius 1) when aim is offset by ``offset`` radii with a
    round Gaussian scatter of ``scatter`` radii per axis (Rice distribution, Simpson's rule)."""
    s2 = max(scatter, 1e-3) ** 2
    def density(rho: float) -> float:
        return rho / s2 * math.exp(-(rho - offset) ** 2 / (2 * s2)) * _i0e(rho * offset / s2)
    h = 1.0 / steps
    total = density(0.0) + density(1.0)
    for i in range(1, steps):
        total += (4 if i % 2 else 2) * density(i * h)
    return min(max(total * h / 3.0, 0.0), 1.0)


def miss_cost(offset: float, scatter: float) -> float:
    """Extra share of objects missed because of a systematic ``offset`` (radii) on top of the ``scatter``."""
    return max(hit_chance(0.0, scatter) - hit_chance(abs(offset), scatter), 0.0)
