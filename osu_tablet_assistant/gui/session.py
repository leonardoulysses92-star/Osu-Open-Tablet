"""Non-visual state machine behind the wizard.

``Session`` holds everything the user decided and measured and implements the
transitions between wizard steps. Every collaborator (OTD access, the
recommendation engine, the pen tests) is reached through :class:`Services`, so
tests inject fakes and the GUI never imports those modules directly. Anything
that changes state outside this process (applying settings, writing presets)
needs ``confirmed=True`` and is preceded by exactly one settings backup.
"""
from __future__ import annotations

import importlib
import math
import re
import sys
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from ..appconfig import AppConfig, preset_file_name
from ..engine import judge
from ..knowledge import tablets as tablet_db
from ..models import (
    ApplyResult,
    Candidate,
    CandidateResult,
    DisplaySpec,
    MetricsReport,
    PluginInfo,
    PluginPolicy,
    ProfileInfo,
    Rect,
    TabletSpec,
    UserPreferences,
)
from .i18n import t

BASELINE_KINDS = ("rate", "stillness")
# Stillness is repeated per candidate: the engine scores jitter, and it is the
# metric smoothing filters improve most (without it no-filter always wins on lag).
CANDIDATE_KINDS = ("stillness", "tracking", "aim")
MAX_REFINE_ROUNDS = 5

_SETTINGS_IO = "osu_tablet_assistant.otd.settings_io"
_PLUGINS = "osu_tablet_assistant.otd.plugins"
_RECOMMEND = "osu_tablet_assistant.engine.recommend"
_COMMUNITY = "osu_tablet_assistant.engine.community"
_UPDATER = "osu_tablet_assistant.knowledge.updater"
COMMUNITY_CANDIDATES = 3     # closest community presets measured per run (plus the chosen start)


class Step(str, Enum):
    """Wizard steps, in order."""
    WELCOME = "welcome"
    DETECT = "detect"
    TABLET = "tablet"
    DISPLAY = "display"
    QUESTIONS = "questions"
    BASELINE = "baseline"
    TESTING = "testing"
    RESULT = "result"


STEP_ORDER = tuple(Step)


class SessionError(Exception):
    """A user-presentable failure: ``key``/``params`` form an i18n message, ``detail`` is technical."""

    def __init__(self, key: str, detail: str = "", params: dict[str, Any] | None = None) -> None:
        super().__init__(key, detail)
        self.key = key
        self.detail = detail
        self.params = params or {}


class ConfirmationRequired(SessionError):
    """A state-changing call was made without the user's explicit confirmation."""

    def __init__(self) -> None:
        super().__init__("core.error.not_confirmed")


def _lazy(module: str, attr: str) -> Callable[..., Any]:
    """Callable that imports ``module.attr`` on first use (modules may not exist yet)."""
    def call(*args: Any, **kwargs: Any) -> Any:
        return getattr(importlib.import_module(module), attr)(*args, **kwargs)
    call.__name__ = attr
    return call


def display_from_tk(widget: Any) -> DisplaySpec:
    """Primary-monitor size from any Tk widget (physical px once DPI aware)."""
    width, height = int(widget.winfo_screenwidth()), int(widget.winfo_screenheight())
    return DisplaySpec(width, height, width / 2, height / 2)


def detect_display_tk() -> DisplaySpec | None:
    """Detect the primary monitor with a throw-away hidden Tk root."""
    import tkinter
    try:
        root = tkinter.Tk()
    except tkinter.TclError:
        return None
    try:
        root.withdraw()
        return display_from_tk(root)
    finally:
        root.destroy()


def desktop_origin() -> tuple[float, float]:
    """Top-left corner of the whole virtual desktop, in OS desktop px.

    OTD measures display areas from this corner (a monitor left of or above
    the primary one makes it negative on Windows), while Tk and GetCursorPos
    use coordinates whose origin is the primary monitor. Elsewhere 0, 0 is
    assumed (best effort).
    """
    if sys.platform != "win32":
        return 0.0, 0.0
    try:
        import ctypes
        user32 = ctypes.windll.user32
        return float(user32.GetSystemMetrics(76)), float(user32.GetSystemMetrics(77))  # SM_[XY]VIRTUALSCREEN
    except (AttributeError, OSError):
        return 0.0, 0.0


def _default_controller(install_dir: Path | None, config_dir: Path | None) -> Any:
    from ..otd.controller import OtdController
    return OtdController(install_dir, config_dir)


@dataclass
class Services:
    """Everything the session calls out to; defaults import the real modules lazily."""
    # osu_tablet_assistant.otd.settings_io
    find_config_dir: Callable[[], Path | None] = _lazy(_SETTINGS_IO, "find_config_dir")
    find_install_dir: Callable[[], Path | None] = _lazy(_SETTINGS_IO, "find_install_dir")
    load_settings: Callable[[Path], dict] = _lazy(_SETTINGS_IO, "load_settings")
    list_profiles: Callable[[dict], list[ProfileInfo]] = _lazy(_SETTINGS_IO, "list_profiles")
    apply_changes: Callable[..., dict] = _lazy(_SETTINGS_IO, "apply_changes")
    backup_settings: Callable[[Path], Path] = _lazy(_SETTINGS_IO, "backup_settings")
    save_preset: Callable[[Path, str, dict], Path] = _lazy(_SETTINGS_IO, "save_preset")
    write_settings: Callable[[Path, dict], None] = _lazy(_SETTINGS_IO, "write_settings")
    # osu_tablet_assistant.otd.plugins / controller
    list_installed_plugins: Callable[[Path], list[PluginInfo]] = _lazy(
        _PLUGINS, "list_installed_plugins")
    available_filter_paths: Callable[[Path], set[str]] = _lazy(_PLUGINS, "available_filter_paths")
    make_controller: Callable[[Path | None, Path | None], Any] = _default_controller
    # osu_tablet_assistant.engine.recommend / knowledge.filters
    generate_candidates: Callable[..., list[Candidate]] = _lazy(_RECOMMEND, "generate_candidates")
    rank: Callable[..., list[CandidateResult]] = _lazy(_RECOMMEND, "rank")
    refine: Callable[..., Candidate | None] = _lazy(_RECOMMEND, "refine")
    load_filter_catalog: Callable[[], dict[str, Any]] = _lazy(
        "osu_tablet_assistant.knowledge.filters", "load_catalog")
    # community presets (knowledge.presets / engine.community), OTD locations, app config
    load_community_presets: Callable[[], Any] = _lazy(
        "osu_tablet_assistant.knowledge.presets", "load_presets")
    community: Callable[[], Any] = lambda: importlib.import_module(_COMMUNITY)
    config_locations: Callable[..., list[Any]] = _lazy(_SETTINGS_IO, "config_locations")
    version_notice: Callable[[dict], Any] = _lazy(_SETTINGS_IO, "version_notice")
    save_app_config: Callable[..., Path] = _lazy("osu_tablet_assistant.appconfig", "save_config")
    # knowledge.updater (community data freshness; network only when the user asks)
    update_community_data: Callable[..., Any] = _lazy(_UPDATER, "update")
    community_data_status: Callable[..., dict[str, Any]] = _lazy(_UPDATER, "status")
    activate_data_cache: Callable[[Path], Any] = _lazy(_UPDATER, "activate_cache")
    # osu_tablet_assistant.gui.live_tests
    run_test: Callable[..., None] = _lazy("osu_tablet_assistant.gui.live_tests", "run_test")
    # this package
    list_known_tablets: Callable[[Path | None], list[TabletSpec]] = tablet_db.list_known_tablets
    detect_display: Callable[[], DisplaySpec | None] = detect_display_tk
    desktop_origin: Callable[[], tuple[float, float]] = desktop_origin


@dataclass(frozen=True)
class TabletChoice:
    """One row of the tablet picker; ``tablet`` is None for an OTD profile we do not know."""
    label: str
    tablet: TabletSpec | None
    profile: ProfileInfo | None


@dataclass(frozen=True)
class StartingPreset:
    """One row of the "starting preset" step (``id`` is the candidate id, texts are translated)."""
    id: str
    label: str
    summary: str
    preset: Any = None          # the knowledge.presets.CommunityPreset


@dataclass
class Session:
    """All wizard state plus its transitions."""
    services: Services = field(default_factory=Services)
    dry_run: bool = False
    step: Step = Step.WELCOME
    history: list[Step] = field(default_factory=list)
    accepted_disclaimer: bool = False
    log: list[str] = field(default_factory=list)
    # OpenTabletDriver
    config_dir: Path | None = None
    install_dir: Path | None = None
    settings: dict | None = None
    profiles: list[ProfileInfo] = field(default_factory=list)
    plugins: list[PluginInfo] = field(default_factory=list)
    filter_paths: set[str] = field(default_factory=set)
    known_tablets: list[TabletSpec] = field(default_factory=list)
    # choices
    tablet: TabletSpec | None = None
    profile: ProfileInfo | None = None
    display: DisplaySpec | None = None
    prefs: UserPreferences = field(default_factory=UserPreferences)
    # measurements and candidates
    baseline: list[MetricsReport] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    results: dict[str, CandidateResult] = field(default_factory=dict)
    chosen_id: str | None = None
    refine_rounds: int = 0
    converged: bool = False
    # community presets ("starting preset" step) and the program's own settings
    starting_presets: list[Any] = field(default_factory=list)
    starting_preset_id: str | None = None
    app_config: AppConfig = field(default_factory=AppConfig)
    app_config_path: Path | None = None      # None = do not persist (tests, --dry-run unaffected)
    locations: list[Any] = field(default_factory=list)   # settings_io.ConfigLocation
    otd_notice: tuple[str, dict[str, Any]] | None = None
    data_cache_dir: Path | None = None       # cached community data; None = updates disabled
    data_bundle: Any = None                  # the active downloaded bundle, if any
    # side effects already performed
    backup_path: Path | None = None
    live_candidate_id: str | None = None
    preset_path: Path | None = None
    applied: ApplyResult | None = None
    _controller: Any = field(default=None, repr=False)
    _fingerprint: str = field(default="", repr=False)
    _wrote_settings: bool = field(default=False, repr=False)   # settings.json was replaced

    # ------------------------------------------------------------------ helpers
    def _call(self, key: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run a service; any failure becomes a SessionError carrying ``key``."""
        try:
            return fn(*args, **kwargs)
        except SessionError:
            raise
        except Exception as exc:  # services are third-party to this module
            detail = f"{getattr(fn, '__name__', 'service')}: {type(exc).__name__}: {exc}"
            self.log.append(detail)
            own_key, own_params = getattr(exc, "key", None), getattr(exc, "params", None)
            if isinstance(own_key, str) and isinstance(own_params, dict):  # translatable service error
                raise SessionError(own_key, detail, own_params) from exc
            raise SessionError(key, detail) from exc

    @staticmethod
    def _require(confirmed: bool) -> None:
        if not confirmed:
            raise ConfirmationRequired()

    @property
    def can_measure(self) -> bool:
        """False in dry-run: nothing may be applied, so nothing can be measured per candidate."""
        return not self.dry_run

    @property
    def target_tablet_name(self) -> str:
        """Name of the OTD profile that receives the changes."""
        if self.profile is None:
            raise SessionError("core.error.incomplete")
        return self.profile.tablet_name

    # --------------------------------------------------------------- navigation
    def can_go_back(self) -> bool:
        """True unless the user is on the first step."""
        return bool(self.history)

    def can_advance(self) -> bool:
        """True when the current step has everything the next one needs."""
        checks = {
            Step.WELCOME: self.accepted_disclaimer,
            Step.DETECT: self.settings is not None,
            Step.TABLET: self.tablet is not None and self.profile is not None,
            Step.DISPLAY: self.display is not None,
            Step.QUESTIONS: True,
            Step.BASELINE: True,
            Step.TESTING: self.chosen_id is not None if self.dry_run
            else bool(self.results_with_reports()),
            Step.RESULT: False,
        }
        return checks[self.step]

    def advance(self) -> Step:
        """Go to the next step, preparing it; raises SessionError when not allowed."""
        if not self.can_advance():
            raise SessionError("core.error.cannot_advance")
        target = STEP_ORDER[STEP_ORDER.index(self.step) + 1]
        if target is Step.TESTING:
            self.ensure_candidates()
        self.history.append(self.step)
        self.step = target
        return target

    def back(self) -> Step:
        """Return to the previous step."""
        if self.history:
            self.step = self.history.pop()
        return self.step

    # ------------------------------------------------------------------ detect
    def detect(self, config_dir: Path | None = None, install_dir: Path | None = None) -> None:
        """Locate OTD (or use the given folders) and load its profiles and plugins."""
        self.settings = None
        self.profiles, self.plugins, self.filter_paths = [], [], set()
        self.install_dir = install_dir or self._call(
            "core.error.detect", self.services.find_install_dir)
        self.known_tablets = self._call(
            "core.error.tablets", self.services.list_known_tablets, self.install_dir)
        self.config_dir = config_dir or self._call(
            "core.error.detect", self.services.find_config_dir)
        if self.config_dir is None:
            raise SessionError("core.error.no_config_dir")
        settings = self._call("core.error.read_settings", self.services.load_settings, self.config_dir)
        profiles = self._call("core.error.read_settings", self.services.list_profiles, settings)
        self.plugins = self._call(
            "core.error.plugins", self.services.list_installed_plugins, self.config_dir)
        self.filter_paths = set(self._call(
            "core.error.plugins", self.services.available_filter_paths, self.config_dir))
        self.settings, self.profiles = settings, list(profiles)
        if self.tablet is not None:
            self.profile = self._profile_for(self.tablet)
        self.otd_notice = self._optional(self.services.version_notice, settings)
        self.refresh_locations()

    def _optional(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Call a decoration-only service; a failure is logged, never raised."""
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # optional features must never break the wizard
            self.log.append(f"{getattr(fn, '__name__', 'service')}: {type(exc).__name__}: {exc}")
            return None

    def refresh_locations(self) -> list[Any]:
        """Every OTD settings location found (per-user folder and portable installs)."""
        extra = [Path(self.app_config.output_dir)] if self.app_config.output_dir else []
        if self.config_dir is not None:
            extra.append(self.config_dir.parent if self.config_dir.name == "userdata" else self.config_dir)
        if self.install_dir is not None:
            extra.append(self.install_dir)
        self.locations = list(self._optional(self.services.config_locations, extra=extra) or [])
        return self.locations

    # ------------------------------------------------------------------ tablet
    def tablet_choices(self) -> list[TabletChoice]:
        """OTD profiles first, then every known tablet not already covered by a profile."""
        choices: list[TabletChoice] = []
        covered: set[str] = set()
        for profile in self.profiles:
            spec = tablet_db.match_tablet(profile.tablet_name, self.known_tablets)
            choices.append(TabletChoice(profile.tablet_name, spec, profile))
            covered.add(tablet_db.normalize_name(profile.tablet_name))
        choices.extend(
            TabletChoice(spec.name, spec, None)
            for spec in self.known_tablets
            if tablet_db.normalize_name(spec.name) not in covered
        )
        return choices

    def _profile_for(self, tablet: TabletSpec) -> ProfileInfo | None:
        key = tablet_db.normalize_name(tablet.name)
        return next((p for p in self.profiles
                     if tablet_db.normalize_name(p.tablet_name) == key), None)

    def select_choice(self, choice: TabletChoice) -> bool:
        """Pick a row of :meth:`tablet_choices`; False if its size is unknown (needs custom entry)."""
        self.profile = choice.profile
        self.tablet = choice.tablet
        return choice.tablet is not None

    def link_profile(self, profile: ProfileInfo) -> None:
        """Choose which OTD profile belongs to the selected tablet."""
        self.profile = profile

    def set_custom_tablet(self, name: str, width_mm: float, height_mm: float,
                          report_rate_hz: float, max_pressure: int = 0) -> TabletSpec:
        """Use a tablet typed by the user (keeps the selected OTD profile, if any)."""
        try:
            spec = tablet_db.custom_tablet(name, width_mm, height_mm, report_rate_hz, max_pressure)
        except ValueError as exc:
            raise SessionError(f"core.error.tablet_{exc}") from exc
        self.tablet = spec
        if self.profile is None:
            self.profile = self._profile_for(spec)
        return spec

    # ----------------------------------------------------------------- display
    def detect_display(self) -> DisplaySpec | None:
        """Fill the display from the OS; None if it could not be detected."""
        found = self._call("core.error.display", self.services.detect_display)
        if found is not None:
            self.display = found
        return found

    def set_display(self, width_px: int, height_px: int, left: float = 0.0, top: float = 0.0,
                    refresh_hz: float = 60.0) -> DisplaySpec:
        """Manual display; ``left``/``top`` is the monitor's top-left corner in desktop px."""
        if width_px <= 0 or height_px <= 0 or refresh_hz <= 0:
            raise SessionError("core.error.display_values")
        self.display = DisplaySpec(
            int(width_px), int(height_px), left + width_px / 2, top + height_px / 2, float(refresh_hz))
        return self.display

    def otd_display(self) -> DisplaySpec:
        """The selected display with its center in OTD's coordinates (see :func:`desktop_origin`).

        ``self.display`` stays in OS desktop px, which the pen-test window and
        the cursor sampler need; whatever is written into OTD uses this one.
        """
        if self.display is None:
            raise SessionError("core.error.incomplete")
        left, top = self._call("core.error.display", self.services.desktop_origin)
        return replace(self.display, offset_x=self.display.offset_x - left,
                       offset_y=self.display.offset_y - top)

    # ----------------------------------------------------------------- measure
    def current_area(self) -> Rect:
        """The tablet area OTD uses right now (the selected profile's)."""
        if self.profile is None:
            raise SessionError("core.error.incomplete")
        return self.profile.tablet_area

    def mm_per_px(self, area: Rect, display_area: Rect | None = None) -> float:
        """Tablet millimetres covered by one display pixel when ``area`` maps onto ``display_area``.

        ``display_area`` is OTD's output area (px); None or empty means the
        whole selected display. An empty tablet area (profile never set up)
        falls back to the full tablet, which is what OTD uses by default.
        """
        if self.display is None:
            raise SessionError("core.error.incomplete")
        width_mm = area.width if area.width > 0 else (self.tablet.width_mm if self.tablet else 0.0)
        width_px = display_area.width if display_area is not None and display_area.width > 0 \
            else self.display.width_px
        return width_mm / width_px

    def run_measurement(self, parent: Any, kind: str, candidate: Candidate | None = None,
                        on_done: Callable[[MetricsReport], None] | None = None,
                        on_cancel: Callable[[], None] | None = None) -> None:
        """Open a pen test; its report is stored (baseline, or under ``candidate``).

        The scale is the one OTD applies while the test runs: the current
        profile's areas for a baseline, the candidate's (falling back to the
        current ones for fields it leaves unchanged) otherwise.
        """
        current = self.current_area()
        current_display = self.profile.display if self.profile is not None else None
        if candidate is None:
            area, display_area = current, current_display
        else:
            area = candidate.changes.tablet_area or current
            display_area = candidate.changes.display_area or current_display
        mm_per_px = self.mm_per_px(area, display_area)

        def done(report: MetricsReport) -> None:
            if candidate is None:
                self.record_baseline(report)
            else:
                self.record_report(candidate.id, report)
            if on_done is not None:
                on_done(report)

        self._call("core.error.run_test", self.services.run_test,
                   parent, kind, mm_per_px, self.display, done, on_cancel)

    def record_baseline(self, report: MetricsReport) -> None:
        """Store a baseline report, replacing an earlier one of the same kind."""
        self.baseline = [r for r in self.baseline if r.test_kind != report.test_kind] + [report]

    def baseline_report(self, kind: str) -> MetricsReport | None:
        """The stored baseline report of ``kind``, if any."""
        return next((r for r in self.baseline if r.test_kind == kind), None)

    # -------------------------------------------------------------- candidates
    def _current_fingerprint(self) -> str:
        return repr((self.tablet, self.display, self.prefs, tuple(self.baseline), self.profile,
                     self.starting_preset_id))

    def ensure_candidates(self) -> None:
        """Generate candidates unless they already match the current inputs."""
        if not self.candidates or self._fingerprint != self._current_fingerprint():
            self.generate_candidates()

    # ------------------------------------------------------- community presets
    def _selection_area(self) -> Rect:
        """Area used to rank presets: the current one, else the whole tablet."""
        if self.profile is not None and self.profile.tablet_area.width > 0:
            return self.profile.tablet_area
        if self.tablet is not None:
            return Rect(self.tablet.width_mm, self.tablet.height_mm, 0, 0)
        return Rect(0, 0, 0, 0)

    def community_presets(self) -> list[Any]:
        """Every validated community preset (empty on any problem: the data is optional)."""
        bundle = self._optional(self.services.load_community_presets)
        return list(getattr(bundle, "presets", ()) or ())

    def refresh_starting_presets(self) -> list[StartingPreset]:
        """Fill :attr:`starting_presets` with the presets usable under the plugin policy."""
        comm = self._optional(self.services.community)
        presets = self.community_presets()
        if comm is None or not presets or self.tablet is None:
            self.starting_presets = []
        else:
            catalog = self._optional(self.services.load_filter_catalog) or {}
            options = self._optional(comm.starting_options, presets, self.tablet, self._selection_area(),
                                     self.profile, self.prefs.plugin_policy, set(self.filter_paths),
                                     catalog) or []
            self.starting_presets = [StartingPreset(p.candidate_id, preset_label(p, catalog),
                                                    preset_summary(p), p) for p in options]
        if self.starting_preset_id not in {p.id for p in self.starting_presets}:
            self.starting_preset_id = None
        return self.starting_presets

    def _chosen_community(self) -> list[Any]:
        """The community presets to turn into candidates (the chosen start + the closest ones)."""
        comm = self._optional(self.services.community)
        presets = self.community_presets()
        if comm is None or not presets or self.tablet is None:
            return []
        catalog = self._optional(self.services.load_filter_catalog) or {}
        include = [self.starting_preset_id] if self.starting_preset_id else []
        return list(self._optional(
            comm.select_presets, presets, self.tablet, self._selection_area(), self.profile,
            self.prefs.plugin_policy, set(self.filter_paths), catalog, COMMUNITY_CANDIDATES, include) or [])

    def plugin_suggestions(self) -> list[Any]:
        """Not-installed plugins worth a look (only with the ``allow_new`` policy)."""
        if self.prefs.plugin_policy is not PluginPolicy.ALLOW_NEW:
            return []
        comm = self._optional(self.services.community)
        if comm is None:
            return []
        catalog = self._optional(self.services.load_filter_catalog) or {}
        refresh = self.display.refresh_hz if self.display is not None else 60.0
        wanted = self._optional(comm.wanted_paths, self.tablet, refresh, set(self.filter_paths),
                                self.prefs.notices_jitter) or []
        return list(self._optional(comm.plugin_suggestions, self.community_presets(), self.tablet,
                                   self._selection_area(), self.profile, set(self.filter_paths),
                                   catalog, wanted) or [])

    def community_comparison(self, best: Candidate, limit: int = 3) -> list[Any]:
        """The closest community presets and how ``best`` differs from them."""
        comm = self._optional(self.services.community)
        presets = self.community_presets()
        if comm is None or not presets:
            return []
        catalog = self._optional(self.services.load_filter_catalog) or {}
        return list(self._optional(comm.compare_with_best, best, presets, self.tablet, self.profile,
                                   catalog, limit) or [])

    def generate_candidates(self) -> list[Candidate]:
        """(Re)build the candidate list from the current choices and baseline.

        Community presets (the chosen starting preset first, then the closest
        usable ones) are passed to the engine as extra candidates; a chosen
        starting preset is measured first.
        """
        if self.tablet is None or self.display is None:
            raise SessionError("core.error.incomplete")
        community = self._chosen_community()
        args: tuple[Any, ...] = (self.tablet, self.otd_display(), self.prefs, set(self.filter_paths),
                                 list(self.baseline) or None, self.profile)
        found = self._call("core.error.generate", self.services.generate_candidates,
                           *(args + ((community,) if community else ())))
        if not found:
            raise SessionError("core.error.no_candidates")
        found = list(found)
        start = next((c for c in found if c.id == self.starting_preset_id), None)
        if start is not None:
            found.remove(start)
            found.insert(0, start)
        self.candidates = found
        self.results, self.chosen_id = {}, None
        self.refine_rounds, self.converged = 0, False
        self._fingerprint = self._current_fingerprint()
        return self.candidates

    def candidate(self, candidate_id: str) -> Candidate:
        """Look a candidate up by id."""
        for cand in self.candidates:
            if cand.id == candidate_id:
                return cand
        raise SessionError("core.error.unknown_candidate", candidate_id)

    def is_complete(self, candidate_id: str) -> bool:
        """True when the candidate has every per-candidate test report."""
        result = self.results.get(candidate_id)
        done = {r.test_kind for r in result.reports} if result else set()
        return all(kind in done for kind in CANDIDATE_KINDS)

    def next_candidate(self) -> Candidate | None:
        """First candidate that has not been fully tested yet."""
        return next((c for c in self.candidates if not self.is_complete(c.id)), None)

    def record_report(self, candidate_id: str, report: MetricsReport) -> None:
        """Add one run of a test to a candidate.

        Runs accumulate per (candidate, kind): ``result.runs`` keeps every one,
        ``result.reports`` holds the aggregate (median, see ``engine.judge``),
        so repeating a test refines the measurement instead of overwriting it.
        """
        result = self.results.setdefault(candidate_id, CandidateResult(candidate_id))
        result.runs = result.runs + (report,)
        result.reports = judge.aggregate_by_kind(result.runs)

    def run_count(self, candidate_id: str, kind: str) -> int:
        """How many runs of ``kind`` the candidate has (the aggregate counts as one)."""
        result = self.results.get(candidate_id)
        return sum(1 for r in (result.runs if result else ()) if r.test_kind == kind)

    def verdict(self) -> judge.Verdict:
        """Grade, reliability and tie group of every measured candidate (ranks first)."""
        return judge.summarize(self.ranked(), self.prefs, CANDIDATE_KINDS)

    def set_rating(self, candidate_id: str, rating: int | None) -> None:
        """Store the optional 1-5 feeling rating."""
        if rating is not None and not 1 <= rating <= 5:
            raise SessionError("core.error.rating")
        self.results.setdefault(candidate_id, CandidateResult(candidate_id)).user_rating = rating

    def results_with_reports(self) -> list[CandidateResult]:
        """Results holding at least one measurement."""
        return [r for r in self.results.values() if r.reports]

    def ranked(self) -> list[CandidateResult]:
        """Measured results, best first (scores are set by the engine)."""
        measured = self.results_with_reports()
        if not measured:
            return []
        return list(self._call("core.error.rank", self.services.rank, measured, self.prefs,
                               required_kinds=CANDIDATE_KINDS))

    def best_candidate(self) -> Candidate | None:
        """The user's explicit choice, else the best-ranked measured candidate."""
        if self.chosen_id is not None:
            return self.candidate(self.chosen_id)
        ranked = self.ranked()
        return self.candidate(ranked[0].candidate_id) if ranked else None

    def choose(self, candidate_id: str) -> Candidate:
        """Override the ranking (and, in dry-run, select what to export)."""
        cand = self.candidate(candidate_id)
        self.chosen_id = cand.id
        return cand

    def refine(self) -> Candidate | None:
        """Ask the engine for the next small tuning step; None once it has converged."""
        ranked = self.ranked()
        if not ranked:
            raise SessionError("core.error.nothing_to_refine")
        if self.refine_rounds >= MAX_REFINE_ROUNDS:
            self.converged = True
            return None
        best = self.candidate(ranked[0].candidate_id)
        nxt = self._call("core.error.refine", self.services.refine, best, ranked,
                         self.prefs, set(self.filter_paths))
        known = next((c for c in self.candidates if nxt is not None and c.id == nxt.id), None)
        if nxt is None or (known is not None and self.is_complete(known.id)):
            self.converged = True
            return None
        if known is not None:   # suggested before but not measured yet: offer it again
            return known
        self.candidates.append(nxt)
        self.refine_rounds += 1
        return nxt

    # ------------------------------------------------------- state-changing I/O
    def ensure_backup(self) -> Path:
        """Back up settings.json once, before the first change."""
        if self.backup_path is None:
            if self.config_dir is None:
                raise SessionError("core.error.no_config_dir")
            self.backup_path = self._call(
                "core.error.backup", self.services.backup_settings, self.config_dir)
            self.log.append(f"backup: {self.backup_path}")
        return self.backup_path

    def _controller_obj(self) -> Any:
        if self._controller is None:
            self._controller = self._call(
                "core.error.controller", self.services.make_controller,
                self.install_dir, self.config_dir)
        return self._controller

    def _push(self, settings: dict) -> ApplyResult:
        result = self._call("core.error.apply", self._controller_obj().apply, settings)
        self.log.append(f"apply[{result.method}] ok={result.ok} {result.detail}".strip())
        return result

    def apply_candidate(self, candidate: Candidate, *, confirmed: bool) -> ApplyResult:
        """Apply one candidate to the running OTD so it can be measured."""
        self._require(confirmed)
        if self.dry_run:
            raise SessionError("core.error.dry_run")
        if self.settings is None:
            raise SessionError("core.error.incomplete")
        self.ensure_backup()
        settings = self._call("core.error.apply", self.services.apply_changes,
                              self.settings, self.target_tablet_name, candidate.changes)
        result = self._push(settings)
        if result.ok:
            self.live_candidate_id = candidate.id
        return result

    def restore_original(self, *, confirmed: bool) -> ApplyResult:
        """Put the settings read at the start back into the running OTD.

        If :meth:`apply_final` already replaced ``settings.json``, the original
        file content is written back too (a live push alone is in-memory only
        and would be undone at the next OTD start).
        """
        self._require(confirmed)
        if self.dry_run:
            raise SessionError("core.error.dry_run")
        if self.settings is None:
            raise SessionError("core.error.incomplete")
        self.ensure_backup()
        if self._wrote_settings:
            if self.config_dir is None:
                raise SessionError("core.error.no_config_dir")
            self._call("core.error.write", self.services.write_settings, self.config_dir,
                       self.settings)
            self._wrote_settings = False
            self.applied = None
        result = self._push(self.settings)
        if result.ok:
            self.live_candidate_id = None
        return result

    @property
    def has_unfinished_changes(self) -> bool:
        """True if a test candidate is live in OTD and was not made final or restored."""
        return self.live_candidate_id is not None and self.applied is None

    def final_settings(self) -> dict:
        """The full settings dict with the best candidate applied (nothing is written)."""
        best = self.best_candidate()
        if best is None or self.settings is None:
            raise SessionError("core.error.incomplete")
        return self._call("core.error.apply", self.services.apply_changes,
                          self.settings, self.target_tablet_name, best.changes)

    def preset_name(self) -> str:
        """Preset file name: tablet and timestamp (to the second, so a re-export never collides)."""
        tablet = re.sub(r"[^\w\- ]+", "", self.tablet.name if self.tablet else "tablet").strip()
        return f"osu-assistant {tablet} {time.strftime('%Y%m%d-%H%M%S')}"

    # ------------------------------------------------- output folder / settings
    @property
    def output_dir(self) -> Path | None:
        """The folder the user chose for saved presets (None = OTD's Presets folder)."""
        return Path(self.app_config.output_dir) if self.app_config.output_dir else None

    def default_export_name(self) -> str:
        """Editable default file name: the configured template, "<tablet> <timestamp>" by default."""
        tablet = self.tablet.name if self.tablet else "tablet"
        return preset_file_name(self.app_config.preset_name_template, tablet)

    def export_target(self, name: str | None = None) -> Path:
        """Where :meth:`export_preset` would write (the GUI asks before overwriting it)."""
        if self.output_dir is not None:
            return self.output_dir / f"{name or self.default_export_name()}.json"
        if self.config_dir is None:
            raise SessionError("core.error.no_config_dir")
        return self.config_dir / "Presets" / f"{name or self.preset_name()}.json"

    def set_output_dir(self, folder: Path | None) -> None:
        """Remember the output folder (persisted when the app config has a path)."""
        self.app_config.output_dir = str(folder) if folder else None
        self._optional(self.save_app_config)

    def set_plugin_policy(self, policy: PluginPolicy) -> None:
        """Change the plugin policy for this run and remember it."""
        self.prefs.plugin_policy = PluginPolicy(policy)
        self.app_config.plugin_policy = self.prefs.plugin_policy.value
        self._optional(self.save_app_config)

    def community_status(self) -> dict[str, Any] | None:
        """Date, preset and source counts, origin and staleness of the community data."""
        return self._optional(self.services.community_data_status, self.data_bundle)

    def update_community_data(self) -> Any:
        """Download, validate and activate the community data (only when the user asks).

        Returns the updater's ``UpdateResult``; the candidates are rebuilt next
        time because the presets may have changed. Raises SessionError when
        updates are disabled (no cache folder).
        """
        if self.data_cache_dir is None:
            raise SessionError("core.update.error.disabled")
        result = self._call("core.update.error.offline", self.services.update_community_data,
                            self.app_config.update_url, self.data_cache_dir)
        if getattr(result, "ok", False):
            self.data_bundle = result.bundle or self.data_bundle
            self.app_config.last_update_check = time.strftime("%Y-%m-%dT%H:%M:%S")
            self._optional(self.save_app_config)
            self._fingerprint = ""   # rebuilt the next time the wizard enters the testing step
            self.refresh_starting_presets()
        self.log.append(f"community data: {getattr(result, 'key', '?')} {getattr(result, 'detail', '')}".strip())
        return result

    def save_app_config(self) -> Path | None:
        """Persist :attr:`app_config` (no-op without :attr:`app_config_path`)."""
        if self.app_config_path is None:
            return None
        return self._call("core.error.app_config", self.services.save_app_config,
                          self.app_config, self.app_config_path)

    def export_preset(self, *, confirmed: bool, name: str | None = None, overwrite: bool = False) -> Path:
        """Save the final settings, as a complete OTD settings file, after a backup.

        Without an output folder it goes to OTD's ``Presets`` folder (as
        before); with one, to ``<output>/<name>.json`` (``name`` defaults to
        :meth:`default_export_name`). Replacing an existing file needs
        ``overwrite=True`` (the GUI confirms first) and keeps a dated copy in
        ``<output>/Backup/``.
        """
        self._require(confirmed)
        if self.config_dir is None:
            raise SessionError("core.error.no_config_dir")
        settings = self.final_settings()
        self.ensure_backup()
        if self.output_dir is None and not overwrite and name is None:
            self.preset_path = self._call(
                "core.error.preset", self.services.save_preset, self.config_dir,
                self.preset_name(), settings)
        else:
            target = self.export_target(name)
            self.preset_path = self._call(
                "core.error.preset", self.services.save_preset, self.config_dir, target.stem, settings,
                overwrite=overwrite, destination=target.parent)
        self.log.append(f"preset: {self.preset_path}")
        return self.preset_path

    def apply_final(self, *, confirmed: bool) -> ApplyResult:
        """Write the final settings to OTD's settings.json and apply them live."""
        self._require(confirmed)
        if self.dry_run:
            raise SessionError("core.error.dry_run")
        if self.config_dir is None:
            raise SessionError("core.error.no_config_dir")
        best = self.best_candidate()
        settings = self.final_settings()
        self.ensure_backup()
        self._call("core.error.write", self.services.write_settings, self.config_dir, settings)
        self._wrote_settings = True
        result = self._push(settings)
        if result.ok:
            self.applied = result
            self.live_candidate_id = best.id if best else None
        return result

    # ------------------------------------------------------------------ report
    def _filter_label(self, path: str) -> str:
        try:
            spec = self.services.load_filter_catalog().get(path)
        except Exception:  # catalog is optional decoration
            spec = None
        return getattr(spec, "display_name", None) or path

    def report_text(self) -> str:
        """Plain-language report of the setup, the recommendation and its evidence."""
        best = self.best_candidate()
        if best is None or self.tablet is None or self.display is None:
            raise SessionError("core.error.incomplete")
        prefs = self.prefs
        lines = [t("core.report.title"), "", t("core.report.section_setup")]
        lines.append(t("core.report.tablet", name=self.tablet.name, w=f"{self.tablet.width_mm:g}",
                       h=f"{self.tablet.height_mm:g}", rate=f"{self.tablet.max_report_rate_hz:g}"))
        lines.append(t("core.report.display", w=self.display.width_px, h=self.display.height_px,
                       hz=f"{self.display.refresh_hz:g}"))
        lines.append(t("core.report.answers",
                       style=t(f"core.pref.play_style.{prefs.play_style.value}"),
                       maps=t(f"core.pref.map_preference.{prefs.map_preference.value}"),
                       priority=t(f"core.pref.priority.{prefs.priority.value}"),
                       skill=t(f"core.pref.skill_level.{prefs.skill_level.value}")))
        lines.append(t("core.report.policy", policy=t(f"core.pref.plugin_policy.{prefs.plugin_policy.value}")))
        lines += ["", t("core.report.section_settings"),
                  t("core.report.chosen", label=candidate_label(self, best), summary=t(best.summary_key))]
        area = best.changes.tablet_area
        if area is not None:
            lines.append(t("core.report.area", w=f"{area.width:.1f}", h=f"{area.height:.1f}",
                           x=f"{area.x:.1f}", y=f"{area.y:.1f}", rot=f"{area.rotation:g}"))
        if best.changes.filters is None:
            lines.append(t("core.report.filters_unchanged"))
        elif not best.changes.filters:
            lines.append(t("core.report.filters_none"))
        else:
            lines.append(t("core.report.filters"))
            for cfg in best.changes.filters:
                params = ", ".join(f"{k}={v}" for k, v in cfg.settings.items())
                lines.append(t("core.report.filter_item", name=self._filter_label(cfg.path),
                               params=params or "-", state=t(
                                   "core.report.on" if cfg.enabled else "core.report.off")))
        if best.est_added_latency_ms:
            lines.append(t("core.report.latency", ms=f"{best.est_added_latency_ms:.1f}"))
        if best.reasons:
            lines += ["", t("core.report.section_reasons")]
            lines += [f"- {t(r.key, **r.params)}" for r in best.reasons]
        result = self.results.get(best.id)
        if result and (result.reports or result.user_rating):
            lines += ["", t("core.report.section_measures")]
            lines += metric_lines(result.reports)
            if result.user_rating:
                lines.append(t("core.report.rating", n=result.user_rating))
        lines += ["", t("core.report.disclaimer")]
        if self.backup_path is not None:
            lines.append(t("core.report.backup", path=str(self.backup_path)))
        return "\n".join(lines)


_METRICS: tuple[tuple[str, str, str], ...] = (
    ("effective_rate_hz", "core.metric.rate", "{:.0f}"),
    ("jitter_rms_mm", "core.metric.jitter", "{:.3f}"),
    ("path_error_mean_mm", "core.metric.path_error", "{:.2f}"),
    ("path_error_p95_mm", "core.metric.path_error_p95", "{:.2f}"),
    ("smoothness_jerk", "core.metric.jerk", "{:.2f}"),
    ("lag_ms_estimate", "core.metric.lag", "{:.1f}"),
    ("aim_error_mean_mm", "core.metric.aim_error", "{:.2f}"),
    ("aim_time_ms_mean", "core.metric.aim_time", "{:.0f}"),
)


def metric_lines(reports: tuple[MetricsReport, ...] | list[MetricsReport]) -> list[str]:
    """Translated line for every metric that has a value."""
    lines = []
    for report in reports:
        for attr, key, fmt in _METRICS:
            value = getattr(report, attr)
            if value is not None:
                lines.append(t(key, value=fmt.format(value)))
    return lines


# Plain-language levels for the detail view: (metric attribute, good up to, fair up to) in the
# metric's unit; lower is better. Heuristic references, not calibrated.
_PLAIN_LEVELS = {
    "stillness": ("jitter_rms_mm", 0.03, 0.08),
    "tracking": ("path_error_mean_mm", 0.7, 1.5),
    "aim": ("aim_error_mean_mm", 1.2, 2.5),
}


def plain_verdict(reports: Any, added_latency_ms: float = 0.0) -> list[tuple[str, dict[str, Any]]]:
    """Short, jargon-free findings as ``(i18n key, params)``: one per measured test, plus the delay.

    Keys are ``gui.verdict.<kind>.<good|fair|poor>`` and ``gui.verdict.latency``.
    """
    lines: list[tuple[str, dict[str, Any]]] = []
    for kind in ("stillness", "tracking", "aim"):
        attr, good, fair = _PLAIN_LEVELS[kind]
        value = _metric(reports, kind, attr)
        if value is None:
            continue
        level = "good" if value <= good else "fair" if value <= fair else "poor"
        lines.append((f"gui.verdict.{kind}.{level}", {}))
    if reports and added_latency_ms:
        lines.append(("gui.verdict.latency", {"ms": f"{added_latency_ms:.0f}"}))
    return lines


_LABEL_REASONS = ("engine.reason.community_source", "engine.reason.community_source_undated",
                  "engine.reason.community_user_values")


def candidate_label(session: Session | None, cand: Candidate) -> str:
    """Translated label of a candidate; community ones carry the publisher's name.

    A refined candidate (``<root>|...``) takes the name from its root candidate.
    """
    sources = [cand]
    if session is not None and "|" in cand.id:
        root = cand.id.split("|", 1)[0]
        sources += [c for c in session.candidates if c.id == root]
    for source in sources:
        for reason in source.reasons:
            if reason.key in _LABEL_REASONS:
                return t(cand.label_key, name=reason.params.get("name", ""))
    return t(cand.label_key)


def preset_label(preset: Any, catalog: dict[str, Any]) -> str:
    """"<who> - <filters> (<tablet>, <area>)" for the starting-preset list."""
    names = ", ".join(getattr(catalog.get(f.path), "display_name", None) or f.path
                      for f in preset.enabled_filters) or t("gui.preset.no_filters")
    who = preset.name if preset.attribution != "user" else t("gui.preset.user_values")
    area = f"{preset.area_mm[0]:g} x {preset.area_mm[1]:g} mm" if preset.area_mm else t("gui.preset.area_unknown")
    return t("gui.preset.item", who=who, filters=names, tablet=preset.tablet or "?", area=area)


def preset_summary(preset: Any) -> str:
    """Source, date and confidence of a preset, translated."""
    return t("gui.preset.item_source", source=preset.source_url or t("gui.preset.no_source"),
             date=preset.source_date or t("gui.preset.undated", retrieved=preset.retrieved),
             confidence=t(f"gui.preset.confidence.{preset.confidence}"))


# --------------------------------------------------------------------------
# Presentation helpers for the result screen (pure: no Tk, no side effects)
# --------------------------------------------------------------------------
KEEP_CURRENT_ID = "keep-current"
# label key, test kind, MetricsReport attribute, unit, decimals (all: lower is better)
_COMPARED: tuple[tuple[str, str, str, str, int], ...] = (
    ("gui.compare.jitter", "stillness", "jitter_rms_mm", "mm", 3),
    ("gui.compare.tracking", "tracking", "path_error_mean_mm", "mm", 2),
    ("gui.compare.aim_time", "aim", "aim_time_ms_mean", "ms", 0),
)


@dataclass(frozen=True)
class ComparisonRow:
    """One before/after metric of the result screen (None = not measured)."""
    key: str
    before: float | None
    after: float | None
    unit: str
    digits: int
    lower_is_better: bool = True

    @property
    def label(self) -> str:
        """Translated metric name."""
        return t(self.key)


def _metric(reports: Any, kind: str, attr: str) -> float | None:
    for report in reports:
        if report.test_kind == kind and getattr(report, attr) is not None:
            return float(getattr(report, attr))
    return None


def build_comparison(before: Any, after: Any, before_latency: float | None,
                     after_latency: float | None) -> list[ComparisonRow]:
    """Rows for jitter, tracking error, aim time and added latency from two report sets."""
    rows = [ComparisonRow(key, _metric(before, kind, attr), _metric(after, kind, attr), unit, digits)
            for key, kind, attr, unit, digits in _COMPARED]
    rows.append(ComparisonRow("gui.compare.latency", before_latency, after_latency, "ms", 1))
    return rows


def comparison_for(session: Session, best: Candidate) -> list[ComparisonRow]:
    """Before (the user's current setup) vs after (``best``) for the result screen.

    "Before" is the measured "keep current" option when there is one, topped
    up with the baseline; its added latency is that option's estimate, or 0
    when the current profile has no enabled filter.
    """
    current = next((c for c in session.candidates if c.id == KEEP_CURRENT_ID), None)
    current_result = session.results.get(current.id) if current is not None else None
    before = list(current_result.reports) if current_result else []
    kinds = {r.test_kind for r in before}
    before += [r for r in session.baseline if r.test_kind not in kinds]
    best_result = session.results.get(best.id)
    after = list(best_result.reports) if best_result else []
    if current is not None:
        before_latency: float | None = current.est_added_latency_ms
    elif session.profile is not None and not any(f.enabled for f in session.profile.filters):
        before_latency = 0.0
    else:
        before_latency = None
    return build_comparison(before, after, before_latency, best.est_added_latency_ms)


def describe_changes(current: ProfileInfo | None, changes: Any, name_of: Callable[[str], str],
                     added_latency_ms: float = 0.0, detail: bool = False) -> list[tuple[str, dict[str, Any]]]:
    """What ``changes`` (a ``ProfileChanges``) does to ``current``, as ``(i18n key, params)`` lines.

    With ``detail`` every changed parameter of a tuned filter gets its own
    ``old -> new`` line (``gui.change.param``).
    """
    out: list[tuple[str, dict[str, Any]]] = []
    area = changes.tablet_area
    old = current.tablet_area if current is not None and current.tablet_area.width > 0 else None
    if area is None:
        out.append(("gui.change.area_kept", {}))
    elif old is None:
        out.append(("gui.change.area_new", {"w": f"{area.width:.1f}", "h": f"{area.height:.1f}"}))
    else:
        ratio = area.width / old.width - 1.0
        params = {"old_w": f"{old.width:.1f}", "old_h": f"{old.height:.1f}", "w": f"{area.width:.1f}",
                  "h": f"{area.height:.1f}", "pct": f"{abs(ratio) * 100:.0f}"}
        key = "gui.change.area_same" if abs(ratio) < 0.01 else (
            "gui.change.area_smaller" if ratio < 0 else "gui.change.area_larger")
        out.append((key, params))
        if math.hypot(area.x - old.x, area.y - old.y) > 1.0:
            out.append(("gui.change.area_moved", {"x": f"{area.x:.1f}", "y": f"{area.y:.1f}"}))
        if (area.rotation or 0.0) != (old.rotation or 0.0):
            out.append(("gui.change.area_rotated", {"rot": f"{area.rotation:g}"}))
    if changes.filters is None:
        out.append(("gui.change.filters_kept", {}))
    else:
        before = {f.path: f for f in (current.filters if current is not None else ()) if f.enabled}
        after = {f.path: f for f in changes.filters if f.enabled}
        if not after:
            out.append(("gui.change.filters_removed_all" if before else "gui.change.filters_none", {}))
        for path, cfg in after.items():
            if path not in before:
                key = "gui.change.filter_added"
            elif dict(before[path].settings) != dict(cfg.settings):
                key = "gui.change.filter_tuned"
            else:
                key = "gui.change.filter_same"
            out.append((key, {"name": name_of(path)}))
            if detail and key == "gui.change.filter_tuned":
                old = dict(before[path].settings)
                out += [("gui.change.param", {"name": name_of(path), "param": param,
                                              "old": format_value(old.get(param, "-")),
                                              "new": format_value(value)})
                        for param, value in cfg.settings.items() if old.get(param) != value]
        if after:   # "all removed" already says it when nothing is left
            out += [("gui.change.filter_removed", {"name": name_of(path)}) for path in before
                    if path not in after]
    if added_latency_ms > 0:
        out.append(("gui.change.latency", {"ms": f"{added_latency_ms:.1f}"}))
    return out


def format_value(value: Any) -> str:
    """A filter setting as the user reads it (booleans translated, floats trimmed)."""
    if isinstance(value, bool):
        return t("gui.value.on" if value else "gui.value.off")
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".") if math.isfinite(value) else str(value)
    return str(value)


def filter_table(filters: Any, catalog: dict[str, Any]) -> list[tuple[str, bool, list[tuple[str, str, str]]]]:
    """``(filter name, enabled, [(parameter, value, unit), ...])`` per filter of a chain."""
    rows = []
    for cfg in filters:
        spec = catalog.get(cfg.path)
        name = getattr(spec, "display_name", None) or cfg.path
        params = []
        for key, value in cfg.settings.items():
            param = spec.param(key) if spec is not None and hasattr(spec, "param") else None
            params.append((key, format_value(value), getattr(param, "unit", "") or ""))
        rows.append((name, bool(cfg.enabled), params))
    return rows

