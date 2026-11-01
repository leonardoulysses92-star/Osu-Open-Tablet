"""Amount of smoothing: five levels around the community's recommendation, and the "too much" warning."""
from __future__ import annotations

import unittest

from osu_tablet_assistant.engine import community, smoothing as sm
from osu_tablet_assistant.knowledge import filters as kf, presets as kp
from osu_tablet_assistant.models import FilterConfig, Rect, TabletSpec
from osu_tablet_assistant.engine import recommend as rec

CATALOG = kf.load_catalog()
TABLET = TabletSpec("Wacom CTL-472", 152.0, 95.0, 133.0)
AREA = Rect(80, 45, 76.571, 45.167, 180)


def ranked() -> list:
    return community.rank_presets(kp.load_presets().presets, TABLET, AREA, None)


class LadderTests(unittest.TestCase):
    def test_community_ladder_is_centred_on_the_median_and_grows_with_the_level(self) -> None:
        ladder = sm.ladder(CATALOG[sm.RADIAL_TABLET], ranked(), AREA, CATALOG)
        self.assertEqual(ladder.source, "community")
        self.assertGreaterEqual(ladder.n_presets, sm.MIN_PRESETS)
        self.assertEqual(len(ladder.levels), 5)
        radius = [level["OuterRadius"] for level in ladder.levels]
        coefficient = [level["SmoothingCoefficient"] for level in ladder.levels]
        self.assertEqual(radius, sorted(radius))                        # more smoothing = bigger radius ...
        self.assertEqual(coefficient, sorted(coefficient))              # ... and a coefficient closer to 1
        self.assertTrue(all(c <= sm.COEFFICIENT_MAX for c in coefficient))
        self.assertLess(radius[0], radius[2])
        self.assertLess(radius[2], radius[4])
        self.assertTrue(ladder.adjustable)

    def test_without_community_data_the_gentle_values_are_the_middle(self) -> None:
        ladder = sm.ladder(CATALOG[sm.HAWKU], [], AREA, CATALOG)
        self.assertEqual((ladder.source, ladder.n_presets), ("gentle", 0))
        latency = [level["Latency"] for level in ladder.levels]
        self.assertEqual(latency[2], 3.0)                               # the documented gentle start
        self.assertEqual(latency, sorted(latency))
        self.assertGreater(latency[4], latency[0])

    def test_level_of_finds_the_nearest_level_and_unknown_filters_have_none(self) -> None:
        ladder = sm.ladder(CATALOG[sm.HAWKU], [], AREA, CATALOG)
        for i, level in enumerate(ladder.levels):
            self.assertEqual(ladder.level_of(level), i)
        self.assertEqual(ladder.level_of({"Latency": 3.2}), 2)
        flat = sm.ladder(CATALOG[rec.CHATTER_SMOOTH], [], AREA, CATALOG)   # no single "amount" knob
        self.assertFalse(flat.adjustable)
        self.assertIsNone(flat.level_of({}))

    def test_levels_stay_inside_the_catalog_and_never_predict(self) -> None:
        for path in sm.offered(CATALOG):
            spec = CATALOG[path]
            for level in sm.ladder(spec, ranked(), AREA, CATALOG).levels:
                for param in spec.params:
                    value = level.get(param.name)
                    if param.kind in ("float", "int") and isinstance(value, (int, float)):
                        self.assertGreaterEqual(value, param.min if param.min is not None else value, (path, param.name))
                        self.assertLessEqual(value, param.max if param.max is not None else value, (path, param.name))
                self.assertFalse(level.get("PredictionEnabled", False), path)
                self.assertLessEqual(level.get("frameShift", 0.5), 0.5, path)

    def test_enable_starts_from_the_given_base_but_the_players_own_values_win(self) -> None:
        filters, previous, own = sm.enable((), sm.HAWKU, CATALOG, {"Latency": 6.0})
        self.assertEqual((filters[0].settings["Latency"], previous, own), (6.0, None, False))
        mine = (FilterConfig(sm.HAWKU, False, {"Latency": 2.0, "Frequency": 1000.0}),)
        filters, previous, own = sm.enable(mine, sm.HAWKU, CATALOG, {"Latency": 6.0})
        self.assertEqual((filters[0].settings["Latency"], own), (2.0, True))


class AssessmentTests(unittest.TestCase):
    def hawku(self, ms: float) -> FilterConfig:
        return FilterConfig(sm.HAWKU, True, {"Latency": ms, "Frequency": 1000.0})

    def test_levels_ok_warn_and_high(self) -> None:
        self.assertEqual(sm.assess([self.hawku(3.0)], 133.0, 0.04).level, "ok")
        self.assertEqual(sm.assess([self.hawku(sm.WARN_MS + 1)], 133.0, 0.04).level, "warn")
        high = sm.assess([self.hawku(sm.HIGH_MS + 1)], 133.0, 0.04)
        self.assertEqual(high.level, "high")
        self.assertGreaterEqual(high.total_ms, sm.HIGH_MS)

    def test_switched_off_filters_add_nothing(self) -> None:
        off = FilterConfig(sm.HAWKU, False, {"Latency": 40.0, "Frequency": 1000.0})
        self.assertEqual(sm.assess([off], 133.0, 0.04).level, "ok")
        self.assertEqual(sm.total_delay_ms([], 133.0, 0.04), 0.0)


if __name__ == "__main__":
    unittest.main()
