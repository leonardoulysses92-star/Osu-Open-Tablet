"""profile.ProfileStore (validation, atomic save, history) and its use by Session (resume, saved options)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from osu_tablet_assistant import profile as pf
from osu_tablet_assistant.gui.session import SAVED_CANDIDATES, Step, SessionError
from osu_tablet_assistant.models import PlayStyle, Priority
from test_integration import TABLET_NAME, Harness


def setting(width: float = 100.0, source: str = "wizard", when: str = "2026-10-30 10:00:00") -> pf.SavedSetting:
    return pf.SavedSetting(when, source, {"width": width, "height": width * 0.5625, "x": 108.0, "y": 67.5,
                                          "rotation": 0.0},
                           [{"path": "TabletDriverFilters.Hawku.Smoothing", "enabled": True,
                             "settings": {"Latency": 2.0}}])


class StoreTests(unittest.TestCase):
    def store(self) -> pf.ProfileStore:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return pf.ProfileStore(Path(tmp.name) / "sub" / "profile.json")

    def test_round_trip(self) -> None:
        store = self.store()
        store.remember_setting("Wacom CTL-472", setting())
        store.profiles["wacomctl472"].prefs = {"play_style": "arm", "notices_jitter": True}
        store.remember_coach("Wacom CTL-472", 5, 38.0)
        self.assertTrue(store.save())
        again = pf.ProfileStore(store.path).load()
        prof = again.get("wacom ctl-472")
        self.assertEqual(prof.last.area["width"], 100.0)
        self.assertEqual(prof.last.filter_configs()[0].settings, {"Latency": 2.0})
        self.assertEqual(prof.prefs, {"play_style": "arm", "notices_jitter": True})
        self.assertEqual((prof.coach[0]["rounds"], prof.coach[0]["gain_pct"]), (5, 38.0))

    def test_missing_corrupt_and_hostile_files(self) -> None:
        store = self.store()
        self.assertEqual(store.load().profiles, {})
        store.path.parent.mkdir(parents=True)
        store.path.write_text("{broken", encoding="utf-8")
        self.assertEqual(store.load().profiles, {})
        self.assertTrue(store.path.with_name("profile.corrupt.json").exists())
        store.path.write_text(json.dumps({"profiles": [
            {"tablet_name": "A", "prefs": {"play_style": "nonsense", "priority": "latency", "notices_jitter": "x"},
             "display": {"width_px": -5, "height_px": 10},
             "history": [{"when": "t", "source": "hack", "area": {}}, {"when": "t", "source": "coach",
                         "area": {"width": 0, "height": 5, "x": 1, "y": 1}},
                         {"when": "t", "source": "live", "area": {"width": 50, "height": 30, "x": 40, "y": 20},
                          "filters": [{"path": 3}, {"path": "P", "settings": {"a": [1], "b": 2}}]}]},
            {"tablet_name": ""}, "junk", {"tablet_name": "B", "history": "no"}]}), encoding="utf-8")
        store.load()
        a = store.get("A")
        self.assertEqual(a.prefs, {"priority": "latency"})          # only valid values survive
        self.assertIsNone(a.display)
        self.assertEqual(len(a.history), 1)
        self.assertEqual(a.history[0].filters, [{"path": "P", "enabled": True, "settings": {"b": 2}}])
        self.assertEqual(store.get("B").history, [])
        self.assertEqual(len(store.profiles), 2)

    def test_history_is_deduped_newest_first_and_bounded(self) -> None:
        store = self.store()
        for i in range(20):
            store.remember_setting("T", setting(80 + i))
        store.remember_setting("T", setting(85, source="coach"))       # same setup as an older entry
        prof = store.get("T")
        self.assertEqual(len(prof.history), pf.MAX_HISTORY)
        self.assertEqual(prof.last.area["width"], 85)
        self.assertEqual(prof.last.source, "coach")
        self.assertEqual(sum(1 for h in prof.history if h.area["width"] == 85), 1)

    def test_forget_one_or_all_and_unwritable_folder(self) -> None:
        store = self.store()
        store.remember_setting("A", setting())
        store.remember_setting("B", setting())
        store.forget("a")
        self.assertIsNone(store.get("A"))
        self.assertIsNotNone(store.get("B"))
        store.forget()
        self.assertEqual(store.profiles, {})
        blocked = pf.ProfileStore(Path(tempfile.gettempdir()) / "x" / "\0bad" / "profile.json")
        blocked.remember_setting("A", setting())
        self.assertFalse(blocked.save())

    def test_latest_picks_the_newest(self) -> None:
        store = self.store()
        store.remember_setting("Old", setting())
        store.get("Old").updated = "2026-01-01 00:00:00"
        store.remember_setting("New", setting())
        store.get("New").updated = "2026-10-01 00:00:00"
        self.assertEqual(store.latest().tablet_name, "New")

    def test_no_play_data_is_part_of_the_file(self) -> None:
        store = self.store()
        store.remember_setting("A", setting())
        store.remember_coach("A", 3, None)
        store.save()
        text = store.path.read_text(encoding="utf-8").lower()
        for word in ("click", "sample", "cursor", "beatmap", "objects"):
            self.assertNotIn(word, text)


class SessionProfileTests(unittest.TestCase):
    def make(self):
        h = Harness(self, dry_run=False)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h.session.profile_store = pf.ProfileStore(Path(tmp.name) / "profile.json")
        return h, h.session

    def test_nothing_is_stored_without_a_store(self) -> None:
        s = Harness(self, dry_run=False).session
        self.assertFalse(s.remember_choices())
        self.assertFalse(s.remember_settings({}, "wizard"))
        self.assertFalse(s.forget_profile())

    def test_finishing_the_questions_remembers_the_choices(self) -> None:
        h, s = self.make()
        s.prefs.play_style = PlayStyle.ARM
        s.prefs.priority = Priority.SMOOTHNESS
        h.drive_to_testing(self)
        prof = s.profile_store.get(TABLET_NAME)
        self.assertEqual((prof.prefs["play_style"], prof.prefs["priority"]), ("wrist", "smoothness"))
        self.assertEqual(prof.display["width_px"], 1920.0)
        self.assertTrue(s.profile_store.path.exists())

    def test_apply_final_remembers_the_settings_and_a_second_run_resumes(self) -> None:
        h, s = self.make()
        h.drive_to_testing(self)
        cand = s.candidate("balanced")
        s.apply_candidate(cand, confirmed=True)
        for kind in ("stillness", "tracking", "aim"):
            s.run_measurement(None, kind, cand)
        s.choose("balanced")
        s.apply_final(confirmed=True)
        saved = s.profile_store.get(TABLET_NAME).last
        self.assertEqual(saved.source, "wizard")
        expected = cand.changes.tablet_area
        self.assertAlmostEqual(saved.area["width"], expected.width, places=3)
        # --- second run: a fresh session with the same store skips the questions and the baseline
        h2 = Harness(self, dry_run=False)
        s2 = h2.session
        s2.profile_store = pf.ProfileStore(s.profile_store.path).load()
        s2.accepted_disclaimer = True
        s2.resume_from_profile()
        self.assertIs(s2.step, Step.TESTING)
        self.assertEqual(s2.tablet.name, TABLET_NAME)
        self.assertEqual(s2.display.width_px, 1920)
        self.assertEqual(s2.baseline, [])
        ids = [c.id for c in s2.candidates]
        self.assertTrue(ids[0].startswith("saved:"), ids)
        self.assertLessEqual(sum(1 for i in ids if i.startswith("saved:")), SAVED_CANDIDATES)
        self.assertIs(s2.back(), Step.BASELINE)               # back still works through the skipped steps

    def test_a_saved_option_can_be_used_without_measuring(self) -> None:
        h, s = self.make()
        h.drive_to_testing(self)
        s.remember_settings(self._settings_with_area(h), "coach")
        s.generate_candidates()
        saved = [c for c in s.candidates if "saved" in c.tags]
        self.assertEqual(len(saved), 1)
        self.assertFalse(s.can_advance())
        s.choose(saved[0].id)
        self.assertTrue(s.can_advance())
        self.assertIs(s.best_candidate(), saved[0])

    def _settings_with_area(self, h) -> dict:
        import copy
        from osu_tablet_assistant.models import FilterConfig, ProfileChanges, Rect
        hawku = FilterConfig("TabletDriverFilters.Hawku.Smoothing", True, {"Latency": 2.0})   # an installed filter
        return h.session.services.apply_changes(
            copy.deepcopy(h.session.settings), TABLET_NAME,
            ProfileChanges(tablet_area=Rect(90.0, 50.625, 108.0, 67.5), filters=(hawku,)))

    def test_saved_options_that_no_longer_fit_are_skipped(self) -> None:
        h, s = self.make()
        h.drive_to_testing(self)
        prof = s.profile_store.ensure(TABLET_NAME)
        prof.history = [pf.SavedSetting("2026-10-01 00:00:00", "wizard",
                                        {"width": 500, "height": 300, "x": 100, "y": 50, "rotation": 0.0}, []),
                        pf.SavedSetting("2026-10-02 00:00:00", "wizard",
                                        {"width": 90, "height": 50, "x": 108, "y": 67, "rotation": 0.0},
                                        [{"path": "Not.Installed.Filter", "enabled": True, "settings": {}}])]
        s.saved_profile = prof
        self.assertEqual(s.saved_candidates(), [])

    def test_resume_without_a_matching_profile_explains_and_leaves_the_normal_flow(self) -> None:
        h, s = self.make()
        s.accepted_disclaimer = True
        with self.assertRaises(SessionError) as ctx:
            s.resume_from_profile()
        self.assertEqual(ctx.exception.key, "core.error.no_saved_profile")
        self.assertIs(s.step, Step.DETECT)
        s2 = Harness(self, dry_run=False).session
        with self.assertRaises(SessionError) as ctx2:
            s2.resume_from_profile()
        self.assertEqual(ctx2.exception.key, "core.error.accept_first")

    def test_forget_profile(self) -> None:
        h, s = self.make()
        h.drive_to_testing(self)
        self.assertTrue(s.forget_profile())
        self.assertEqual(pf.ProfileStore(s.profile_store.path).load().profiles, {})
        self.assertIsNone(s.saved_profile)


if __name__ == "__main__":
    unittest.main()
