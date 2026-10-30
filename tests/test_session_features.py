"""Session: output folder, plugin policy, community presets, data updates (temp folders only)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from osu_tablet_assistant.appconfig import load_config
from osu_tablet_assistant.gui import i18n
from osu_tablet_assistant.gui.app import prepare_session
from osu_tablet_assistant.gui.session import (SessionError, Step, candidate_label, describe_changes,
                                              preset_label)
from osu_tablet_assistant.models import FilterConfig, PluginPolicy, ProfileChanges
from osu_tablet_assistant.otd import settings_io
from test_integration import Harness


class SessionFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(i18n.set_language, "es")
        i18n.set_language("en")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.h = Harness(self, dry_run=False)
        self.s = self.h.session

    def at_testing(self):
        s = self.h.drive_to_testing(self)
        s.choose(s.candidates[2].id)
        return s

    def test_export_to_output_folder_full_format_with_overwrite_backup(self) -> None:
        s = self.at_testing()
        out = self.tmp / "opentablet"
        s.set_output_dir(out)
        self.assertTrue(s.default_export_name().startswith("Wacom CTL-672 "))
        path = s.export_preset(confirmed=True, name="Osu")
        self.assertEqual(path, out / "Osu.json")
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(settings_io.is_full_settings(doc))
        self.assertEqual(self.h.events, ["backup_settings", "save_preset"])   # OTD backup first
        with self.assertRaises(SessionError) as ctx:
            s.export_preset(confirmed=True, name="Osu")
        self.assertEqual(ctx.exception.key, "otd.error.preset_exists")
        self.assertEqual(s.export_target("Osu"), path)
        s.export_preset(confirmed=True, name="Osu", overwrite=True)
        self.assertEqual(len(list((out / "Backup").glob("Osu-*.json"))), 1)
        self.assertEqual(self.h.settings_bytes(), self.h.original_bytes)      # real settings untouched
        s.set_output_dir(None)
        self.assertEqual(s.export_target().parent, s.config_dir / "Presets")

    def test_version_notice_after_detect(self) -> None:
        self.h.drive_to_testing(self)
        key, params = self.s.otd_notice
        self.assertEqual((key, params["version"]), ("otd.notice.old_version", "0.6.7"))

    def test_policies_through_the_session(self) -> None:
        s = self.h.drive_to_testing(self)
        s.set_plugin_policy(PluginPolicy.TUNE_ONLY)
        s.ensure_candidates()
        ids = [c.id for c in s.candidates]
        self.assertNotIn("no-filter", ids)
        current = [f.path for f in s.profile.filters]
        for cand in s.candidates:
            self.assertEqual([f.path for f in cand.changes.filters], current, cand.id)
        self.assertEqual(s.plugin_suggestions(), [])
        s.set_plugin_policy(PluginPolicy.ALLOW_NEW)
        suggestions = s.plugin_suggestions()
        self.assertTrue(suggestions)
        for item in suggestions:
            self.assertNotIn(item.path, s.filter_paths)
            self.assertTrue(item.homepage.startswith("https://"))

    def test_starting_presets_follow_the_policy_and_lead_the_candidates(self) -> None:
        s = self.h.drive_to_testing(self)
        options = s.refresh_starting_presets()
        self.assertTrue(options)
        for option in options:
            self.assertTrue(option.id.startswith("community:"))
            self.assertLessEqual(option.preset.required_paths, s.filter_paths)
            self.assertIn("Source", option.summary)
        s.starting_preset_id = options[-1].id
        s.ensure_candidates()
        first = s.candidates[0]
        self.assertEqual(first.id, options[-1].id)
        self.assertIn(options[-1].preset.name, candidate_label(s, first))
        s.set_plugin_policy(PluginPolicy.TUNE_ONLY)
        tuned = s.refresh_starting_presets()
        mine = {f.path for f in s.profile.filters if f.enabled}
        self.assertTrue(all(o.preset.required_paths <= mine for o in tuned))
        if s.starting_preset_id is not None:
            self.assertIn(s.starting_preset_id, {o.id for o in tuned})

    def test_comparison_report_and_labels(self) -> None:
        s = self.at_testing()
        best = s.best_candidate()
        rows = s.community_comparison(best)
        self.assertEqual(len(rows), 3)
        self.assertIn("Plugins:", s.report_text())
        community = next(c for c in s.candidates if c.id.startswith("community:"))
        self.assertNotIn("{name}", candidate_label(s, community))
        refined = SimpleNamespace(id=community.id + "|OuterRadius:0.5>0.6", label_key=community.label_key,
                                  reasons=())
        self.assertEqual(candidate_label(s, refined), candidate_label(s, community))
        user = next(p for p in s.community_presets() if p.attribution == "user")
        self.assertIn("Your own values", preset_label(user, {}))

    def test_describe_changes_lists_each_parameter(self) -> None:
        s = self.h.drive_to_testing(self)
        current = s.profile
        first = current.filters[0]
        tuned = FilterConfig(first.path, True, dict(first.settings, Latency=12.5))
        lines = describe_changes(current, ProfileChanges(filters=(tuned,)), lambda p: p, detail=True)
        params = [p for k, p in lines if k == "gui.change.param"]
        self.assertEqual([(p["param"], p["new"]) for p in params], [("Latency", "12.5")])
        self.assertFalse([k for k, _ in describe_changes(current, ProfileChanges(filters=(tuned,)),
                                                          lambda p: p) if k == "gui.change.param"])

    def test_data_update_flow(self) -> None:
        with self.assertRaises(SessionError):
            self.s.update_community_data()
        self.s.data_cache_dir = self.tmp / "data"
        calls = []

        def fake_update(url, cache_dir):
            calls.append((url, cache_dir))
            return SimpleNamespace(ok=True, key="core.update.up_to_date", params={"date": "x"}, detail="",
                                   bundle=None)
        self.s.services.update_community_data = fake_update
        result = self.s.update_community_data()
        self.assertTrue(result.ok)
        self.assertEqual(calls, [(self.s.app_config.update_url, self.tmp / "data")])
        self.assertIsNotNone(self.s.app_config.last_update_check)
        status = self.s.community_status()
        self.assertEqual(status["origin"], "bundled")

    def test_prepare_session_persists_config(self) -> None:
        app_dir = self.tmp / "OsuOpenTablet"
        (app_dir).mkdir()
        (app_dir / "config.json").write_text(json.dumps({"plugin_policy": "tune_only"}), encoding="utf-8")
        prepare_session(self.s, app_dir)
        self.assertIs(self.s.prefs.plugin_policy, PluginPolicy.TUNE_ONLY)
        self.assertIsNone(self.s.data_bundle)          # no cache yet -> packaged data
        self.s.set_plugin_policy(PluginPolicy.ALLOW_NEW)
        self.s.set_output_dir(self.tmp / "out")
        cfg = load_config(app_dir / "config.json")
        self.assertEqual((cfg.plugin_policy, cfg.output_dir), ("allow_new", str(self.tmp / "out")))
        self.assertIs(self.s.step, Step.WELCOME)


if __name__ == "__main__":
    unittest.main()
