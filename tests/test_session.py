"""Session state machine, driven entirely by fake services."""
from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from osu_tablet_assistant.gui import i18n
from osu_tablet_assistant.gui.session import (
    CANDIDATE_KINDS,
    MAX_REFINE_ROUNDS,
    ConfirmationRequired,
    Services,
    Session,
    SessionError,
    Step,
)
from osu_tablet_assistant.knowledge import tablets as tablet_db
from osu_tablet_assistant.models import (
    ApplyResult,
    Candidate,
    CandidateResult,
    DisplaySpec,
    FilterConfig,
    MetricsReport,
    PlayStyle,
    ProfileChanges,
    ProfileInfo,
    Reason,
    Rect,
)

CONFIG = Path("cfg")
AREA = Rect(152.0, 95.0, 76.0, 47.5)


def profile(name: str = "Wacom CTL-472") -> ProfileInfo:
    return ProfileInfo(name, "Absolute", Rect(1920, 1080, 960, 540), AREA, True, ())


def candidate(cid: str, width: float = 100.0) -> Candidate:
    area = Rect(width, width * 0.625, 76.0, 47.5)
    return Candidate(cid, f"engine.{cid}.label", f"engine.{cid}.summary",
                     ProfileChanges(tablet_area=area, filters=(FilterConfig("Some.Filter", True, {"Latency": 2}),)),
                     (Reason("engine.reason.test", {"n": 1}),), 2.0)


class FakeController:
    def __init__(self, calls: list[str], ok: bool = True) -> None:
        self.calls, self.ok, self.applied = calls, ok, []

    def apply(self, settings: dict) -> ApplyResult:
        self.calls.append("apply")
        self.applied.append(settings)
        return ApplyResult(self.ok, "console", "otd.apply.ok_console" if self.ok else "otd.apply.failed", "d")


class Fixture:
    """A Session wired to recording fakes."""

    def __init__(self, dry_run: bool = False, profiles: list[ProfileInfo] | None = None) -> None:
        self.calls: list[str] = []
        self.controller = FakeController(self.calls)
        self.tests: list[tuple[str, float]] = []
        self.refine_queue = [candidate("refined", 90.0)]
        self.profiles = [profile()] if profiles is None else profiles
        self.fail: set[str] = set()
        self.generated = 0
        self.services = Services(
            find_config_dir=lambda: CONFIG,
            find_install_dir=lambda: None,
            load_settings=self.load_settings,
            list_profiles=lambda settings: self.profiles,
            apply_changes=self.apply_changes,
            backup_settings=self.backup,
            save_preset=self.save_preset,
            write_settings=lambda cfg, settings: self.calls.append("write"),
            list_installed_plugins=lambda cfg: [],
            available_filter_paths=lambda cfg: {"Some.Filter"},
            make_controller=lambda install, cfg: self.controller,
            generate_candidates=self.generate,
            rank=self.rank,
            refine=self.refine,
            load_filter_catalog=lambda: {},
            run_test=self.run_test,
            list_known_tablets=lambda install: tablet_db.builtin_tablets(),
            detect_display=lambda: DisplaySpec(1920, 1080, 960, 540),
            desktop_origin=lambda: (0.0, 0.0),
            config_locations=lambda **_kw: [],
        )
        self.session = Session(self.services, dry_run=dry_run)

    def load_settings(self, cfg: Path) -> dict:
        if "load" in self.fail:
            raise ValueError("broken json")
        return {"Profiles": []}

    def apply_changes(self, settings: dict, name: str, changes: ProfileChanges) -> dict:
        self.calls.append(f"changes:{name}")
        return {"name": name, "area": changes.tablet_area}

    def backup(self, cfg: Path) -> Path:
        self.calls.append("backup")
        return Path("backup.json")

    def save_preset(self, cfg: Path, name: str, settings: dict) -> Path:
        self.calls.append("preset")
        return cfg / "Presets" / f"{name}.json"

    def generate(self, *args: Any) -> list[Candidate]:
        self.generated += 1
        self.generate_args = args
        if "generate" in self.fail:
            raise RuntimeError("engine down")
        return [candidate("a"), candidate("b", 120.0)]

    def rank(self, results: list[CandidateResult], prefs: Any) -> list[CandidateResult]:
        for r in results:
            r.score = float(len(r.reports)) + (r.user_rating or 0) + (10 if r.candidate_id == "b" else 0)
        return sorted(results, key=lambda r: r.score or 0, reverse=True)

    def refine(self, best: Candidate, results: Any, prefs: Any, paths: set[str]) -> Candidate | None:
        return self.refine_queue.pop(0) if self.refine_queue else None

    def run_test(self, parent: Any, kind: str, mm_per_px: float, display: Any, on_done: Any, on_cancel: Any) -> None:
        self.tests.append((kind, mm_per_px))
        on_done(MetricsReport(kind, 100, 5.0, effective_rate_hz=200.0 if kind == "rate" else None))

    # --- scripted journeys
    def reach_tablet(self) -> Session:
        s = self.session
        s.accepted_disclaimer = True
        s.advance()
        s.detect()
        s.advance()
        return s

    def reach_testing(self) -> Session:
        s = self.reach_tablet()
        s.select_choice(s.tablet_choices()[0])
        s.advance()
        s.set_display(1920, 1080)
        s.advance()
        s.advance()
        s.advance()
        return s

    def measure(self, cid: str) -> None:
        cand = self.session.candidate(cid)
        for kind in CANDIDATE_KINDS:
            self.session.run_measurement(None, kind, cand)


class DetectTests(unittest.TestCase):
    def test_detect_loads_profiles_and_plugins(self) -> None:
        fx = Fixture()
        fx.session.detect()
        s = fx.session
        self.assertEqual(s.config_dir, CONFIG)
        self.assertEqual(len(s.profiles), 1)
        self.assertEqual(s.filter_paths, {"Some.Filter"})
        self.assertTrue(len(s.known_tablets) >= 30)

    def test_explicit_folders_win(self) -> None:
        fx = Fixture()
        fx.services.find_config_dir = lambda: self.fail("must not search")
        fx.session.detect(Path("mine"), Path("prog"))
        self.assertEqual(fx.session.config_dir, Path("mine"))
        self.assertEqual(fx.session.install_dir, Path("prog"))

    def test_missing_config_dir(self) -> None:
        fx = Fixture()
        fx.services.find_config_dir = lambda: None
        with self.assertRaises(SessionError) as ctx:
            fx.session.detect()
        self.assertEqual(ctx.exception.key, "core.error.no_config_dir")
        self.assertIsNone(fx.session.settings)

    def test_unreadable_settings_become_session_error_with_detail(self) -> None:
        fx = Fixture()
        fx.fail.add("load")
        with self.assertRaises(SessionError) as ctx:
            fx.session.detect()
        self.assertEqual(ctx.exception.key, "core.error.read_settings")
        self.assertIn("broken json", ctx.exception.detail)

    def test_translatable_service_errors_keep_their_own_key(self) -> None:
        class Bad(Exception):
            key, params = "otd.error.settings_missing", {"path": "x"}

        fx = Fixture()
        fx.services.load_settings = lambda cfg: (_ for _ in ()).throw(Bad("m"))
        with self.assertRaises(SessionError) as ctx:
            fx.session.detect()
        self.assertEqual(ctx.exception.key, "otd.error.settings_missing")
        self.assertEqual(ctx.exception.params, {"path": "x"})


class NavigationTests(unittest.TestCase):
    def test_steps_are_gated(self) -> None:
        fx = Fixture()
        s = fx.session
        self.assertFalse(s.can_advance())
        with self.assertRaises(SessionError):
            s.advance()
        s.accepted_disclaimer = True
        s.advance()
        self.assertEqual(s.step, Step.DETECT)
        self.assertFalse(s.can_advance())
        s.detect()
        s.advance()
        self.assertEqual(s.step, Step.TABLET)
        self.assertFalse(s.can_advance())
        s.select_choice(s.tablet_choices()[0])
        self.assertTrue(s.can_advance())

    def test_back_returns_through_history(self) -> None:
        fx = Fixture()
        s = fx.reach_tablet()
        self.assertTrue(s.can_go_back())
        self.assertEqual(s.back(), Step.DETECT)
        self.assertEqual(s.back(), Step.WELCOME)
        self.assertFalse(s.can_go_back())

    def test_full_journey_reaches_result(self) -> None:
        fx = Fixture()
        s = fx.reach_testing()
        self.assertEqual(s.step, Step.TESTING)
        self.assertFalse(s.can_advance())
        fx.measure("a")
        self.assertTrue(s.can_advance())
        s.advance()
        self.assertEqual(s.step, Step.RESULT)
        self.assertFalse(s.can_advance())

    def test_generation_error_keeps_step(self) -> None:
        fx = Fixture()
        fx.fail.add("generate")
        s = fx.reach_tablet()
        s.select_choice(s.tablet_choices()[0])
        s.advance()
        s.set_display(1920, 1080)
        s.advance()
        s.advance()
        with self.assertRaises(SessionError) as ctx:
            s.advance()
        self.assertEqual(ctx.exception.key, "core.error.generate")
        self.assertEqual(s.step, Step.BASELINE)

    def test_candidates_regenerate_only_when_inputs_change(self) -> None:
        fx = Fixture()
        s = fx.reach_testing()
        self.assertEqual(fx.generated, 1)
        s.back()
        s.advance()
        self.assertEqual(fx.generated, 1)
        s.back()
        s.prefs.play_style = PlayStyle.ARM
        s.advance()
        self.assertEqual(fx.generated, 2)


class ChoiceTests(unittest.TestCase):
    def test_profile_maps_to_builtin_tablet(self) -> None:
        fx = Fixture()
        fx.session.detect()
        first = fx.session.tablet_choices()[0]
        self.assertEqual(first.profile, fx.profiles[0])
        self.assertEqual(first.tablet.name, "Wacom CTL-472")
        self.assertTrue(fx.session.select_choice(first))
        self.assertEqual(fx.session.profile, fx.profiles[0])
        names = [c.label for c in fx.session.tablet_choices()]
        self.assertEqual(names.count("Wacom CTL-472"), 1)

    def test_unknown_profile_needs_custom_entry(self) -> None:
        fx = Fixture(profiles=[profile("Mystery Pad 9000")])
        fx.session.detect()
        choice = fx.session.tablet_choices()[0]
        self.assertIsNone(choice.tablet)
        self.assertFalse(fx.session.select_choice(choice))
        self.assertFalse(fx.session.can_advance())
        fx.session.set_custom_tablet("Mystery Pad 9000", 200, 125, 200)
        self.assertEqual(fx.session.profile.tablet_name, "Mystery Pad 9000")
        fx.session.step = Step.TABLET
        self.assertTrue(fx.session.can_advance())

    def test_builtin_tablet_without_profile_needs_linking(self) -> None:
        fx = Fixture(profiles=[profile("Something Else")])
        s = fx.session
        s.detect()
        spec = next(c for c in s.tablet_choices() if c.label == "Wacom CTL-672")
        s.select_choice(spec)
        s.step = Step.TABLET
        self.assertIsNone(s.profile)
        self.assertFalse(s.can_advance())
        s.link_profile(s.profiles[0])
        self.assertTrue(s.can_advance())

    def test_invalid_custom_tablet(self) -> None:
        fx = Fixture()
        with self.assertRaises(SessionError) as ctx:
            fx.session.set_custom_tablet("x", 0, 10, 100)
        self.assertEqual(ctx.exception.key, "core.error.tablet_size")

    def test_manual_display_is_center_based(self) -> None:
        fx = Fixture()
        d = fx.session.set_display(2560, 1440, left=1920, top=0, refresh_hz=144)
        self.assertEqual((d.offset_x, d.offset_y, d.refresh_hz), (3200.0, 720.0, 144.0))
        with self.assertRaises(SessionError):
            fx.session.set_display(0, 100)

    def test_detect_display_uses_service(self) -> None:
        fx = Fixture()
        self.assertEqual(fx.session.detect_display().width_px, 1920)
        fx.services.detect_display = lambda: None
        self.assertIsNone(fx.session.detect_display())
        self.assertEqual(fx.session.display.width_px, 1920)


class MeasurementTests(unittest.TestCase):
    def test_baseline_scale_and_replacement(self) -> None:
        fx = Fixture()
        s = fx.reach_tablet()
        s.select_choice(s.tablet_choices()[0])
        s.set_display(1520, 1080)
        s.run_measurement(None, "rate")
        s.run_measurement(None, "rate")
        # The current area maps onto the profile's own display area (1920 px wide),
        # not onto the monitor typed in the display step.
        self.assertEqual(fx.tests[0], ("rate", AREA.width / 1920))
        self.assertEqual(len(s.baseline), 1)
        self.assertEqual(s.baseline_report("rate").effective_rate_hz, 200.0)
        self.assertIsNone(s.baseline_report("stillness"))

    def test_scale_fallbacks(self) -> None:
        empty = ProfileInfo("Wacom CTL-472", "Absolute", Rect(0, 0, 0, 0), Rect(0, 0, 0, 0), True, ())
        fx = Fixture(profiles=[empty])
        s = fx.reach_tablet()
        s.select_choice(s.tablet_choices()[0])
        s.set_display(1520, 1080)
        s.run_measurement(None, "stillness")
        # never configured: OTD maps the whole tablet onto the whole display
        self.assertEqual(fx.tests[-1], ("stillness", s.tablet.width_mm / 1520))

    def test_candidate_scale_uses_candidate_area(self) -> None:
        fx = Fixture()
        s = fx.reach_testing()
        fx.measure("a")
        self.assertEqual(fx.tests[-1], ("aim", 100.0 / 1920))
        self.assertTrue(s.is_complete("a"))
        self.assertFalse(s.is_complete("b"))
        self.assertEqual(s.next_candidate().id, "b")

    def test_baseline_is_passed_to_engine(self) -> None:
        fx = Fixture()
        s = fx.reach_tablet()
        s.select_choice(s.tablet_choices()[0])
        s.advance()
        s.set_display(1920, 1080)
        s.advance()
        s.advance()
        s.run_measurement(None, "rate")
        s.advance()
        baseline_arg = fx.generate_args[4]
        self.assertEqual(baseline_arg[0].test_kind, "rate")

    def test_engine_gets_display_in_otd_coordinates(self) -> None:
        """A monitor left of/above the primary shifts OTD's origin (seen on a real 0.6.7 setup)."""
        fx = Fixture()
        fx.services.desktop_origin = lambda: (-1080.0, -127.0)
        s = fx.reach_testing()
        display = fx.generate_args[1]
        self.assertEqual((display.offset_x, display.offset_y), (960.0 + 1080.0, 540.0 + 127.0))
        self.assertEqual(s.display.offset_x, 960.0)          # test windows keep OS coordinates
        fx.measure("a")
        self.assertEqual([k for k, _ in fx.tests], list(CANDIDATE_KINDS))

    def test_rating_validation(self) -> None:
        fx = Fixture()
        s = fx.reach_testing()
        s.set_rating("a", 4)
        self.assertEqual(s.results["a"].user_rating, 4)
        s.set_rating("a", None)
        self.assertIsNone(s.results["a"].user_rating)
        with self.assertRaises(SessionError):
            s.set_rating("a", 6)

    def test_unknown_candidate(self) -> None:
        fx = Fixture()
        s = fx.reach_testing()
        with self.assertRaises(SessionError):
            s.candidate("nope")


class RefineTests(unittest.TestCase):
    def test_refine_adds_candidate_then_converges(self) -> None:
        fx = Fixture()
        s = fx.reach_testing()
        with self.assertRaises(SessionError):
            s.refine()
        fx.measure("a")
        new = s.refine()
        self.assertEqual(new.id, "refined")
        self.assertIn("refined", [c.id for c in s.candidates])
        self.assertIsNone(s.refine())
        self.assertTrue(s.converged)

    def test_refine_stops_on_repeated_candidate(self) -> None:
        fx = Fixture()
        s = fx.reach_testing()
        fx.measure("a")
        fx.refine_queue = [candidate("a")]
        self.assertIsNone(s.refine())

    def test_refine_again_before_measuring_offers_same_candidate(self) -> None:
        fx = Fixture()
        s = fx.reach_testing()
        fx.measure("a")
        fx.refine_queue = [candidate("r1"), candidate("r1")]
        first = s.refine()
        self.assertIs(s.refine(), first)       # engine repeats itself until r1 is measured
        self.assertFalse(s.converged)
        self.assertEqual([c.id for c in s.candidates].count("r1"), 1)
        self.assertEqual(s.refine_rounds, 1)

    def test_refine_round_limit(self) -> None:
        fx = Fixture()
        s = fx.reach_testing()
        fx.measure("a")
        fx.refine_queue = [candidate(f"r{i}") for i in range(MAX_REFINE_ROUNDS + 3)]
        added = 0
        while s.refine() is not None:
            added += 1
        self.assertEqual(added, MAX_REFINE_ROUNDS)

    def test_best_follows_ranking_unless_chosen(self) -> None:
        fx = Fixture()
        s = fx.reach_testing()
        fx.measure("a")
        fx.measure("b")
        self.assertEqual(s.best_candidate().id, "b")
        s.choose("a")
        self.assertEqual(s.best_candidate().id, "a")


class StateChangeTests(unittest.TestCase):
    def test_apply_requires_confirmation_and_changes_nothing(self) -> None:
        fx = Fixture()
        s = fx.reach_testing()
        with self.assertRaises(ConfirmationRequired):
            s.apply_candidate(s.candidate("a"), confirmed=False)
        with self.assertRaises(ConfirmationRequired):
            s.export_preset(confirmed=False)
        with self.assertRaises(ConfirmationRequired):
            s.apply_final(confirmed=False)
        with self.assertRaises(ConfirmationRequired):
            s.restore_original(confirmed=False)
        self.assertEqual(fx.calls, [])

    def test_backup_happens_once_and_first(self) -> None:
        fx = Fixture()
        s = fx.reach_testing()
        s.apply_candidate(s.candidate("a"), confirmed=True)
        s.apply_candidate(s.candidate("b"), confirmed=True)
        self.assertEqual(fx.calls, ["backup", "changes:Wacom CTL-472", "apply",
                                    "changes:Wacom CTL-472", "apply"])
        self.assertEqual(s.live_candidate_id, "b")
        self.assertTrue(s.has_unfinished_changes)

    def test_failed_apply_is_reported_not_live(self) -> None:
        fx = Fixture()
        fx.controller.ok = False
        s = fx.reach_testing()
        result = s.apply_candidate(s.candidate("a"), confirmed=True)
        self.assertFalse(result.ok)
        self.assertIsNone(s.live_candidate_id)

    def test_restore_original_pushes_initial_settings(self) -> None:
        fx = Fixture()
        s = fx.reach_testing()
        s.apply_candidate(s.candidate("a"), confirmed=True)
        s.restore_original(confirmed=True)
        self.assertEqual(fx.controller.applied[-1], {"Profiles": []})
        self.assertFalse(s.has_unfinished_changes)

    def test_final_apply_order(self) -> None:
        fx = Fixture()
        s = fx.reach_testing()
        fx.measure("a")
        result = s.apply_final(confirmed=True)
        self.assertTrue(result.ok)
        self.assertLess(fx.calls.index("backup"), fx.calls.index("write"))
        self.assertLess(fx.calls.index("write"), fx.calls.index("apply"))
        self.assertFalse(s.has_unfinished_changes)

    def test_restore_after_final_apply_rewrites_settings_file(self) -> None:
        fx = Fixture()
        written: list[dict] = []
        fx.services.write_settings = lambda cfg, settings: written.append(settings)
        s = fx.reach_testing()
        fx.measure("a")
        s.apply_final(confirmed=True)
        s.restore_original(confirmed=True)
        self.assertEqual(written[-1], {"Profiles": []})     # original back on disk
        self.assertEqual(fx.controller.applied[-1], {"Profiles": []})
        self.assertIsNone(s.applied)
        s.restore_original(confirmed=True)                    # nothing left to rewrite
        self.assertEqual(len(written), 2)

    def test_restore_without_final_apply_never_writes(self) -> None:
        fx = Fixture()
        s = fx.reach_testing()
        s.apply_candidate(s.candidate("a"), confirmed=True)
        s.restore_original(confirmed=True)
        self.assertNotIn("write", fx.calls)

    def test_export_preset(self) -> None:
        fx = Fixture()
        s = fx.reach_testing()
        fx.measure("b")
        path = s.export_preset(confirmed=True)
        self.assertEqual(path.parent, CONFIG / "Presets")
        self.assertIn("Wacom CTL-472", path.name)
        self.assertLess(fx.calls.index("backup"), fx.calls.index("preset"))

    def test_dry_run_never_applies(self) -> None:
        fx = Fixture(dry_run=True)
        s = fx.reach_testing()
        self.assertFalse(s.can_measure)
        self.assertFalse(s.can_advance())
        s.choose("b")
        self.assertTrue(s.can_advance())
        for action in (lambda: s.apply_candidate(s.candidate("a"), confirmed=True),
                       lambda: s.apply_final(confirmed=True),
                       lambda: s.restore_original(confirmed=True)):
            with self.assertRaises(SessionError) as ctx:
                action()
            self.assertEqual(ctx.exception.key, "core.error.dry_run")
        s.export_preset(confirmed=True)
        self.assertNotIn("apply", fx.calls)
        self.assertEqual(fx.calls.count("preset"), 1)

    def test_controller_failure_is_wrapped(self) -> None:
        fx = Fixture()
        fx.services.make_controller = lambda install, cfg: (_ for _ in ()).throw(OSError("no exe"))
        s = fx.reach_testing()
        with self.assertRaises(SessionError) as ctx:
            s.apply_candidate(s.candidate("a"), confirmed=True)
        self.assertEqual(ctx.exception.key, "core.error.controller")


class ReportTests(unittest.TestCase):
    def tearDown(self) -> None:
        i18n.set_language("es")

    def test_report_mentions_setup_choice_and_disclaimer(self) -> None:
        i18n.set_language("en")
        fx = Fixture()
        s = fx.reach_testing()
        fx.measure("a")
        s.set_rating("a", 5)
        s.apply_candidate(s.candidate("a"), confirmed=True)
        text = s.report_text()
        for needle in ("Wacom CTL-472", "1920 x 1080", "100.0 x 62.5 mm", "Some.Filter", "Latency=2",
                       "about 2.0 ms", "Your feel rating: 5 / 5", "not a guaranteed optimum",
                       "backup.json"):
            self.assertIn(needle, text)
        self.assertNotIn("core.", text)

    def test_report_needs_a_choice(self) -> None:
        fx = Fixture()
        s = fx.reach_testing()
        with self.assertRaises(SessionError):
            s.report_text()


if __name__ == "__main__":
    unittest.main()
