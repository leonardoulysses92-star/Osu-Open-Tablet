"""Pure presentation logic of the GUI: geometry, bar scaling, formatting, result texts."""
from __future__ import annotations

import math
import unittest

from osu_tablet_assistant.gui import i18n
from osu_tablet_assistant.gui.app import window_size
from osu_tablet_assistant.gui.session import (
    ComparisonRow,
    build_comparison,
    comparison_for,
    describe_changes,
    filter_table,
    format_value,
)
from osu_tablet_assistant.gui.widgets import (
    COLORS,
    LABEL_ROLES,
    NOTICE_KINDS,
    area_geometry,
    bar_lengths,
    compare_values,
    display_box,
    format_measure,
    rect_corners,
    relative_change,
    stepper_centers,
)
from osu_tablet_assistant.knowledge.filters import load_catalog
from osu_tablet_assistant.models import (
    Candidate,
    CandidateResult,
    FilterConfig,
    MetricsReport,
    ProfileChanges,
    ProfileInfo,
    Rect,
)

HAWKU = "TabletDriverFilters.Hawku.Smoothing"
NOISE = "OpenTabletDriver.Plugin.NoiseReduction"


def profile(area: Rect = Rect(100, 56.25, 108, 67.5), filters: tuple = ()) -> ProfileInfo:
    return ProfileInfo("Wacom CTL-672", "Absolute", Rect(1920, 1080, 960, 540), area, True, filters)


class TokenTests(unittest.TestCase):
    def test_palette_matches_live_tests(self) -> None:
        self.assertEqual(COLORS.bg, "#14161a")
        self.assertEqual(COLORS.accent, "#ff66aa")

    def test_roles_reference_existing_colors(self) -> None:
        for _font, color in LABEL_ROLES.values():
            self.assertTrue(getattr(COLORS, color).startswith("#"))
        for fg, bg, _glyph in NOTICE_KINDS.values():
            self.assertTrue(getattr(COLORS, fg) and getattr(COLORS, bg))


class StepperTests(unittest.TestCase):
    def test_even_slots_centered(self) -> None:
        self.assertEqual(stepper_centers(800, 8), [50 + 100 * i for i in range(8)])
        self.assertEqual(stepper_centers(300, 1), [150])
        self.assertEqual(stepper_centers(300, 0), [])


class BarTests(unittest.TestCase):
    def test_scaled_to_largest(self) -> None:
        self.assertEqual(bar_lengths((2.0, 1.0), 200), [200, 100])
        self.assertEqual(bar_lengths((0.5, 2.0), 100), [25, 100])

    def test_missing_zero_and_tiny_values(self) -> None:
        self.assertEqual(bar_lengths((None, 4.0), 100), [0, 100])
        self.assertEqual(bar_lengths((0.0, 0.0), 100), [0, 0])
        self.assertEqual(bar_lengths((0.001, 10.0), 100, min_px=3), [3, 100])
        self.assertEqual(bar_lengths((float("nan"), 1.0), 50), [0, 50])

    def test_relative_change_and_verdict(self) -> None:
        self.assertAlmostEqual(relative_change(2.0, 1.0), -0.5)
        self.assertIsNone(relative_change(0.0, 1.0))
        self.assertIsNone(relative_change(None, 1.0))
        self.assertEqual(compare_values(2.0, 1.0), "better")
        self.assertEqual(compare_values(1.0, 2.0), "worse")
        self.assertEqual(compare_values(1.0, 1.03), "same")
        self.assertEqual(compare_values(1.0, 2.0, lower_is_better=False), "better")
        self.assertEqual(compare_values(0.0, 0.0), "same")
        self.assertEqual(compare_values(0.0, 1.0), "worse")
        self.assertIsNone(compare_values(None, 1.0))

    def test_format_measure(self) -> None:
        self.assertEqual(format_measure(0.04213, 3, "mm"), "0.042 mm")
        self.assertEqual(format_measure(360.4, 0, "ms"), "360 ms")
        self.assertEqual(format_measure(1.5, 1, ""), "1.5")


class AreaGeometryTests(unittest.TestCase):
    def test_tablet_fits_box_with_aspect_and_padding(self) -> None:
        geo = area_geometry(160, 100, Rect(80, 50, 80, 50), None, 400, 300, pad=20)
        x0, y0, x1, y1 = geo.tablet
        self.assertAlmostEqual(geo.scale, 360 / 160)
        self.assertAlmostEqual((x1 - x0) / (y1 - y0), 1.6)
        self.assertAlmostEqual(x0, 20)
        self.assertAlmostEqual((y0 + y1) / 2, 150)       # vertically centered
        ax0, ay0, ax1, ay1 = geo.area_bbox
        self.assertAlmostEqual((ax0 + ax1) / 2, (x0 + x1) / 2)
        self.assertAlmostEqual(ax1 - ax0, 80 * geo.scale)
        self.assertIsNone(geo.current)

    def test_offset_area_and_current_outline(self) -> None:
        current = Rect(100, 62.5, 50, 31.25)
        geo = area_geometry(160, 100, Rect(40, 25, 20, 12.5), current, 160, 100, pad=0)
        self.assertEqual(geo.area_bbox, (0.0, 0.0, 40.0, 25.0))
        self.assertIsNotNone(geo.current)
        self.assertIsNone(area_geometry(160, 100, current, current, 160, 100, pad=0).current)

    def test_rotation_turns_the_corners(self) -> None:
        corners = rect_corners(Rect(20, 10, 50, 50, 90))
        xs = sorted(round(x, 6) for x, _ in corners)
        ys = sorted(round(y, 6) for _, y in corners)
        self.assertEqual((xs[0], xs[-1]), (45, 55))     # width and height swapped
        self.assertEqual((ys[0], ys[-1]), (40, 60))
        self.assertEqual(len(rect_corners(Rect(1, 1, 0, 0))), 4)

    def test_degenerate_tablet_falls_back_to_area(self) -> None:
        geo = area_geometry(0, 0, Rect(50, 30, 25, 15), None, 200, 200, pad=0)
        self.assertTrue(math.isfinite(geo.scale) and geo.scale > 0)

    def test_display_box_keeps_aspect_and_fits(self) -> None:
        x0, y0, x1, y1 = display_box((1920, 1080), 100, 300, 200, max_fraction=0.5)
        self.assertAlmostEqual((x1 - x0) / (y1 - y0), 1920 / 1080)
        self.assertLessEqual(y1 - y0, 100.0001)
        self.assertGreaterEqual(x0, 100)
        self.assertLessEqual(x1, 300)
        self.assertAlmostEqual((y0 + y1) / 2, 100)


class WindowSizeTests(unittest.TestCase):
    def test_capped_by_screen_and_min_not_above_window(self) -> None:
        (w, h, x, y), (mw, mh) = window_size((1920, 1080), (1080, 820), (860, 600))
        self.assertEqual((w, h), (1080, 820))
        self.assertEqual((x, y), (420, 86))
        self.assertEqual((mw, mh), (860, 600))
        (w, h, _x, _y), (mw, mh) = window_size((800, 600), (1080, 820), (860, 600))
        self.assertEqual((w, h), (736, 528))
        self.assertEqual((mw, mh), (736, 528))


class _Session:
    """Just what ``comparison_for`` reads."""

    def __init__(self, candidates, results, baseline, prof) -> None:
        self.candidates, self.results, self.baseline, self.profile = candidates, results, baseline, prof


def cand(cid: str, latency: float = 0.0) -> Candidate:
    return Candidate(cid, f"engine.candidate.{cid}.label", "x", ProfileChanges(), (), latency)


class ComparisonTests(unittest.TestCase):
    def test_build_rows(self) -> None:
        before = [MetricsReport("stillness", 10, 1.0, jitter_rms_mm=0.08)]
        after = [MetricsReport("stillness", 10, 1.0, jitter_rms_mm=0.04),
                 MetricsReport("tracking", 10, 1.0, path_error_mean_mm=0.5),
                 MetricsReport("aim", 10, 1.0, aim_time_ms_mean=300.0)]
        rows = build_comparison(before, after, 0.0, 2.5)
        self.assertEqual([r.key for r in rows], ["gui.compare.jitter", "gui.compare.tracking",
                                                 "gui.compare.aim_time", "gui.compare.latency"])
        self.assertEqual((rows[0].before, rows[0].after), (0.08, 0.04))
        self.assertEqual((rows[1].before, rows[1].after), (None, 0.5))
        self.assertEqual((rows[3].before, rows[3].after, rows[3].unit), (0.0, 2.5, "ms"))
        self.assertTrue(all(r.lower_is_better for r in rows))

    def test_before_prefers_keep_current_then_baseline(self) -> None:
        keep, best = cand("keep-current", 1.0), cand("balanced", 3.0)
        results = {
            "keep-current": CandidateResult("keep-current", (MetricsReport("tracking", 1, 1, path_error_mean_mm=0.9),)),
            "balanced": CandidateResult("balanced", (MetricsReport("tracking", 1, 1, path_error_mean_mm=0.6),)),
        }
        baseline = [MetricsReport("stillness", 1, 1, jitter_rms_mm=0.1),
                    MetricsReport("tracking", 1, 1, path_error_mean_mm=5.0)]
        rows = {r.key: r for r in comparison_for(_Session([keep, best], results, baseline, profile()), best)}
        self.assertEqual(rows["gui.compare.tracking"].before, 0.9)    # keep-current wins over baseline
        self.assertEqual(rows["gui.compare.jitter"].before, 0.1)      # topped up from the baseline
        self.assertEqual(rows["gui.compare.tracking"].after, 0.6)
        self.assertEqual((rows["gui.compare.latency"].before, rows["gui.compare.latency"].after), (1.0, 3.0))

    def test_latency_before_without_keep_current(self) -> None:
        best = cand("smooth", 4.0)
        no_filters = _Session([best], {}, [], profile())
        self.assertEqual(comparison_for(no_filters, best)[-1].before, 0.0)
        filtered = _Session([best], {}, [], profile(filters=(FilterConfig(HAWKU, True, {}),)))
        self.assertIsNone(comparison_for(filtered, best)[-1].before)

    def test_row_label_is_translated(self) -> None:
        i18n.set_language("en")
        self.addCleanup(i18n.set_language, "es")
        self.assertEqual(ComparisonRow("gui.compare.jitter", 1, 1, "mm", 2).label, "Shake while still")


class DescribeChangesTests(unittest.TestCase):
    name = staticmethod(lambda path: path.split(".")[-1])

    def keys(self, current, changes, latency=0.0):
        return [k for k, _ in describe_changes(current, changes, self.name, latency)]

    def test_smaller_area_and_filter_diff(self) -> None:
        current = profile(filters=(FilterConfig(HAWKU, True, {"Latency": 2.0}),
                                   FilterConfig("Old.Filter", True, {})))
        changes = ProfileChanges(tablet_area=Rect(80, 45, 108, 67.5),
                                 filters=(FilterConfig(HAWKU, True, {"Latency": 5.0}),
                                          FilterConfig(NOISE, True, {})))
        lines = describe_changes(current, changes, self.name, 4.8)
        self.assertEqual(lines[0], ("gui.change.area_smaller", {
            "old_w": "100.0", "old_h": "56.2", "w": "80.0", "h": "45.0", "pct": "20"}))
        self.assertEqual([k for k, _ in lines[1:]], ["gui.change.filter_tuned", "gui.change.filter_added",
                                                     "gui.change.filter_removed", "gui.change.latency"])
        self.assertEqual(lines[-2][1], {"name": "Filter"})
        self.assertEqual(lines[-1][1], {"ms": "4.8"})

    def test_larger_same_moved_rotated(self) -> None:
        self.assertEqual(self.keys(profile(), ProfileChanges(tablet_area=Rect(120, 67.5, 108, 67.5)))[0],
                         "gui.change.area_larger")
        same = self.keys(profile(), ProfileChanges(tablet_area=Rect(100.4, 56.25, 108, 67.5), filters=()))
        self.assertEqual(same, ["gui.change.area_same", "gui.change.filters_none"])
        moved = self.keys(profile(), ProfileChanges(tablet_area=Rect(100, 56.25, 60, 40, 180)))
        self.assertEqual(moved[:3], ["gui.change.area_same", "gui.change.area_moved", "gui.change.area_rotated"])

    def test_untouched_and_unknown_current(self) -> None:
        self.assertEqual(self.keys(profile(), ProfileChanges()),
                         ["gui.change.area_kept", "gui.change.filters_kept"])
        self.assertEqual(self.keys(None, ProfileChanges(tablet_area=Rect(90, 50, 1, 1), filters=())),
                         ["gui.change.area_new", "gui.change.filters_none"])
        unset = profile(area=Rect(0, 0, 0, 0), filters=(FilterConfig(HAWKU, True, {}),))
        self.assertEqual(self.keys(unset, ProfileChanges(tablet_area=Rect(90, 50, 1, 1), filters=())),
                         ["gui.change.area_new", "gui.change.filters_removed_all"])

    def test_disabled_filters_do_not_count(self) -> None:
        current = profile(filters=(FilterConfig(HAWKU, False, {}),))
        self.assertEqual(self.keys(current, ProfileChanges(filters=(FilterConfig(HAWKU, True, {}),)))[1],
                         "gui.change.filter_added")

    def test_every_key_formats_in_both_languages(self) -> None:
        params = {"old_w": "1", "old_h": "1", "w": "1", "h": "1", "pct": "1", "x": "1", "y": "1",
                  "rot": "1", "name": "n", "ms": "1"}
        for lang in i18n.SUPPORTED:
            catalog = i18n.load_catalog(lang)
            for key in ("area_kept", "area_new", "area_same", "area_smaller", "area_larger", "area_moved",
                        "area_rotated", "filters_kept", "filters_removed_all", "filters_none", "filter_added",
                        "filter_tuned", "filter_same", "filter_removed", "latency"):
                catalog[f"gui.change.{key}"].format(**params)
            for verdict in ("better", "worse", "same"):
                catalog[f"gui.compare.verdict.{verdict}"].format(pct="5")
            self.assertIn("gui.step.preset", catalog)


class FilterTableTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n.set_language("en")
        self.addCleanup(i18n.set_language, "es")

    def test_values_units_and_order(self) -> None:
        rows = filter_table((FilterConfig(HAWKU, True, {"Latency": 2.5, "Frequency": 1000.0}),
                             FilterConfig("Unknown.Plugin", False, {"Flag": True, "Mode": "x"})), load_catalog())
        self.assertEqual(rows[0], ("Hawku Smoothing", True, [("Latency", "2.5", "ms"), ("Frequency", "1000", "Hz")]))
        self.assertEqual(rows[1], ("Unknown.Plugin", False, [("Flag", "on", ""), ("Mode", "x", "")]))
        self.assertEqual(filter_table((), {}), [])

    def test_format_value(self) -> None:
        self.assertEqual(format_value(0.250000), "0.25")
        self.assertEqual(format_value(3.0), "3")
        self.assertEqual(format_value(False), "off")
        self.assertEqual(format_value(7), "7")
        self.assertEqual(format_value(float("inf")), "inf")


if __name__ == "__main__":
    unittest.main()
