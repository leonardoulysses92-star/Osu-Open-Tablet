"""Tablet database: built-in data, name matching, OTD configuration parsing, custom entries."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from osu_tablet_assistant.knowledge import tablets
from osu_tablet_assistant.models import TabletSource


def otd_json(name: str, width: float, height: float, pressure: int = 1023) -> str:
    return json.dumps({
        "Name": name,
        "Specifications": {
            "Digitizer": {"Width": width, "Height": height, "MaxX": width * 100, "MaxY": height * 100},
            "Pen": {"MaxPressure": pressure},
        },
    })


class BuiltinTests(unittest.TestCase):
    def test_database_is_large_and_sane(self) -> None:
        specs = tablets.builtin_tablets()
        self.assertGreaterEqual(len(specs), 30)
        for spec in specs:
            self.assertGreater(spec.width_mm, 20, spec.name)
            self.assertGreater(spec.height_mm, 20, spec.name)
            self.assertGreaterEqual(spec.max_report_rate_hz, 100, spec.name)
            self.assertEqual(spec.source, TabletSource.BUILTIN)
        keys = [tablets.normalize_name(s.name) for s in specs]
        self.assertEqual(len(keys), len(set(keys)), "duplicate tablet names")

    def test_required_tablets_present_with_known_values(self) -> None:
        by_name = {s.name: s for s in tablets.builtin_tablets()}
        star06 = by_name["XP-Pen Star 06"]
        self.assertEqual((star06.width_mm, star06.height_mm), (254.0, 152.4))
        ctl472 = by_name["Wacom CTL-472"]
        self.assertEqual((ctl472.width_mm, ctl472.height_mm, ctl472.max_pressure), (152.0, 95.0, 2047))
        for name in ("Wacom CTL-471", "Wacom CTL-671", "Wacom CTL-672", "Wacom CTH-480", "Wacom CTH-680",
                     "XP-Pen Star G640", "XP-Pen Deco 01 V2", "Huion H640P", "Huion H950P",
                     "Gaomon S620", "VEIKK S640"):
            self.assertIn(name, by_name)

    def test_verified_flag(self) -> None:
        self.assertTrue(tablets.is_verified("wacom ctl-472"))
        self.assertFalse(tablets.is_verified("Unknown Pad"))


class MatchingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.specs = tablets.builtin_tablets()

    def test_case_space_and_punctuation_insensitive(self) -> None:
        for name in ("Wacom CTL-472", "wacom ctl-472", "  WACOM   CTL 472 ", "Wacom_CTL472"):
            match = tablets.match_tablet(name, self.specs)
            self.assertIsNotNone(match, name)
            self.assertEqual(match.name, "Wacom CTL-472")

    def test_versions_are_not_confused(self) -> None:
        self.assertEqual(tablets.match_tablet("XP-Pen Deco 01", self.specs).name, "XP-Pen Deco 01")
        self.assertEqual(tablets.match_tablet("XP-Pen Deco 01 V2", self.specs).name, "XP-Pen Deco 01 V2")
        self.assertIsNone(tablets.match_tablet("XP-Pen Deco 01 V9", self.specs))

    def test_unknown_and_empty(self) -> None:
        self.assertIsNone(tablets.match_tablet("Mystery Pad", self.specs))
        self.assertIsNone(tablets.match_tablet("   ", self.specs))
        self.assertIsNone(tablets.match_tablet("Wacom CTL-472", []))


class OtdConfigTests(unittest.TestCase):
    def test_no_install_dir_or_no_configurations_folder(self) -> None:
        builtin = len(tablets.builtin_tablets())
        self.assertEqual(len(tablets.list_known_tablets(None)), builtin)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(len(tablets.list_known_tablets(Path(tmp))), builtin)

    def test_otd_configurations_are_parsed_and_override_builtin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vendor = Path(tmp) / "Configurations" / "Acme"
            vendor.mkdir(parents=True)
            (vendor / "Pad.json").write_text(otd_json("Acme Pad 1", 200, 125, 4095), encoding="utf-8-sig")
            (vendor / "Ctl.json").write_text(otd_json("Wacom CTL-472", 150, 90), encoding="utf-8")
            (vendor / "broken.json").write_text("{nope", encoding="utf-8")
            (vendor / "partial.json").write_text('{"Name": "X"}', encoding="utf-8")
            (vendor / "zero.json").write_text(otd_json("Zero Pad", 0, 0), encoding="utf-8")
            known = {s.name: s for s in tablets.list_known_tablets(Path(tmp))}
            acme = known["Acme Pad 1"]
            self.assertEqual((acme.width_mm, acme.height_mm, acme.max_pressure), (200, 125, 4095))
            self.assertEqual(acme.source, TabletSource.OTD_CONFIG)
            self.assertEqual(acme.resolution_lpmm, 100)
            self.assertEqual(acme.max_report_rate_hz, 133.0)
            override = known["Wacom CTL-472"]
            self.assertEqual(override.width_mm, 150)
            self.assertEqual(override.source, TabletSource.OTD_CONFIG)
            self.assertEqual(override.max_report_rate_hz, 133.0)
            self.assertNotIn("X", known)
            self.assertNotIn("Zero Pad", known)
            self.assertEqual(sum(1 for s in known.values() if "CTL-472" in s.name), 1)

    def test_list_is_sorted_by_name(self) -> None:
        names = [s.name.casefold() for s in tablets.list_known_tablets(None)]
        self.assertEqual(names, sorted(names))


class CustomTabletTests(unittest.TestCase):
    def test_builds_spec(self) -> None:
        spec = tablets.custom_tablet("  My Pad ", 160, 100, 200, 8191)
        self.assertEqual((spec.name, spec.width_mm, spec.max_report_rate_hz, spec.max_pressure),
                         ("My Pad", 160.0, 200.0, 8191))
        self.assertEqual(spec.source, TabletSource.CUSTOM)

    def test_defaults(self) -> None:
        spec = tablets.custom_tablet("Pad", 100, 60)
        self.assertEqual((spec.max_report_rate_hz, spec.max_pressure, spec.resolution_lpmm), (133.0, 0, None))

    def test_rejects_invalid_values(self) -> None:
        for args in (("", 10, 10), ("  ", 10, 10), ("p", 0, 10), ("p", 10, -1), ("p", 10, 10, 0),
                     ("p", 10, 10, 133, -5)):
            with self.assertRaises(ValueError, msg=str(args)):
                tablets.custom_tablet(*args)

    def test_malformed_rows_are_rejected(self) -> None:
        for row in ({}, {"name": "x"}, {"name": "x", "width_mm": "a", "height_mm": 1},
                    {"name": "", "width_mm": 1, "height_mm": 1}, {"name": "x", "width_mm": -1, "height_mm": 1}):
            self.assertIsNone(tablets._spec_from_row(row, TabletSource.BUILTIN), row)


if __name__ == "__main__":
    unittest.main()
