"""knowledge.presets: the curated community data and its strict validator."""
from __future__ import annotations

import copy
import unittest
from datetime import date

from osu_tablet_assistant.knowledge import presets as kp
from osu_tablet_assistant.knowledge.filters import load_catalog

RADIAL = "RadialFollow.RadialFollowSmoothingTabletSpace"


def _doc(*entries: dict) -> dict:
    return {"version": 1, "generated": "2026-10-30", "schema": 1, "presets": list(entries)}


def _entry(**overrides) -> dict:
    entry = {"id": "x", "player": "Someone (rank)", "tablet": "Wacom CTL-472",
             "area_mm": {"width": 60, "height": 40}, "rotation_deg": None,
             "filters": [{"path": RADIAL, "enabled": True,
                          "settings": {"OuterRadius": 0.5, "InnerRadius": 0.3}}],
             "style_tags": [], "source_url": "https://setup.moe/u/1", "source_date": None,
             "retrieved": "2026-10-30", "confidence": "high"}
    entry.update(overrides)
    return entry


class PackagedDataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bundle = kp.load_presets()

    def test_loads_cleanly_with_sources(self) -> None:
        self.assertGreaterEqual(len(self.bundle.presets), 25)
        self.assertEqual(self.bundle.version, 1)
        self.assertGreater(self.bundle.source_count, 20)
        for p in self.bundle.presets:
            with self.subTest(p.id):
                self.assertIn(p.confidence, kp.CONFIDENCES)
                self.assertTrue(p.retrieved)
                if p.attribution != "user":
                    self.assertTrue(p.source_url.startswith("https://"))
        # only nixie's out-of-range Devocub strength is clamped
        self.assertEqual([w for w in self.bundle.warnings if "skipped" in w], [])

    def test_user_values_are_unattributed_low_confidence(self) -> None:
        user = self.bundle.by_id("user-provided-radial-follow")
        self.assertIsNotNone(user)
        self.assertIsNone(user.player)
        self.assertEqual(user.attribution, "user")
        self.assertEqual(user.confidence, "low")
        self.assertIsNone(user.area_mm)
        self.assertEqual(user.filters[0].settings, {"OuterRadius": 0.71, "InnerRadius": 0.37,
                                                    "SmoothingCoefficient": 0.37, "SoftKneeScale": 0.38,
                                                    "SmoothingLeakCoefficient": 0.26})
        self.assertIn("unverified", user.flags)
        for p in self.bundle.presets:   # never tied to a player in the data
            if p.player and "mrekk" in p.player.lower():
                self.assertEqual(p.filters, ())

    def test_typos_and_placeholders_dropped_or_flagged(self) -> None:
        self.assertIsNone(self.bundle.by_id("chocopafe-tr-radial"))       # inner > outer
        self.assertIsNone(self.bundle.by_id("exxotl-full-area-radial"))   # near defaults
        self.assertEqual({i for i, _ in self.bundle.excluded},
                         {"chocopafe-tr-radial", "exxotl-full-area-radial"})
        self.assertIn("untouched_defaults", self.bundle.by_id("pabloniichan-chatter-exterminator").flags)
        self.assertIn("possible_typo", self.bundle.by_id("cryshina-setupmoe").flags)
        for p in self.bundle.presets:
            for f in p.filters:
                if f.path == RADIAL and {"InnerRadius", "OuterRadius"} <= set(f.settings):
                    self.assertLessEqual(f.settings["InnerRadius"], f.settings["OuterRadius"], p.id)

    def test_clamp_is_recorded(self) -> None:
        nixie = self.bundle.by_id("community:nixie-tr-devocub-prediction")
        self.assertEqual(nixie.clamped, (("TabletDriverFilters.Devocub.Antichatter",
                                          "AntichatterStrength", 20, 10.0),))

    def test_status(self) -> None:
        status = kp.data_status(self.bundle, "bundled")
        self.assertEqual(status.preset_count, len(self.bundle.presets))
        self.assertEqual(status.age_days(date(2026, 11, 9)), 10)
        self.assertIsNone(kp.DataStatus("", 0, 0).age_days())


class ValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = load_catalog()

    def test_valid_entry(self) -> None:
        bundle = kp.parse_presets(_doc(_entry()), self.catalog)
        (preset,) = bundle.presets
        self.assertEqual(preset.candidate_id, "community:x")
        self.assertEqual(preset.name, "Someone")
        self.assertEqual(preset.required_paths, {RADIAL})

    def test_bad_entries_skipped_with_warning(self) -> None:
        bad = [
            _entry(id="unknown-path", filters=[{"path": "Evil.Plugin", "enabled": True, "settings": {}}]),
            _entry(id="unknown-prop", filters=[{"path": RADIAL, "enabled": True,
                                                "settings": {"Initial Smoothing Coefficient": 0.5}}]),
            _entry(id="Bad Id"),
            _entry(id="no-source", source_url=None),
            _entry(id="http", source_url="http://example.org"),
            _entry(id="js", source_url="javascript:alert(1)"),
            _entry(id="conf", confidence="certain"),
            _entry(id="date", source_date="yesterday"),
            _entry(id="area", area_mm={"width": -1, "height": 4}),
            _entry(id="extra-key", script="rm -rf"),
            _entry(id="flag", flags=["nope"]),
            _entry(id="type", filters=[{"path": RADIAL, "enabled": True, "settings": {"OuterRadius": "big"}}]),
            _entry(id="bool", filters=[{"path": "TabletDriverFilters.Devocub.Antichatter", "enabled": True,
                                        "settings": {"PredictionEnabled": 1}}]),
            "not an object",
        ]
        bundle = kp.parse_presets(_doc(_entry(id="good"), *bad, _entry(id="good")), self.catalog)
        self.assertEqual([p.id for p in bundle.presets], ["good"])
        self.assertEqual(len([w for w in bundle.warnings if w.startswith("skipped")]), len(bad) + 1)

    def test_user_entry_needs_no_source_and_values_are_clamped(self) -> None:
        entry = _entry(id="mine", player=None, attribution="user", source_url=None,
                       filters=[{"path": RADIAL, "enabled": None, "settings": {"OuterRadius": 99}}])
        (preset,) = kp.parse_presets(_doc(entry), self.catalog).presets
        self.assertEqual(preset.filters[0].settings["OuterRadius"], 5.0)
        self.assertEqual(preset.clamped, ((RADIAL, "OuterRadius", 99, 5.0),))
        self.assertIsNone(preset.filters[0].enabled)

    def test_document_level_errors_raise(self) -> None:
        for doc in ([], {"version": 1}, dict(_doc(), schema=2), dict(_doc(), version="1"),
                    dict(_doc(), generated="today"), dict(_doc(), presets={}), dict(_doc(), extra=1)):
            with self.subTest(doc=doc), self.assertRaises(kp.PresetsError):
                kp.parse_presets(doc, self.catalog)

    def test_override_and_never_crash(self) -> None:
        bundle = kp.parse_presets(_doc(_entry()), self.catalog)
        kp.set_presets_override(bundle)
        self.addCleanup(kp.set_presets_override, None)
        self.assertIs(kp.load_presets(), bundle)
        self.assertEqual(kp.data_status().origin, "downloaded")
        kp.set_presets_override(None)
        self.assertGreater(len(kp.load_presets().presets), 1)
        doc = copy.deepcopy(kp.packaged_document())
        self.assertEqual(len(kp.parse_presets(doc).presets), len(kp.load_presets().presets))


if __name__ == "__main__":
    unittest.main()
