"""The coach's rulebook: what an experienced player would change for these habits (pure, no tkinter).

The 18 rules of the community research (``community-practice.md``, section 2) are kept as data
(:data:`RULES`, with their sources) and :func:`suggest` turns pooled habits (``engine.habitstore.HabitView``)
plus the current area, filters and tablet into ordered :class:`Suggestion` s. The setup adapts to the player;
nothing here ever asks the player to change how they play.

How a rule fires (every threshold is a heuristic, named below):

* the habit is **actionable** in the long-term view: significant (> 2 SE), repeated in at least two plays and
  backed by 50+ objects / jumps (``habitstore``), and this session does not clearly say the opposite;
* it **plausibly costs hits**: a systematic error of that size, on top of the player's own scatter, misses at
  least ``MISS_COST_MIN`` more objects (``habits.miss_cost``; research rule 16: "big enough to feel" and it
  costs points). Otherwise the verdict is *keep the current setting* (rule 16);
* steps obey the caps of the research: about 3 % of size, at most 1.5 mm of shift and 2 degrees of rotation per
  round (S20, S26), times nothing else: the optimizer's trust region still applies on top.

Order (the optimizer applies only the first *change*: one change per round): interference (10) and the
adaptation window (11) block everything; then smoothing problems before the area (18 stacked filters, 3 trail
on fast jumps "reduce smoothing first"); the offset before the size (4, then 13; S22 "fix skew first"); rotation
(6); one axis (5, only with the free proportion); size (1/2); a bigger area for tremor (9); hover-height note (8)
before the lightest anti-jitter filter (7); informational notes (15 global offset, 12 hardware smoothing, the
edge-stop term of rule 2); keep (16).

Never: prediction or input-assist filters (rule 17, peppy's notice of 2026-10-14, S1): the only filter this
module ever adds is Radial Follow (tablet space, the lightest values) and only when no other smoothing is on;
lightening a filter only moves down its ``smoothing.ladder`` (which keeps prediction off). Never two smoothing
filters at once (rule 18).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from ..knowledge.filters import FilterSpec
from ..models import FilterConfig, Reason, Rect, TabletSpec
from . import habits as hb
from . import smoothing
from .habitstore import Adaptation, HabitView, PooledHabit
from .hotspots import direction_id
from .recommend import RADIAL_TABLET

# ------------------------------------------------------------- actions
SHIFT, SIZE, ROTATE = "area_shift", "area_size", "area_rotate"
LIGHTER, REMOVE, ADD_LIGHT = "smoothing_lighter", "smoothing_remove", "smoothing_add_light"
EMI, WAIT, OFFSET, NOTE, KEEP = "flag_emi", "wait", "global_offset", "note", "keep"
CHANGES = (SHIFT, SIZE, ROTATE, LIGHTER, REMOVE, ADD_LIGHT)
BLOCKING = (EMI, WAIT)
# ------------------------------------------------------------- caps (research, section 2)
SIZE_CAP = 0.03            # about 3 % of size per round (S20)
SIZE_MIN = 0.01            # smaller changes cannot be felt (and are below the optimizer's steps)
SHIFT_CAP_MM = 1.5         # at most 1.5 mm per round (S26)
SHIFT_MIN_MM = 0.3
ROTATE_CAP_DEG = 2.0       # at most 2 degrees per round (S20)
ROTATE_MIN_DEG = 0.5
# ------------------------------------------------------------- thresholds (heuristics)
MISS_COST_MIN = 0.005      # the habit must cost at least 0.5 % more misses to be worth a change
DEFAULT_SCATTER = 0.3      # aim scatter (radii) assumed when it was not measured
OFFSET_MIN_R = 0.15        # S21: a constant offset below 0.15 radii is not acted on
ASPECT_MIN = 0.02          # the two axes must differ by at least 2 % of gain
TRAIL_MIN = 0.1            # radii per m/s of jump speed before the trail is blamed on smoothing
TAP_EARLY_MS = 15.0        # tapping this much before arriving makes "falling short" a timing matter, not the area
JITTER_MIN_R = 0.01        # still-cursor jitter this far above normal (radii) asks for the lightest filter ...
TREMOR_R = 0.05            # ... and this far for "a bigger area makes the same shake smaller" (rule 9)
HOVER_MIN_R = 0.01         # breaks shake this much more than play: a pen held high (rule 8)
PEN_LIFT_MIN = 0.01        # 1 % of objects with the pen out of range
CLAMP_MIN = 0.02           # 2 % of approaches with the cursor pinned at one edge
TIMING_MIN_MS = 10.0       # mean lateness worth pointing at the global offset
LIGHT_FILTER_MAX_MS = 3.0  # the lightest filter may add at most this (S26's "low" cap)
NOTABLE = {hb.EDGE_STOP: 0.1, hb.TAP_LEAD: 15.0, hb.KEY_GAP: 8.0, hb.DRIFT_X: 0.05, hb.DRIFT_Y: 0.05,
           hb.FATIGUE: 0.1, "long_jump": 0.15, "zone": 0.0}


@dataclass(frozen=True)
class Rule:
    """One rule of the research rulebook (``community-practice.md`` section 2), as data."""
    id: int
    symptom: str
    adaptation: str
    confidence: str            # as the research rates it
    sources: tuple[str, ...]
    measured: bool = True      # False: needs the player to tell (not measurable from a play)


RULES: tuple[Rule, ...] = (
    Rule(1, "systematic overshoot on long jumps, growing with distance", "bigger area, both axes, ~3 %/round",
         "high (direction), medium (step)", ("S20", "S21", "S22", "S8")),
    Rule(2, "systematic undershoot on long jumps, growing with distance; a constant edge stop is separated",
         "smaller area, same step; never for the constant edge stop", "high (direction), medium (separation)",
         ("S20", "S22", "S8")),
    Rule(3, "undershoot on fast jumps only, with filters on (trail)", "lighter smoothing before the area", "medium",
         ("S14", "S15", "S16")),
    Rule(4, "constant click offset, also near the centre", "shift the area, <= 1.5 mm, only if it costs misses",
         "medium", ("S21", "S22", "S26")),
    Rule(5, "horizontal and vertical errors differ", "one axis only, ~3 %, report the ratio", "medium",
         ("S20", "S22", "S23", "S24")),
    Rule(6, "rotated error, same way whatever the direction", "rotate the area the same way, <= 2 deg", "medium",
         ("S20", "S21", "S22")),
    Rule(7, "jitter while still, aim on jumps fine", "lightest Radial Follow (<= 3 ms)", "medium",
         ("S16", "S26", "S12", "S13")),
    Rule(8, "jitter only with the pen high (breaks) or tracking lost", "no smoothing; hover-height note", "medium",
         ("S28", "S8")),
    Rule(9, "big regular shake (tremor-like), not EMI", "bigger area; a mild filter second", "medium (area)",
         ("S3", "S4", "S18", "S29")),
    Rule(10, "cursor teleports / impossible speeds", "flag EMI or hardware; do not tune on this play", "high",
         ("S3", "S4")),
    Rule(11, "just moved to a new setup or away from heavy smoothing", "wait (days to two weeks) before tuning",
         "medium", ("S18", "S9")),
    Rule(12, "hardware smoothing suspected (trail with no filter on)", "advanced note only; nothing changed",
         "low-medium", ("S17", "S19")),
    Rule(13, "reach failure / the pen goes past one edge of the area", "shift towards that side", "medium",
         ("S6", "S8", "S9")),
    Rule(14, "discomfort or overstretch (reported by the player)", "smaller area or move it to the hand", "medium",
         ("S6", "S9"), measured=False),
    Rule(15, "early / late tapping, aim on target", "global offset, never the area", "high", ("S5",)),
    Rule(16, "small effect or noisy data", "keep the current setting", "high (tool practice)", ("S20", "S21")),
    Rule(17, "prediction or input-assist plugins", "never recommended", "high (risk)", ("S1", "S11")),
    Rule(18, "smoothing filters stacked", "at most one smoothing filter", "medium-high", ("S30",)),
)
SOURCES = {rule.id: rule.sources for rule in RULES}


@dataclass(frozen=True)
class Suggestion:
    """What the rulebook proposes for one habit. Only ``CHANGES`` touch the setup."""
    rule: int
    action: str
    reason: Reason                             # in the coach's voice ("un jugador con experiencia haría ...")
    confidence: str
    habits: tuple[str, ...] = ()
    accommodation: Reason | None = None        # short: "por eso el área sube un 3 %"
    axis: str = ""                             # size: both | x | y
    sign: int = 0
    step: float = 0.0                          # size: fraction; rotation: degrees
    dx: float = 0.0                            # shift, player's view mm
    dy: float = 0.0
    filter_change: tuple[str, FilterConfig | None] | None = None    # path -> new store (None: take it out)
    sources: tuple[str, ...] = ()

    @property
    def is_change(self) -> bool:
        return self.action in CHANGES

    @property
    def blocks(self) -> bool:
        return self.action in BLOCKING


@dataclass(frozen=True)
class Context:
    """The setup the habits are judged against."""
    area: Rect
    filters: tuple[FilterConfig, ...] = ()
    tablet: TabletSpec | None = None
    aspect_lock: bool = True
    catalog: Mapping[str, FilterSpec] = field(default_factory=dict)
    installed: frozenset = frozenset()
    ladders: Mapping[str, smoothing.Ladder] = field(default_factory=dict)
    rate_hz: float = 133.0
    mm_per_px: float = 0.04
    adaptation: Adaptation | None = None
    spikes_now: int = 0                        # EMI-like spikes in the play just measured
    session: HabitView | None = None           # this coach session alone (a contradiction blocks a change)
    player_filters: frozenset = frozenset()    # smoothing the player switched on / off here: theirs to decide


def _make(rule: int, action: str, key: str, params: Mapping | None = None, confidence: str = "medium",
          habits: Sequence[str] = (), acc: str | None = None, acc_params: Mapping | None = None, **kw) -> Suggestion:
    return Suggestion(rule, action, Reason(f"play.rule.why.{key}", dict(params or {})), confidence, tuple(habits),
                      Reason(f"play.rule.acc.{acc or key}", dict(acc_params if acc_params is not None else params or {})),
                      sources=SOURCES.get(rule, ()), **kw)


def _worst(*levels: str) -> str:
    order = ("low", "medium", "high")
    return min(levels, key=order.index) if levels else "low"


def smoothing_on(filters: Sequence[FilterConfig], catalog: Mapping[str, FilterSpec]) -> list[FilterConfig]:
    """Enabled smoothing filters (the known ones and every catalog filter the smoothing card offers)."""
    known = set(smoothing.IDS) | set(smoothing.offered(catalog))
    return [fc for fc in filters if fc.enabled and fc.path in known]


def is_prediction(path: str, catalog: Mapping[str, FilterSpec]) -> bool:
    """Filters the coach must never switch on: anything the catalog keeps away from automatic picks (prediction,
    input assist) and everything that is not an ordinary offered smoothing filter."""
    spec = catalog.get(path)
    return spec is None or not spec.auto_recommend or spec.advanced or path not in smoothing.offered(catalog)


class _Judge:
    """The checks every area / filter rule shares."""

    def __init__(self, view: HabitView, ctx: Context) -> None:
        self.view, self.ctx = view, ctx
        scatter = view.get(hb.SCATTER)
        self.scatter = scatter.value if scatter is not None and scatter.value > 0 else DEFAULT_SCATTER
        self.reach_r = view.reach_mm / view.r_mm if view.r_mm > 0 else 0.0

    def habit(self, key: str) -> PooledHabit | None:
        return self.view.get(key)

    def ready(self, h: PooledHabit | None) -> bool:
        """Actionable in the long-term view and not contradicted by this session."""
        if h is None or not h.actionable:
            return False
        now = self.ctx.session.get(h.key) if self.ctx.session is not None else None
        return not (now is not None and now.significant and now.value * h.value < 0)

    def costs(self, offset_r: float) -> bool:
        return hb.miss_cost(offset_r, self.scatter) >= MISS_COST_MIN


def suggest(view: HabitView, ctx: Context) -> list[Suggestion]:
    """Ordered suggestions for these habits (module docstring). Always ends with something: ``keep`` at least."""
    j = _Judge(view, ctx)
    out: list[Suggestion] = []
    if ctx.spikes_now >= hb.EMI_MIN_SPIKES:                                                   # rule 10
        out.append(_make(10, EMI, "emi", {"n": ctx.spikes_now}, "high", (hb.SPIKES,)))
    if ctx.adaptation is not None:                                                            # rule 11
        out.append(_make(11, WAIT, f"wait_{ctx.adaptation.kind}",
                         {"days": max(1, int(math.ceil(ctx.adaptation.days_left)))}, "medium"))
    if out:
        return out
    on = smoothing_on(ctx.filters, ctx.catalog)
    out += _stacked(on, ctx)                                                                  # rule 18
    out += _trail(j, on, ctx)                                                                 # rules 3, 12
    early = j.habit(hb.TAP_LEAD)
    early_tapper = early is not None and early.significant and early.value < -TAP_EARLY_MS
    out += _offset(j)                                                                         # rule 4
    if not early_tapper:
        out += _clamp(j)                                                                      # rule 13
    out += _rotation(j)                                                                       # rule 6
    if not early_tapper:
        out += _size(j, ctx)                                                                  # rules 5, 1, 2
    out += _shake(j, on, ctx)                                                                 # rules 9, 8, 7
    out += _notes(j, early_tapper)                                                            # rules 15, 2
    if not any(s.is_change for s in out):
        out.append(_make(16, KEEP, "keep", {}, "high"))
    changes = [s for s in out if s.is_change]
    rest = [s for s in out if not s.is_change]
    return changes + rest


# ------------------------------------------------------------------- the rules
def _stacked(on: Sequence[FilterConfig], ctx: Context) -> list[Suggestion]:
    """Rule 18: two smoothing filters at once -> switch the heavier one off (its values are kept)."""
    if len(on) < 2 or any(fc.path in ctx.player_filters for fc in on):
        return []                                       # a filter the player is trying: the smoothing card decides
    def delay(fc: FilterConfig) -> float:
        return smoothing.delay_ms(fc.path, fc.settings, ctx.rate_hz, ctx.mm_per_px) or 0.0
    heavy = max(on, key=delay)
    names = [_name(fc.path, ctx) for fc in on[:2]]
    return [_make(18, REMOVE, "stacked", {"a": names[0], "b": names[1], "name": _name(heavy.path, ctx)}, "medium",
                  filter_change=(heavy.path, FilterConfig(heavy.path, False, dict(heavy.settings))))]


def _name(path: str, ctx: Context) -> str:
    spec = ctx.catalog.get(path)
    return spec.display_name if spec is not None else path.rsplit(".", 1)[-1]


def _trail(j: _Judge, on: Sequence[FilterConfig], ctx: Context) -> list[Suggestion]:
    """Rule 3: the cursor trails on fast jumps only -> lighter smoothing first. Rule 12: no filter on (note)."""
    h = j.habit(hb.TRAIL)
    if not j.ready(h) or h.value > -TRAIL_MIN or not j.costs(-h.value * max(j.view.fast_speed, 0.5)):
        return []
    if not on:
        return [_make(12, NOTE, "hardware", {}, "low", (hb.TRAIL,), acc="keep")]
    if len(on) != 1 or on[0].path in ctx.player_filters:
        return []                                       # rule 18 comes first; the player's own trial is theirs
    fc = on[0]
    ladder = ctx.ladders.get(fc.path)
    level = ladder.level_of(fc.settings) if ladder is not None and ladder.adjustable else None
    if level is not None and level > 0:
        values = dict(fc.settings)
        values.update(ladder.levels[level - 1])
        new = FilterConfig(fc.path, True, values)
        key = "lighter"
    else:
        new, key = FilterConfig(fc.path, False, dict(fc.settings)), "off"
    return [_make(3, LIGHTER if key == "lighter" else REMOVE, key, {"name": _name(fc.path, ctx)},
                  h.confidence, (hb.TRAIL,), filter_change=(fc.path, new))]


def _offset(j: _Judge) -> list[Suggestion]:
    """Rule 4: a constant click offset -> move the area the same way (the cursor then lands back on the centre)."""
    bx, by = j.habit(hb.AIM_X), j.habit(hb.AIM_Y)
    if bx is None or by is None or not (j.ready(bx) or j.ready(by)):
        return []
    x = bx.value if j.ready(bx) else 0.0
    y = by.value if j.ready(by) else 0.0
    size = math.hypot(x, y)
    if size < OFFSET_MIN_R or not j.costs(size):
        return []
    mm = min(size * j.view.r_mm, SHIFT_CAP_MM)
    mm = round(mm, 1)
    if mm < SHIFT_MIN_MM:
        return []
    params = {"mm": float(mm), "towards": direction_id(x, y), "plays": min(bx.plays, by.plays)}
    used = [h.key for h in (bx, by) if j.ready(h)]
    return [_make(4, SHIFT, "shift", params, _worst(*(j.habit(k).confidence for k in used)), used,
                  dx=x / size * mm, dy=y / size * mm)]


def _clamp(j: _Judge) -> list[Suggestion]:
    """Rule 13: the cursor is pinned at one edge while approaching objects (the pen went past the area there)."""
    pinned = [h for h in j.view.with_prefix(hb.EDGE_CLAMP) if j.ready(h) and h.value >= CLAMP_MIN]
    if len(pinned) != 1:
        return []                                       # none, or both sides: that is the size rule's business
    side = pinned[0].key[len(hb.EDGE_CLAMP):]
    ux, uy = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}[side]
    mm = SHIFT_CAP_MM if pinned[0].value >= 2 * CLAMP_MIN else 1.0
    params = {"mm": float(mm), "towards": direction_id(ux, uy), "plays": pinned[0].plays}
    return [_make(13, SHIFT, "clamp", params, pinned[0].confidence, (pinned[0].key,), acc="shift",
                  dx=ux * mm, dy=uy * mm)]


def _rotation(j: _Judge) -> list[Suggestion]:
    """Rule 6: jumps land rotated the same way -> rotate the area the same way, at most 2 degrees a round."""
    h = j.habit(hb.ROTATION)
    if not j.ready(h) or abs(h.value) < ROTATE_MIN_DEG or not j.costs(math.radians(abs(h.value)) * j.reach_r):
        return []
    step = round(min(abs(h.value), ROTATE_CAP_DEG) * 2) / 2
    if step < ROTATE_MIN_DEG:
        return []
    spin = "cw" if h.value > 0 else "ccw"
    return [_make(6, ROTATE, f"rotate_{spin}", {"deg": float(step), "plays": h.plays}, h.confidence, (hb.ROTATION,),
                  sign=1 if h.value > 0 else -1, step=step)]


def _size_step(value: float) -> float:
    return round(min(abs(value), SIZE_CAP) * 200) / 200        # 0.5 % grid


def _size(j: _Judge, ctx: Context) -> list[Suggestion]:
    """Rule 5 (one axis, free proportion only) before rules 1 / 2 (both axes)."""
    out = []
    aspect, size = j.habit(hb.ASPECT), j.habit(hb.SIZE)
    if not ctx.aspect_lock and j.ready(aspect) and abs(aspect.value) >= ASPECT_MIN:
        gx, gy = j.habit(hb.GAIN_X), j.habit(hb.GAIN_Y)
        if gx is not None and gy is not None:
            axis, gain = ("x", gx) if abs(gx.value) >= abs(gy.value) else ("y", gy)
            step = _size_step(gain.value)
            if step >= SIZE_MIN and j.costs(abs(gain.value) * j.reach_r):
                sign = 1 if gain.value > 0 else -1
                way = {("x", 1): "wider", ("x", -1): "narrower", ("y", 1): "taller", ("y", -1): "shorter"}[(axis, sign)]
                w, h = ctx.area.width, ctx.area.height
                fx, fy = (1 + sign * step, 1.0) if axis == "x" else (1.0, 1 + sign * step)
                vw, vh = (w * fx, h * fy) if abs(math.sin(math.radians(ctx.area.rotation))) < 0.5 else (w * fy, h * fx)
                params = {"pct": float(round(step * 100, 1)), "way": way, "ratio": f"{vw / vh:.2f}",
                          "plays": aspect.plays}
                out.append(_make(5, SIZE, "axis", params, _worst(aspect.confidence, gain.confidence),
                                 (hb.ASPECT, gain.key), axis=axis, sign=sign, step=step))
    if j.ready(size) and abs(size.value) >= SIZE_MIN and j.costs(abs(size.value) * j.reach_r):
        step = _size_step(size.value)
        if step >= SIZE_MIN:
            sign = 1 if size.value > 0 else -1
            out.append(_make(1 if sign > 0 else 2, SIZE, "size_up" if sign > 0 else "size_down",
                             {"pct": float(round(step * 100, 1)), "plays": size.plays}, size.confidence, (hb.SIZE,),
                             axis="both", sign=sign, step=step))
    return out


def _shake(j: _Judge, on: Sequence[FilterConfig], ctx: Context) -> list[Suggestion]:
    """Rules 9 (tremor: bigger area), 8 (a pen held high: no smoothing) and 7 (the lightest anti-jitter filter)."""
    out = []
    jitter, hover, lift = j.habit(hb.JITTER), j.habit(hb.JITTER_HOVER), j.habit(hb.PEN_LIFT)
    shaky = j.ready(jitter) and jitter.value >= JITTER_MIN_R
    if (j.ready(hover) and hover.value >= HOVER_MIN_R) or (j.ready(lift) and lift.value >= PEN_LIFT_MIN):
        keys = tuple(h.key for h in (hover, lift) if h is not None and j.ready(h))
        return [_make(8, NOTE, "hover", {}, _worst(*(j.habit(k).confidence for k in keys)), keys)]
    if not shaky:
        return out
    size = j.habit(hb.SIZE)
    if jitter.value >= TREMOR_R and not (j.ready(size) and size.value < 0):
        out.append(_make(9, SIZE, "tremor", {"pct": float(SIZE_CAP * 100)}, "low" if jitter.confidence == "low"
                         else "medium", (hb.JITTER,), acc="size_up", axis="both", sign=1, step=SIZE_CAP))
    if not on and RADIAL_TABLET in ctx.installed and RADIAL_TABLET in ctx.catalog \
            and RADIAL_TABLET not in ctx.player_filters and not is_prediction(RADIAL_TABLET, ctx.catalog):
        ladder = ctx.ladders.get(RADIAL_TABLET)
        base = ladder.levels[0] if ladder is not None and ladder.adjustable else None
        filters, _prev, _own = smoothing.enable(ctx.filters, RADIAL_TABLET, ctx.catalog, base)
        new = next(fc for fc in filters if fc.path == RADIAL_TABLET)
        delay = smoothing.delay_ms(RADIAL_TABLET, new.settings, ctx.rate_hz, ctx.mm_per_px)
        if delay is not None and delay <= LIGHT_FILTER_MAX_MS:
            out.append(_make(7, ADD_LIGHT, "add_light", {"name": _name(RADIAL_TABLET, ctx)}, jitter.confidence,
                             (hb.JITTER,), filter_change=(RADIAL_TABLET, new)))
    return out


def _notes(j: _Judge, early_tapper: bool) -> list[Suggestion]:
    """Informational: global offset (rule 15), timing instead of reach (15), the edge-stop term (2)."""
    out = []
    timing = j.habit(hb.TIMING)
    if timing is not None and j.ready(timing) and abs(timing.value) >= TIMING_MIN_MS:
        out.append(_make(15, OFFSET, "late" if timing.value > 0 else "early_offset",
                         {"ms": int(round(abs(timing.value)))}, timing.confidence, (hb.TIMING,), acc="offset"))
    if early_tapper:
        out.append(_make(15, NOTE, "early_tap", {}, j.habit(hb.TAP_LEAD).confidence, (hb.TAP_LEAD,), acc="timing"))
    edge = j.habit(hb.EDGE_STOP)
    if edge is not None and edge.significant and edge.value <= -NOTABLE[hb.EDGE_STOP]:
        out.append(_make(2, NOTE, "edge_stop", {}, edge.confidence, (hb.EDGE_STOP,), acc="edge_stop"))
    return out


def next_change(suggestions: Sequence[Suggestion]) -> Suggestion | None:
    """The one change for this round (None when something blocks or there is nothing to change)."""
    if any(s.blocks for s in suggestions):
        return None
    return next((s for s in suggestions if s.is_change), None)


# ------------------------------------------------------------------- "your playing style" in words
@dataclass(frozen=True)
class StyleItem:
    key: str
    text: Reason                               # the habit in neutral words
    accommodation: Reason | None               # what the setup does about it
    confidence: str


@dataclass(frozen=True)
class StyleSummary:
    """What the "Tu estilo de juego" card shows."""
    plays: int
    acted: tuple[StyleItem, ...] = ()          # the setup adapts to these
    fine: tuple[StyleItem, ...] = ()           # seen, not worth changing anything
    unsure: tuple[StyleItem, ...] = ()         # "todavía no seguro"
    notes: tuple[Reason, ...] = ()             # interference, adaptation window
    headline: Reason | None = None


MAX_ITEMS = 5


def _describe(h: PooledHabit, view: HabitView) -> Reason | None:
    """The habit in neutral, friendly words, or None when it is too small to mention."""
    key, v = h.key, h.value
    if key == hb.SIZE and abs(v) >= SIZE_MIN:
        return Reason("play.style.size_over" if v > 0 else "play.style.size_short")
    if key == hb.ASPECT and abs(v) >= ASPECT_MIN:
        return Reason("play.style.aspect_x" if v > 0 else "play.style.aspect_y")
    if key == hb.ROTATION and abs(v) >= ROTATE_MIN_DEG:
        return Reason("play.style.rotation_cw" if v > 0 else "play.style.rotation_ccw")
    if key == hb.EDGE_STOP and abs(v) >= NOTABLE[key]:
        return Reason("play.style.edge_stop" if v < 0 else "play.style.edge_past")
    if key == hb.TRAIL and v <= -TRAIL_MIN:
        return Reason("play.style.trail")
    if key == hb.TAP_LEAD and abs(v) >= NOTABLE[key]:
        return Reason("play.style.tap_early" if v < 0 else "play.style.tap_after", {"ms": int(round(abs(v)))})
    if key == hb.TIMING and abs(v) >= TIMING_MIN_MS:
        return Reason("play.style.timing_late" if v > 0 else "play.style.timing_early", {"ms": int(round(abs(v)))})
    if key == hb.KEY_GAP and abs(v) >= NOTABLE[key] and "|" in h.detail:
        a, b = h.detail.split("|", 1)
        late, early = (a, b) if v > 0 else (b, a)
        return Reason("play.style.key_gap", {"late": late.upper(), "early": early.upper(), "ms": int(round(abs(v)))})
    if key == hb.JITTER:
        if v >= JITTER_MIN_R:
            return Reason("play.style.jitter")
        if v < 0:
            return Reason("play.style.steady")
        return None
    if key == hb.JITTER_HOVER and v >= HOVER_MIN_R:
        return Reason("play.style.hover")
    if key == hb.PEN_LIFT and v >= PEN_LIFT_MIN:
        return Reason("play.style.pen_lift")
    if key in (hb.DRIFT_X, hb.DRIFT_Y) and abs(v) >= NOTABLE[key]:
        dx, dy = (v, 0.0) if key == hb.DRIFT_X else (0.0, v)
        return Reason("play.style.drift", {"towards": direction_id(dx, dy)})
    if key == hb.FATIGUE and v >= NOTABLE[key]:
        return Reason("play.style.fatigue")
    if key == hb.SPIKES and h.value > 0 and h.n:
        return Reason("play.style.spikes")
    if key.startswith(hb.EDGE_CLAMP) and v >= CLAMP_MIN:
        return Reason("play.style.clamp", {"towards": direction_id(*{"left": (-1, 0), "right": (1, 0), "top": (0, -1),
                                                                    "bottom": (0, 1)}[key[len(hb.EDGE_CLAMP):]])})
    if key.startswith(hb.LONG_JUMP) and abs(v) >= NOTABLE["long_jump"]:
        name = key[len(hb.LONG_JUMP):]
        ux, uy = {"right": (1, 0), "left": (-1, 0), "up": (0, -1), "down": (0, 1)}[name]
        return Reason("play.style.long_short" if v < 0 else "play.style.long_over", {"towards": direction_id(ux, uy)})
    if key.startswith(hb.ZONE) and h.detail:
        kind = h.detail.split(":", 1)[0]
        return Reason(f"play.style.zone_{kind if kind in ('short', 'over') else 'off'}",
                      {"place": key[len(hb.ZONE):]})
    return None


def _aim_item(view: HabitView) -> tuple[PooledHabit, Reason] | None:
    """The two aim-bias axes as one habit ("tus clics caen un poco a la derecha del centro")."""
    bx, by = view.get(hb.AIM_X), view.get(hb.AIM_Y)
    if bx is None or by is None:
        return None
    size = math.hypot(bx.value, by.value)
    if size < OFFSET_MIN_R / 2:
        return None
    se = math.hypot(bx.value * bx.se, by.value * by.se) / size
    joined = PooledHabit("aim_bias", size, se, min(bx.n, by.n), min(bx.plays, by.plays), min(bx.agree, by.agree)
                         if bx.agree and by.agree else max(bx.agree, by.agree), min(bx.replays, by.replays),
                         bx.capped or by.capped)
    return joined, Reason("play.style.aim_bias", {"towards": direction_id(bx.value, by.value)})


_RELATED = {hb.AIM_X: "aim_bias", hb.AIM_Y: "aim_bias", hb.GAIN_X: hb.SIZE, hb.GAIN_Y: hb.SIZE}
_FINE_ACC = {hb.EDGE_STOP: "edge_stop", hb.TAP_LEAD: "tap", hb.TIMING: "offset", hb.KEY_GAP: "offset",
             hb.SPIKES: "emi", hb.JITTER_HOVER: "hover", hb.PEN_LIFT: "hover", hb.DRIFT_X: "drift",
             hb.DRIFT_Y: "drift", hb.FATIGUE: "fatigue"}


def describe(view: HabitView, suggestions: Sequence[Suggestion]) -> StyleSummary:
    """The card's content: what the setup adapts to, what was seen but is fine, what is not sure yet."""
    acted_keys: dict[str, Suggestion] = {}
    first = next_change(suggestions)
    for s in suggestions:
        if s.is_change or s.action in (OFFSET, NOTE):
            for k in s.habits:
                acted_keys.setdefault(_RELATED.get(k, k), s)
    items: list[tuple[PooledHabit, Reason]] = []
    aim = _aim_item(view)
    if aim is not None:
        items.append(aim)
    long_jumps = [h for h in view.with_prefix(hb.LONG_JUMP) if abs(h.value) >= NOTABLE["long_jump"]]
    strongest = max(long_jumps, key=lambda h: h.z, default=None)
    for key, h in view.habits.items():
        if key in (hb.AIM_X, hb.AIM_Y, hb.GAIN_X, hb.GAIN_Y, hb.SCATTER, hb.OVERSHOOT, hb.UR) \
                or (key.startswith(hb.LONG_JUMP) and h is not strongest):
            continue
        if key.startswith(hb.EDGE_CLAMP) and h.value < CLAMP_MIN:
            continue
        text = _describe(h, view)
        if text is not None:
            items.append((h, text))
    acted, fine, unsure = [], [], []
    for h, text in sorted(items, key=lambda it: -it[0].z):
        s = acted_keys.get(h.key)
        if s is not None and s.is_change:
            acc = s.accommodation if s is first else Reason("play.rule.acc.later")
            acted.append(StyleItem(h.key, text, acc, s.confidence))
        elif h.confidence == "low" or not h.significant:
            if h.z >= 1.0:
                unsure.append(StyleItem(h.key, text, Reason("play.rule.acc.unsure"), "low"))
        else:
            acc = s.accommodation if s is not None else Reason(f"play.rule.acc.{_FINE_ACC.get(h.key, 'keep')}")
            fine.append(StyleItem(h.key, text, acc, h.confidence))
    notes = tuple(s.reason for s in suggestions if s.blocks)
    if notes:
        headline = notes[0]
    elif acted:
        headline = Reason("play.style.headline.acted")
    elif view.plays < 2:
        headline = Reason("play.style.headline.learning")
    else:
        headline = Reason("play.style.headline.fits")
    return StyleSummary(view.plays, tuple(acted[:MAX_ITEMS]), tuple(fine[:MAX_ITEMS]), tuple(unsure[:MAX_ITEMS]),
                        notes, headline)
