"""Tests for osu_tablet_assistant.otd.settings_io (fixtures and temp dirs only)."""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from osu_tablet_assistant.models import FilterConfig, ProfileChanges, Rect
from osu_tablet_assistant.otd import settings_io as sio

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "settings_sample.json"
LOCALES = ROOT / "osu_tablet_assistant" / "locales"
OTD_SRC = ROOT / "osu_tablet_assistant" / "otd"


def sample() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class TempConfigCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def with_fixture(self) -> Path:
        shutil.copy(FIXTURE, self.dir / sio.SETTINGS_FILE)
        return self.dir


class LoadSettingsTest(TempConfigCase):
    def test_loads_fixture(self) -> None:
        data = sio.load_settings(self.with_fixture())
        self.assertEqual(data["Revision"], "0.6.7")
        self.assertEqual(len(data["Profiles"]), 2)

    def test_accepts_utf8_bom(self) -> None:
        (self.dir / sio.SETTINGS_FILE).write_bytes(b"\xef\xbb\xbf" + FIXTURE.read_bytes())
        self.assertIn("Profiles", sio.load_settings(self.dir))

    def test_missing_file(self) -> None:
        with self.assertRaises(sio.OtdSettingsError) as ctx:
            sio.load_settings(self.dir)
        self.assertEqual(ctx.exception.key, "otd.error.settings_missing")

    def test_malformed_json(self) -> None:
        (self.dir / sio.SETTINGS_FILE).write_text('{"Profiles": [', encoding="utf-8")
        with self.assertRaises(sio.OtdSettingsError) as ctx:
            sio.load_settings(self.dir)
        self.assertEqual(ctx.exception.key, "otd.error.settings_invalid_json")

    def test_not_an_object(self) -> None:
        (self.dir / sio.SETTINGS_FILE).write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(sio.OtdSettingsError) as ctx:
            sio.load_settings(self.dir)
        self.assertEqual(ctx.exception.key, "otd.error.settings_not_object")


class ListProfilesTest(unittest.TestCase):
    def test_parses_profiles(self) -> None:
        wacom, huion = sio.list_profiles(sample())
        self.assertEqual(wacom.tablet_name, "Wacom CTL-672")
        self.assertEqual(wacom.output_mode_path, "OpenTabletDriver.Desktop.Output.AbsoluteMode")
        self.assertEqual(wacom.display, Rect(1920.0, 1080.0, 960.0, 540.0, 0.0))
        self.assertEqual(wacom.tablet_area, Rect(100.0, 56.25, 108.0, 67.5, 0.0))
        self.assertTrue(wacom.lock_aspect_ratio)
        self.assertEqual([f.path for f in wacom.filters], [
            "TabletDriverFilters.Hawku.Smoothing", "OpenTabletDriver.Plugin.NoiseReduction",
            "TabletDriverFilters.Devocub.Antichatter"])
        self.assertFalse(wacom.filters[1].enabled)
        self.assertEqual(wacom.filters[1].settings, {"Samples": 10, "DistanceThreshold": 0.5})
        self.assertIsInstance(wacom.filters[1].settings["Samples"], int)
        self.assertIs(wacom.filters[2].settings["PredictionEnabled"], False)
        self.assertEqual(huion.tablet_area.rotation, 180.0)
        self.assertFalse(huion.lock_aspect_ratio)

    def test_malformed_input_is_tolerated(self) -> None:
        self.assertEqual(sio.list_profiles({}), [])
        self.assertEqual(sio.list_profiles({"Profiles": "nope"}), [])
        broken = {"Profiles": [None, 5, {"NoTablet": 1},
                               {"Tablet": "T", "AbsoluteModeSettings": None,
                                "Filters": [None, {"Path": 3}, {"Path": "A", "Settings": [None, {"Value": 1}]}],
                                "OutputMode": None}]}
        (prof,) = sio.list_profiles(broken)
        self.assertEqual(prof.tablet_name, "T")
        self.assertEqual(prof.display, Rect(0.0, 0.0, 0.0, 0.0, 0.0))
        self.assertEqual(prof.output_mode_path, "")
        self.assertEqual(prof.filters, (FilterConfig("A", True, {}),))


class ApplyChangesTest(unittest.TestCase):
    def test_pure_and_deep_copy(self) -> None:
        original = sample()
        snapshot = json.dumps(original, sort_keys=True)
        new = sio.apply_changes(original, "Wacom CTL-672", ProfileChanges(
            tablet_area=Rect(90, 50.625, 100, 60), filters=()))
        self.assertEqual(json.dumps(original, sort_keys=True), snapshot)
        self.assertIsNot(new["Profiles"][1], original["Profiles"][1])
        self.assertEqual(new["Profiles"][1], original["Profiles"][1])

    def test_unknown_tablet_raises_keyerror(self) -> None:
        with self.assertRaises(KeyError) as ctx:
            sio.apply_changes(sample(), "wacom ctl-672", ProfileChanges())
        self.assertIn("wacom ctl-672", str(ctx.exception))
        with self.assertRaises(KeyError):
            sio.apply_changes({}, "X", ProfileChanges())

    def test_no_changes_is_identity(self) -> None:
        self.assertEqual(sio.apply_changes(sample(), "Huion H640P", ProfileChanges()), sample())

    def test_area_and_lock(self) -> None:
        new = sio.apply_changes(sample(), "Huion H640P", ProfileChanges(
            tablet_area=Rect(70, 39.375, 75, 45, 180), display_area=Rect(2560, 1440, 1280, 720),
            lock_aspect_ratio=True))
        abs_mode = new["Profiles"][1]["AbsoluteModeSettings"]
        self.assertEqual(abs_mode["Tablet"], {"Width": 70.0, "Height": 39.375, "X": 75.0, "Y": 45.0,
                                              "Rotation": 180.0})
        self.assertIsInstance(abs_mode["Tablet"]["Width"], float)
        self.assertTrue(abs_mode["LockAspectRatio"])
        self.assertFalse(abs_mode["EnableClipping"])   # untouched
        self.assertEqual(new["Profiles"][0], sample()["Profiles"][0])

    def test_only_lock_changes(self) -> None:
        new = sio.apply_changes(sample(), "Wacom CTL-672", ProfileChanges(lock_aspect_ratio=False))
        expected = sample()
        expected["Profiles"][0]["AbsoluteModeSettings"]["LockAspectRatio"] = False
        self.assertEqual(new, expected)

    def test_creates_missing_sections(self) -> None:
        settings = {"Profiles": [{"Tablet": "T", "AbsoluteModeSettings": None}]}
        new = sio.apply_changes(settings, "T", ProfileChanges(display_area=Rect(1, 2, 3, 4)))
        self.assertEqual(new["Profiles"][0]["AbsoluteModeSettings"]["Display"]["Height"], 2.0)

    def test_filter_roundtrip_is_lossless(self) -> None:
        original = sample()
        for profile in sio.list_profiles(original):
            new = sio.apply_changes(original, profile.tablet_name, ProfileChanges(filters=profile.filters))
            self.assertEqual(new, original)

    def test_filter_replacement_keeps_unknown_and_unmentioned(self) -> None:
        new = sio.apply_changes(sample(), "Wacom CTL-672", ProfileChanges(filters=(
            FilterConfig("TabletDriverFilters.Devocub.Antichatter", False, {"AntichatterStrength": 5.5}),
            FilterConfig("RadialFollow.RadialFollowSmoothingScreenSpace", True, {"OuterRadius": 5.0, "Enabled": True}),
        )))
        filters = new["Profiles"][0]["Filters"]
        self.assertEqual([f["Path"] for f in filters], [
            "TabletDriverFilters.Devocub.Antichatter", "RadialFollow.RadialFollowSmoothingScreenSpace"])
        anti = filters[0]
        self.assertEqual(anti["FutureFilterKey"], "keep-me")
        self.assertFalse(anti["Enable"])
        self.assertEqual([s["Property"] for s in anti["Settings"]], [
            "Latency", "AntichatterStrength", "AntichatterMultiplier", "PredictionEnabled", "Frequency"])
        self.assertEqual(anti["Settings"][1]["Value"], 5.5)
        self.assertEqual(anti["Settings"][0]["Value"], 2.0)
        self.assertEqual(filters[1], {"Path": "RadialFollow.RadialFollowSmoothingScreenSpace", "Settings": [
            {"Property": "OuterRadius", "Value": 5.0}, {"Property": "Enabled", "Value": True}], "Enable": True})
        self.assertEqual(new["Profiles"][0]["FutureProfileKey"], {"Nested": [1, 2, 3]})

    def test_empty_filters_removes_all(self) -> None:
        new = sio.apply_changes(sample(), "Huion H640P", ProfileChanges(filters=()))
        self.assertEqual(new["Profiles"][1]["Filters"], [])

    def test_duplicate_paths_match_in_order(self) -> None:
        settings = {"Profiles": [{"Tablet": "T", "Filters": [
            {"Path": "P", "Settings": [{"Property": "a", "Value": 1}], "Enable": True, "tag": 1},
            {"Path": "P", "Settings": [{"Property": "a", "Value": 2}], "Enable": True, "tag": 2}]}]}
        new = sio.apply_changes(settings, "T", ProfileChanges(filters=(
            FilterConfig("P", True, {}), FilterConfig("P", False, {}), FilterConfig("P", True, {"a": 9}))))
        self.assertEqual([f.get("tag") for f in new["Profiles"][0]["Filters"]], [1, 2, None])


class WriteTest(TempConfigCase):
    def test_backup_copies_with_timestamp(self) -> None:
        self.with_fixture()
        first = sio.backup_settings(self.dir)
        second = sio.backup_settings(self.dir)
        self.assertNotEqual(first, second)
        for path in (first, second):
            self.assertEqual(path.parent, self.dir / "Backup")
            self.assertRegex(path.name, r"^osu-assistant-settings-\d{8}-\d{6}(-\d+)?\.json$")
            self.assertEqual(path.read_bytes(), FIXTURE.read_bytes())

    def test_backup_without_settings(self) -> None:
        with self.assertRaises(sio.OtdSettingsError):
            sio.backup_settings(self.dir)

    def test_write_settings_atomic_roundtrip(self) -> None:
        self.with_fixture()
        new = sio.apply_changes(sample(), "Wacom CTL-672", ProfileChanges(lock_aspect_ratio=False))
        sio.write_settings(self.dir, new)
        self.assertEqual(sio.load_settings(self.dir), new)
        self.assertEqual([p.name for p in self.dir.iterdir()], [sio.SETTINGS_FILE])  # no temp left
        text = (self.dir / sio.SETTINGS_FILE).read_text(encoding="utf-8")
        self.assertIn('\n  "Profiles": [', text)
        self.assertIn('"Value": 10\n', text)            # ints stay ints
        self.assertIn('"Value": 1000.0\n', text)        # floats stay floats

    def test_write_failure_keeps_original(self) -> None:
        self.with_fixture()
        with mock.patch.object(sio.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                sio.write_settings(self.dir, {"Profiles": []})
        self.assertEqual((self.dir / sio.SETTINGS_FILE).read_bytes(), FIXTURE.read_bytes())
        self.assertEqual(len(list(self.dir.iterdir())), 1)

    def test_write_settings_validation(self) -> None:
        with self.assertRaises(TypeError):
            sio.write_settings(self.dir, [])  # type: ignore[arg-type]
        with self.assertRaises(sio.OtdSettingsError):
            sio.write_settings(self.dir / "missing", {})

    def test_save_preset(self) -> None:
        path = sio.save_preset(self.dir, "osu wrist v1", sample())
        self.assertEqual(path, self.dir / "Presets" / "osu wrist v1.json")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), sample())
        with self.assertRaises(sio.OtdSettingsError) as ctx:
            sio.save_preset(self.dir, "osu wrist v1", {})
        self.assertEqual(ctx.exception.key, "otd.error.preset_exists")
        sio.save_preset(self.dir, "osu wrist v1", {"a": 1}, overwrite=True)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 1})

    def test_preset_name_validation(self) -> None:
        for bad in ("", ".", "..", "a/b", "..\\x", "a:b", " lead", "trail.", "x\n"):
            with self.subTest(bad=bad), self.assertRaises(sio.OtdSettingsError):
                sio.save_preset(self.dir, bad, {})
        self.assertFalse((self.dir / "Presets").exists())


class LocateTest(unittest.TestCase):
    def test_default_config_dir_per_os(self) -> None:
        home = Path("/home/u")
        self.assertEqual(sio.default_config_dir("Windows", {"LOCALAPPDATA": "C:/L"}, home),
                         Path("C:/L/OpenTabletDriver"))
        self.assertEqual(sio.default_config_dir("Windows", {}, home),
                         home / "AppData" / "Local" / "OpenTabletDriver")
        self.assertEqual(sio.default_config_dir("Linux", {}, home), home / ".config" / "OpenTabletDriver")
        self.assertEqual(sio.default_config_dir("Linux", {"XDG_CONFIG_HOME": "/x"}, home),
                         Path("/x/OpenTabletDriver"))
        self.assertEqual(sio.default_config_dir("Darwin", {}, home),
                         home / "Library" / "Application Support" / "OpenTabletDriver")
        self.assertEqual(sio.default_config_dir("Haiku", {}, home), home / ".config" / "OpenTabletDriver")

    def test_install_candidates_find_portable_unzip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            portable = home / "Documents" / "opentablet"
            portable.mkdir(parents=True)
            (portable / "OpenTabletDriver.Console.exe").write_bytes(b"")
            cands = sio.install_candidates("Windows", {}, home)
            self.assertIn(portable, cands)
            self.assertTrue(sio.is_install_dir(portable, "Windows"))
            self.assertFalse(sio.is_install_dir(home, "Windows"))
        for system in ("Linux", "Darwin", "Plan9"):
            self.assertTrue(sio.install_candidates(system, {}, Path("/h")))

    def test_find_config_dir_prefers_portable_userdata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "install" / "userdata").mkdir(parents=True)
            (root / "install" / "userdata" / sio.SETTINGS_FILE).write_text("{}", encoding="utf-8")
            (root / "local" / "OpenTabletDriver").mkdir(parents=True)
            with mock.patch.object(sio, "find_install_dir", return_value=root / "install"), \
                    mock.patch.object(sio, "default_config_dir", return_value=root / "local" / "OpenTabletDriver"):
                self.assertEqual(sio.find_config_dir(), root / "install" / "userdata")
            with mock.patch.object(sio, "find_install_dir", return_value=None), \
                    mock.patch.object(sio, "default_config_dir", return_value=root / "local" / "OpenTabletDriver"):
                self.assertEqual(sio.find_config_dir(), root / "local" / "OpenTabletDriver")
            with mock.patch.object(sio, "find_install_dir", return_value=None), \
                    mock.patch.object(sio, "default_config_dir", return_value=root / "nope"):
                self.assertIsNone(sio.find_config_dir())

    def test_find_install_dir_none_when_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(sio, "_running_daemon_dirs", return_value=[]), \
                mock.patch.object(sio.shutil, "which", return_value=None), \
                mock.patch.object(sio, "install_candidates", return_value=[Path(tmp)]):
            self.assertIsNone(sio.find_install_dir())

    def test_find_install_dir_uses_running_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exe_dir = Path(tmp)
            (exe_dir / "OpenTabletDriver.Daemon.exe").write_bytes(b"")
            with mock.patch.object(sio, "_running_daemon_dirs", return_value=[exe_dir]), \
                    mock.patch.object(sio.platform, "system", return_value="Windows"):
                self.assertEqual(sio.find_install_dir(), exe_dir)

    def test_real_detection_does_not_crash(self) -> None:
        for result in (sio.find_install_dir(), sio.find_config_dir()):
            self.assertTrue(result is None or isinstance(result, Path))


class LocaleTest(unittest.TestCase):
    def test_keys_in_both_languages_and_used_keys_exist(self) -> None:
        es = json.loads((LOCALES / "es" / "otd.json").read_text(encoding="utf-8"))
        en = json.loads((LOCALES / "en" / "otd.json").read_text(encoding="utf-8"))
        self.assertEqual(set(es), set(en))
        self.assertTrue(all(k.startswith("otd.") for k in es))
        for key in es:
            self.assertEqual(set(re.findall(r"{(\w+)}", es[key])), set(re.findall(r"{(\w+)}", en[key])), key)
        used = set()
        for src in OTD_SRC.glob("*.py"):
            used |= set(re.findall(r'"(otd\.(?:apply|error)\.\w+)"', src.read_text(encoding="utf-8")))
        self.assertTrue(used)
        self.assertEqual(used - set(es), set())


if __name__ == "__main__":
    unittest.main()
