"""engine.area: size heuristics, aspect ratio, centering and clamping."""
from __future__ import annotations

import itertools
import unittest

from osu_tablet_assistant.engine.area import MIN_AREA_WIDTH_MM, recommend_area
from osu_tablet_assistant.models import (DisplaySpec, MapPreference, PlayStyle, SkillLevel,
                                         TabletSpec, UserPreferences)

SMALL = TabletSpec("CTL-472-like", 152.0, 95.0, 133.0)
LARGE = TabletSpec("CTL-672-like", 216.0, 135.0, 133.0)
FHD = DisplaySpec(1920, 1080)            # 16:9
WUXGA = DisplaySpec(1920, 1200)          # 16:10


def _keys(reasons) -> list[str]:
    return [r.key for r in reasons]


class AreaTests(unittest.TestCase):
    def test_wrist_on_small_tablet_16_9(self) -> None:
        area, reasons = recommend_area(SMALL, FHD, UserPreferences(play_style=PlayStyle.WRIST))
        self.assertAlmostEqual(area.width, 80.0)
        self.assertAlmostEqual(area.height, 45.0)
        self.assertEqual((area.x, area.y, area.rotation), (76.0, 47.5, 0.0))
        keys = _keys(reasons)
        self.assertIn("engine.reason.area_style_wrist", keys)
        self.assertIn("engine.reason.area_heuristic", keys)
        self.assertIn("engine.reason.area_aspect", keys)
        self.assertIn("engine.reason.area_centered", keys)

    def test_wrist_on_large_tablet_16_10(self) -> None:
        area, _ = recommend_area(LARGE, WUXGA, UserPreferences(play_style=PlayStyle.WRIST))
        self.assertAlmostEqual(area.width, 80.0)
        self.assertAlmostEqual(area.height, 50.0)
        self.assertEqual((area.x, area.y), (108.0, 67.5))

    def test_style_ordering(self) -> None:
        widths = [recommend_area(LARGE, FHD, UserPreferences(play_style=s))[0].width
                  for s in (PlayStyle.FINGER, PlayStyle.WRIST, PlayStyle.MIXED, PlayStyle.ARM)]
        self.assertEqual(widths, sorted(widths))
        self.assertEqual(len(set(widths)), 4)

    def test_map_preference_and_skill_nudge_size(self) -> None:
        def width(**kw) -> float:
            return recommend_area(LARGE, FHD, UserPreferences(**kw))[0].width
        self.assertLess(width(map_preference=MapPreference.JUMPS), width())
        self.assertGreater(width(map_preference=MapPreference.STREAMS), width())
        self.assertGreater(width(skill_level=SkillLevel.BEGINNER), width())
        self.assertLess(width(skill_level=SkillLevel.ADVANCED), width())
        _, reasons = recommend_area(LARGE, FHD, UserPreferences(
            map_preference=MapPreference.JUMPS, skill_level=SkillLevel.ADVANCED))
        self.assertIn("engine.reason.area_map_jumps", _keys(reasons))
        self.assertIn("engine.reason.area_skill_advanced", _keys(reasons))

    def test_arm_area_clamped_to_small_tablet(self) -> None:
        tablet = TabletSpec("tiny", 100.0, 60.0)
        area, reasons = recommend_area(tablet, WUXGA, UserPreferences(play_style=PlayStyle.ARM))
        self.assertAlmostEqual(area.width, 96.0)      # limited by 60 mm height * 1.6
        self.assertAlmostEqual(area.height, 60.0)
        self.assertIn("engine.reason.area_clamped_tablet", _keys(reasons))

    def test_preferred_width_is_respected(self) -> None:
        area, reasons = recommend_area(SMALL, FHD, UserPreferences(
            play_style=PlayStyle.ARM, preferred_area_width_mm=90.0))
        self.assertAlmostEqual(area.width, 90.0)
        self.assertAlmostEqual(area.height, 90.0 * 9 / 16)
        self.assertIn("engine.reason.area_preferred", _keys(reasons))
        self.assertNotIn("engine.reason.area_style_arm", _keys(reasons))

    def test_preferred_width_only_clamped(self) -> None:
        area, reasons = recommend_area(SMALL, FHD, UserPreferences(preferred_area_width_mm=300.0))
        self.assertAlmostEqual(area.width, 152.0)
        self.assertAlmostEqual(area.height, 85.5)
        self.assertIn("engine.reason.area_clamped_tablet", _keys(reasons))
        area, reasons = recommend_area(SMALL, FHD, UserPreferences(preferred_area_width_mm=5.0))
        self.assertAlmostEqual(area.width, MIN_AREA_WIDTH_MM)
        self.assertIn("engine.reason.area_clamped_min", _keys(reasons))

    def test_never_exceeds_tablet_and_keeps_aspect(self) -> None:
        tablets = (SMALL, LARGE, TabletSpec("odd", 70.0, 90.0))
        for tablet, display, style, mp, skill, pref in itertools.product(
                tablets, (FHD, WUXGA, DisplaySpec(1280, 1024)), PlayStyle, MapPreference,
                SkillLevel, (None, 50.0, 500.0)):
            prefs = UserPreferences(play_style=style, map_preference=mp, skill_level=skill,
                                    preferred_area_width_mm=pref)
            area, _ = recommend_area(tablet, display, prefs)
            with self.subTest(tablet=tablet.name, display=display, prefs=prefs):
                self.assertLessEqual(area.width, tablet.width_mm + 1e-9)
                self.assertLessEqual(area.height, tablet.height_mm + 1e-9)
                self.assertAlmostEqual(area.width / area.height,
                                       display.width_px / display.height_px)
                self.assertEqual((area.x, area.y), (tablet.width_mm / 2, tablet.height_mm / 2))

    def test_invalid_dimensions(self) -> None:
        with self.assertRaises(ValueError):
            recommend_area(TabletSpec("bad", 0.0, 95.0), FHD, UserPreferences())
        with self.assertRaises(ValueError):
            recommend_area(SMALL, DisplaySpec(1920, 0), UserPreferences())


if __name__ == "__main__":
    unittest.main()
