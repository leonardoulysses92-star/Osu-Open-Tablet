"""Tests for osu_tablet_assistant.otd.plugins (synthetic plugin folders in temp dirs)."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from osu_tablet_assistant.otd import plugins

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "settings_sample.json"


def make_plugin(config: Path, folder: str, dlls: tuple[str, ...], metadata: dict | str | None) -> Path:
    path = config / "Plugins" / folder
    path.mkdir(parents=True)
    for dll in dlls:
        (path / dll).write_bytes(b"MZ")
    if isinstance(metadata, dict):
        (path / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    elif isinstance(metadata, str):
        (path / "metadata.json").write_text(metadata, encoding="utf-8")
    return path


class PluginsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.config = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def install_real_like_set(self) -> None:
        make_plugin(self.config, "HawkuFilters", ("HawkuFilters.dll",),
                    {"Name": "HawkuFilters", "Owner": "OpenTabletDriver", "PluginVersion": "0.3.1.0"})
        make_plugin(self.config, "Temporal Resampler", ("TemporalResampler.dll",),
                    {"Name": "Temporal Resampler", "PluginVersion": "1.5.1"})
        make_plugin(self.config, "AbstractQbit's Radial Follow Smoothing", ("RadialFollow.dll",),
                    {"Name": "AbstractQbit's Radial Follow Smoothing", "PluginVersion": "0.3.0.0"})
        make_plugin(self.config, "The Saturn Collection", ("Saturn.dll",),
                    {"Name": "The Saturn Collection", "PluginVersion": "1.0.0"})

    def test_missing_plugins_dir(self) -> None:
        self.assertEqual(plugins.list_installed_plugins(self.config), [])
        self.assertEqual(plugins.available_filter_paths(self.config), set(plugins.BUILTIN_FILTER_PATHS))
        self.assertEqual(plugins.list_installed_plugins(self.config / "does-not-exist"), [])

    def test_lists_plugins_with_metadata(self) -> None:
        self.install_real_like_set()
        infos = plugins.list_installed_plugins(self.config)
        self.assertEqual([p.name for p in infos], [
            "AbstractQbit's Radial Follow Smoothing", "HawkuFilters", "Temporal Resampler",
            "The Saturn Collection"])
        by_name = {p.name: p for p in infos}
        self.assertEqual(by_name["HawkuFilters"].version, "0.3.1.0")
        self.assertEqual(by_name["HawkuFilters"].directory, str(self.config / "Plugins" / "HawkuFilters"))
        self.assertEqual(by_name["HawkuFilters"].provides, (
            "OpenTabletDriver.Plugin.NoiseReduction", "TabletDriverFilters.Hawku.Smoothing"))
        self.assertIn("Saturn.MultifilterExtraSettings", by_name["The Saturn Collection"].provides)

    def test_available_filter_paths_matches_listfilters_on_real_setup(self) -> None:
        # Same set OTD 0.6.7 `Console listfilters` printed with these four plugins.
        self.install_real_like_set()
        self.assertEqual(plugins.available_filter_paths(self.config), {
            "Saturn.Multifilter", "TabletDriverFilters.Hawku.Smoothing",
            "RadialFollow.RadialFollowSmoothingTabletSpace", "Saturn.AreaRoundingDFC", "TemporalResampler",
            "OpenTabletDriver.Plugin.NoiseReduction", "RadialFollow.RadialFollowSmoothingScreenSpace"})

    def test_not_installed_plugin_paths_are_excluded(self) -> None:
        shutil.copy(FIXTURE, self.config / "settings.json")   # references Devocub, not installed
        make_plugin(self.config, "HawkuFilters", ("HawkuFilters.dll",), {"Name": "HawkuFilters"})
        paths = plugins.available_filter_paths(self.config)
        self.assertNotIn("TabletDriverFilters.Devocub.Antichatter", paths)
        self.assertIn("TabletDriverFilters.Hawku.Smoothing", paths)

    def test_fallbacks_and_inference_from_dll_and_settings(self) -> None:
        settings = {"Profiles": [{"Tablet": "T", "Filters": [
            {"Path": "Kuuube_s_Chatter_Exterminator.Kuuube_s_CHATTER_EXTERMINATOR_RAW", "Settings": [], "Enable": True},
            {"Path": "MyFilters.Cool", "Settings": [], "Enable": True},
            {"Path": "Orphan.Filter", "Settings": [], "Enable": True}]}],
            "Tools": [{"Path": "MyFilters.Tool", "Settings": [], "Enable": True}]}
        (self.config / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        make_plugin(self.config, "Kuuube's Chatter Exterminator", ("Kuuube_s_Chatter_Exterminator.dll",), None)
        make_plugin(self.config, "my-filters", ("MyFilters.dll",), "{broken json")
        make_plugin(self.config, ".hidden", (), None)
        infos = {p.name: p for p in plugins.list_installed_plugins(self.config)}
        self.assertEqual(set(infos), {"Kuuube's Chatter Exterminator", "my-filters"})
        self.assertIsNone(infos["my-filters"].version)
        self.assertEqual(infos["my-filters"].provides, ("MyFilters.Cool", "MyFilters.Tool"))
        paths = plugins.available_filter_paths(self.config)
        self.assertIn("Kuuube_s_Chatter_Exterminator.Kuuube_s_CHATTER_EXTERMINATOR_SMOOTH", paths)
        self.assertIn("MyFilters.Cool", paths)
        self.assertNotIn("MyFilters.Tool", paths)
        self.assertNotIn("Orphan.Filter", paths)

    def test_malformed_settings_is_ignored(self) -> None:
        (self.config / "settings.json").write_text("not json", encoding="utf-8")
        make_plugin(self.config, "Temporal Resampler", ("TemporalResampler.dll",), '["not", "an", "object"]')
        self.assertEqual(plugins.available_filter_paths(self.config), {"TemporalResampler"})
        (info,) = plugins.list_installed_plugins(self.config)
        self.assertEqual(info.name, "Temporal Resampler")


if __name__ == "__main__":
    unittest.main()
