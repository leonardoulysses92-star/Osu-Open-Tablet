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

from typing import Any, Mapping, Sequence

from ..knowledge.filters import FilterParam, FilterSpec
from ..models import FilterConfig
from .adapt import insert_in_chain
from .recommend import ANTICHATTER, HAWKU, NOISE_REDUCTION, RADIAL_SCREEN, RADIAL_TABLET, RESAMPLER, chain_latency

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


def start_settings(spec: FilterSpec, existing: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], bool]:
    """The values a trial runs with, and whether any of the player's own values were kept."""
    values = spec.defaults()
    values.update(GENTLE.get(spec.path, {}))
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


def enable(filters: Sequence[FilterConfig], path: str, catalog: Mapping[str, FilterSpec]
           ) -> tuple[tuple[FilterConfig, ...], FilterConfig | None, bool]:
    """Switch ``path`` on: ``(new filters, the store it replaced or None when it was added, own values kept)``.

    A store already in the list (switched off) is switched on in place; otherwise the filter is inserted at
    its place in the chain (``adapt.insert_in_chain``). Never installs anything: ``path`` must be installed.
    """
    spec = catalog[path]
    idx = find(filters, path)
    if idx is not None:
        previous = filters[idx]
        values, own = start_settings(spec, previous.settings)
        out = list(filters)
        out[idx] = FilterConfig(path, True, values)
        return tuple(out), previous, own
    values, _ = start_settings(spec)
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
