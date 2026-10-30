"""engine.areafit: view <-> native conversions, shifts, scaling, tablet limits."""
from __future__ import annotations

import unittest

from osu_tablet_assistant.engine import areafit as af
from osu_tablet_assistant.models import Rect, TabletSpec

TABLET = TabletSpec("CTL-472", 152.0, 95.0)
AREA180 = Rect(71.66, 41.44, 76.0, 47.5, 180.0)       # the user's setup (left-handed)
AREA0 = Rect(100.0, 56.25, 76.0, 47.5, 0.0)


class ConversionTests(unittest.TestCase):
    def test_rotation_vectors(self) -> None:
        self.assertEqual(af.rotate_to_tablet(3, 4, 0), (3, 4))
        self.assertEqual(af.rotate_to_tablet(3, 4, 180), (-3, -4))
        for rot in (0, 90, 180, 270):
            back = af.tablet_to_view(*af.rotate_to_tablet(3, 4, rot), rot)
            self.assertAlmostEqual(back[0], 3)
            self.assertAlmostEqual(back[1], 4)

    def test_corners_with_180(self) -> None:
        # the player's top-left corner is the tablet's bottom-right one
        x, y = af.area_to_tablet_mm(AREA180, 0.0, 0.0)
        self.assertAlmostEqual(x, 76 + 71.66 / 2)
        self.assertAlmostEqual(y, 47.5 + 41.44 / 2)
        self.assertEqual(af.area_to_tablet_mm(AREA0, 0.0, 0.0), (76 - 50, 47.5 - 56.25 / 2))

    def test_round_trip(self) -> None:
        for area in (AREA0, AREA180, Rect(40, 60, 70, 50, 90)):
            for u, v in ((0, 0), (1, 1), (0.3, 0.8)):
                back = af.tablet_mm_to_area(area, *af.area_to_tablet_mm(area, u, v))
                self.assertAlmostEqual(back[0], u)
                self.assertAlmostEqual(back[1], v)

    def test_verified_rotations(self) -> None:
        self.assertTrue(af.rotation_verified(AREA0) and af.rotation_verified(AREA180))
        self.assertFalse(af.rotation_verified(Rect(10, 10, 5, 5, 90)))

    def test_view_size_swaps_for_90(self) -> None:
        self.assertEqual(af.view_size(Rect(40, 60, 0, 0, 90)), (60, 40))
        self.assertEqual(af.view_size(AREA180), (71.66, 41.44))


class ShiftTests(unittest.TestCase):
    def test_shift_is_in_the_players_view(self) -> None:
        moved, reasons = af.shift_area(AREA180, 5.0, -2.0, TABLET)
        self.assertAlmostEqual(moved.x, 71.0)       # right on screen = smaller tablet x when rotated 180
        self.assertAlmostEqual(moved.y, 49.5)
        self.assertEqual(reasons[0].key, "play.area.shift")
        self.assertEqual((moved.width, moved.height, moved.rotation), (71.66, 41.44, 180.0))

    def test_shift_clamped_to_the_tablet(self) -> None:
        moved, reasons = af.shift_area(AREA0, 500.0, 0.0, TABLET)
        self.assertTrue(af.area_fits(moved, TABLET))
        self.assertAlmostEqual(moved.x + 50.0, TABLET.width_mm)
        self.assertIn("play.area.clamped", [r.key for r in reasons])

    def test_no_room_returns_same_area(self) -> None:
        edge = Rect(100.0, 56.25, 102.0, 47.5, 0.0)
        moved, reasons = af.shift_area(edge, 10.0, 0.0, TABLET)
        self.assertEqual(moved, edge)
        self.assertEqual(reasons[0].key, "play.area.no_room")

    def test_unverified_rotation_is_flagged(self) -> None:
        _moved, reasons = af.shift_area(Rect(40, 60, 76, 47.5, 90), 2.0, 0.0, TABLET)
        self.assertIn("play.area.rotation_unverified", [r.key for r in reasons])

    def test_unknown_tablet_does_not_clamp(self) -> None:
        moved, _ = af.shift_area(AREA0, 500.0, 0.0, None)
        self.assertAlmostEqual(moved.x, 576.0)


class ScaleTests(unittest.TestCase):
    def test_uniform_scale_keeps_aspect_and_centre(self) -> None:
        out, reasons = af.scale_area_axes(AREA180, 1.1, 1.1, TABLET)
        self.assertAlmostEqual(out.width / out.height, AREA180.width / AREA180.height, places=2)
        self.assertEqual((out.x, out.y), (76.0, 47.5))
        self.assertEqual(reasons[0].key, "play.area.scale")

    def test_locked_aspect_uses_the_mean_and_says_so(self) -> None:
        out, reasons = af.scale_area_axes(AREA0, 1.2, 1.0, TABLET)
        self.assertAlmostEqual(out.width, 110.0, places=2)
        self.assertIn("play.area.scale_mean", [r.key for r in reasons])

    def test_axis_scale_changes_aspect_only_when_unlocked(self) -> None:
        out, reasons = af.scale_area_axes(AREA0, 1.2, 0.9, TABLET, lock_aspect=False)
        self.assertAlmostEqual(out.width, 120.0, places=2)
        self.assertAlmostEqual(out.height, 50.625, places=2)
        self.assertIn("play.area.aspect_changed", [r.key for r in reasons])

    def test_anchor_keeps_the_corner_fixed(self) -> None:
        before = af.area_to_tablet_mm(AREA0, 1.0, 1.0)
        out, _ = af.scale_area_axes(AREA0, 0.9, 0.9, TABLET, anchor=(1.0, 1.0))
        after = af.area_to_tablet_mm(out, 1.0, 1.0)
        self.assertAlmostEqual(before[0], after[0], places=2)
        self.assertAlmostEqual(before[1], after[1], places=2)

    def test_anchor_with_rotation_180(self) -> None:
        before = af.area_to_tablet_mm(AREA180, 1.0, 1.0)
        out, _ = af.scale_area_axes(AREA180, 1.1, 1.1, TABLET, anchor=(1.0, 1.0))
        after = af.area_to_tablet_mm(out, 1.0, 1.0)
        self.assertAlmostEqual(before[0], after[0], places=2)
        self.assertAlmostEqual(before[1], after[1], places=2)
        self.assertTrue(af.area_fits(out, TABLET))

    def test_growth_is_capped_to_the_tablet(self) -> None:
        out, reasons = af.scale_area_axes(AREA180, 5.0, 5.0, TABLET)
        self.assertTrue(af.area_fits(out, TABLET))
        self.assertIn("play.area.capped", [r.key for r in reasons])
        self.assertLessEqual(out.width, TABLET.width_mm + 1e-9)

    def test_minimum_width(self) -> None:
        out, reasons = af.scale_area_axes(AREA0, 0.01, 0.01, TABLET)
        self.assertGreaterEqual(out.width, af.MIN_AREA_WIDTH_MM - 1e-9)
        self.assertIn("play.area.at_min", [r.key for r in reasons])

    def test_bad_input_returns_unchanged(self) -> None:
        for sx, sy in ((float("nan"), 1.0), (0.0, 1.0), (-1.0, 1.0)):
            out, reasons = af.scale_area_axes(AREA0, sx, sy, TABLET)
            self.assertEqual(out, AREA0)
            self.assertEqual(reasons[0].key, "play.area.unknown")

    def test_decimals_never_leave_the_tablet(self) -> None:
        area = Rect(152.0, 95.0, 76.0, 47.5, 0.0)
        out, _ = af.scale_area_axes(area, 1.0371, 1.0371, TABLET)
        self.assertTrue(af.area_fits(out, TABLET))


if __name__ == "__main__":
    unittest.main()
