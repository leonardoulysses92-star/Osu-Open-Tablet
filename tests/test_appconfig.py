"""appconfig: the assistant's own config file (synthetic temp folders only)."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from osu_tablet_assistant import appconfig as ac


class AppConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "OsuOpenTablet" / "config.json"

    def test_paths(self) -> None:
        self.assertEqual(ac.app_data_dir("Windows", {"APPDATA": r"C:\Users\x\AppData\Roaming"}, Path("/h")),
                         Path(r"C:\Users\x\AppData\Roaming") / "OsuOpenTablet")
        self.assertEqual(ac.app_data_dir("Linux", {}, Path("/home/x")), Path("/home/x/.config/OsuOpenTablet"))

    def test_round_trip_and_defaults(self) -> None:
        self.assertEqual(ac.load_config(self.path), ac.AppConfig())
        cfg = ac.AppConfig(language="en", output_dir=r"C:\Users\x\Documents\opentablet",
                           plugin_policy="tune_only", check_updates_on_start=True)
        ac.save_config(cfg, self.path)
        self.assertEqual(ac.load_config(self.path), cfg)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        self.assertFalse(ac.AppConfig().check_updates_on_start)     # no network unless asked

    def test_corrupt_and_invalid_values(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(ac.load_config(self.path), ac.AppConfig())
        self.assertTrue((self.path.parent / "config.corrupt.json").is_file())
        self.path.write_text(json.dumps({"language": "fr", "plugin_policy": "install_everything",
                                         "update_url": "http://evil.example/", "output_dir": 5,
                                         "check_updates_on_start": "yes", "unknown": 1,
                                         "preset_name_template": "{tablet}"}), encoding="utf-8")
        cfg = ac.load_config(self.path)
        self.assertEqual((cfg.language, cfg.plugin_policy, cfg.update_url, cfg.output_dir,
                          cfg.check_updates_on_start), ("es", "installed", ac.DEFAULT_UPDATE_URL, None, False))
        self.assertEqual(cfg.preset_name_template, "{tablet}")
        self.path.write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(ac.load_config(self.path), ac.AppConfig())

    def test_click_keys_pen_and_detail_round_trip(self) -> None:
        cfg = ac.AppConfig(click_keys=["a", "mouse4"], pen_clicks=True, analysis_detail="very_fine")
        ac.save_config(cfg, self.path)
        loaded = ac.load_config(self.path)
        self.assertEqual((loaded.click_keys, loaded.pen_clicks, loaded.analysis_detail),
                         (["a", "mouse4"], True, "very_fine"))
        self.assertEqual(loaded.effective_click_keys, ("a", "mouse4", "mouse1"))
        defaults = ac.AppConfig()
        self.assertEqual((defaults.click_keys, defaults.pen_clicks, defaults.analysis_detail), (["z", "x"], False, "fine"))
        self.assertEqual(defaults.effective_click_keys, ("z", "x"))           # the pen tip is not a click by default

    def test_old_config_files_without_the_new_fields_still_load(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"language": "en", "plugin_policy": "tune_only", "version": 1}),
                             encoding="utf-8")
        cfg = ac.load_config(self.path)
        self.assertEqual((cfg.language, cfg.plugin_policy), ("en", "tune_only"))
        self.assertEqual((cfg.click_keys, cfg.pen_clicks, cfg.analysis_detail), (["z", "x"], False, "fine"))

    def test_click_keys_are_validated(self) -> None:
        cases = [({"click_keys": ["Z", "bogus", "z", "Mouse 4"]}, (["z", "mouse4"], False)),
                 ({"click_keys": "zx"}, (["z", "x"], False)),
                 ({"click_keys": [1, 2]}, (["z", "x"], False)),
                 ({"click_keys": []}, (["z", "x"], False)),                 # never zero ways to click
                 ({"click_keys": [], "pen_clicks": True}, ([], True)),       # the pen tip alone is fine
                 ({"click_keys": ["z", "mouse1"]}, (["z"], True)),          # mouse1 is the pen switch
                 ({"pen_clicks": "yes"}, (["z", "x"], False))]
        for data, expected in cases:
            cfg = ac.from_dict(data)
            self.assertEqual((cfg.click_keys, cfg.pen_clicks), expected, data)
        self.assertEqual(ac.from_dict({"analysis_detail": "ultra"}).analysis_detail, "fine")

    def test_preset_file_name(self) -> None:
        now = datetime(2026, 10, 30, 14, 5, 9)
        self.assertEqual(ac.preset_file_name(ac.DEFAULT_NAME_TEMPLATE, "Wacom CTL-472", now),
                         "Wacom CTL-472 2026-10-30 14-05-09")
        self.assertEqual(ac.preset_file_name("osu/{tablet}:{bad}", "A", now), "A 2026-10-30 14-05-09")
        self.assertEqual(ac.preset_file_name("x/y:z.", "A", now), "x y z")
        self.assertEqual(ac.preset_file_name("...", "A", now), "preset 2026-10-30 14-05-09")


if __name__ == "__main__":
    unittest.main()
