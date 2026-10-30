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

    def test_preset_file_name(self) -> None:
        now = datetime(2026, 10, 30, 14, 5, 9)
        self.assertEqual(ac.preset_file_name(ac.DEFAULT_NAME_TEMPLATE, "Wacom CTL-472", now),
                         "Wacom CTL-472 2026-10-30 14-05-09")
        self.assertEqual(ac.preset_file_name("osu/{tablet}:{bad}", "A", now), "A 2026-10-30 14-05-09")
        self.assertEqual(ac.preset_file_name("x/y:z.", "A", now), "x y z")
        self.assertEqual(ac.preset_file_name("...", "A", now), "preset 2026-10-30 14-05-09")


if __name__ == "__main__":
    unittest.main()
