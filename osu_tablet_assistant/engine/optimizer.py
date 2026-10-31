"""Round-by-round tuning of area and filters from real plays (pure, deterministic).

One round = the player plays a map, ``playobserve`` produces a ``PlayReport``,
``record_round`` scores it with ONE comparable cost (lower is better), and
``propose`` suggests ONE change so the effect can be attributed:

1. **shift** the area by the average cursor offset (the "bias" of the reach fit);
2. **size**: scale the area by the per-axis gain (with the aspect locked, the mean of both
   gains is used and a reason says so);
3. **local**: when the whole-play pattern needs nothing but the fine grid found problem regions
   (``report.hotspots``), a shift weighted by the share of objects in them (at most
   ``LOCAL_MAX_SHIFT_MM``, needs ``LOCAL_MIN_N`` objects). One small corner patch therefore moves the area
   little or not at all; when nothing is worth changing the proposal is "none" with "play more of that
   zone" and where it is;
4. **filters**, only through ``adapt.step`` so the plugin policy and the safety limits of the
   companion mode apply: lag on fast jumps -> "more responsive", jitter on slow parts -> "steadier
   at rest", both -> nothing (ambiguous).

Direction of a shift. The cursor error is ``cursor - object``. Moving the area by ``+e`` (in the
player's view) shifts the cursor by ``-e`` for the same pen position, so a cursor that lands left of
where it should (``e_x < 0``) is fixed by moving the area left: the screen edge then maps to a place the
pen really reaches.

Safety: a trust region (halved when a round is worse or a movement reverses), never leave the tablet
(``areafit``), never repeat a movement that was just undone, and return to the best round when the
current one is worse by more than the noise. **Stop rule**: when the last two accepted rounds each
improve the cost by less than ``max(2 %, noise)`` or the proposed movement is below the minimum step,
the verdict is ``plateau``. With too little data (few matched objects, bad alignment) the verdict is
``insufficient`` and nothing changes.

Every reference value below is an estimate that has not been calibrated on real players.
"""
from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

from ..knowledge.filters import FilterSpec, load_catalog
from ..models import FilterConfig, Priority, ProfileChanges, ProfileInfo, Reason, Rect, TabletSpec, UserPreferences
from . import adapt, areafit
from .findings import hotspot_reason
from .hotspots import place_id
from .playobserve import MIN_MATCHED, MIN_REACH_POINTS
from .playtypes import (HIT, IMPROVING, INSUFFICIENT, PLATEAU, START, WORSE, Convergence, PlayReport, RoundRecord)

# ----------------------------------------------------------------- cost
# Each component becomes badness v / (v + ref) in [0, 1): 0 perfect, ``ref`` = judged half-bad.
COST_REF = {"miss": 0.10,      # fraction of objects missed / clicked outside the circle
            "error": 0.50,     # mean distance to the centre, in circle radii
            "timing": 30.0,    # ms, robust spread of click times
            "reach": 0.05,     # mean |gain - 1|
            "lag": 1.0,        # mm behind the path on fast jumps
            "jitter": 0.05}    # mm RMS while resting
COST_WEIGHTS = {"miss": 0.35, "error": 0.25, "timing": 0.10, "reach": 0.15, "lag": 0.075, "jitter": 0.075}
NEUTRAL_BADNESS = 0.5          # a component that could not be measured counts as half-bad (fixed basis)
BOOTSTRAP_ROUNDS = 60
MIN_JUDGED = 30                # fewer judged objects: the round is not accepted
# ------------------------------------------------------------ stop rule / trust
PLATEAU_GAIN = 0.02            # a round must improve the cost by this fraction ...
MAX_NOISE_FRACTION = 0.25      # ... or the noise (capped at this fraction of the cost)
MIN_ABSOLUTE_WORSE = 0.01      # a round is "worse" than the best only past this absolute cost
START_TRUST, MIN_TRUST = 1.0, 0.125
MAX_SHIFT_MM = 3.0             # largest shift per round at full trust
MIN_SHIFT_MM = 0.5             # smaller shifts are noise
MAX_SCALE_STEP = 0.06          # largest relative size change per round at full trust
MIN_SCALE_STEP = 0.015
LAG_MM, LAG_RADIUS_FRACTION = 0.8, 0.25
JITTER_MM = 0.06
MIN_FAST, MIN_SLOW = 4, 5
UNDO_MEMORY = 6
LOCAL_MIN_N = 20               # objects inside the hotspots before they may move the area at all
LOCAL_MAX_SHIFT_MM = 1.5       # a shift for local problems is never larger than this


# ------------------------------------------------------------------- cost
def _badness(value: float | None, ref: float) -> float:
    if value is None or not math.isfinite(value):
        return NEUTRAL_BADNESS
    value = abs(value)
    return value / (value + ref)


def _weights(prefs: UserPreferences | None) -> dict[str, float]:
    w = dict(COST_WEIGHTS)
    if prefs is not None:
        if prefs.priority is Priority.LATENCY:
            w["lag"], w["jitter"] = 0.12, 0.03
        elif prefs.priority is Priority.SMOOTHNESS:
            w["lag"], w["jitter"] = 0.03, 0.12
    return w


def _object_terms(results: Sequence[Any], radius_mm: float) -> tuple[float, float | None, float | None] | None:
    """(miss rate, mean error / radius, timing spread) over some ObjectResults; None when empty."""
    if not results:
        return None
    miss = sum(1 for r in results if r.verdict != HIT) / len(results)
    clicked = [r for r in results if r.err_mm is not None]
    err = statistics.fmean(r.err_mm for r in clicked) / radius_mm if clicked and radius_mm > 0 else None
    times = [r.time_error_ms for r in clicked if r.time_error_ms is not None]
    spread = None
    if len(times) >= 3:
        med = statistics.median(times)
        spread = statistics.median(abs(t - med) for t in times) * 1.4826
    return miss, err, spread


def _assemble(terms: tuple[float, float | None, float | None], report: PlayReport,
              weights: Mapping[str, float]) -> float:
    miss, err, timing = terms
    reach = None
    if report.reach is not None and report.reach.n >= MIN_REACH_POINTS:
        reach = (abs(report.reach.gain_x - 1.0) + abs(report.reach.gain_y - 1.0)) / 2.0
    feel = report.feel
    parts = {"miss": _badness(miss, COST_REF["miss"]), "error": _badness(err, COST_REF["error"]),
             "timing": _badness(timing, COST_REF["timing"]), "reach": _badness(reach, COST_REF["reach"]),
             "lag": _badness(feel.lag_mm if feel and feel.lag_mm is not None and feel.lag_mm < 0 else
                             (0.0 if feel and feel.lag_mm is not None else None), COST_REF["lag"]),
             "jitter": _badness(feel.jitter_mm if feel else None, COST_REF["jitter"])}
    total = sum(weights.values())
    return sum(weights[k] * parts[k] for k in weights) / total


def round_cost(report: PlayReport, prefs: UserPreferences | None = None, seed: int = 0
               ) -> tuple[float | None, tuple[float, float] | None]:
    """ONE comparable number for a round (lower is better) and its 90 % bootstrap interval.

    ``(None, None)`` while the report cannot be trusted or has too few judged objects. Reference
    values are absolute (not relative to other rounds); a component that was not measurable counts as
    ``NEUTRAL_BADNESS`` so the basis does not change from round to round. The interval resamples the
    judged objects; the reach and feel terms are held fixed.
    """
    if not report.trusted or len(report.results) < MIN_JUDGED or not report.radius_mm:
        return None, None
    weights = _weights(prefs)
    terms = _object_terms(report.results, report.radius_mm)
    if terms is None:
        return None, None
    cost = _assemble(terms, report, weights)
    rng = random.Random(seed)
    samples = []
    for _ in range(BOOTSTRAP_ROUNDS):
        pick = rng.choices(report.results, k=len(report.results))
        t = _object_terms(pick, report.radius_mm)
        if t is not None:
            samples.append(_assemble(t, report, weights))
    samples.sort()
    if len(samples) < BOOTSTRAP_ROUNDS // 2:
        return cost, None
    lo, hi = samples[int(0.05 * (len(samples) - 1))], samples[int(round(0.95 * (len(samples) - 1)))]
    return cost, (min(lo, cost), max(hi, cost))


# ------------------------------------------------------------------ state
@dataclass(frozen=True)
class OptimizerState:
    rounds: tuple[RoundRecord, ...] = ()
    adapt: adapt.AdaptState = field(default_factory=adapt.AdaptState)
    trust: float = START_TRUST
    last_move: tuple[str, float, float] | None = None       # (kind, x, y) of the last applied movement
    undone: tuple[tuple[str, int, int], ...] = ()           # recent movements that made things worse
    best: int | None = None                                  # index of the best accepted round


def new_state() -> OptimizerState:
    return OptimizerState()


def _sign(value: float) -> int:
    return 0 if abs(value) < 1e-9 else (1 if value > 0 else -1)


def _noise(rec: RoundRecord) -> float:
    if rec.cost is None or rec.cost_ci is None:
        return 0.0
    return (rec.cost_ci[1] - rec.cost_ci[0]) / 2.0


def accepted_rounds(state: OptimizerState) -> list[RoundRecord]:
    return [r for r in state.rounds if r.accepted and r.cost is not None]


def record_round(state: OptimizerState, report: PlayReport, area: Rect, filters: tuple[FilterConfig, ...] = (),
                 *, change: Sequence[Reason] = (), kind: str = "start", move: tuple[float, float] = (0.0, 0.0),
                 new_adapt: adapt.AdaptState | None = None, prefs: UserPreferences | None = None
                 ) -> tuple[OptimizerState, PlayReport]:
    """Score a played round and add it. Returns the new state and the report with ``cost``/``cost_ci`` filled.

    ``change`` / ``kind`` / ``move`` describe what was applied BEFORE this round (a proposal's
    ``reasons``, ``kind`` and ``move``). A round whose cost is worse than the previous accepted one by more
    than the noise halves the trust region and remembers the movement so it is not repeated.
    """
    cost, ci = round_cost(report, prefs, seed=len(state.rounds))
    accepted = cost is not None and report.alignment.matched >= MIN_MATCHED
    if not accepted:
        cost, ci = None, None
    rec = RoundRecord(len(state.rounds), cost, ci, report.alignment.matched, area, tuple(filters), tuple(change),
                      accepted, math.hypot(*move), kind)
    previous = accepted_rounds(state)
    trust, undone, last_move = state.trust, state.undone, state.last_move
    if accepted and kind in ("shift", "scale", "filter") and previous:
        before = previous[-1]
        if rec.cost > before.cost + max(_noise(rec), _noise(before), MIN_ABSOLUTE_WORSE):
            trust = max(MIN_TRUST, trust / 2.0)
            undone = (undone + ((kind, _sign(move[0]), _sign(move[1])),))[-UNDO_MEMORY:]
    if kind in ("shift", "scale") and last_move and last_move[0] == kind \
            and last_move[1] * move[0] + last_move[2] * move[1] < 0:
        trust = max(MIN_TRUST, trust / 2.0)       # the movement reversed: finer steps
    if kind in ("shift", "scale"):
        last_move = (kind, move[0], move[1])
    rounds = state.rounds + (rec,)
    best = state.best
    if accepted and (best is None or rec.cost < rounds[best].cost):
        best = rec.index
    new = replace(state, rounds=rounds, trust=trust, undone=undone, last_move=last_move, best=best,
                  adapt=new_adapt if new_adapt is not None else state.adapt)
    return new, replace(report, cost=cost, cost_ci=ci)


def mark_undone(state: OptimizerState, kind: str, move: tuple[float, float]) -> OptimizerState:
    """The player undid a change: never propose that movement again right away."""
    entry = (kind, _sign(move[0]), _sign(move[1]))
    return replace(state, undone=(state.undone + (entry,))[-UNDO_MEMORY:])


# ------------------------------------------------------------ convergence
def _gain(prev: RoundRecord, cur: RoundRecord) -> float:
    return (prev.cost - cur.cost) / prev.cost if prev.cost > 1e-9 else 0.0


def _threshold(*recs: RoundRecord) -> float:
    noise = max((_noise(r) / r.cost for r in recs if r.cost), default=0.0)
    return max(PLATEAU_GAIN, min(noise, MAX_NOISE_FRACTION))


def convergence(state: OptimizerState) -> Convergence:
    """Where the tuning stands: start / improving / plateau / worse / insufficient."""
    if state.rounds and not state.rounds[-1].accepted:
        return Convergence(INSUFFICIENT, Reason("play.conv.insufficient"), len(accepted_rounds(state)))
    done = accepted_rounds(state)
    if len(done) < 2:
        return Convergence(START, Reason("play.conv.start"), len(done))
    last_gain = _gain(done[-2], done[-1])
    thr = _threshold(done[-2], done[-1])
    if last_gain < -thr:
        return Convergence(WORSE, Reason("play.conv.worse"), len(done), last_gain)
    if len(done) >= 3 and last_gain < thr and _gain(done[-3], done[-2]) < _threshold(done[-3], done[-2]):
        return Convergence(PLATEAU, Reason("play.conv.plateau"), len(done), last_gain)
    return Convergence(IMPROVING, Reason("play.conv.improving"), len(done), last_gain)


# ---------------------------------------------------------------- proposal
@dataclass(frozen=True)
class RoundProposal:
    """What to do next. ``kind`` is shift | scale | filter | revert | none."""
    kind: str
    verdict: str
    message: Reason | None = None
    reasons: tuple[Reason, ...] = ()
    area: Rect | None = None
    filters: tuple[FilterConfig, ...] | None = None
    move: tuple[float, float] = (0.0, 0.0)       # shift: view mm; scale: relative change per axis
    new_adapt: adapt.AdaptState | None = None
    revert_to: int | None = None
    lock_aspect_note: bool = False

    @property
    def changes(self) -> ProfileChanges | None:
        """The profile changes to apply (None when the proposal is "do nothing")."""
        if self.kind == "none" or (self.area is None and self.filters is None):
            return None
        return ProfileChanges(tablet_area=self.area, filters=self.filters)

    @property
    def move_mm(self) -> float:
        return math.hypot(*self.move) if self.kind == "shift" else 0.0


def _none(verdict: str, message: Reason, reasons: Sequence[Reason] = ()) -> RoundProposal:
    return RoundProposal("none", verdict, message, tuple(reasons))


def _repeats(state: OptimizerState, kind: str, x: float, y: float) -> bool:
    return (kind, _sign(x), _sign(y)) in state.undone


def _shift(state: OptimizerState, profile: ProfileInfo, report: PlayReport, tablet: TabletSpec | None
           ) -> RoundProposal | None:
    reach = report.reach
    if reach is None or reach.n < MIN_REACH_POINTS:
        return None
    ex, ey = reach.bias_x_mm, reach.bias_y_mm
    length = math.hypot(ex, ey)
    if length < MIN_SHIFT_MM:
        return None
    cap = MAX_SHIFT_MM * state.trust
    if length > cap:
        ex, ey = ex * cap / length, ey * cap / length
    if _repeats(state, "shift", ex, ey):
        return None
    area, reasons = areafit.shift_area(profile.tablet_area, ex, ey, tablet)
    if area == profile.tablet_area:
        return None
    # what really moved, in the player's view
    mx, my = areafit.tablet_to_view(area.x - profile.tablet_area.x, area.y - profile.tablet_area.y,
                                    area.rotation)
    if math.hypot(mx, my) < MIN_SHIFT_MM * 0.5:
        return None
    lead = Reason("play.prop.shift", {"dx": f"{mx:+.1f}", "dy": f"{my:+.1f}"})
    return RoundProposal("shift", IMPROVING, Reason("play.msg.shift"), (lead,) + tuple(reasons), area=area,
                         move=(mx, my))


def _local(state: OptimizerState, profile: ProfileInfo, report: PlayReport, tablet: TabletSpec | None
           ) -> RoundProposal | None:
    """A shift for local problem regions (hotspots), weighted by how much of the play they cover.

    Each hotspot's error counts in proportion to its share of the clicked objects, so one small corner patch
    moves the area only a little (and usually not at all: below the minimum step). Opposite hotspots cancel.
    Without enough objects in them, nothing moves (``propose`` then says "play more of that zone").
    """
    spots = report.hotspots
    if not spots or sum(s.n for s in spots) < LOCAL_MIN_N:
        return None
    ex = sum(s.dx_mm * s.share for s in spots)
    ey = sum(s.dy_mm * s.share for s in spots)
    length = math.hypot(ex, ey)
    if length < MIN_SHIFT_MM:
        return None
    cap = min(MAX_SHIFT_MM * state.trust, LOCAL_MAX_SHIFT_MM)
    if length > cap:
        ex, ey = ex * cap / length, ey * cap / length
    if _repeats(state, "shift", ex, ey):
        return None
    area, reasons = areafit.shift_area(profile.tablet_area, ex, ey, tablet)
    if area == profile.tablet_area:
        return None
    mx, my = areafit.tablet_to_view(area.x - profile.tablet_area.x, area.y - profile.tablet_area.y, area.rotation)
    if math.hypot(mx, my) < MIN_SHIFT_MM * 0.5:
        return None
    lead = Reason("play.prop.shift", {"dx": f"{mx:+.1f}", "dy": f"{my:+.1f}"})
    why = Reason("play.prop.local", {"place": place_id(spots[0].fx, spots[0].fy)})
    return RoundProposal("shift", IMPROVING, Reason("play.msg.shift_local"), (lead, why) + tuple(reasons), area=area,
                         move=(mx, my))


def _practice(report: PlayReport) -> RoundProposal | None:
    """Only a small region is off and nothing is worth changing: say where, and to play that zone more."""
    if not report.hotspots:
        return None
    top = report.hotspots[0]
    return _none(PLATEAU, Reason("play.msg.practice_zone", {"place": place_id(top.fx, top.fy)}),
                 (hotspot_reason(top),))


def _significant(gain: float, ci: tuple[float, float]) -> bool:
    return (ci[1] < 1.0 or ci[0] > 1.0) and abs(gain - 1.0) >= MIN_SCALE_STEP


def _scale(state: OptimizerState, profile: ProfileInfo, report: PlayReport, tablet: TabletSpec | None,
           aspect_lock: bool) -> RoundProposal | None:
    reach = report.reach
    if reach is None or reach.n < MIN_REACH_POINTS:
        return None
    cap = MAX_SCALE_STEP * state.trust
    factors = []
    for gain, ci in ((reach.gain_x, reach.ci_x), (reach.gain_y, reach.ci_y)):
        f = gain if _significant(gain, ci) else 1.0
        factors.append(min(max(f, 1.0 - cap), 1.0 + cap))
    fx, fy = factors
    if aspect_lock:
        mean = (fx + fy) / 2.0
        if abs(mean - 1.0) < MIN_SCALE_STEP / 2:
            return None
    elif max(abs(fx - 1.0), abs(fy - 1.0)) < MIN_SCALE_STEP:
        return None
    if _repeats(state, "scale", fx - 1.0, fy - 1.0):
        return None
    area, reasons = areafit.scale_area_axes(profile.tablet_area, fx, fy, tablet, lock_aspect=aspect_lock)
    if area == profile.tablet_area:
        return None
    rel = (area.width / profile.tablet_area.width - 1.0, area.height / profile.tablet_area.height - 1.0)
    return RoundProposal("scale", IMPROVING, Reason("play.msg.scale"), tuple(reasons), area=area, move=rel,
                         lock_aspect_note=aspect_lock and abs(fx - fy) > 0.01)


def _filter(state: OptimizerState, profile: ProfileInfo, report: PlayReport, tablet: TabletSpec | None,
            policy: adapt.TuningPolicy | None, catalog: Mapping[str, FilterSpec] | None
            ) -> RoundProposal | None:
    feel = report.feel
    if feel is None:
        return None
    lag_limit = max(LAG_MM, LAG_RADIUS_FRACTION * (report.radius_mm or 0.0))
    laggy = feel.lag_mm is not None and feel.n_fast >= MIN_FAST and feel.lag_mm < -lag_limit
    shaky = feel.jitter_mm is not None and feel.n_slow >= MIN_SLOW and feel.jitter_mm > JITTER_MM
    if laggy == shaky:       # nothing wrong, or both (a filter would fix one by worsening the other)
        return None
    intent = adapt.Intent.RESPONSIVE if laggy else adapt.Intent.PRECISE
    if ("filter", 1 if shaky else -1, 0) in state.undone:
        return None
    result = adapt.step(state.adapt, profile, intent, policy, catalog, tablet)
    if result.changes is None or result.changes.filters is None:
        return None
    message = Reason("play.msg.filter_responsive" if laggy else "play.msg.filter_steady")
    return RoundProposal("filter", IMPROVING, message, tuple(result.reasons), filters=result.changes.filters,
                         move=(1.0 if shaky else -1.0, 0.0), new_adapt=result.state)


def propose(state: OptimizerState, profile: ProfileInfo, report: PlayReport, tablet: TabletSpec | None = None,
            policy: adapt.TuningPolicy | None = None, aspect_lock: bool = True, allow_filters: bool = True,
            catalog: Mapping[str, FilterSpec] | None = None) -> RoundProposal:
    """ONE change for the next round, or none. ``state`` must already contain the round of ``report``.

    ``profile`` carries the area and filters that were live during that round. With too little data
    nothing changes (verdict ``insufficient``); when the stop rule holds the verdict is ``plateau``.
    """
    if not state.rounds or not state.rounds[-1].accepted or not report.trusted:
        return _none(INSUFFICIENT, Reason("play.msg.play_more"), report.warnings)
    verdict = convergence(state)
    last = state.rounds[-1]
    if state.best is not None and state.best != last.index and last.kind != "revert":
        best = state.rounds[state.best]
        if last.cost > best.cost + max(_noise(last), _noise(best), MIN_ABSOLUTE_WORSE):
            return RoundProposal("revert", WORSE, Reason("play.msg.revert"),
                                 (Reason("play.prop.revert", {"round": best.index + 1}),),
                                 area=best.area, filters=best.filters, revert_to=best.index)
    if verdict.verdict == PLATEAU:
        return _none(PLATEAU, verdict.message)
    catalog = catalog if catalog is not None else load_catalog()
    for make in (lambda: _shift(state, profile, report, tablet),
                 lambda: _scale(state, profile, report, tablet, aspect_lock),
                 lambda: _local(state, profile, report, tablet),
                 lambda: _filter(state, profile, report, tablet, policy, catalog) if allow_filters else None):
        proposal = make()
        if proposal is not None:
            return replace(proposal, verdict=verdict.verdict if verdict.verdict != START else IMPROVING)
    return _practice(report) or _none(PLATEAU, Reason("play.conv.nothing_left"))
