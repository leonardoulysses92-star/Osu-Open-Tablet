"""Optional smoothing the player decides on (the Play Coach "Smoothing" card). Pure, no I/O.

Many players have smoothing filters installed but switched off (OpenTabletDriver lists them unticked). The
coach never switches them on by itself; the player can *try* one (live, not saved, undoable) and then decide
to keep it or take it out. This module only knows which filters are offered, which values a trial starts
with, and how to switch one on and back off in a filter list.

Starting values (heuristic, chosen to be gentle; not calibrated on real players):

* Radial Follow, tablet coordinates: outer radius 0.5 mm, no inner radius (dead zone), coefficient 0.9
  (the same mild start ``engine.adapt`` uses when it adds the filter);
* Radial Follow, screen coordinates: outer radius 5 px (the plugin default), no inner radius, coefficient 0.9;
* Hawku Smoothing: latency 3 ms (community presets use about 2-10 ms; 3 ms is at the mild end);
* Hawku Noise Reduction: 10 samples, distance threshold 0.3 mm (milder than the 0.5 mm default);
* Devocub Antichatter: latency 4 ms, prediction off;
* Temporal Resampler: no extra smoothing latency and ``frameShift`` at its neutral default 0.5 (never above:
  higher values predict ahead, and this project never adds prediction);
* any other offered filter: the catalog (plugin) defaults.

When the filter is already in the profile but switched off, the player's own values are kept when they are
inside the catalog bounds (the safety rules above still apply: prediction stays off, ``frameShift`` never above
0.5).
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..knowledge.filters import FilterParam, FilterSpec
from ..models import FilterConfig, Rect
from . import community
from .adapt import insert_in_chain
from .recommend import (ANTICHATTER, HAWKU, NOISE_REDUCTION, RADIAL_SCREEN, RADIAL_TABLET, RESAMPLER, chain_latency,
                        enforce_constraints)

NEUTRAL_FRAME_SHIFT = 0.5
GENTLE: dict[str, dict[str, Any]] = {
    RADIAL_TABLET: {"OuterRadius": 0.5, "InnerRadius": 0.0, "SmoothingCoefficient": 0.9},
    RADIAL_SCREEN: {"OuterRadius": 5.0, "InnerRadius": 0.0, "SmoothingCoefficient": 0.9},
    HAWKU: {"Latency": 3.0},
    NOISE_REDUCTION: {"Samples": 10, "DistanceThreshold": 0.3},
    ANTICHATTER: {"Latency": 4.0},
    RESAMPLER: {"latency": 0.0, "frameShift": NEUTRAL_FRAME_SHIFT},
}
FORCED: dict[str, dict[str, Any]] = {ANTICHATTER: {"PredictionEnabled": False}}     # prediction: never on
CAPS: dict[str, dict[str, float]] = {RESAMPLER: {"frameShift": NEUTRAL_FRAME_SHIFT}}  # never predict ahead
# Short ids for the texts (``play.smooth.what.<id>`` / ``play.smooth.feel.<id>``); others use "other".
IDS = {RADIAL_TABLET: "radial_tablet", RADIAL_SCREEN: "radial_screen", HAWKU: "hawku", NOISE_REDUCTION: "noise",
       ANTICHATTER: "antichatter", RESAMPLER: "resampler"}
ORDER = (RADIAL_TABLET, RADIAL_SCREEN, HAWKU, NOISE_REDUCTION, ANTICHATTER, RESAMPLER)


def text_id(path: str) -> str:
    return IDS.get(path, "other")


def offered(catalog: Mapping[str, FilterSpec]) -> list[str]:
    """Catalog filters that may be tried: ordinary smoothing filters (not advanced, not prediction-like)."""
    paths = [p for p, spec in catalog.items() if spec.auto_recommend and not spec.advanced]
    rank = {p: i for i, p in enumerate(ORDER)}
    return sorted(paths, key=lambda p: (rank.get(p, len(ORDER)), paths.index(p)))


def _inside(param: FilterParam, value: Any) -> bool:
    if param.kind == "bool":
        return isinstance(value, bool)
    if param.kind == "enum":
        return value in param.choices
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return (param.min is None or value >= param.min) and (param.max is None or value <= param.max)


def start_settings(spec: FilterSpec, existing: Mapping[str, Any] | None = None,
                   base: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], bool]:
    """The values a trial runs with, and whether any of the player's own values were kept.

    ``base`` replaces the gentle starting values (e.g. the community's recommended level, see :func:`ladder`).
    """
    values = spec.defaults()
    values.update(base if base is not None else GENTLE.get(spec.path, {}))
    own = False
    for name, value in (existing or {}).items():
        param = spec.param(name)
        if param is None:
            values[name] = value                  # a property the catalog does not know: leave it alone
        elif _inside(param, value):
            values[name] = value
            own = True
    values.update(FORCED.get(spec.path, {}))
    for name, cap in CAPS.get(spec.path, {}).items():
        value = values.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > cap:
            values[name] = cap
    return values, own


def find(filters: Sequence[FilterConfig], path: str) -> int | None:
    return next((i for i, fc in enumerate(filters) if fc.path == path), None)


def enable(filters: Sequence[FilterConfig], path: str, catalog: Mapping[str, FilterSpec],
           base: Mapping[str, Any] | None = None) -> tuple[tuple[FilterConfig, ...], FilterConfig | None, bool]:
    """Switch ``path`` on: ``(new filters, the store it replaced or None when it was added, own values kept)``.

    A store already in the list (switched off) is switched on in place; otherwise the filter is inserted at
    its place in the chain (``adapt.insert_in_chain``). Never installs anything: ``path`` must be installed.
    ``base`` are the starting values to use instead of the gentle ones (the player's own values still win).
    """
    spec = catalog[path]
    idx = find(filters, path)
    if idx is not None:
        previous = filters[idx]
        values, own = start_settings(spec, previous.settings, base)
        out = list(filters)
        out[idx] = FilterConfig(path, True, values)
        return tuple(out), previous, own
    values, _ = start_settings(spec, None, base)
    return insert_in_chain(filters, FilterConfig(path, True, values)), None, False


def disable(filters: Sequence[FilterConfig], path: str, previous: FilterConfig | None) -> tuple[FilterConfig, ...]:
    """Undo :func:`enable`: put the previous store back (switched off, the player's values) or remove it."""
    idx = find(filters, path)
    if idx is None:
        return tuple(filters)
    out = list(filters)
    if previous is None:
        del out[idx]
    else:
        out[idx] = previous
    return tuple(out)


def delay_ms(path: str, settings: Mapping[str, Any], rate_hz: float, mm_per_px: float) -> float | None:
    """Estimated extra delay of the filter with these values (``recommend.chain_latency``; rough)."""
    total, unknown = chain_latency([FilterConfig(path, True, dict(settings))], rate_hz, mm_per_px)
    return None if unknown else total


# --------------------------------------------------------------------------
# Amount of smoothing: five levels around the community's recommendation
# --------------------------------------------------------------------------
LEVEL_FACTORS = (0.5, 0.75, 1.0, 1.4, 2.0)      # how much of the recommended smoothing each level has (heuristic)
RECOMMENDED_LEVEL = 2
MIN_PRESETS = 2                                  # fewer community presets than this: the gentle values are used
NEAREST = 5                                      # the closest community presets that count
KNOBS: dict[str, tuple[str, ...]] = {            # the value(s) that scale the amount of smoothing
    RADIAL_TABLET: ("OuterRadius",), RADIAL_SCREEN: ("OuterRadius",), HAWKU: ("Latency",),
    ANTICHATTER: ("Latency",), NOISE_REDUCTION: ("DistanceThreshold",)}
COEFFICIENT_MAX = 0.99                           # same soft limit as the companion (adapt)


@dataclass(frozen=True)
class Ladder:
    """Five settings for one filter, from "very little" to "a lot", with the recommended one in the middle."""
    path: str
    source: str                                   # "community" (median of the closest presets) | "gentle" (heuristic)
    n_presets: int                                # presets behind a community ladder
    levels: tuple[dict[str, Any], ...]
    recommended: int = RECOMMENDED_LEVEL

    @property
    def adjustable(self) -> bool:
        """False for a filter with no single knob for the amount (all levels are the same)."""
        return any(level != self.levels[0] for level in self.levels)

    def level_of(self, settings: Mapping[str, Any]) -> int | None:
        """The level whose knob values are nearest to ``settings`` (None when the filter has no ladder)."""
        if not self.adjustable:
            return None
        knobs = KNOBS.get(self.path, ("latency",))
        def gap(level: Mapping[str, Any]) -> float:
            total = 0.0
            for name in knobs:
                a, b = settings.get(name), level.get(name)
                if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
                    total += abs(float(a) - float(b)) / (abs(float(b)) + 1.0)
            return total
        return min(range(len(self.levels)), key=lambda i: (gap(self.levels[i]), abs(i - self.recommended)))


def community_base(path: str, presets: Sequence[Any], catalog: Mapping[str, FilterSpec], area: Rect | None
                   ) -> tuple[dict[str, Any] | None, int]:
    """The median values of ``path`` in the closest community presets (adapted to ``area``), and how many.

    ``presets`` must come closest first (``community.rank_presets``). ``(None, n)`` when fewer than
    :data:`MIN_PRESETS` of them use the filter.
    """
    rows: list[dict[str, Any]] = []
    spec = catalog.get(path)
    for preset in presets:
        if len(rows) >= NEAREST or spec is None:
            break
        if not any(pf.path == path and pf.enabled is not False for pf in preset.filters):
            continue
        try:
            adapted, _ = community.adapt_preset(preset, area, dict(catalog))
        except (KeyError, ValueError):
            continue
        rows += [dict(fc.settings) for fc in adapted if fc.path == path]
    if len(rows) < MIN_PRESETS:
        return None, len(rows)
    base: dict[str, Any] = {}
    for param in spec.params:
        values = [r[param.name] for r in rows if isinstance(r.get(param.name), (int, float))
                  and not isinstance(r.get(param.name), bool)]
        if param.kind in ("float", "int") and len(values) >= MIN_PRESETS:
            median = statistics.median(values)
            base[param.name] = int(round(median)) if param.kind == "int" else float(median)
    return base, len(rows)


def ladder(spec: FilterSpec, presets: Sequence[Any] = (), area: Rect | None = None,
           catalog: Mapping[str, FilterSpec] | None = None) -> Ladder:
    """Five levels of smoothing for ``spec``: the community's median is the middle one, the others scale it."""
    path = spec.path
    base, n = community_base(path, presets, catalog or {path: spec}, area)
    source = "community" if base is not None else "gentle"
    middle, _ = start_settings(spec, None, base)
    levels = []
    for factor in LEVEL_FACTORS:
        values = dict(middle)
        for name in KNOBS.get(path, ()):
            param = spec.param(name)
            value = middle.get(name)
            if param is not None and isinstance(value, (int, float)) and not isinstance(value, bool):
                values[name] = param.clamp(float(value) * factor)
        coefficient = spec.param("SmoothingCoefficient")
        if path in (RADIAL_TABLET, RADIAL_SCREEN) and coefficient is not None:
            current = float(middle.get("SmoothingCoefficient", 0.9))
            new = 1.0 - (1.0 - current) / factor      # a bigger radius goes with a coefficient closer to 1
            values["SmoothingCoefficient"] = coefficient.clamp(min(new, COEFFICIENT_MAX))
        if path == RESAMPLER and spec.param("latency") is not None:
            values["latency"] = spec.param("latency").clamp(max(float(middle.get("latency", 0.0)) + 2.0 * (factor - 1.0), 0.0))
        enforce_constraints(path, values)
        for name, cap in CAPS.get(path, {}).items():
            if isinstance(values.get(name), (int, float)) and values[name] > cap:
                values[name] = cap
        values.update(FORCED.get(path, {}))
        levels.append(values)
    return Ladder(path, source, n if base is not None else 0, tuple(levels))


# --------------------------------------------------------------------------
# "Too much smoothing" warning
# --------------------------------------------------------------------------
WARN_MS = 8.0          # total estimated delay of the enabled filters from which the player is warned (heuristic)
HIGH_MS = 15.0         # from here "more smoothing" is refused (heuristic)


@dataclass(frozen=True)
class Assessment:
    level: str             # "ok" | "warn" | "high"
    total_ms: float        # estimated extra delay of every enabled filter together
    warn_ms: float
    high_ms: float


def total_delay_ms(filters: Sequence[FilterConfig], rate_hz: float, mm_per_px: float) -> float:
    """Estimated extra delay of the enabled filters together (filters that are not known add nothing)."""
    total, _ = chain_latency([fc for fc in filters if fc.enabled], rate_hz, mm_per_px)
    return round(total, 2)


def assess(filters: Sequence[FilterConfig], rate_hz: float, mm_per_px: float) -> Assessment:
    """Is the player already using a lot of smoothing? Compared with fixed, rough limits (not calibrated)."""
    total = total_delay_ms(filters, rate_hz, mm_per_px)
    level = "high" if total >= HIGH_MS else "warn" if total >= WARN_MS else "ok"
    return Assessment(level, total, WARN_MS, HIGH_MS)
