"""Shared data contracts between modules.

This file is the single source of truth for the types that cross module
boundaries. It is owned by the integrator: do not edit it from a module
workstream. If a contract needs to change, report it instead.

Conventions
-----------
* Tablet-space lengths are millimetres, display-space lengths are pixels.
* Like OpenTabletDriver (OTD), every ``Rect`` is described by its *center*
  (``x``, ``y``) plus ``width``/``height`` and a rotation in degrees.
* User-visible text never lives in these types: they carry i18n keys and
  parameters (see ``Reason``) and the GUI translates them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# --------------------------------------------------------------------------
# Enumerations (values are stable strings, safe to store in JSON)
# --------------------------------------------------------------------------
class PlayStyle(str, Enum):
    FINGER = "finger"
    WRIST = "wrist"
    ARM = "arm"
    MIXED = "mixed"


class MapPreference(str, Enum):
    JUMPS = "jumps"
    STREAMS = "streams"
    BALANCED = "balanced"


class Priority(str, Enum):
    LATENCY = "latency"          # least possible input delay, accept some noise
    BALANCED = "balanced"
    SMOOTHNESS = "smoothness"    # cleanest cursor, accept some added delay


class SkillLevel(str, Enum):
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


class PluginPolicy(str, Enum):
    """Which OTD plugins the assistant may use (it never installs plugins itself)."""
    TUNE_ONLY = "tune_only"      # keep the current filters, only adjust their parameters
    INSTALLED = "installed"      # choose among plugins found in OTD's Plugins folder
    ALLOW_NEW = "allow_new"      # also suggest plugins that are not installed (with source + hint)


class TabletSource(str, Enum):
    OTD_CONFIG = "otd-config"    # read from the OTD installation
    BUILTIN = "builtin"          # shipped with this project
    CUSTOM = "custom"            # typed in by the user


# --------------------------------------------------------------------------
# Hardware and user description
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Rect:
    """Center-based rectangle, OTD style."""
    width: float
    height: float
    x: float
    y: float
    rotation: float = 0.0


@dataclass(frozen=True)
class TabletSpec:
    name: str
    width_mm: float                       # full active area
    height_mm: float
    max_report_rate_hz: float = 133.0     # polling rate the hardware can reach
    max_pressure: int = 0
    resolution_lpmm: float | None = None  # lines per mm, if known
    source: TabletSource = TabletSource.CUSTOM


@dataclass(frozen=True)
class DisplaySpec:
    width_px: int
    height_px: int
    offset_x: float = 0.0                 # center of the display in desktop px
    offset_y: float = 0.0
    refresh_hz: float = 60.0
    diagonal_inch: float | None = None


@dataclass
class UserPreferences:
    play_style: PlayStyle = PlayStyle.WRIST
    map_preference: MapPreference = MapPreference.BALANCED
    priority: Priority = Priority.BALANCED
    skill_level: SkillLevel = SkillLevel.INTERMEDIATE
    notices_jitter: bool = False          # visible cursor shake when holding still
    notices_input_lag: bool = False
    preferred_area_width_mm: float | None = None  # keep my current size, if set
    language: str = "es"                  # "es" | "en"
    plugin_policy: PluginPolicy = PluginPolicy.INSTALLED


# --------------------------------------------------------------------------
# OpenTabletDriver settings, as understood by this project
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class FilterConfig:
    path: str                             # OTD "Path", e.g. "TabletDriverFilters.Hawku.Smoothing"
    enabled: bool = True
    settings: dict[str, Any] = field(default_factory=dict)   # property -> value


@dataclass(frozen=True)
class PluginInfo:
    name: str
    version: str | None
    directory: str
    provides: tuple[str, ...] = ()        # OTD paths (filters/tools) this plugin offers, if known


@dataclass(frozen=True)
class ProfileInfo:
    """One tablet profile found inside OTD's settings.json."""
    tablet_name: str
    output_mode_path: str
    display: Rect
    tablet_area: Rect
    lock_aspect_ratio: bool
    filters: tuple[FilterConfig, ...]


@dataclass(frozen=True)
class ProfileChanges:
    """What the assistant wants to change. ``None`` means "leave as it is"."""
    tablet_area: Rect | None = None
    display_area: Rect | None = None
    lock_aspect_ratio: bool | None = None
    # Full replacement of the filter list. () means "remove every filter".
    filters: tuple[FilterConfig, ...] | None = None


@dataclass(frozen=True)
class ApplyResult:
    ok: bool
    method: str                           # "console" | "rpc" | "preset_file" | "none"
    message_key: str                      # i18n key
    detail: str = ""                      # raw technical detail, shown in "details"


# --------------------------------------------------------------------------
# Recommendation
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Reason:
    """A translatable explanation line: ``t(key, **params)``."""
    key: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Candidate:
    id: str                               # stable, e.g. "low-latency"
    label_key: str
    summary_key: str
    changes: ProfileChanges
    reasons: tuple[Reason, ...] = ()
    est_added_latency_ms: float = 0.0     # rough estimate, documented per filter
    tags: tuple[str, ...] = ()


# --------------------------------------------------------------------------
# Measurements
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class PenSample:
    t: float                              # seconds, monotonic clock
    x: float                              # desktop pixels
    y: float
    pressure: float | None = None


@dataclass(frozen=True)
class MetricsReport:
    test_kind: str                        # "rate" | "stillness" | "tracking" | "aim"
    sample_count: int
    duration_s: float
    effective_rate_hz: float | None = None
    jitter_rms_mm: float | None = None        # stillness: RMS wobble while holding still
    path_error_mean_mm: float | None = None   # tracking: mean distance to ideal path
    path_error_p95_mm: float | None = None
    smoothness_jerk: float | None = None      # tracking: lower = smoother
    lag_ms_estimate: float | None = None      # tracking: time shift vs. the target
    aim_error_mean_mm: float | None = None    # aim: mean miss distance to circle center
    aim_time_ms_mean: float | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class CandidateResult:
    candidate_id: str
    # One aggregated report per test kind (median of ``runs``, see engine.judge);
    # readers that only look at ``reports`` keep working.
    reports: tuple[MetricsReport, ...] = ()
    user_rating: int | None = None        # subjective feel, 1 (bad) .. 5 (great)
    score: float | None = None            # filled by engine.recommend.rank (objective +/- rating)
    runs: tuple[MetricsReport, ...] = ()  # every individual run, oldest first
    objective: float | None = None        # rank(): score without the rating; the grade uses it
    complete: bool = True                 # rank(): every required test kind was measured
