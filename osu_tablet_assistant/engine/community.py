"""Community presets as engine candidates: policy filter, area adaptation, comparison.

Community values are STARTING POINTS. They become ordinary candidates
(``community:<id>``) that the wizard measures and ranks like any other; nothing
here claims they are optimal.

Area adaptation rule (HEURISTIC, documented in docs/ARCHITECTURE.md)
--------------------------------------------------------------------
Radial Follow (tablet space) radii are millimetres on the tablet, so the same
radius is a different on-screen distance on a different area. The research
(docs/research/players.md) found that most top-player values come from the
Mixmg calculator, which computes ``OuterRadius = 0.00894 x area width`` and
``InnerRadius = SmoothingCoefficient = 0.0065 x area height``; cryshina's
profile also tells players to configure the filter "to your own area". So:

* ``OuterRadius`` scales with the area width ratio, ``InnerRadius`` with the
  height ratio (user area / the area the values were published for);
* ``SmoothingCoefficient`` scales with the height ratio only for presets with
  the calculator signature (InnerRadius == SmoothingCoefficient), otherwise it
  is kept (it is not a length);
* SoftKneeScale / SmoothingLeakCoefficient are kept (dimensionless);
* within 5 % of the source area nothing is scaled; with an unknown source area
  the values are used as published and the Reasons say so;
* pixel (screen-space) and raw-tablet-unit values are not scaled: their
  physical size depends on the source's display/area/LPI, which is unknown.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Sequence

from ..knowledge.filters import FilterParam, FilterSpec
from ..knowledge.presets import CommunityPreset, PresetFilter
from ..models import (Candidate, FilterConfig, PluginPolicy, ProfileChanges, ProfileInfo, Reason, Rect,
                      TabletSpec)
from . import recommend as rec

MIXMG_OUTER_PER_WIDTH = 0.00894     # Mixmg calculator, measured by the research on five areas
MIXMG_INNER_PER_HEIGHT = 0.0065
SIMILAR_AREA = 0.05                 # within 5 %: keep the published values
DEFAULT_LIMIT = 3                   # community candidates generated per run
# Properties expressed in screen pixels or raw tablet units: never scaled.
PIXEL_PARAMS = {rec.RADIAL_SCREEN: ("OuterRadius", "InnerRadius"),
                rec.CHATTER_RAW: (rec.CHATTER_STRENGTH,)}
RAW_UNIT_PARAMS = {rec.CHATTER_SMOOTH: (rec.CHATTER_STRENGTH,),
                   rec.SATURN_MULTI: ("rInner", "smoothDist")}
# Values the engine never writes on its own (prediction past the pen): forced off.
SAFE_VALUES = {(rec.ANTICHATTER, "PredictionEnabled"): False}
# Reverse-EMA style settings depend on the tablet's own hardware smoothing.
TABLET_SPECIFIC = {(rec.RECONSTRUCTOR, "EMAWeight"), (rec.RESAMPLER, "reverseSmoothing"),
                   (rec.SATURN_MULTI, "reverseSmoothing")}
_MODEL = re.compile(r"\b([A-Z]{1,4})-?(\d{3,4}[A-Z]?)\b")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def tablet_models(name: str | None) -> set[str]:
    """Model codes in a tablet name: "Wacom CTL-472" -> {"CTL472"}."""
    return {a + b for a, b in _MODEL.findall((name or "").upper())}


def same_tablet(preset: CommunityPreset, tablet: TabletSpec | None) -> bool | None:
    """True/False when both models are known, None when either is unknown."""
    mine = tablet_models(tablet.name if tablet else None)
    theirs = tablet_models(preset.tablet)
    if not mine or not theirs:
        return None
    return bool(mine & theirs)


def _bound(param: FilterParam, value: Any) -> Any:
    """Bounds only (no step grid): published values keep their precision."""
    if param.kind in ("bool", "enum"):
        return param.clamp(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return param.default
    if not math.isfinite(number):
        return param.default
    number = min(max(number, float(param.min)), float(param.max))
    return int(round(number)) if param.kind == "int" else round(number, 6)


def _is_mixmg(pf: PresetFilter) -> bool:
    inner, coef = pf.settings.get("InnerRadius"), pf.settings.get("SmoothingCoefficient")
    return isinstance(inner, (int, float)) and isinstance(coef, (int, float)) and inner > 0 \
        and abs(inner - coef) < 1e-9


def _order(filters: Sequence[FilterConfig]) -> tuple[FilterConfig, ...]:
    """Research order (rec.RESEARCH_ORDER); unknown paths keep their relative place at the end."""
    rank = {p: i for i, p in enumerate(rec.RESEARCH_ORDER)}
    return tuple(sorted(filters, key=lambda f: rank.get(f.path, len(rank))))


def allowed_paths(policy: PluginPolicy, available: set[str], current: ProfileInfo | None,
                  catalog: dict[str, FilterSpec]) -> set[str]:
    """Filter paths a preset may use under ``policy``.

    tune_only: only the filters already enabled in the current profile;
    installed / allow_new: detected plugins that the engine may recommend.
    """
    if policy is PluginPolicy.TUNE_ONLY:
        return {f.path for f in (current.filters if current else ()) if f.enabled and f.path in catalog}
    return {p for p in available if p in catalog and catalog[p].auto_recommend}


def unusable_reason(preset: CommunityPreset, policy: PluginPolicy, available: set[str],
                    current: ProfileInfo | None, tablet: TabletSpec | None,
                    catalog: dict[str, FilterSpec]) -> str | None:
    """Why ``preset`` cannot be a candidate (None = it can).

    Codes: ``no_filters``, ``user_only`` (a filter the engine never picks),
    ``missing_plugin``, ``not_in_profile`` (tune_only), ``other_tablet``
    (tablet-specific values such as a reverse EMA for another model).
    """
    if not preset.enabled_filters:
        return "no_filters"
    if any(not catalog[p].auto_recommend for p in preset.required_paths if p in catalog):
        return "user_only"
    if not preset.required_paths <= allowed_paths(policy, available, current, catalog):
        return "not_in_profile" if policy is PluginPolicy.TUNE_ONLY else "missing_plugin"
    specific = "tablet_specific" in preset.flags or any(
        (f.path, name) in TABLET_SPECIFIC and value != 1 for f in preset.enabled_filters
        for name, value in f.settings.items())
    if specific and same_tablet(preset, tablet) is not True:
        return "other_tablet"
    return None


def distance(preset: CommunityPreset, tablet: TabletSpec | None, area: Rect,
             current: ProfileInfo | None) -> float:
    """How far a preset is from the user's setup (lower = closer). Heuristic weights."""
    same = same_tablet(preset, tablet)
    d = 0.0 if same else (0.3 if same is None else 0.4)
    src = preset.area_mm
    if src and area.width > 0 and area.height > 0:
        d += abs(math.log(area.width / src[0])) + 0.5 * abs(math.log(area.height / src[1]))
    else:
        d += 0.7
    d += {"high": 0.0, "medium": 0.15, "low": 0.3}[preset.confidence]
    if "stale" in preset.flags:
        d += 0.2
    if current is not None and preset.required_paths & {f.path for f in current.filters if f.enabled}:
        d -= 0.2
    return round(d, 6)


def rank_presets(presets: Sequence[CommunityPreset], tablet: TabletSpec | None, area: Rect,
                 current: ProfileInfo | None) -> list[CommunityPreset]:
    """Presets sorted closest first (ties: confidence, then id)."""
    return sorted(presets, key=lambda p: (distance(p, tablet, area, current), p.id))


def starting_options(presets: Sequence[CommunityPreset], tablet: TabletSpec | None, area: Rect,
                     current: ProfileInfo | None, policy: PluginPolicy, available: set[str],
                     catalog: dict[str, FilterSpec]) -> list[CommunityPreset]:
    """Every preset usable as a starting point under ``policy``, closest first."""
    usable = [p for p in presets
              if unusable_reason(p, policy, available, current, tablet, catalog) is None]
    return rank_presets(usable, tablet, area, current)


def wanted_paths(tablet: TabletSpec | None, refresh_hz: float, available: set[str],
                 notices_jitter: bool) -> list[str]:
    """Plugins the standard candidates would have used but are not installed (for suggestions)."""
    wanted = []
    if not available & {rec.RADIAL_TABLET, rec.HAWKU, rec.ANTICHATTER, rec.RADIAL_SCREEN}:
        wanted.append(rec.RADIAL_TABLET)
    rate = tablet.max_report_rate_hz if tablet and tablet.max_report_rate_hz > 0 else rec.DEFAULT_REPORT_RATE_HZ
    if refresh_hz >= rec.RESAMPLE_REFRESH_RATIO * rate and rec.RESAMPLER not in available:
        wanted.append(rec.RESAMPLER)
    if notices_jitter and not available & set(rec.NOISE_PREFERENCE):
        wanted.append(rec.CHATTER_SMOOTH)
    return wanted


def select_presets(presets: Sequence[CommunityPreset], tablet: TabletSpec | None, area: Rect,
                   current: ProfileInfo | None, policy: PluginPolicy, available: set[str],
                   catalog: dict[str, FilterSpec], limit: int = DEFAULT_LIMIT,
                   include: Sequence[str] = ()) -> list[CommunityPreset]:
    """The usable presets to turn into candidates: ``include`` ids first, then the closest."""
    usable = [p for p in presets
              if unusable_reason(p, policy, available, current, tablet, catalog) is None]
    wanted = [p for p in usable if p.id in {i.removeprefix("community:") for i in include}]
    rest = [p for p in rank_presets(usable, tablet, area, current) if p not in wanted]
    return wanted + rest[:max(limit - len(wanted), 0)]


# --------------------------------------------------------------------------
# Adaptation
# --------------------------------------------------------------------------
def adapt_preset(preset: CommunityPreset, area: Rect | None,
                 catalog: dict[str, FilterSpec]) -> tuple[tuple[FilterConfig, ...], list[Reason]]:
    """The preset's enabled filters adapted to ``area`` (see the module docstring), with reasons."""
    reasons: list[Reason] = []
    src = preset.source_area
    ratio = None
    if src and area is not None and area.width > 0 and area.height > 0:
        ratio = (area.width / src[0], area.height / src[1])
    filters: list[FilterConfig] = []
    if any(f.enabled is None for f in preset.enabled_filters):
        reasons.append(Reason("engine.reason.community_enabled_unclear"))
    for pf in preset.enabled_filters:
        spec = catalog[pf.path]
        settings = spec.defaults()
        settings.update(pf.settings)
        if pf.path == rec.RADIAL_TABLET:
            reasons += _scale_radial(pf, settings, ratio, src, area, spec)
        if pf.path in PIXEL_PARAMS and set(PIXEL_PARAMS[pf.path]) & set(pf.settings):
            reasons.append(Reason("engine.reason.community_px_unscaled", {"filter": spec.display_name}))
        if pf.path in RAW_UNIT_PARAMS and set(RAW_UNIT_PARAMS[pf.path]) & set(pf.settings):
            reasons.append(Reason("engine.reason.community_raw_units", {"filter": spec.display_name}))
        freq = spec.param("Frequency")
        if freq is not None and settings.get("Frequency") != freq.default:
            reasons.append(Reason("engine.reason.community_frequency_reset",
                                  {"filter": spec.display_name, "value": settings.get("Frequency")}))
            settings["Frequency"] = freq.default
        for (path, name), safe in SAFE_VALUES.items():
            if pf.path == path and settings.get(name) != safe:
                settings[name] = safe
                reasons.append(Reason("engine.reason.community_prediction_off",
                                      {"filter": spec.display_name}))
        for param in spec.params:
            settings[param.name] = _bound(param, settings[param.name])
        rec.enforce_constraints(pf.path, settings)
        filters.append(FilterConfig(pf.path, True, settings))
    for path, name, value, used in preset.clamped:
        reasons.append(Reason("engine.reason.community_clamped", {
            "filter": catalog[path].display_name, "param": name, "value": value, "new": used}))
    reasons += [Reason(f"engine.reason.community_flag_{flag}") for flag in preset.flags]
    return _order(filters), reasons


def _scale_radial(pf: PresetFilter, settings: dict[str, Any], ratio: tuple[float, float] | None,
                  src: tuple[float, float] | None, area: Rect | None, spec: FilterSpec) -> list[Reason]:
    if ratio is None:
        return [Reason("engine.reason.community_area_unknown", {"filter": spec.display_name})]
    sw, sh = ratio
    if abs(sw - 1) <= SIMILAR_AREA and abs(sh - 1) <= SIMILAR_AREA:
        return [Reason("engine.reason.community_area_similar", {"filter": spec.display_name})]
    reasons = [Reason("engine.reason.community_scaling_rule", {
        "src_w": f"{src[0]:g}", "src_h": f"{src[1]:g}", "w": f"{area.width:.1f}", "h": f"{area.height:.1f}"})]
    scaled = [("OuterRadius", sw), ("InnerRadius", sh)]
    if _is_mixmg(pf):
        scaled.append(("SmoothingCoefficient", sh))
    for name, factor in scaled:
        if name not in pf.settings:
            continue
        old = float(pf.settings[name])
        new = round(old * factor, 6)
        settings[name] = new
        reasons.append(Reason("engine.reason.community_scaled", {
            "filter": spec.display_name, "param": name, "old": round(old, 4), "new": round(new, 4)}))
    if not 0.5 <= sw <= 2.0 or not 0.5 <= sh <= 2.0:
        reasons.append(Reason("engine.reason.community_area_far"))
    return reasons


def map_onto_current(current: Sequence[FilterConfig], adapted: Sequence[FilterConfig],
                     catalog: dict[str, FilterSpec]) -> tuple[tuple[FilterConfig, ...], list[Reason]]:
    """tune_only: the user's chain (same order, same on/off) with the preset's values, change by change."""
    wanted = {f.path: f for f in adapted}
    out: list[FilterConfig] = []
    reasons: list[Reason] = []
    for fc in current:
        target = wanted.get(fc.path)
        spec = catalog.get(fc.path)
        if not fc.enabled or target is None or spec is None:
            out.append(fc)
            continue
        settings = dict(fc.settings)
        for param in spec.params:
            if param.name == "Frequency" or param.name not in target.settings:
                continue
            old, new = settings.get(param.name, param.default), target.settings[param.name]
            if old != new:
                settings[param.name] = new
                reasons.append(change_reason(spec, param, old, new))
        out.append(FilterConfig(fc.path, fc.enabled, settings))
    return tuple(out), reasons


def change_reason(spec: FilterSpec, param: FilterParam, old: Any, new: Any) -> Reason:
    """``Filter Param: old -> new unit`` (one changed parameter)."""
    def fmt(value: Any) -> Any:
        return round(value, 4) if isinstance(value, float) else value
    return Reason("engine.reason.tune_change", {"filter": spec.display_name, "param": param.name,
                                                "old": fmt(old), "new": fmt(new), "unit": param.unit})


# --------------------------------------------------------------------------
# Candidates
# --------------------------------------------------------------------------
def source_reason(preset: CommunityPreset) -> Reason:
    """First reason of a community candidate; also carries the candidate's label name."""
    if preset.attribution == "user":
        return Reason("engine.reason.community_user_values", {"name": preset.name})
    if preset.source_date:
        return Reason("engine.reason.community_source", {
            "name": preset.name, "source": preset.source_url, "date": preset.source_date})
    return Reason("engine.reason.community_source_undated", {
        "name": preset.name, "source": preset.source_url, "retrieved": preset.retrieved})


def community_candidate(preset: CommunityPreset, base: ProfileChanges, catalog: dict[str, FilterSpec],
                        rate_hz: float, mm_per_px: float, policy: PluginPolicy,
                        current: ProfileInfo | None) -> Candidate:
    """One measurable candidate built from ``preset`` on the shared ``base`` area."""
    filters, reasons = adapt_preset(preset, base.tablet_area, catalog)
    if policy is PluginPolicy.TUNE_ONLY and current is not None:
        filters, changes = map_onto_current(current.filters, filters, catalog)
        reasons = [Reason("engine.reason.community_tune_only")] + changes + reasons
    latency, _ = rec.chain_latency(filters, rate_hz, mm_per_px)
    head = [source_reason(preset), Reason(f"engine.reason.community_confidence_{preset.confidence}"),
            Reason("engine.reason.community_starting_point")]
    label = "engine.candidate.community_user.label" if preset.attribution == "user" \
        else "engine.candidate.community.label"
    return Candidate(
        id=preset.candidate_id, label_key=label, summary_key="engine.candidate.community.summary",
        changes=ProfileChanges(base.tablet_area, base.display_area, base.lock_aspect_ratio, filters),
        reasons=tuple(head + reasons + [Reason("engine.reason.latency_estimate", {"ms": latency})]),
        est_added_latency_ms=latency, tags=("community", f"confidence-{preset.confidence}"))


def label_params(candidate: Candidate) -> dict[str, Any]:
    """Params for ``t(candidate.label_key, **params)`` (community candidates carry a name)."""
    for reason in candidate.reasons:
        if reason.key in ("engine.reason.community_source", "engine.reason.community_source_undated",
                          "engine.reason.community_user_values"):
            return {"name": reason.params.get("name", "")}
    return {}


# --------------------------------------------------------------------------
# Plugin suggestions (policy allow_new) and comparison with the best result
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class PluginSuggestion:
    """A plugin that is not installed and could help; the program never installs it."""
    path: str
    display_name: str
    plugin_name: str
    homepage: str
    reason: Reason


def plugin_suggestions(presets: Sequence[CommunityPreset], tablet: TabletSpec | None, area: Rect,
                       current: ProfileInfo | None, available: set[str],
                       catalog: dict[str, FilterSpec], wanted: Sequence[str] = (),
                       limit: int = 5) -> list[PluginSuggestion]:
    """Not-installed plugins worth considering (engine wishes first, then close presets).

    ``wanted`` are paths the engine would have used (e.g. a smoother when none
    is installed). Plugins the engine never auto-recommends are never suggested.
    """
    out: dict[str, PluginSuggestion] = {}

    def add(path: str, reason: Reason) -> None:
        spec = catalog.get(path)
        if spec is None or path in available or path in out or not spec.auto_recommend:
            return
        out[path] = PluginSuggestion(path, spec.display_name, spec.plugin_name,
                                     spec.homepage or spec.sources[0], reason)

    for path in wanted:
        add(path, Reason("engine.suggest.reason_engine", {"filter": catalog[path].display_name}
                         if path in catalog else {}))
    for preset in rank_presets(presets, tablet, area, current):
        if same_tablet(preset, tablet) is False and "tablet_specific" in preset.flags:
            continue
        for path in sorted(preset.required_paths):
            add(path, Reason("engine.suggest.reason_preset", {
                "name": preset.name, "source": preset.source_url or "-"}))
    return list(out.values())[:limit]


@dataclass(frozen=True)
class PresetComparison:
    """How the best result differs from one community preset (lines are i18n key + params)."""
    preset: CommunityPreset
    distance: float
    lines: tuple[tuple[str, dict[str, Any]], ...]


def _fmt(value: Any) -> Any:
    return round(value, 3) if isinstance(value, float) else value


def compare_with_best(best: Candidate, presets: Sequence[CommunityPreset], tablet: TabletSpec | None,
                      current: ProfileInfo | None, catalog: dict[str, FilterSpec],
                      limit: int = DEFAULT_LIMIT) -> list[PresetComparison]:
    """The ``limit`` closest presets and what differs from ``best`` (adapted to its area)."""
    area = best.changes.tablet_area or (current.tablet_area if current else Rect(0, 0, 0, 0))
    mine_filters = best.changes.filters if best.changes.filters is not None else (
        current.filters if current else ())
    mine = {f.path: f for f in mine_filters if f.enabled}
    result = []
    for preset in rank_presets(presets, tablet, area, current)[:limit]:
        lines: list[tuple[str, dict[str, Any]]] = []
        if preset.area_mm:
            lines.append(("engine.compare.area", {
                "w": f"{preset.area_mm[0]:g}", "h": f"{preset.area_mm[1]:g}",
                "my_w": f"{area.width:.1f}", "my_h": f"{area.height:.1f}"}))
        else:
            lines.append(("engine.compare.area_unknown", {}))
        if preset.tablet:
            lines.append(("engine.compare.tablet", {"tablet": preset.tablet}))
        if not preset.filters and "filters_unknown" in preset.style_tags:
            lines.append(("engine.compare.filters_unknown", {}))
            result.append(PresetComparison(preset, distance(preset, tablet, area, current), tuple(lines)))
            continue
        adapted, _ = adapt_preset(preset, area, catalog) if preset.enabled_filters else ((), [])
        theirs = {f.path: f for f in adapted}
        head = len(lines)
        if not theirs and not mine:
            lines.append(("engine.compare.both_none", {}))
        for path, fc in theirs.items():
            name = catalog[path].display_name
            if path not in mine:
                lines.append(("engine.compare.only_theirs", {"filter": name}))
                continue
            published = next(pf.settings for pf in preset.enabled_filters if pf.path == path)
            for key in published:
                if key == "Frequency":
                    continue
                a, b = mine[path].settings.get(key), fc.settings.get(key)
                if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
                        and not isinstance(a, bool) and abs(a - b) <= 0.05 * max(abs(a), abs(b), 1e-9):
                    continue
                if a == b:
                    continue
                lines.append(("engine.compare.param", {"filter": name, "param": key, "mine": _fmt(a),
                                                       "theirs": _fmt(b), "published": _fmt(published[key])}))
        for path in mine:
            if path not in theirs:
                lines.append(("engine.compare.only_mine", {"filter": catalog[path].display_name
                                                           if path in catalog else path}))
        if theirs and len(lines) == head:
            lines.append(("engine.compare.same", {}))
        result.append(PresetComparison(preset, distance(preset, tablet, area, current), tuple(lines)))
    return result
