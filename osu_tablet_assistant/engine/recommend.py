"""Candidate profiles, scoring, ranking and one-step refinement.

Everything here is a pure, deterministic function of its inputs. Numbers are
heuristics with documented rationale; latencies are *estimates* derived from
each filter's algorithm, never measurements.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from statistics import median
from typing import Any, Sequence

from ..knowledge.filters import FilterParam, FilterSpec, load_catalog
from ..models import (Candidate, CandidateResult, DisplaySpec, FilterConfig, MetricsReport,
                      PluginPolicy, Priority, ProfileChanges, ProfileInfo, Reason, Rect, SkillLevel,
                      TabletSpec, UserPreferences)
from .area import recommend_area

NOISE_REDUCTION = "OpenTabletDriver.Plugin.NoiseReduction"
HAWKU = "TabletDriverFilters.Hawku.Smoothing"
ANTICHATTER = "TabletDriverFilters.Devocub.Antichatter"
RESAMPLER = "TemporalResampler"
RADIAL_TABLET = "RadialFollow.RadialFollowSmoothingTabletSpace"
RADIAL_SCREEN = "RadialFollow.RadialFollowSmoothingScreenSpace"
SATURN_MULTI = "Saturn.Multifilter"
SATURN_ROUNDING = "Saturn.AreaRoundingDFC"
CHATTER_RAW = "Kuuube_s_Chatter_Exterminator.Kuuube_s_CHATTER_EXTERMINATOR_RAW"
CHATTER_SMOOTH = "Kuuube_s_Chatter_Exterminator.Kuuube_s_CHATTER_EXTERMINATOR_SMOOTH"
CHATTER_STRENGTH = "Chatter_Extermination_Strength"
RECONSTRUCTOR = "VoiDPlugins.Filter.Reconstructor"
SPECTRAL = "TrajectoryEigenFilter.OTD.TrajectoryEigenFilterPlugin"
SATURN_HRRNC = "Saturn.HRRNC"
BEZIER = "BezierInterpolator.BezierInterp"
SCUFFED = "ScuffedInterpolator.ScuffedInterp"
FRENET = "FrenetSerretFramePredictor.FrenetSerretFramePredictor"
HOVER_LIMITER = "Hover_Distance_Limiter.Hover_Distance_Limiter"

# Order in which the engine writes a NEW chain (docs/research/filters.md §0, §3).
# In OTD 0.6 ``PipelinePosition.Raw`` is an alias of PreTransform, so inside the
# pre-transform group filters run exactly in the order of the profile's Filters
# list: RESEARCH_ORDER *is* the execution order. A user's existing chain is never
# re-sorted by stage (tune_only keeps it as it is).
# 1. Report gating (Hover Distance Limiter, OTD 0.6.8 Internal stage) first.
# 2. Noise compensation and reconstruction before anything else (HRRNC tooltip
#    "should probably be first", Spectral Reconstructor sorts itself first,
#    Reconstructor undoes hardware smoothing on the raw reports).
# 3. Noise/chatter removal before smoothing: a smoother spreads a noise spike
#    over many frames, while removing it first lets the smoother use less latency.
# 4. Synchronous smoothers (Radial Follow) before timer-driven ones (Hawku,
#    Devocub): an async element re-emits frames at its own Frequency, so any
#    filter after it would process interpolated frames instead of reports.
# 5. Resamplers after smoothing (public osu! preset order: CHATTER_SMOOTH ->
#    Radial Follow -> Temporal Resampler).
# 6. Screen-space (post-transform) filters last; area reshaping at the very end.
RESEARCH_ORDER = (HOVER_LIMITER, SATURN_HRRNC, SPECTRAL, RECONSTRUCTOR, NOISE_REDUCTION, CHATTER_SMOOTH,
                  SATURN_MULTI, RADIAL_TABLET, ANTICHATTER, HAWKU, RESAMPLER, SCUFFED, CHATTER_RAW,
                  RADIAL_SCREEN, BEZIER, FRENET, SATURN_ROUNDING)
# DEPRECATED pre-audit order (Temporal Resampler first, as if "raw" ran before
# "pre"). Kept unchanged only because engine.adapt (companion mode, another
# workstream) imports it; new code uses RESEARCH_ORDER.
CHAIN_ORDER = (RESAMPLER, NOISE_REDUCTION, CHATTER_SMOOTH, SATURN_MULTI, RADIAL_TABLET,
               ANTICHATTER, HAWKU, CHATTER_RAW, RADIAL_SCREEN, SATURN_ROUNDING)
ASYNC_SMOOTHERS = (HAWKU, ANTICHATTER)
NOISE_PREFERENCE = (CHATTER_SMOOTH, NOISE_REDUCTION, CHATTER_RAW)
# Radial Follow first where latency matters: it never delays a jump by more
# than its outer radius. Devocub first for "smooth": it adds extra smoothing
# only on slow/small movement, which is where the cursor looks shaky.
SMOOTHER_PREFERENCE: dict[str, tuple[str, ...]] = {
    "low-latency": (RADIAL_TABLET, RADIAL_SCREEN, HAWKU, ANTICHATTER),
    "balanced": (RADIAL_TABLET, HAWKU, ANTICHATTER, RADIAL_SCREEN),
    "smooth": (ANTICHATTER, HAWKU, RADIAL_TABLET, RADIAL_SCREEN),
}
# Relative smoothing level of each generated candidate (1.0 = balanced).
LEVEL = {"low-latency": 0.4, "balanced": 1.0, "smooth": 2.0}
FILTERED_IDS = tuple(LEVEL)

PRIORITY_FACTOR = {Priority.LATENCY: 0.7, Priority.BALANCED: 1.0, Priority.SMOOTHNESS: 1.4}
SKILL_FACTOR = {SkillLevel.BEGINNER: 1.1, SkillLevel.INTERMEDIATE: 1.0, SkillLevel.ADVANCED: 0.9}
LAG_NOTICED_FACTOR = 0.8
JITTER_NOTICED_FACTOR = 1.2

BASE_SMOOTHING_MS = 10.0      # Hawku/Devocub latency at level 1.0
MIN_ASYNC_SMOOTHING_MS = 4.0  # Hawku tooltip: below ~4 ms rather disable it
BASE_RADIUS_MM = 0.8          # Radial Follow outer radius at level 1.0 (default 1.0 mm)
DEFAULT_REPORT_RATE_HZ = 133.0
JITTER_FLOOR_MM = 0.02        # below this RMS wobble nothing is visible anyway
VISIBLE_JITTER_PX = 0.5       # RMS wobble (in screen px) that makes the cursor flicker
LOW_RATE_RATIO = 0.8          # measured rate below 80% of the hardware rate
RESAMPLE_REFRESH_RATIO = 1.5  # monitor refreshes >= 1.5x more often than reports arrive
# Typical osu! cursor speed (250 mm/s) used to turn distance-based filter
# lag (radii, thresholds) into a time estimate.
TYPICAL_SPEED_MM_PER_MS = 0.25
DEFAULT_MM_PER_PX = 80.0 / 1920.0


# --------------------------------------------------------------------------
# Latency estimates (rough; documented per filter, never presented as measured)
# --------------------------------------------------------------------------
def _num(settings: dict[str, Any], name: str, default: float) -> float:
    value = settings.get(name, default)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        else default


def _filter_latency_ms(fc: FilterConfig, rate_hz: float, mm_per_px: float) -> float | None:
    """Estimated extra delay of one filter for a steadily moving pen; None if unknown.

    * Hawku: EMA whose time constant is ``Latency`` -> ramp lag = Latency.
    * Devocub: base weight reaches 90% within ``Latency`` -> time constant
      Latency/ln 10 for fast moves (slow moves get more smoothing).
    * Temporal Resampler: (1 - frameShift) report intervals + its EMA ``latency``.
    * Radial Follow: lag is bounded by the outer radius -> radius / typical speed.
    * Noise Reduction: output waits until the pen leaves DistanceThreshold.
    * Chatter Exterminator / Saturn Multifilter: no documented figure; assume
      half a report interval ("low latency" per their authors).
    * Saturn Area Rounding, Hover Distance Limiter: remap / gate, no delay.
    * Reconstructor: reverse EMA, removes delay rather than adding it -> 0.
    * Spectral Reconstructor: ``LatencySamples`` reports (README).
    * Bezier Interpolator: about one report interval (code estimate in the audit).
    * Scuffed Interpolator: up to one interval scaled by its amount (estimate).
    * Saturn HRRNC, Frenet-Serret predictor: no figure -> unknown.
    """
    s = fc.settings
    interval = 1000.0 / rate_hz
    if fc.path in (SATURN_ROUNDING, HOVER_LIMITER, RECONSTRUCTOR):
        return 0.0
    if fc.path == SPECTRAL:
        return _num(s, "LatencySamples", 4.0) * interval
    if fc.path == BEZIER:
        return interval
    if fc.path == SCUFFED:
        return min(max(_num(s, "_getSetScuffedInterpAmount", 0.5), 0.0), 1.0) * interval
    if fc.path == HAWKU:
        return _num(s, "Latency", 2.0)
    if fc.path == ANTICHATTER:
        return _num(s, "Latency", 2.0) / math.log(10)
    if fc.path == RESAMPLER:
        return (1.0 - _num(s, "frameShift", 0.5)) * interval + _num(s, "latency", 0.0)
    if fc.path == RADIAL_TABLET:
        return _num(s, "OuterRadius", 1.0) / TYPICAL_SPEED_MM_PER_MS
    if fc.path == RADIAL_SCREEN:
        return _num(s, "OuterRadius", 5.0) * mm_per_px / TYPICAL_SPEED_MM_PER_MS
    if fc.path == NOISE_REDUCTION:
        return _num(s, "DistanceThreshold", 0.5) / TYPICAL_SPEED_MM_PER_MS
    if fc.path in (CHATTER_RAW, CHATTER_SMOOTH, SATURN_MULTI):
        return 0.5 * interval
    return None


def chain_latency(filters: Sequence[FilterConfig], rate_hz: float,
                   mm_per_px: float) -> tuple[float, tuple[str, ...]]:
    """Sum of estimates for enabled filters, plus the paths with no estimate."""
    total, unknown = 0.0, []
    for fc in filters:
        if not fc.enabled:
            continue
        est = _filter_latency_ms(fc, rate_hz, mm_per_px)
        if est is None:
            unknown.append(fc.path)
        else:
            total += est
    return round(total, 1), tuple(unknown)


# --------------------------------------------------------------------------
# Candidate generation
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class _Context:
    prefs: UserPreferences
    rate_hz: float               # tablet report rate (latency estimates, Saturn Frequency)
    effective_rate_hz: float     # min(hardware, measured) report rate
    refresh_hz: float
    mm_per_px: float
    jitter_mm: float | None
    noisy: bool
    want_resampler: bool
    strength: float              # user/priority multiplier of every LEVEL
    reasons: tuple[Reason, ...]  # measurement/preference reasons shared by candidates


def _median_of(values: Sequence[float | None]) -> float | None:
    clean = [v for v in values if v is not None and math.isfinite(v)]
    return median(clean) if clean else None


def _context(tablet: TabletSpec, display: DisplaySpec, prefs: UserPreferences, area: Rect,
             baseline: Sequence[MetricsReport] | None) -> _Context:
    rate_hw = tablet.max_report_rate_hz if tablet.max_report_rate_hz > 0 else DEFAULT_REPORT_RATE_HZ
    mm_per_px = area.width / display.width_px
    reports = list(baseline or ())
    jitter = _median_of([r.jitter_rms_mm for r in reports if r.test_kind == "stillness"])
    measured = _median_of([r.effective_rate_hz for r in reports if r.test_kind == "rate"]) \
        or _median_of([r.effective_rate_hz for r in reports])
    reasons: list[Reason] = []

    noisy = prefs.notices_jitter
    if jitter is not None:
        jitter_px = jitter / mm_per_px
        if jitter > max(JITTER_FLOOR_MM, VISIBLE_JITTER_PX * mm_per_px):
            noisy = True
            reasons.append(Reason("engine.reason.jitter_measured_high",
                                  {"jitter": round(jitter, 3), "px": round(jitter_px, 2)}))
        else:
            reasons.append(Reason("engine.reason.jitter_measured_low",
                                  {"jitter": round(jitter, 3), "px": round(jitter_px, 2)}))
    if prefs.notices_jitter:
        reasons.append(Reason("engine.reason.jitter_reported"))

    rate_low = measured is not None and measured < LOW_RATE_RATIO * rate_hw
    if rate_low:
        reasons.append(Reason("engine.reason.rate_low",
                              {"measured": round(measured, 0), "hardware": round(rate_hw, 0)}))
    effective = min(rate_hw, measured) if measured else rate_hw
    want_resampler = rate_low or display.refresh_hz >= RESAMPLE_REFRESH_RATIO * effective

    strength = PRIORITY_FACTOR[prefs.priority] * SKILL_FACTOR[prefs.skill_level]
    if prefs.priority is not Priority.BALANCED:
        reasons.append(Reason(f"engine.reason.priority_{prefs.priority.value}"))
    if prefs.notices_input_lag:
        strength *= LAG_NOTICED_FACTOR
        reasons.append(Reason("engine.reason.input_lag_reported"))
    if prefs.notices_jitter:
        strength *= JITTER_NOTICED_FACTOR
    return _Context(prefs, rate_hw, effective, display.refresh_hz, mm_per_px, jitter, noisy,
                    want_resampler, strength, tuple(reasons))


def _noise_settings(path: str, level: float, ctx: _Context) -> dict[str, Any]:
    """Noise/chatter filter strength for a smoothing ``level``."""
    if path == CHATTER_SMOOTH:   # README: 6-7 dragging, 15-16 hovering
        return {CHATTER_STRENGTH: min(max(10.0 * level, 6.0), 16.0)}
    if path == CHATTER_RAW:      # README: 2-3 dragging, 5-6 hovering
        return {CHATTER_STRENGTH: min(max(3.0 * level, 2.0), 6.0)}
    # Noise Reduction: the threshold must cover the measured wobble amplitude.
    base = 3.0 * ctx.jitter_mm if ctx.jitter_mm else 0.3
    return {"Samples": round(4 + 3 * level), "DistanceThreshold": base * math.sqrt(level)}


def _smoother_settings(path: str, level: float, ctx: _Context,
                       deadzone_mm: float) -> dict[str, Any]:
    """Smoothing filter settings for a ``level``; ``deadzone_mm`` > 0 fights chatter."""
    if path == HAWKU:
        return {"Latency": max(BASE_SMOOTHING_MS * level, MIN_ASYNC_SMOOTHING_MS)}
    if path == ANTICHATTER:
        return {"Latency": max(BASE_SMOOTHING_MS * level, MIN_ASYNC_SMOOTHING_MS),
                "AntichatterStrength": 2.0 if level < 0.7 else 3.0}
    outer_mm = BASE_RADIUS_MM * level
    inner_mm = min(deadzone_mm, 0.5 * outer_mm)
    scale = 1.0 if path == RADIAL_TABLET else 1.0 / ctx.mm_per_px
    return {"OuterRadius": outer_mm * scale, "InnerRadius": inner_mm * scale,
            "SmoothingCoefficient": min(max(1.0 - 0.08 / level, 0.5), 0.99),
            "SoftKneeScale": 1.0, "SmoothingLeakCoefficient": 0.0}


def enforce_constraints(path: str, settings: dict[str, Any]) -> None:
    """Cross-parameter rules the catalog bounds cannot express."""
    if path in (RADIAL_TABLET, RADIAL_SCREEN) and "InnerRadius" in settings and "OuterRadius" in settings:
        settings["InnerRadius"] = min(settings["InnerRadius"], settings["OuterRadius"])


def _make_filter(spec: FilterSpec, overrides: dict[str, Any]) -> FilterConfig:
    """Full settings (defaults + overrides), clamped.

    ``Frequency`` always keeps the catalog default (1000 Hz): for Hawku,
    Devocub, Bezier and the other async filters it is the filter's own timer
    rate, Temporal Resampler only logs it, and the old "Saturn Multifilter
    Frequency = report rate" rule was dropped (the 2026-10-30 audit found that
    MultifilterCore compares it with the measured *update* interval and no
    Saturn doc asks for the report rate).
    """
    settings = spec.defaults()
    settings.update(overrides)
    for param in spec.params:
        settings[param.name] = param.clamp(settings[param.name])
    enforce_constraints(spec.path, settings)
    return FilterConfig(path=spec.path, enabled=True, settings=settings)


def policy_filters(available_filters: set[str], prefs: UserPreferences, current: ProfileInfo | None,
                   catalog: dict[str, FilterSpec]) -> set[str]:
    """Paths the engine may put into a candidate under ``prefs.plugin_policy``.

    * tune_only: only the filters enabled in the current profile (nothing is added);
    * installed / allow_new: detected plugins (``allow_new`` adds *suggestions*
      elsewhere, never unmeasurable candidates).
    Filters with ``auto_recommend`` False (prediction / input-assist risk, not
    smoothing filters) are never picked.
    """
    if prefs.plugin_policy is PluginPolicy.TUNE_ONLY:
        paths = {f.path for f in (current.filters if current else ()) if f.enabled}
    else:
        paths = set(available_filters)
    return {p for p in paths if p in catalog and catalog[p].auto_recommend}


def _build_filtered(cid: str, ctx: _Context, available: set[str],
                    catalog: dict[str, FilterSpec]) -> tuple[tuple[FilterConfig, ...], list[Reason]]:
    level = LEVEL[cid] * ctx.strength
    chain: dict[str, dict[str, Any]] = {}
    reasons: list[Reason] = []

    if ctx.want_resampler and RESAMPLER in available:
        # Prediction 0.5 is the README default; 0.75 trims latency for low-latency.
        chain[RESAMPLER] = {"frameShift": 0.75 if cid == "low-latency" else 0.5, "latency": 0.0}
        reasons.append(Reason("engine.reason.resampler_added",
                              {"refresh": round(ctx.refresh_hz), "rate": round(ctx.effective_rate_hz)}))
    noise_path = None
    if ctx.noisy:
        noise_path = next((p for p in NOISE_PREFERENCE if p in available), None)
        if noise_path:
            chain[noise_path] = _noise_settings(noise_path, level, ctx)
            reasons.append(Reason("engine.reason.noise_filter_added",
                                  {"filter": catalog[noise_path].display_name}))
        else:
            reasons.append(Reason("engine.reason.noise_filter_missing"))

    # Low latency + a noise filter is already enough: skip the smoother.
    if not (cid == "low-latency" and noise_path):
        preference = SMOOTHER_PREFERENCE[cid]
        if ctx.noisy and not noise_path:   # the smoother must also fight chatter
            preference = (ANTICHATTER,) + tuple(p for p in preference if p != ANTICHATTER)
        smoother = next((p for p in preference if p in available), None)
        deadzone = 2.0 * (ctx.jitter_mm or 0.05) if ctx.noisy and not noise_path else 0.0
        if smoother is None:
            reasons.append(Reason("engine.reason.no_smoother_available"))
        elif smoother in ASYNC_SMOOTHERS and RESAMPLER in chain:
            # Two timer-driven elements in a row would resample twice: use the
            # resampler's own Hawku-like smoothing instead.
            latency = max(BASE_SMOOTHING_MS * level, MIN_ASYNC_SMOOTHING_MS)
            chain[RESAMPLER]["latency"] = latency
            reasons.append(Reason("engine.reason.resampler_smoothing",
                                  {"latency": round(latency, 1)}))
        else:
            chain[smoother] = _smoother_settings(smoother, level, ctx, deadzone)
            reasons.append(Reason("engine.reason.smoother_added",
                                  {"filter": catalog[smoother].display_name}))

    filters = tuple(_make_filter(catalog[p], chain[p]) for p in RESEARCH_ORDER if p in chain)
    if not filters:
        reasons.append(Reason("engine.reason.nothing_to_add"))
    else:
        kept = [catalog[f.path].display_name for f in filters if catalog[f.path].param("Frequency")]
        if kept:   # guides often say to set it to the report rate: explain why we do not
            reasons.append(Reason("engine.reason.frequency_default", {"filters": ", ".join(kept)}))
        reasons += prediction_notes(filters)
    return filters, reasons


def prediction_notes(filters: Sequence[FilterConfig]) -> list[Reason]:
    """Factual note when a chain uses a prediction setting whose osu! policy status is unknown."""
    predicting = [f for f in filters if f.enabled and f.path in (RESAMPLER, SATURN_MULTI)
                  and _num(f.settings, "frameShift", 0.0) > 0]
    return [Reason("engine.reason.prediction_policy_unknown")] if predicting else []


def _recommended_area(tablet: TabletSpec, display: DisplaySpec, prefs: UserPreferences,
                      current: ProfileInfo | None) -> tuple[Rect, tuple[Reason, ...]]:
    """``recommend_area`` keeping the rotation of the current profile.

    A rotated area (e.g. 180 deg for left-handed play) must stay rotated. OTD
    rotates the rect about its center, so at 90/270 deg the area's width lies
    along the tablet's height: fit it against the swapped tablet size.
    """
    rotation = current.tablet_area.rotation % 360.0 if current is not None else 0.0
    if not rotation:
        return recommend_area(tablet, display, prefs)
    quarter_turn = round(rotation / 90.0) % 2 == 1
    fit = replace(tablet, width_mm=tablet.height_mm, height_mm=tablet.width_mm) \
        if quarter_turn else tablet
    area, reasons = recommend_area(fit, display, prefs)
    area = Rect(area.width, area.height, tablet.width_mm / 2, tablet.height_mm / 2, rotation)
    return area, reasons + (Reason("engine.reason.area_rotation_kept",
                                   {"rotation": round(rotation, 1)}),)


def generate_candidates(tablet: TabletSpec, display: DisplaySpec, prefs: UserPreferences,
                        available_filters: set[str], baseline: list[MetricsReport] | None,
                        current: ProfileInfo | None, community: Sequence[Any] | None = None
                        ) -> list[Candidate]:
    """``no-filter``, ``low-latency``, ``balanced``, ``smooth`` (+ community, + ``keep-current``).

    All generated candidates share the recommended area so that measurements
    compare filters only. Unknown paths in ``available_filters`` are ignored.
    ``prefs.plugin_policy`` decides which filters may appear (see
    :func:`policy_filters`); with ``tune_only`` the generated options keep the
    current chain and change one parameter (``tune-less`` / ``tune-more``).
    ``community`` (``knowledge.presets.CommunityPreset`` items, already chosen
    by the caller, e.g. with ``engine.community.select_presets``) become
    ``community:<id>`` candidates; presets whose plugins the policy does not
    allow are skipped.
    """
    from . import community as comm   # community imports this module

    catalog = load_catalog()
    available = policy_filters(available_filters, prefs, current, catalog)
    area, area_reasons = _recommended_area(tablet, display, prefs, current)
    ctx = _context(tablet, display, prefs, area, baseline)
    base = ProfileChanges(
        tablet_area=area,
        display_area=Rect(display.width_px, display.height_px, display.offset_x, display.offset_y),
        lock_aspect_ratio=True, filters=())
    tune_only = prefs.plugin_policy is PluginPolicy.TUNE_ONLY

    candidates: list[Candidate] = []
    if not tune_only:
        candidates.append(Candidate(
            id="no-filter", label_key="engine.candidate.no-filter.label",
            summary_key="engine.candidate.no-filter.summary", changes=base,
            reasons=(Reason("engine.reason.candidate_no-filter"),) + area_reasons,
            est_added_latency_ms=0.0, tags=("baseline",)))
        for cid in FILTERED_IDS:
            filters, reasons = _build_filtered(cid, ctx, available, catalog)
            latency, _ = chain_latency(filters, ctx.rate_hz, ctx.mm_per_px)
            reasons.append(Reason("engine.reason.latency_estimate", {"ms": latency}))
            candidates.append(Candidate(
                id=cid, label_key=f"engine.candidate.{cid}.label",
                summary_key=f"engine.candidate.{cid}.summary",
                changes=replace(base, filters=filters),
                reasons=(Reason(f"engine.reason.candidate_{cid}"),) + ctx.reasons
                + tuple(reasons) + area_reasons,
                est_added_latency_ms=latency, tags=(cid,)))
    else:
        candidates += _tune_candidates(current, ctx, base, catalog, area_reasons)
    for preset in community or ():
        if comm.unusable_reason(preset, prefs.plugin_policy, set(available_filters), current, tablet,
                                catalog) is None:
            candidates.append(comm.community_candidate(preset, base, catalog, ctx.rate_hz, ctx.mm_per_px,
                                                       prefs.plugin_policy, current))
    if current is not None:
        candidates.append(_keep_current(current, ctx))
    if not candidates:   # tune_only without a profile: nothing of the user's to tune
        candidates.append(Candidate(
            id="no-filter", label_key="engine.candidate.no-filter.label",
            summary_key="engine.candidate.no-filter.summary", changes=base,
            reasons=(Reason("engine.reason.tune_only_no_profile"),) + area_reasons,
            est_added_latency_ms=0.0, tags=("baseline",)))
    return candidates


TUNE_FACTORS = (("less", 0.75), ("more", 1.33))   # one step down / up of the main knob


def _tune_candidates(current: ProfileInfo | None, ctx: _Context, base: ProfileChanges,
                     catalog: dict[str, FilterSpec], area_reasons: tuple[Reason, ...]) -> list[Candidate]:
    """tune_only: the current chain (same filters, order and on/off) with ONE knob moved.

    The knob is the most influential one present (``KNOBS`` order); a second
    parameter only changes when a constraint forces it (Radial Follow inner
    radius <= outer radius), and every change is listed as old -> new.
    """
    from .community import change_reason

    if current is None:
        return []
    knob = next(((i, catalog[path], catalog[path].param(name))
                 for path, name in KNOBS for i, fc in enumerate(current.filters)
                 if fc.path == path and fc.enabled and path in catalog
                 and catalog[path].param(name) is not None), None)
    if knob is None:
        return []
    index, spec, param = knob
    fc = current.filters[index]
    raw = fc.settings.get(param.name, param.default)
    # The user's real value (bounds only, no step rounding): it is what the "old" in the reason shows.
    old = float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw) \
        else float(param.default)
    old = min(max(old, float(param.min)), float(param.max))
    out = []
    for direction, factor in TUNE_FACTORS:
        new = float(param.clamp(old * factor if old > 0 else
                                (param.step or 0.0) * (1 if direction == "more" else 0)))
        if abs(new - old) <= (param.step or 1e-6) / 2:
            continue
        settings = dict(fc.settings)
        settings[param.name] = param.clamp(new)
        before = dict(settings)
        enforce_constraints(spec.path, settings)
        reasons = [Reason("engine.reason.tune_only_kept"), change_reason(spec, param, old, settings[param.name])]
        reasons += [change_reason(spec, spec.param(k), fc.settings.get(k), settings[k])
                    for k in settings if before.get(k) != settings[k] and spec.param(k) is not None]
        filters = list(current.filters)
        filters[index] = replace(fc, settings=settings)
        latency, _ = chain_latency(filters, ctx.rate_hz, ctx.mm_per_px)
        reasons.append(Reason("engine.reason.latency_estimate", {"ms": latency}))
        cid = f"tune-{direction}"
        out.append(Candidate(
            id=cid, label_key=f"engine.candidate.{cid}.label", summary_key=f"engine.candidate.{cid}.summary",
            changes=replace(base, filters=tuple(filters)),
            reasons=(Reason(f"engine.reason.candidate_{cid}"),) + ctx.reasons + tuple(reasons) + area_reasons,
            est_added_latency_ms=latency, tags=(cid, "tune-only")))
    return out


def _keep_current(current: ProfileInfo, ctx: _Context) -> Candidate:
    """The user's present profile, untouched (values are not clamped: it is a reference)."""
    latency, unknown = chain_latency(current.filters, ctx.rate_hz, ctx.mm_per_px)
    reasons = [Reason("engine.reason.candidate_keep-current")]
    reasons += [Reason("engine.reason.latency_unknown_filter", {"filter": p}) for p in unknown]
    reasons.append(Reason("engine.reason.latency_estimate", {"ms": latency}))
    changes = ProfileChanges(tablet_area=current.tablet_area, display_area=current.display,
                             lock_aspect_ratio=current.lock_aspect_ratio, filters=current.filters)
    return Candidate(id="keep-current", label_key="engine.candidate.keep-current.label",
                     summary_key="engine.candidate.keep-current.summary", changes=changes,
                     reasons=tuple(reasons), est_added_latency_ms=latency, tags=("current",))


# --------------------------------------------------------------------------
# Scoring and ranking
# --------------------------------------------------------------------------
# Each metric becomes a "badness" b = v / (v + ref) in [0, 1): 0 is perfect and
# ``ref`` is the value judged half-bad. References are heuristics.
METRIC_REF = {
    "jitter": 0.05,     # mm RMS while holding still
    "path_mean": 1.0,   # mm mean distance to the ideal path
    "path_p95": 2.5,    # mm 95th percentile distance
    "jerk": 1.0,        # pen jerk / target jerk ratio (1 = as smooth as the target)
    "lag": 20.0,        # ms behind the target (absolute value: prediction may lead)
    "aim": 2.0,         # mm mean miss distance
}
# Metric weights per priority (each row sums to 1). Missing metrics are
# dropped and the remaining weights renormalized.
WEIGHTS: dict[Priority, dict[str, float]] = {
    Priority.LATENCY: {"jitter": 0.10, "path": 0.20, "jerk": 0.10, "lag": 0.40, "aim": 0.20},
    Priority.BALANCED: {"jitter": 0.20, "path": 0.25, "jerk": 0.15, "lag": 0.20, "aim": 0.20},
    Priority.SMOOTHNESS: {"jitter": 0.30, "path": 0.20, "jerk": 0.25, "lag": 0.05, "aim": 0.20},
}
NOTICED_BONUS_WEIGHT = 0.05   # added to jitter/lag when the user notices them
NEUTRAL_SCORE = 50.0          # objective score when nothing was measured
RATING_POINTS = 5.0           # per star away from 3 -> bounded to +/-10 points


def _badness(value: float | None, ref: float) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    value = abs(value)
    return value / (value + ref)


def _metric_badness(reports: Sequence[MetricsReport]) -> dict[str, float]:
    """Mean badness per metric over every report that has it."""
    pools: dict[str, list[float]] = {}
    for r in reports:
        for key, b in (("jitter", _badness(r.jitter_rms_mm, METRIC_REF["jitter"])),
                       ("path", _badness(r.path_error_mean_mm, METRIC_REF["path_mean"])),
                       ("path", _badness(r.path_error_p95_mm, METRIC_REF["path_p95"])),
                       ("jerk", _badness(r.smoothness_jerk, METRIC_REF["jerk"])),
                       ("lag", _badness(r.lag_ms_estimate, METRIC_REF["lag"])),
                       ("aim", _badness(r.aim_error_mean_mm, METRIC_REF["aim"]))):
            if b is not None:
                pools.setdefault(key, []).append(b)
    return {key: sum(v) / len(v) for key, v in pools.items()}


def score_reports(reports: Sequence[MetricsReport], prefs: UserPreferences,
                  user_rating: int | None) -> float:
    """Deterministic score, higher is better: 0..100 objective +/- 10 from the rating."""
    weights = dict(WEIGHTS[prefs.priority])
    if prefs.notices_jitter:
        weights["jitter"] += NOTICED_BONUS_WEIGHT
    if prefs.notices_input_lag:
        weights["lag"] += NOTICED_BONUS_WEIGHT
    badness = _metric_badness(reports)
    total_weight = sum(weights[k] for k in badness)
    if total_weight > 0:
        objective = 100.0 * (1.0 - sum(weights[k] * b for k, b in badness.items()) / total_weight)
    else:
        objective = NEUTRAL_SCORE
    adjustment = 0.0
    if user_rating is not None:
        adjustment = (min(max(int(user_rating), 1), 5) - 3) * RATING_POINTS
    return round(objective + adjustment, 6)


def rank(results: list[CandidateResult], prefs: UserPreferences,
         required_kinds: Sequence[str] | None = None) -> list[CandidateResult]:
    """Fill ``.score`` (and ``.objective``, ``.complete``) on every result; best first.

    Weights are renormalized over the metrics that exist, so a result that
    lacks a test kind is scored on another basis. With ``required_kinds`` such
    a result is flagged ``complete=False`` and always ranked after every
    complete one. Ties keep input order.
    """
    for result in results:
        result.score = score_reports(result.reports, prefs, result.user_rating)
        result.objective = score_reports(result.reports, prefs, None)
        done = {r.test_kind for r in result.reports}
        result.complete = required_kinds is None or all(kind in done for kind in required_kinds)
    return sorted(results, key=lambda r: (not r.complete, -r.score))


# --------------------------------------------------------------------------
# Refinement (one hill-climbing step per call)
# --------------------------------------------------------------------------
MAX_REFINE_STEPS = 4
RELATIVE_STEP = 0.25          # step = 25% of the current value ...
MIN_SPAN_STEP = 0.02          # ... but at least 2% of the catalog range
# The knob tuned per filter, most influential first. Raising any of them
# means "more smoothing".
KNOBS = ((ANTICHATTER, "Latency"), (HAWKU, "Latency"), (RADIAL_TABLET, "OuterRadius"),
         (RADIAL_SCREEN, "OuterRadius"), (RESAMPLER, "latency"), (SATURN_MULTI, "smoothDist"),
         (CHATTER_SMOOTH, CHATTER_STRENGTH), (CHATTER_RAW, CHATTER_STRENGTH),
         (NOISE_REDUCTION, "DistanceThreshold"))
# Refined ids: "<root>|<param>:<root value>><value>", e.g. "balanced|Latency:10>12.5".
_REFINED_ID = re.compile(r"^(?P<root>[^|]+)\|(?P<param>[^:]+):(?P<base>[^>]+)>(?P<value>.+)$")


def _fmt(value: float) -> str:
    return format(value, ".6g")


def _parse_refined(cid: str) -> tuple[str, str, float, float] | None:
    match = _REFINED_ID.match(cid)
    if not match:
        return None
    try:
        return match["root"], match["param"], float(match["base"]), float(match["value"])
    except ValueError:
        return None


def _pick_knob(best: Candidate, available: set[str],
               catalog: dict[str, FilterSpec]) -> tuple[int, FilterSpec, FilterParam] | None:
    filters = best.changes.filters or ()
    for path, name in KNOBS:
        if path not in available or path not in catalog:
            continue
        for index, fc in enumerate(filters):
            param = catalog[path].param(name)
            if fc.path == path and fc.enabled and param is not None:
                return index, catalog[path], param
    return None


def refine(best: Candidate, results: list[CandidateResult], prefs: UserPreferences,
           available_filters: set[str]) -> Candidate | None:
    """Next small tuning step of ``best``'s most influential knob, or None.

    Hill climbing over the family of ``best`` (its root candidate and earlier
    refinements found in ``results``): move away from tested values that
    scored worse, halve the step once both sides were tried, and stop when no
    untested value is left on the catalog grid or after MAX_REFINE_STEPS.
    """
    catalog = load_catalog()
    parsed = _parse_refined(best.id)
    root = parsed[0] if parsed else best.id
    family = [r.candidate_id for r in results
              if r.candidate_id == root or r.candidate_id.startswith(root + "|")]
    if sum(1 for cid in family if cid != root) >= MAX_REFINE_STEPS:
        return None
    knob = _pick_knob(best, set(available_filters), catalog)
    if knob is None:
        return None
    index, spec, param = knob
    fc = best.changes.filters[index]
    current = float(param.clamp(fc.settings.get(param.name, param.default)))

    tested = {current}
    if parsed and parsed[1] == param.name:
        tested.add(parsed[2])
    for cid in family:
        other = _parse_refined(cid)
        if other and other[1] == param.name:
            tested.update((other[2], other[3]))
    tol = (param.step or 1e-6) / 2
    lower = any(t < current - tol for t in tested)
    higher = any(t > current + tol for t in tested)
    preferred = 1.0 if (prefs.priority is Priority.SMOOTHNESS or prefs.notices_jitter) \
        and not (prefs.priority is Priority.LATENCY or prefs.notices_input_lag) else -1.0
    if lower and higher:
        factors = (0.5 * preferred, -0.5 * preferred)
    elif lower or higher:
        away = 1.0 if lower else -1.0     # the worse neighbour lies on the other side
        factors = (away, 0.5 * away)
    else:
        factors = (preferred, -preferred)
    step = max(abs(current) * RELATIVE_STEP, param.step or 0.0,
               (param.max - param.min) * MIN_SPAN_STEP)

    for factor in factors:
        new = float(param.clamp(current + factor * step))
        if any(abs(new - t) <= tol for t in tested):
            continue
        settings = dict(fc.settings)
        settings[param.name] = param.clamp(new)
        enforce_constraints(spec.path, settings)
        filters = list(best.changes.filters)
        filters[index] = replace(fc, settings=settings)
        return _refined_candidate(best, root, parsed[2] if parsed else current,
                                  spec, param, current, new, index, tuple(filters))
    return None


def _refined_candidate(best: Candidate, root: str, base: float, spec: FilterSpec,
                       param: FilterParam, old: float, new: float, index: int,
                       filters: tuple[FilterConfig, ...]) -> Candidate:
    changes = replace(best.changes, filters=filters)
    mm_per_px = DEFAULT_MM_PER_PX
    if changes.tablet_area and changes.display_area and changes.display_area.width > 0:
        mm_per_px = changes.tablet_area.width / changes.display_area.width
    # The report rate is not known here (Frequency is not the report rate for
    # most filters), but no knob changes a rate-dependent term, so the new
    # estimate is best's estimate plus the change of the tuned filter alone.
    before = _filter_latency_ms(best.changes.filters[index], DEFAULT_REPORT_RATE_HZ, mm_per_px)
    after = _filter_latency_ms(filters[index], DEFAULT_REPORT_RATE_HZ, mm_per_px)
    latency = round(max(best.est_added_latency_ms + (after or 0.0) - (before or 0.0), 0.0), 1)
    direction = "more" if new > old else "less"
    reasons = (
        Reason(f"engine.reason.refine_{direction}_smoothing",
               {"filter": spec.display_name, "param": param.name,
                "old": round(old, 3), "new": round(new, 3), "unit": param.unit}),
        Reason("engine.reason.latency_estimate", {"ms": latency}),
    )
    tags = best.tags + (() if "refined" in best.tags else ("refined",))
    return Candidate(id=f"{root}|{param.name}:{_fmt(base)}>{_fmt(new)}",
                     label_key=best.label_key, summary_key="engine.candidate.refined.summary",
                     changes=changes, reasons=reasons, est_added_latency_ms=latency, tags=tags)
