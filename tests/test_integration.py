"""End-to-end: the real modules wired together, fakes only at the edges.

Real: ``otd.settings_io`` (on a temp copy of the fixture), ``otd.plugins``,
``knowledge.filters`` / ``knowledge.tablets``, ``engine.area`` /
``engine.recommend`` / ``engine.metrics``, the pure layer of
``gui.live_tests`` and ``gui.session.Session`` with default ``Services``.
Fake: the OTD controller (records what it was asked to apply) and the pen
test window (synthesizes cursor samples whose noise and lag depend on the
filters that are live in the fake OTD, then runs the real metrics).
"""
from __future__ import annotations

import copy
import itertools
import json
import random
import re
import shutil
import string
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

from osu_tablet_assistant.engine import recommend
from osu_tablet_assistant.gui import i18n, live_tests
from osu_tablet_assistant.gui.session import (
    BASELINE_KINDS, CANDIDATE_KINDS, MAX_REFINE_ROUNDS, STEP_ORDER, ConfirmationRequired, Services,
    Session, SessionError, Step)
from osu_tablet_assistant.knowledge.filters import load_catalog
from osu_tablet_assistant.models import (ApplyResult, CandidateResult, DisplaySpec, FilterConfig,
                                         MapPreference, MetricsReport, PenSample, PlayStyle,
                                         Priority, ProfileInfo, Rect, SkillLevel, TabletSpec,
                                         UserPreferences)
from osu_tablet_assistant.otd import settings_io

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "osu_tablet_assistant"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "settings_sample.json"
TABLET_NAME = "Wacom CTL-672"
OTHER_TABLET = "Huion H640P"
DISPLAY = DisplaySpec(1920, 1080, 960, 540, refresh_hz=144)
PRIMARY = (1920, 1080)
# Plugin folders as OTD lays them out (only names/DLL stems matter for discovery).
PLUGINS = {"HawkuFilters": "HawkuFilters.dll", "RadialFollow": "RadialFollow.dll",
           "Temporal Resampler": "TemporalResampler.dll"}


# --------------------------------------------------------------------------
# Edge fakes
# --------------------------------------------------------------------------
class FakeOtd:
    """Records every settings dict pushed to the 'running daemon'."""

    def __init__(self, config_dir: Path, events: list[str], ok: Callable[[int], bool]) -> None:
        self.config_dir, self.events, self._ok = config_dir, events, ok
        self.applied: list[dict] = []
        self.created = 0

    def make(self, install_dir: Path | None, config_dir: Path | None) -> "FakeOtd":
        self.created += 1
        return self

    def apply(self, settings: dict) -> ApplyResult:
        # A backup must already exist before OTD is touched.
        assert list((self.config_dir / settings_io.BACKUP_DIR).glob("*.json")), "apply before backup"
        self.events.append("apply")
        ok = self._ok(len(self.applied))
        self.applied.append(copy.deepcopy(settings))
        if ok:
            return ApplyResult(True, "console", "otd.apply.ok_console", "fake")
        return ApplyResult(False, "preset_file", "otd.apply.manual_preset", "fake failure")

    def live_profile(self, original: dict) -> dict:
        settings = self.applied[-1] if self.applied else original
        return next(p for p in settings["Profiles"] if p["Tablet"] == TABLET_NAME)


def _smoothing_ms(profile: dict) -> float:
    """Rough 'amount of smoothing' of an OTD profile, used to synthesize the pen."""
    total = 0.0
    for store in profile.get("Filters") or []:
        if not store.get("Enable", True):
            continue
        values = {i["Property"]: i["Value"] for i in store.get("Settings") or []}
        if store["Path"] in (recommend.HAWKU, recommend.ANTICHATTER):
            total += float(values.get("Latency", 2.0))
        elif store["Path"] in (recommend.RADIAL_TABLET,):
            total += 4.0 * float(values.get("OuterRadius", 1.0))
        elif store["Path"] == recommend.RESAMPLER:
            total += float(values.get("latency", 0.0))
        elif store["Path"] == recommend.NOISE_REDUCTION:
            total += 3.0
    return total


class FakePen:
    """Stands in for ``gui.live_tests.run_test``: synthetic samples -> real metrics."""

    RATE_HZ = 133.0

    def __init__(self, otd: FakeOtd, original: dict) -> None:
        self.otd, self.original = otd, original
        self.calls: list[tuple[str, float, dict]] = []    # kind, mm_per_px, live profile

    def __call__(self, parent: Any, kind: str, mm_per_px: float, display: DisplaySpec | None,
                 on_done: Callable[[MetricsReport], None],
                 on_cancel: Callable[[], None] | None = None) -> None:
        live = copy.deepcopy(self.otd.live_profile(self.original))
        self.calls.append((kind, mm_per_px, live))
        smooth = _smoothing_ms(live)
        jitter_px = 2.0 / (1.0 + smooth / 4.0)
        lag_s = 0.008 + smooth / 1000.0
        rng = random.Random(len(self.calls))
        rect = live_tests.window_rect(display, PRIMARY)
        dt = 1.0 / self.RATE_HZ
        spec = live_tests.SPECS[kind]
        if kind == "rate":
            samples = [PenSample(i * dt, 900 + 200 * (i % 40) / 40, 500 + 3 * i % 7)
                       for i in range(int(spec.duration_s * self.RATE_HZ))]
            report = live_tests.build_report(kind, mm_per_px, samples, started_at=0.0)
        elif kind == "stillness":
            samples = [PenSample(i * dt, round(800 + rng.gauss(0, jitter_px)),
                                 round(450 + rng.gauss(0, jitter_px)))
                       for i in range(int(spec.duration_s * self.RATE_HZ))]
            report = live_tests.build_report(kind, mm_per_px, samples, started_at=0.0)
        elif kind == "tracking":
            course = live_tests.make_tracking_course(rect, spec.duration_s)
            samples = []
            for i in range(int(spec.duration_s * self.RATE_HZ)):
                x, y = course.position(i * dt - lag_s)
                samples.append(PenSample(i * dt, round(x + rng.gauss(0, jitter_px)),
                                         round(y + rng.gauss(0, jitter_px))))
            report = live_tests.build_report(kind, mm_per_px, samples, started_at=0.0, course=course)
        else:
            aim = live_tests.AimSession(live_tests.plan_aim_targets(rect, 100.0, 10, rng))
            now = 0.0
            aim.start(now)
            while not aim.done:
                now += 0.35 + lag_s
                tx, ty = aim.current
                aim.click(now, tx + rng.gauss(0, 4 + jitter_px), ty + rng.gauss(0, 4 + jitter_px))
            report = live_tests.build_report(kind, mm_per_px, [], started_at=0.0, aim=aim)
        on_done(report)


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------
class Harness:
    """A temp OTD config dir plus a Session wired to the real modules."""

    def __init__(self, test: unittest.TestCase, dry_run: bool,
                 apply_ok: Callable[[int], bool] = lambda n: True) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="osu-assistant-it-")
        test.addCleanup(tmp.cleanup)
        self.config = Path(tmp.name) / "OpenTabletDriver"
        self.config.mkdir()
        shutil.copyfile(FIXTURE, self.config / settings_io.SETTINGS_FILE)
        for folder, dll in PLUGINS.items():
            (self.config / "Plugins" / folder).mkdir(parents=True)
            (self.config / "Plugins" / folder / dll).write_bytes(b"")
        self.original_bytes = self.settings_bytes()
        self.original = json.loads(self.original_bytes.decode("utf-8"))
        self.events: list[str] = []
        self.otd = FakeOtd(self.config, self.events, apply_ok)
        self.pen = FakePen(self.otd, self.original)
        services = Services(find_config_dir=lambda: self.config, find_install_dir=lambda: None,
                            make_controller=self.otd.make, run_test=self.pen,
                            detect_display=lambda: DISPLAY, desktop_origin=lambda: (0.0, 0.0))
        for name in ("backup_settings", "write_settings", "save_preset"):
            setattr(services, name, self._recorded(name, getattr(services, name)))
        self.session = Session(services, dry_run=dry_run)

    def _recorded(self, name: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def call(*args: Any) -> Any:
            self.events.append(name)
            return real(*args)
        call.__name__ = name
        return call

    def settings_bytes(self) -> bytes:
        return (self.config / settings_io.SETTINGS_FILE).read_bytes()

    def on_disk(self) -> dict:
        return json.loads(self.settings_bytes().decode("utf-8"))

    def backups(self) -> list[Path]:
        return sorted((self.config / settings_io.BACKUP_DIR).glob("*.json"))

    def drive_to_testing(self, test: unittest.TestCase) -> Session:
        s = self.session
        s.accepted_disclaimer = True
        test.assertEqual(s.advance(), Step.DETECT)
        s.detect()
        test.assertEqual({p.tablet_name for p in s.profiles}, {TABLET_NAME, OTHER_TABLET})
        test.assertTrue({recommend.HAWKU, recommend.RADIAL_TABLET, recommend.RESAMPLER}
                        <= s.filter_paths)
        test.assertEqual(s.advance(), Step.TABLET)
        choice = next(c for c in s.tablet_choices() if c.label == TABLET_NAME)
        test.assertTrue(s.select_choice(choice))
        test.assertEqual((s.tablet.width_mm, s.tablet.height_mm), (216.0, 135.0))
        test.assertEqual(s.advance(), Step.DISPLAY)
        test.assertEqual(s.detect_display(), DISPLAY)
        test.assertEqual(s.advance(), Step.QUESTIONS)
        s.prefs.play_style = PlayStyle.WRIST
        s.prefs.notices_jitter = True
        test.assertEqual(s.advance(), Step.BASELINE)
        for kind in BASELINE_KINDS:
            s.run_measurement(None, kind)
        test.assertEqual(s.advance(), Step.TESTING)
        return s


def _diff_paths(a: Any, b: Any, path: tuple = ()) -> set[tuple]:
    """Paths (key tuples) where two JSON values differ."""
    if isinstance(a, dict) and isinstance(b, dict):
        out: set[tuple] = set()
        for key in set(a) | set(b):
            if key not in a or key not in b:
                out.add(path + (key,))
            else:
                out |= _diff_paths(a[key], b[key], path + (key,))
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return set().union(*(_diff_paths(x, y, path + (i,)) for i, (x, y) in enumerate(zip(a, b))))
    return set() if a == b else {path}


# --------------------------------------------------------------------------
# Journeys
# --------------------------------------------------------------------------
class ConfirmedJourneyTests(unittest.TestCase):
    def test_full_wizard_with_real_modules(self) -> None:
        h = Harness(self, dry_run=False)
        s = h.drive_to_testing(self)

        # Baseline: measured on the current profile's own scale (100 mm onto 1920 px).
        for kind, mm_per_px, _live in h.pen.calls:
            self.assertAlmostEqual(mm_per_px, 100.0 / 1920.0, msg=kind)
        self.assertIsNotNone(s.baseline_report("stillness").jitter_rms_mm)
        self.assertAlmostEqual(s.baseline_report("rate").effective_rate_hz, FakePen.RATE_HZ, delta=1)

        # Nothing has been touched yet: no backup, no write, no apply.
        self.assertEqual(h.events, [])
        self.assertEqual(h.settings_bytes(), h.original_bytes)
        ids = [c.id for c in s.candidates]
        self.assertEqual(ids, ["no-filter", "low-latency", "balanced", "smooth", "keep-current"])

        # Generated candidates: Frequency untouched, catalog values only, areas centered.
        catalog = load_catalog()
        for cand in s.candidates[:-1]:
            area = cand.changes.tablet_area
            self.assertEqual((area.x, area.y), (108.0, 67.5))
            self.assertAlmostEqual(area.width / area.height, 1920 / 1080)
            self.assertEqual(cand.changes.display_area, Rect(1920, 1080, 960, 540))
            for fc in cand.changes.filters:
                if catalog[fc.path].param("Frequency") is not None:
                    self.assertEqual(fc.settings["Frequency"], 1000.0)

        # Nothing is applied without confirmed=True.
        with self.assertRaises(ConfirmationRequired):
            s.apply_candidate(s.candidates[1], confirmed=False)
        self.assertEqual(h.events, [])

        # Candidate loop: apply, then every per-candidate test runs while it is live.
        def test_candidate(cand_id: str) -> None:
            cand = s.candidate(cand_id)
            self.assertTrue(s.apply_candidate(cand, confirmed=True).ok)
            expected = settings_io.apply_changes(h.original, TABLET_NAME, cand.changes)
            self.assertEqual(h.otd.applied[-1], expected)
            before = len(h.pen.calls)
            for kind in CANDIDATE_KINDS:
                s.run_measurement(None, kind, cand)
            for kind, mm_per_px, live in h.pen.calls[before:]:
                self.assertEqual(live, next(p for p in expected["Profiles"]
                                            if p["Tablet"] == TABLET_NAME), kind)
                area = cand.changes.tablet_area
                self.assertAlmostEqual(mm_per_px, area.width / cand.changes.display_area.width)
            self.assertTrue(s.is_complete(cand_id))

        for cand_id in ids:
            test_candidate(cand_id)
        self.assertEqual(h.backups()[0].read_bytes(), h.original_bytes)
        self.assertEqual(h.events[0], "backup_settings")
        self.assertEqual(h.events.count("backup_settings"), 1)
        self.assertEqual(h.settings_bytes(), h.original_bytes)   # live apply is in-memory only

        ranked = s.ranked()
        self.assertEqual(len(ranked), len(ids))
        self.assertEqual(ranked, sorted(ranked, key=lambda r: -r.score))
        scores = {r.candidate_id: r.score for r in ranked}
        # The synthetic pen shakes less with smoothing: jitter now reaches the score.
        no_filter = s.results["no-filter"].reports
        smooth = s.results["smooth"].reports
        jitter = {r.test_kind: r.jitter_rms_mm for r in no_filter}["stillness"]
        self.assertGreater(jitter, {r.test_kind: r.jitter_rms_mm for r in smooth}["stillness"])
        self.assertNotEqual(scores["no-filter"], scores["smooth"])

        # Refinement: real engine ids encode the hill-climb history; each one is tested.
        refined = 0
        while (nxt := s.refine()) is not None:
            self.assertIn("|", nxt.id)
            test_candidate(nxt.id)
            refined += 1
        self.assertTrue(s.converged)
        self.assertLessEqual(refined, MAX_REFINE_ROUNDS)
        self.assertGreater(refined, 0)

        best = s.best_candidate()
        self.assertEqual(best.id, s.ranked()[0].candidate_id)
        self.assertEqual(s.advance(), Step.RESULT)
        for lang in i18n.SUPPORTED:
            i18n.set_language(lang)
            text = s.report_text()
            self.assertIsNone(re.search(r"\b(core|engine|otd|gui|tests)\.[a-z_]+\.", text), text)
        i18n.set_language("es")

        # Preset export, then final apply (persisted + live).
        writes_before = h.events.count("write_settings")
        preset = s.export_preset(confirmed=True)
        self.assertEqual(preset.parent, h.config / "Presets")
        final = s.final_settings()
        self.assertEqual(json.loads(preset.read_text(encoding="utf-8")), final)
        self.assertEqual(h.events.count("write_settings"), writes_before)
        self.assertEqual(h.settings_bytes(), h.original_bytes)

        result = s.apply_final(confirmed=True)
        self.assertTrue(result.ok)
        self.assertLess(h.events.index("backup_settings"), h.events.index("write_settings"))
        self.assertLess(h.events.index("write_settings"), len(h.events) - 1)   # then apply
        self.assertEqual(h.events[-1], "apply")
        self.assertEqual(h.otd.applied[-1], final)
        on_disk = h.on_disk()
        self.assertEqual(on_disk, final)
        self.assertEqual(len(h.backups()), 1)
        self.assertFalse(s.has_unfinished_changes)

        # Only the chosen profile's area/display/lock/filters changed.
        index = next(i for i, p in enumerate(h.original["Profiles"]) if p["Tablet"] == TABLET_NAME)
        allowed = ("AbsoluteModeSettings", "Tablet"), ("AbsoluteModeSettings", "Display"), \
            ("AbsoluteModeSettings", "LockAspectRatio"), ("Filters",)
        for path in _diff_paths(h.original, on_disk):
            self.assertEqual(path[:2], ("Profiles", index), path)
            self.assertTrue(any(path[2:2 + len(p)] == p for p in allowed), path)
        other = next(i for i, p in enumerate(h.original["Profiles"]) if p["Tablet"] == OTHER_TABLET)
        self.assertEqual(on_disk["Profiles"][other], h.original["Profiles"][other])
        self.assertEqual(on_disk["Profiles"][index]["FutureProfileKey"], {"Nested": [1, 2, 3]})
        self.assertEqual(on_disk["Tools"], h.original["Tools"])
        # Unknown keys of filter stores that stay in the chain survive the rewrite.
        old_stores = {f["Path"]: f for f in h.original["Profiles"][index]["Filters"]}
        for store in on_disk["Profiles"][index]["Filters"]:
            for key, value in old_stores.get(store["Path"], {}).items():
                if key not in ("Settings", "Enable"):
                    self.assertEqual(store[key], value)
        # The written profile reads back as exactly the chosen candidate.
        profile = next(p for p in settings_io.list_profiles(on_disk) if p.tablet_name == TABLET_NAME)
        self.assertEqual(profile.tablet_area, best.changes.tablet_area)
        self.assertEqual(tuple(f.path for f in profile.filters),
                         tuple(f.path for f in best.changes.filters))

        # Restore after the final apply puts the original file back, too.
        s.restore_original(confirmed=True)
        self.assertEqual(h.on_disk(), h.original)
        self.assertEqual(h.otd.applied[-1], h.original)

    def test_controller_failure_mid_loop_is_reported_and_restorable(self) -> None:
        h = Harness(self, dry_run=False, apply_ok=lambda n: n != 1)   # 2nd apply fails
        s = h.drive_to_testing(self)
        first, second = s.candidates[0], s.candidates[1]
        self.assertTrue(s.apply_candidate(first, confirmed=True).ok)
        failed = s.apply_candidate(second, confirmed=True)
        self.assertFalse(failed.ok)
        self.assertIn(failed.message_key, i18n.load_catalog("en"))
        self.assertEqual(s.live_candidate_id, first.id)     # OTD still runs the first one
        self.assertTrue(s.has_unfinished_changes)            # closing offers a restore
        self.assertTrue(s.restore_original(confirmed=True).ok)
        self.assertEqual(h.otd.applied[-1], h.original)
        self.assertFalse(s.has_unfinished_changes)
        self.assertEqual(h.settings_bytes(), h.original_bytes)
        self.assertNotIn("write_settings", h.events)

    def test_settings_errors_reach_the_user_as_translated_keys(self) -> None:
        h = Harness(self, dry_run=False)
        (h.config / settings_io.SETTINGS_FILE).write_text("{broken", encoding="utf-8")
        with self.assertRaises(SessionError) as ctx:
            h.session.detect()
        self.assertEqual(ctx.exception.key, "otd.error.settings_invalid_json")
        message = i18n.t(ctx.exception.key, **ctx.exception.params)
        self.assertNotIn("otd.error", message)
        self.assertIn(str(h.config), message)


class DryRunJourneyTests(unittest.TestCase):
    def test_dry_run_never_writes_or_applies(self) -> None:
        h = Harness(self, dry_run=True)
        s = h.drive_to_testing(self)
        self.assertFalse(s.can_measure)
        self.assertFalse(s.can_advance())
        for cand in s.candidates:
            with self.assertRaises(SessionError) as ctx:
                s.apply_candidate(cand, confirmed=True)
            self.assertEqual(ctx.exception.key, "core.error.dry_run")
        s.choose("balanced")
        self.assertEqual(s.advance(), Step.RESULT)
        s.report_text()
        for action in (lambda: s.apply_final(confirmed=True),
                       lambda: s.restore_original(confirmed=True)):
            with self.assertRaises(SessionError):
                action()
        preset = s.export_preset(confirmed=True)
        self.assertTrue(preset.is_file())
        self.assertEqual(preset.parent, h.config / "Presets")
        self.assertEqual(json.loads(preset.read_text(encoding="utf-8")), s.final_settings())
        self.assertEqual(h.events, ["backup_settings", "save_preset"])
        self.assertEqual(h.settings_bytes(), h.original_bytes)
        self.assertEqual(h.otd.created, 0)
        self.assertEqual(h.otd.applied, [])
        self.assertEqual({kind for kind, _, _ in h.pen.calls}, set(BASELINE_KINDS))


# --------------------------------------------------------------------------
# i18n coverage of everything the wired modules can emit
# --------------------------------------------------------------------------
_LITERAL_KEY = re.compile(r"""["']((?:core|gui|otd|engine|tests)\.[A-Za-z0-9_.\-]+)["']""")
_SOURCES = ("gui/session.py", "gui/wizard.py", "gui/app.py", "gui/widgets.py", "gui/live_tests.py",
            "otd/settings_io.py", "otd/controller.py", "otd/plugins.py",
            "engine/area.py", "engine/recommend.py", "engine/metrics.py")


def _fields(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def _engine_reasons() -> list:
    """Reasons from a broad sweep of the real engine (incl. rotated and refined profiles)."""
    catalog = load_catalog()
    tablets = (TabletSpec("small", 152, 95, 133), TabletSpec("big", 216, 135, 266),
               TabletSpec("tiny", 60, 40, 200))
    displays = (DisplaySpec(1920, 1080, refresh_hz=60), DisplaySpec(2560, 1440, refresh_hz=240))
    baselines = (None, [MetricsReport("stillness", 100, 2.0, jitter_rms_mm=0.2),
                        MetricsReport("rate", 100, 1.0, effective_rate_hz=60.0)],
                 [MetricsReport("stillness", 100, 2.0, jitter_rms_mm=0.001)])
    plugin_sets = (set(), set(catalog), {recommend.HAWKU}, {recommend.NOISE_REDUCTION},
                   {recommend.RESAMPLER, recommend.HAWKU})
    currents = (None, ProfileInfo("t", "abs", Rect(1920, 1080, 960, 540), Rect(80, 45, 76, 47.5, 180),
                                  True, (FilterConfig("Some.Unknown.Filter", True, {}),)))
    reasons = []
    for tablet, display, baseline, plugins, style, prio, extra, current in itertools.product(
            tablets, displays, baselines, plugin_sets, PlayStyle, Priority, (0, 1, 2), currents):
        prefs = UserPreferences(play_style=style, priority=prio, skill_level=list(SkillLevel)[extra],
                                map_preference=list(MapPreference)[(extra + 1) % 3],
                                notices_jitter=extra == 1, notices_input_lag=extra == 2,
                                preferred_area_width_mm=(None, 5.0, None)[extra])
        for cand in recommend.generate_candidates(tablet, display, prefs, plugins, baseline, current):
            reasons += cand.reasons
            nxt = recommend.refine(cand, [CandidateResult(cand.id)], prefs, plugins)
            if nxt is not None:
                reasons += nxt.reasons
    return reasons


class LocaleCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.es, self.en = i18n.load_catalog("es"), i18n.load_catalog("en")

    def assertInBoth(self, key: str, params: dict | None = None) -> None:
        for lang, catalog in (("es", self.es), ("en", self.en)):
            self.assertIn(key, catalog, f"{lang}: {key}")
            if params is not None:
                catalog[key].format(**params)
                self.assertLessEqual(_fields(catalog[key]), set(params), f"{lang}: {key}")

    def test_literal_keys_in_wired_modules_exist(self) -> None:
        used = set()
        for name in _SOURCES:
            used |= set(_LITERAL_KEY.findall((PACKAGE / name).read_text(encoding="utf-8")))
        self.assertGreater(len(used), 150)
        for key in sorted(used):
            self.assertInBoth(key)

    def test_dynamic_key_families_exist(self) -> None:
        keys = set(live_tests.all_keys())
        keys |= {f"gui.test.{k}.{p}" for k in BASELINE_KINDS + CANDIDATE_KINDS for p in ("name", "desc")}
        keys |= {f"gui.step.{s.value}" for s in STEP_ORDER}
        keys |= {f"core.error.tablet_{k}" for k in ("name", "size", "rate", "pressure")}
        for field_name, enum in (("play_style", PlayStyle), ("map_preference", MapPreference),
                                 ("priority", Priority), ("skill_level", SkillLevel)):
            keys |= {f"core.pref.{field_name}.{m.value}" for m in enum}
        for cid in ("no-filter", *recommend.FILTERED_IDS, "keep-current"):
            keys |= {f"engine.candidate.{cid}.label", f"engine.candidate.{cid}.summary"}
        for spec in load_catalog().values():
            keys |= {spec.summary_key, spec.added_latency_note}
        for key in sorted(keys):
            self.assertInBoth(key)

    def test_every_engine_reason_formats_in_both_languages(self) -> None:
        seen = {}
        for reason in _engine_reasons():
            seen.setdefault(reason.key, reason.params)
        for key in ("engine.reason.area_rotation_kept", "engine.reason.frequency_default",
                    "engine.reason.refine_less_smoothing", "engine.reason.jitter_measured_high"):
            self.assertIn(key, seen)
        for key, params in sorted(seen.items()):
            self.assertInBoth(key, params)

    def test_otd_error_params_format(self) -> None:
        samples = {"path": "C:\\x\\settings.json", "detail": "boom", "name": "a?b"}
        for key in [k for k in self.en if k.startswith("otd.")]:
            self.assertInBoth(key, samples)


if __name__ == "__main__":
    unittest.main()
