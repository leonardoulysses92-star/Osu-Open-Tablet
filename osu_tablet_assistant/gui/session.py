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
import re
import sys
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from ..knowledge import tablets as tablet_db
from ..models import (
    ApplyResult,
    Candidate,
    CandidateResult,
    DisplaySpec,
    MetricsReport,
    PluginInfo,
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
        return repr((self.tablet, self.display, self.prefs, tuple(self.baseline), self.profile))

    def ensure_candidates(self) -> None:
        """Generate candidates unless they already match the current inputs."""
        if not self.candidates or self._fingerprint != self._current_fingerprint():
            self.generate_candidates()

    def generate_candidates(self) -> list[Candidate]:
        """(Re)build the candidate list from the current choices and baseline."""
        if self.tablet is None or self.display is None:
            raise SessionError("core.error.incomplete")
        found = self._call(
            "core.error.generate", self.services.generate_candidates, self.tablet,
            self.otd_display(), self.prefs, set(self.filter_paths), list(self.baseline) or None,
            self.profile)
        if not found:
            raise SessionError("core.error.no_candidates")
        self.candidates = list(found)
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
        """Attach a measurement to a candidate, replacing the same kind."""
        result = self.results.setdefault(candidate_id, CandidateResult(candidate_id))
        kept = tuple(r for r in result.reports if r.test_kind != report.test_kind)
        result.reports = kept + (report,)

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
        return list(self._call("core.error.rank", self.services.rank, measured, self.prefs))

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

    def export_preset(self, *, confirmed: bool) -> Path:
        """Save the final settings as an OTD preset (after a backup)."""
        self._require(confirmed)
        if self.config_dir is None:
            raise SessionError("core.error.no_config_dir")
        settings = self.final_settings()
        self.ensure_backup()
        self.preset_path = self._call(
            "core.error.preset", self.services.save_preset, self.config_dir,
            self.preset_name(), settings)
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
        lines += ["", t("core.report.section_settings"),
                  t("core.report.chosen", label=t(best.label_key), summary=t(best.summary_key))]
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

