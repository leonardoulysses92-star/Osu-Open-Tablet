"""Live "feel" adaptation: user intents -> small, coordinated parameter steps.

Pure and deterministic. The companion mode calls :func:`step` with the
profile that is live in OTD and one plain-language intent ("smoother", "more
responsive", "more precise at rest"...). The result carries the new filter
list / area as ``ProfileChanges``, a new :class:`AdaptState` and translatable
``Reason`` lines with old -> new values.

Model
-----
* Three **axes**: ``motion`` (SMOOTHER +1 / RESPONSIVE -1), ``rest``
  (PRECISE +1 = less jitter while holding still / LOOSER -1 = less sticky
  micro-movement) and ``area`` (AREA_BIGGER +1 / AREA_SMALLER -1).
* Every supported filter family maps an axis to ordered **tiers** of
  parameter moves; the first tier that still has room in the requested
  direction is applied (e.g. Temporal Resampler: lower its EMA latency first,
  raise the prediction ratio only once the latency is already 0).
* Each axis keeps a **gain** (1.0 at start). When the user reverses
  direction (overshoot) the gain halves, so feedback that alternates converges
  on a point; a run in the same direction lets the gain grow back. Moves are
  always at least one grid step so they never vanish in rounding.
* A **step level** (normal / fine / very_fine) multiplies the gain (x1 / x0.4 /
  x0.2; area 2 % / 1 % / 0.5 %) and, for floats, uses a finer grid (catalog
  step / 1, 5, 10). It never touches the gain bookkeeping, so convergence
  works the same at every level.
* An axis is **converged** when its last ``CONVERGE_WINDOW`` moves alternate
  and the gain is at its floor; the companion then recommends Keep.
* Policies (:class:`TuningPolicy`) decide what may happen when the profile has
  nothing to tune for an axis: re-enable/add an installed filter, or only
  *suggest* a plugin that is not installed (never installed by this program).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping, Sequence

from ..knowledge.filters import FilterParam, FilterSpec, load_catalog
from ..models import FilterConfig, ProfileChanges, ProfileInfo, Reason, Rect, TabletSpec
from .recommend import (ANTICHATTER, CHAIN_ORDER, CHATTER_RAW, CHATTER_SMOOTH, CHATTER_STRENGTH, HAWKU,
                        NOISE_REDUCTION, RADIAL_SCREEN, RADIAL_TABLET, RESAMPLER, SATURN_MULTI,
                        SATURN_ROUNDING)


class Intent(str, Enum):
    SMOOTHER = "smoother"
    RESPONSIVE = "responsive"
    PRECISE = "precise"          # less jitter while the pen rests
    LOOSER = "looser"            # less sticky small movements
    AREA_BIGGER = "area_bigger"
    AREA_SMALLER = "area_smaller"
    UNDO = "undo"                # handled by the caller (state stack)
    KEEP = "keep"                # accept the current point (see :func:`accept`)


AXIS_OF: dict[Intent, tuple[str, int]] = {
    Intent.SMOOTHER: ("motion", +1), Intent.RESPONSIVE: ("motion", -1),
    Intent.PRECISE: ("rest", +1), Intent.LOOSER: ("rest", -1),
    Intent.AREA_BIGGER: ("area", +1), Intent.AREA_SMALLER: ("area", -1),
}
AXES = ("motion", "rest", "area")

START_GAIN = 1.0
MIN_GAIN = 0.125
REVERSAL_FACTOR = 0.5
STREAK_GROWTH = 1.25          # same direction twice in a row
CONVERGE_WINDOW = 4
CONVERGE_MAX_GAIN = 0.25
AREA_STEP = 0.02              # 2 % of the area per move at gain 1
MIN_AREA_WIDTH_MM = 10.0
AREA_DECIMALS = 3

# Step levels (the companion's "Step" selector). The level MULTIPLIES the axis gain (it never replaces
# it, so reversals still halve the step and convergence works the same way) and, for float params,
# moves on a finer grid than the catalog step: catalog step / GRID_DIVISOR (knowledge.filters.GRID_FLOOR
# is the absolute floor; ints keep whole steps). The area uses its own factor (2 % -> 1 % -> 0.5 %).
STEP_LEVELS = ("normal", "fine", "very_fine")
DEFAULT_STEP_LEVEL = "normal"
STEP_SCALE = {"normal": 1.0, "fine": 0.4, "very_fine": 0.2}
GRID_DIVISOR = {"normal": 1, "fine": 5, "very_fine": 10}
AREA_LEVEL_SCALE = {"normal": 1.0, "fine": 0.5, "very_fine": 0.25}


def step_level(level: str | None) -> str:
    """Validated step level (None = the default); raises ValueError for an unknown name."""
    level = DEFAULT_STEP_LEVEL if level is None else str(level)
    if level not in STEP_LEVELS:
        raise ValueError(f"unknown step level {level!r}")
    return level


def next_step_level(level: str | None) -> str:
    """The level after ``level`` in the cycle normal -> fine -> very_fine -> normal."""
    return STEP_LEVELS[(STEP_LEVELS.index(step_level(level)) + 1) % len(STEP_LEVELS)]


def area_step_pct(level: str | None = None, gain: float = 1.0) -> float:
    """Area change of one move in percent at ``gain`` (2 / 1 / 0.5 at gain 1)."""
    return round(AREA_STEP * AREA_LEVEL_SCALE[step_level(level)] * gain * 100, 3)


def grid_step(param: FilterParam, level: str | None = None) -> float | None:
    """The grid one move of ``param`` snaps to at ``level`` (normal: the catalog step)."""
    return param.grid(GRID_DIVISOR[step_level(level)])


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------
class PolicyMode(str, Enum):
    TUNE_ONLY = "tune_only"      # keep the profile's filters, only tweak their values
    INSTALLED = "installed"      # may enable / add filters from installed plugins
    ALLOW_NEW = "allow_new"      # like INSTALLED, plus suggestions of not-installed plugins


@dataclass(frozen=True)
class TuningPolicy:
    """What the adaptation may do besides changing parameter values."""
    mode: PolicyMode = PolicyMode.TUNE_ONLY
    installed: frozenset[str] = frozenset()     # filter paths available in this OTD install

    def may_enable(self, path: str) -> bool:
        """Re-enable a filter that is in the profile but switched off."""
        return self.mode is not PolicyMode.TUNE_ONLY

    def may_add(self, path: str) -> bool:
        """Insert a filter that is not in the profile (only if installed)."""
        return self.mode is not PolicyMode.TUNE_ONLY and path in self.installed

    def may_suggest(self, path: str) -> bool:
        """Mention a plugin the user would have to install themselves."""
        return self.mode is PolicyMode.ALLOW_NEW and path not in self.installed


@dataclass(frozen=True)
class Suggestion:
    """A plugin worth installing; nothing is installed or applied."""
    path: str
    plugin_name: str
    reason: Reason


# --------------------------------------------------------------------------
# Families (which parameters each filter exposes on each axis)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Move:
    """How one parameter moves when its axis goes in the "more" (+1) direction.

    ``mode``: "log" multiplies the value by ``1 + ratio*gain`` (from ``seed``
    when it is ~0, snapping to the minimum below ``seed/2``); "one_minus" does
    the same on ``1 - value`` (coefficients that approach 1); "linear" adds
    ``ratio*gain`` units. ``sign`` -1 means the value goes down for "more".
    ``soft_min``/``soft_max`` narrow the catalog range for automatic moves only.
    """
    param: str
    mode: str = "log"
    ratio: float = 0.2
    seed: float = 0.0
    sign: int = +1
    soft_min: float | None = None
    soft_max: float | None = None


Tier = tuple[Move, ...]

FAMILIES: dict[str, dict[str, tuple[Tier, ...]]] = {
    RADIAL_TABLET: {
        "motion": ((Move("OuterRadius", "log", 0.2, 0.3, soft_max=3.0),
                    Move("SmoothingCoefficient", "one_minus", 0.2, soft_min=0.5, soft_max=0.99)),),
        "rest": ((Move("InnerRadius", "log", 0.3, 0.05, soft_max=1.0),),),
    },
    RADIAL_SCREEN: {
        "motion": ((Move("OuterRadius", "log", 0.2, 2.0, soft_max=30.0),
                    Move("SmoothingCoefficient", "one_minus", 0.2, soft_min=0.5, soft_max=0.99)),),
        "rest": ((Move("InnerRadius", "log", 0.3, 0.5, soft_max=10.0),),),
    },
    HAWKU: {
        "motion": ((Move("Latency", "log", 0.25, 2.0, soft_max=30.0),),),
        "rest": ((Move("Latency", "log", 0.25, 2.0, soft_max=30.0),),),
    },
    ANTICHATTER: {
        "motion": ((Move("Latency", "log", 0.25, 2.0, soft_max=30.0),),),
        "rest": ((Move("AntichatterStrength", "linear", 0.3, soft_max=6.0),),),
    },
    RESAMPLER: {
        "motion": ((Move("latency", "log", 0.25, 0.5, soft_max=20.0),),
                   # Never above the plugin default 0.5: more prediction is not added automatically
                   # (osu! policy on prediction plugins is unresolved, docs/research/filters.md §4).
                   (Move("frameShift", "linear", 0.05, sign=-1, soft_max=0.5),)),
        "rest": ((Move("followRadius", "log", 0.3, 0.05, soft_max=1.0),),),
    },
    NOISE_REDUCTION: {
        "motion": ((Move("Samples", "linear", 1.0, soft_min=5, soft_max=20),),),
        "rest": ((Move("DistanceThreshold", "log", 0.25, 0.2, soft_min=0.2, soft_max=1.0),),),
    },
    CHATTER_RAW: {
        "motion": ((Move(CHATTER_STRENGTH, "log", 0.2, 1.0, soft_max=10.0),),),
        "rest": ((Move(CHATTER_STRENGTH, "log", 0.2, 1.0, soft_max=10.0),),),
    },
    CHATTER_SMOOTH: {
        "motion": ((Move(CHATTER_STRENGTH, "log", 0.2, 2.0, soft_max=20.0),),),
        "rest": ((Move(CHATTER_STRENGTH, "log", 0.2, 2.0, soft_max=20.0),),),
    },
}
# Families where an axis reuses the other axis' parameter (no dedicated control).
SHARED_AXIS = {(HAWKU, "rest"), (CHATTER_RAW, "motion"), (CHATTER_SMOOTH, "motion")}
UNSUPPORTED = {SATURN_MULTI: "adapt.skip.saturn", SATURN_ROUNDING: "adapt.skip.not_smoothing"}
# Which filter answers an axis first when the profile has several.
PREFERENCE: dict[str, tuple[str, ...]] = {
    "motion": (RADIAL_TABLET, RADIAL_SCREEN, ANTICHATTER, HAWKU, RESAMPLER, NOISE_REDUCTION,
               CHATTER_SMOOTH, CHATTER_RAW),
    "rest": (RADIAL_TABLET, RADIAL_SCREEN, CHATTER_SMOOTH, CHATTER_RAW, NOISE_REDUCTION, ANTICHATTER,
             RESAMPLER, HAWKU),
}
# Filter to enable/add/suggest when the profile has nothing for an axis.
ADD_PREFERENCE: dict[str, tuple[str, ...]] = {
    "motion": (RADIAL_TABLET, HAWKU, ANTICHATTER),
    "rest": (RADIAL_TABLET, CHATTER_SMOOTH, NOISE_REDUCTION),
}
# Starting values for a filter added by the policy (mild; others: catalog default).
ADD_START: dict[str, dict[str, Any]] = {
    RADIAL_TABLET: {"OuterRadius": 0.5, "InnerRadius": 0.0, "SmoothingCoefficient": 0.9},
    HAWKU: {"Latency": 4.0},
    ANTICHATTER: {"Latency": 4.0},
}


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class AxisState:
    gain: float = START_GAIN
    last_dir: int = 0
    streak: int = 0
    position: float = 0.0                    # signed sum of gain-weighted moves
    moves: tuple[int, ...] = ()              # directions of the last moves (bounded)


@dataclass(frozen=True)
class AcceptedPoint:
    """A point the user said felt right (Keep, or "good" in the quick poll)."""
    motion: float
    rest: float
    area: float
    values: tuple[tuple[str, str, Any], ...]  # (filter path, property, value)


@dataclass(frozen=True)
class AdaptState:
    axes: Mapping[str, AxisState] = field(default_factory=lambda: {a: AxisState() for a in AXES})
    accepted: tuple[AcceptedPoint, ...] = ()
    change_count: int = 0

    def axis(self, name: str) -> AxisState:
        return self.axes.get(name, AxisState())


@dataclass(frozen=True)
class StepResult:
    state: AdaptState
    changes: ProfileChanges | None           # None: nothing to apply
    reasons: tuple[Reason, ...] = ()
    suggestions: tuple[Suggestion, ...] = ()
    skipped: tuple[Reason, ...] = ()
    converged: bool = False

    @property
    def moved(self) -> bool:
        return self.changes is not None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def fmt(value: Any) -> str:
    """Compact human number: 0.6 -> "0.6", 2.0 -> "2", True -> "on"."""
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (int, float)):
        text = f"{float(value):.4f}".rstrip("0").rstrip(".")
        return text if text not in ("", "-0") else "0"
    return str(value)


def _number(value: Any, default: float) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else float(default)


def _limits(param: FilterParam, move: Move) -> tuple[float, float]:
    lo = param.min if param.min is not None else -math.inf
    hi = param.max if param.max is not None else math.inf
    if move.soft_min is not None:
        lo = max(lo, move.soft_min)
    if move.soft_max is not None:
        hi = min(hi, move.soft_max)
    return lo, hi


def _inside(new: float, grid: float, lo: float, hi: float) -> float:
    """Keep a grid-snapped value inside the soft limits (one grid step back inside when rounding left them)."""
    if new > hi and math.isfinite(hi):
        new = math.floor(hi / grid + 1e-9) * grid
    if new < lo and math.isfinite(lo):
        new = math.ceil(lo / grid - 1e-9) * grid
    return new


def move_value(param: FilterParam, move: Move, current: float, direction: int, gain: float,
               level: str | None = None) -> float:
    """New value of one parameter, clamped, on the grid of ``level``; ``current`` if no room.

    ``level`` "normal" (default) is the original behaviour (catalog grid, full gain). "fine" and
    "very_fine" multiply the gain by :data:`STEP_SCALE` and snap floats to a finer grid
    (:func:`grid_step`); the catalog bounds and the soft limits are the same at every level.
    """
    level = step_level(level)
    scale = STEP_SCALE[level]
    grid = grid_step(param, level) if level != DEFAULT_STEP_LEVEL else param.step
    fine = level != DEFAULT_STEP_LEVEL

    def snap(value: float) -> Any:
        new = param.clamp(value, grid=grid) if fine else param.clamp(value)
        if fine and grid and param.kind != "int":
            new = param.clamp(_inside(new, grid, lo, hi), grid=grid)
        return new

    d = direction * move.sign
    lo, hi = _limits(param, move)
    if (d > 0 and current >= hi) or (d < 0 and current <= lo):
        return current
    gain = gain * scale
    seed = move.seed * scale
    factor = 1.0 + move.ratio * gain
    if move.mode == "log":
        if d > 0:
            target = max(current * factor, seed) if current < seed else current * factor
        else:
            target = current / factor
            if target < seed / 2:
                target = max(lo, param.min if param.min is not None else 0.0)
    elif move.mode == "one_minus":
        rest = max(1.0 - current, 0.0)
        target = 1.0 - (rest / factor if d > 0 else rest * factor)
    else:
        delta = move.ratio * gain
        if param.kind == "int":
            delta = max(1.0, round(delta))
        target = current + d * delta
    target = min(max(target, lo), hi)
    new = snap(target)
    if new == snap(current) and grid:       # rounding ate the move: one grid step
        new = snap(current + d * grid)
        new = min(max(new, lo), hi) if param.kind != "int" else new
        new = snap(new)
    if (d > 0 and new < current) or (d < 0 and new > current):
        return current
    return new


def _family_tiers(path: str, axis: str) -> tuple[Tier, ...]:
    return FAMILIES.get(path, {}).get(axis, ())


def _apply_tier(fc: FilterConfig, spec: FilterSpec, tier: Tier, direction: int,
                gain: float, level: str | None = None) -> tuple[dict[str, Any], list[Reason]]:
    """New settings for ``fc`` after one tier, with one Reason per changed value."""
    settings = dict(fc.settings)
    reasons: list[Reason] = []
    for move in tier:
        param = spec.param(move.param)
        if param is None or param.kind not in ("float", "int"):
            continue
        old = _number(settings.get(move.param), param.default)
        new = move_value(param, move, old, direction, gain, level)
        if new != old:
            settings[move.param] = new
            reasons.append(Reason("adapt.change", {"filter": spec.display_name, "param": move.param,
                                                   "old": fmt(old), "new": fmt(new), "unit": param.unit}))
    _radial_constraint(fc.path, spec, settings, reasons)
    return settings, reasons


def _radial_constraint(path: str, spec: FilterSpec, settings: dict[str, Any], reasons: list[Reason]) -> None:
    """Radial Follow: the inner radius (deadzone) must not exceed the outer radius."""
    if path not in (RADIAL_TABLET, RADIAL_SCREEN):
        return
    inner_p, outer_p = spec.param("InnerRadius"), spec.param("OuterRadius")
    if inner_p is None or outer_p is None:
        return
    inner = _number(settings.get("InnerRadius"), inner_p.default)
    outer = _number(settings.get("OuterRadius"), outer_p.default)
    if inner > outer:
        settings["InnerRadius"] = inner_p.clamp(outer)
        reasons.append(Reason("adapt.inner_capped", {"filter": spec.display_name, "old": fmt(inner),
                                                     "new": fmt(settings["InnerRadius"])}))


def _next_axis_state(ax: AxisState, direction: int, scale: float = 1.0) -> AxisState:
    """Gain/streak bookkeeping of one move; ``scale`` (the step level) only weights the position."""
    gain, streak = ax.gain, ax.streak
    if ax.last_dir == -direction:
        gain, streak = max(gain * REVERSAL_FACTOR, MIN_GAIN), 1
    elif ax.last_dir == direction:
        streak += 1
        if streak >= 2:
            gain = min(gain * STREAK_GROWTH, START_GAIN)
    else:
        streak = 1
    moves = (ax.moves + (direction,))[-(CONVERGE_WINDOW * 2):]
    return AxisState(gain=gain, last_dir=direction, streak=streak,
                     position=round(ax.position + direction * gain * scale, 6), moves=moves)


def axis_converged(ax: AxisState) -> bool:
    """Last ``CONVERGE_WINDOW`` moves alternate and the step is already small."""
    last = ax.moves[-CONVERGE_WINDOW:]
    if len(last) < CONVERGE_WINDOW or ax.gain > CONVERGE_MAX_GAIN:
        return False
    return all(a == -b for a, b in zip(last, last[1:]))


def is_converged(state: AdaptState) -> bool:
    """True when every axis the user moved enough has converged (and at least one has)."""
    used = [state.axis(a) for a in AXES if len(state.axis(a).moves) >= CONVERGE_WINDOW]
    return bool(used) and all(axis_converged(ax) for ax in used)


def _with_axis(state: AdaptState, axis: str, ax: AxisState, converged_reason: list[Reason]) -> AdaptState:
    axes = dict(state.axes)
    axes[axis] = ax
    new = replace(state, axes=axes, change_count=state.change_count + 1)
    if axis_converged(ax):
        converged_reason.append(Reason("adapt.converged", {"axis": axis}))
    return new


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def tunable_paths() -> frozenset[str]:
    """Filter paths with a feel axis."""
    return frozenset(FAMILIES)


def step(state: AdaptState, profile: ProfileInfo, intent: Intent | str, policy: TuningPolicy | None = None,
         catalog: Mapping[str, FilterSpec] | None = None, tablet: TabletSpec | None = None, *,
         level: str | None = None) -> StepResult:
    """Apply one intent to ``profile``; UNDO/KEEP are not handled here (see module doc).

    ``level`` is the step level (:data:`STEP_LEVELS`; default "normal" = the original step size).
    """
    intent = Intent(intent)
    level = step_level(level)
    if intent not in AXIS_OF:
        raise ValueError(f"{intent.value} is handled by the caller, not by step()")
    policy = policy or TuningPolicy()
    catalog = catalog if catalog is not None else load_catalog()
    axis, direction = AXIS_OF[intent]
    if axis == "area":
        return _area_step(state, profile, direction, tablet, level)

    ax = state.axis(axis)
    trial = _next_axis_state(ax, direction, STEP_SCALE[level])
    filters = list(profile.filters)
    skipped: list[Reason] = []
    for fc in filters:
        if fc.path in UNSUPPORTED:
            skipped.append(Reason(UNSUPPORTED[fc.path], {"filter": _name(fc.path, catalog)}))
        elif fc.path not in FAMILIES:
            skipped.append(Reason("adapt.skip.unknown", {"filter": fc.path}))
        elif not fc.enabled and not policy.may_enable(fc.path):
            skipped.append(Reason("adapt.skip.disabled", {"filter": _name(fc.path, catalog)}))

    order = {p: i for i, p in enumerate(PREFERENCE[axis])}
    candidates = sorted((i for i, fc in enumerate(filters) if fc.enabled and fc.path in FAMILIES
                         and fc.path in catalog), key=lambda i: (order.get(filters[i].path, 99), i))
    for idx in candidates:
        fc, spec = filters[idx], catalog[filters[idx].path]
        for tier in _family_tiers(fc.path, axis):
            settings, reasons = _apply_tier(fc, spec, tier, direction, trial.gain, level)
            if reasons:
                filters[idx] = replace(fc, settings=settings)
                notes: list[Reason] = []
                if (fc.path, axis) in SHARED_AXIS:
                    notes.append(Reason("adapt.shared_axis", {"filter": spec.display_name}))
                conv: list[Reason] = []
                new_state = _with_axis(state, axis, trial, conv)
                return StepResult(new_state, ProfileChanges(filters=tuple(filters)),
                                  tuple(reasons + notes + conv), (), tuple(skipped), is_converged(new_state))
    if candidates:   # something to tune, but every value is at its limit
        key = "adapt.limit.more" if direction > 0 else "adapt.limit.less"
        return StepResult(state, None, (Reason(key, {"axis": axis}),), (), tuple(skipped),
                          is_converged(state))
    return _add_or_suggest(state, profile, axis, direction, trial, policy, catalog, skipped)


def _add_or_suggest(state: AdaptState, profile: ProfileInfo, axis: str, direction: int, trial: AxisState,
                    policy: TuningPolicy, catalog: Mapping[str, FilterSpec],
                    skipped: list[Reason]) -> StepResult:
    """Nothing enabled to tune for ``axis``: enable, add or suggest a filter (policy permitting)."""
    if direction < 0:   # less filtering with no filter is already the most direct setting
        return StepResult(state, None, (Reason("adapt.nothing_to_reduce", {"axis": axis}),), (),
                          tuple(skipped), is_converged(state))
    filters = list(profile.filters)
    for idx, fc in enumerate(filters):   # 1. a supported filter that is switched off
        if not fc.enabled and fc.path in FAMILIES and fc.path in catalog and policy.may_enable(fc.path) \
                and _family_tiers(fc.path, axis):
            filters[idx] = replace(fc, enabled=True)
            conv: list[Reason] = []
            new_state = _with_axis(state, axis, trial, conv)
            return StepResult(new_state, ProfileChanges(filters=tuple(filters)),
                              (Reason("adapt.enabled", {"filter": catalog[fc.path].display_name}), *conv),
                              (), tuple(skipped), is_converged(new_state))
    present = {fc.path for fc in filters}
    for path in ADD_PREFERENCE[axis]:      # 2. an installed one that is not in the profile
        if path in present or path not in catalog or not policy.may_add(path):
            continue
        spec = catalog[path]
        values = spec.defaults()
        values.update(ADD_START.get(path, {}))
        new_fc = FilterConfig(path=path, enabled=True, settings=values)
        conv = []
        new_state = _with_axis(state, axis, trial, conv)
        return StepResult(new_state, ProfileChanges(filters=insert_in_chain(filters, new_fc)),
                          (Reason("adapt.added", {"filter": spec.display_name}), *conv),
                          (), tuple(skipped), is_converged(new_state))
    suggestions = tuple(
        Suggestion(path, catalog[path].plugin_name,
                   Reason("adapt.suggest", {"filter": catalog[path].display_name,
                                            "plugin": catalog[path].plugin_name}))
        for path in ADD_PREFERENCE[axis] if path in catalog and path not in present and policy.may_suggest(path))
    key = "adapt.nothing_to_tune" if policy.mode is PolicyMode.TUNE_ONLY else "adapt.nothing_installed"
    return StepResult(state, None, (Reason(key, {"axis": axis}),), suggestions[:1], tuple(skipped),
                      is_converged(state))


def insert_in_chain(filters: Sequence[FilterConfig], new: FilterConfig) -> tuple[FilterConfig, ...]:
    """Insert ``new`` at its ``CHAIN_ORDER`` position relative to known filters."""
    rank = {p: i for i, p in enumerate(CHAIN_ORDER)}
    target = rank.get(new.path, len(CHAIN_ORDER))
    out = list(filters)
    pos = len(out)
    for i, fc in enumerate(out):
        if rank.get(fc.path, -1) > target:
            pos = i
            break
    out.insert(pos, new)
    return tuple(out)


def _name(path: str, catalog: Mapping[str, FilterSpec]) -> str:
    spec = catalog.get(path)
    return spec.display_name if spec else path


# --------------------------------------------------------------------------
# Area
# --------------------------------------------------------------------------
def _extent(rect: Rect) -> tuple[float, float]:
    """Axis-aligned width/height of a rotated rect."""
    rad = math.radians(rect.rotation)
    c, s = abs(math.cos(rad)), abs(math.sin(rad))
    return rect.width * c + rect.height * s, rect.width * s + rect.height * c


def max_area_scale(rect: Rect, tablet: TabletSpec | None) -> float:
    """Largest factor ``rect`` may grow by while keeping its center inside the tablet."""
    if tablet is None:
        return math.inf
    ew, eh = _extent(rect)
    limits = []
    if ew > 0:
        limits.append(min(rect.x, tablet.width_mm - rect.x) / (ew / 2))
    if eh > 0:
        limits.append(min(rect.y, tablet.height_mm - rect.y) / (eh / 2))
    return max(min(limits), 0.0) if limits else math.inf


def nudge_area(rect: Rect, tablet: TabletSpec | None, direction: int,
               gain: float = 1.0, level: str | None = None) -> tuple[Rect | None, tuple[Reason, ...]]:
    """Scale the area by ``±AREA_STEP*gain`` (times the level's area factor) keeping aspect, center, rotation.

    Growth is capped so the area stays on the tablet; returns ``(None, reasons)``
    when there is no room or the area is unknown.
    """
    if rect.width <= 0 or rect.height <= 0:
        return None, (Reason("adapt.area.unknown"),)
    step = AREA_STEP * AREA_LEVEL_SCALE[step_level(level)] * gain
    factor = (1.0 + step) if direction > 0 else 1.0 / (1.0 + step)
    if direction > 0:
        cap = max_area_scale(rect, tablet)
        if cap <= 1.0 + 1e-6:
            return None, (Reason("adapt.area.at_max"),)
        factor = min(factor, cap)
    elif rect.width * factor < MIN_AREA_WIDTH_MM:
        return None, (Reason("adapt.area.at_min", {"min": fmt(MIN_AREA_WIDTH_MM)}),)
    aspect = rect.width / rect.height
    width = round(rect.width * factor, AREA_DECIMALS)
    height = round(width / aspect, AREA_DECIMALS)
    if tablet is not None and direction > 0:   # rounding must not push it off the tablet
        while max_area_scale(Rect(width, height, rect.x, rect.y, rect.rotation), tablet) < 1.0 - 1e-9:
            width = round(width - 10 ** -AREA_DECIMALS, AREA_DECIMALS)
            height = round(width / aspect, AREA_DECIMALS)
    new = Rect(width, height, rect.x, rect.y, rect.rotation)
    if new == rect:
        return None, (Reason("adapt.area.at_max" if direction > 0 else "adapt.area.at_min",
                             {"min": fmt(MIN_AREA_WIDTH_MM)}),)
    return new, (Reason("adapt.area.change", {"old_w": fmt(rect.width), "old_h": fmt(rect.height),
                                              "new_w": fmt(width), "new_h": fmt(height),
                                              "pct": fmt(round((width / rect.width - 1) * 100, 2))}),)


def _area_step(state: AdaptState, profile: ProfileInfo, direction: int, tablet: TabletSpec | None,
               level: str = DEFAULT_STEP_LEVEL) -> StepResult:
    trial = _next_axis_state(state.axis("area"), direction, AREA_LEVEL_SCALE[level])
    rect, reasons = nudge_area(profile.tablet_area, tablet, direction, trial.gain, level)
    if rect is None:
        return StepResult(state, None, reasons, converged=is_converged(state))
    conv: list[Reason] = []
    new_state = _with_axis(state, "area", trial, conv)
    return StepResult(new_state, ProfileChanges(tablet_area=rect), tuple(reasons) + tuple(conv),
                      converged=is_converged(new_state))


# --------------------------------------------------------------------------
# Accepting points and the quick poll
# --------------------------------------------------------------------------
def tunable_values(profile: ProfileInfo) -> tuple[tuple[str, str, Any], ...]:
    """(path, property, value) of every parameter the feel axes can move."""
    out: list[tuple[str, str, Any]] = []
    for fc in profile.filters:
        names = {m.param for tiers in FAMILIES.get(fc.path, {}).values() for tier in tiers for m in tier}
        out += [(fc.path, n, fc.settings[n]) for n in sorted(names) if n in fc.settings]
    return tuple(out)


def accept(state: AdaptState, profile: ProfileInfo) -> AdaptState:
    """Record the current point as one the user liked; streaks restart from here."""
    point = AcceptedPoint(state.axis("motion").position, state.axis("rest").position,
                          state.axis("area").position, tunable_values(profile))
    axes = {name: replace(ax, streak=0) for name, ax in state.axes.items()}
    return replace(state, axes=axes, accepted=state.accepted + (point,))


def repeated_acceptance(state: AdaptState, times: int = 2) -> bool:
    """The user accepted the very same values ``times`` times in a row (another stop signal)."""
    pts = state.accepted[-times:]
    return len(pts) == times and all(p.values == pts[0].values for p in pts)


POLL_MOTION = ("shaky", "good", "laggy")
POLL_REST = ("steady", "jittery")


def poll_to_intents(motion: str | None, rest: str | None) -> tuple[Intent, ...]:
    """Quick-poll answers -> intents. ("good", "steady") means "accept this point" -> ()."""
    if motion is not None and motion not in POLL_MOTION:
        raise ValueError(f"unknown motion answer {motion!r}")
    if rest is not None and rest not in POLL_REST:
        raise ValueError(f"unknown rest answer {rest!r}")
    out: list[Intent] = []
    if motion == "shaky":
        out.append(Intent.SMOOTHER)
    elif motion == "laggy":
        out.append(Intent.RESPONSIVE)
    if rest == "jittery":
        out.append(Intent.PRECISE)
    return tuple(out)
