"""Tests for osu_tablet_assistant.engine.adapt (pure feel-axis adaptation)."""
from __future__ import annotations

import unittest
from dataclasses import replace

from osu_tablet_assistant.engine import adapt
from osu_tablet_assistant.engine.adapt import AdaptState, Intent, PolicyMode, TuningPolicy
from osu_tablet_assistant.engine.recommend import (ANTICHATTER, CHATTER_RAW, CHATTER_SMOOTH, HAWKU,
                                                   NOISE_REDUCTION, RADIAL_SCREEN, RADIAL_TABLET, RESAMPLER,
                                                   SATURN_MULTI)
from osu_tablet_assistant.knowledge.filters import load_catalog
from osu_tablet_assistant.models import FilterConfig, ProfileInfo, Rect, TabletSpec

CATALOG = load_catalog()
CTL472 = TabletSpec("Wacom CTL-472", 152.0, 95.0, 133.0)
USER_AREA = Rect(71.66, 41.44, 80.17, 26.72, 180.0)
USER_RADIAL = FilterConfig(RADIAL_TABLET, True, {"OuterRadius": 0.6, "InnerRadius": 0.04,
                                                 "SmoothingCoefficient": 0.88, "SoftKneeScale": 1.0,
                                                 "SmoothingLeakCoefficient": 0.0})


def profile(*filters: FilterConfig, area: Rect = USER_AREA) -> ProfileInfo:
    return ProfileInfo("Wacom CTL-472", "OpenTabletDriver.Desktop.Output.AbsoluteMode",
                       Rect(1920, 1080, 960, 540), area, True, tuple(filters))


def apply(prof: ProfileInfo, res: adapt.StepResult) -> ProfileInfo:
    ch = res.changes
    if ch is None:
        return prof
    return replace(prof, filters=ch.filters if ch.filters is not None else prof.filters,
                   tablet_area=ch.tablet_area or prof.tablet_area)


def run(prof: ProfileInfo, intents, policy=None, tablet=CTL472):
    state = AdaptState()
    results = []
    for intent in intents:
        res = adapt.step(state, prof, intent, policy, CATALOG, tablet)
        results.append(res)
        state, prof = res.state, apply(prof, res)
    return state, prof, results


def value(prof: ProfileInfo, path: str, name: str):
    return next(f for f in prof.filters if f.path == path).settings[name]


def assert_in_bounds(test: unittest.TestCase, prof: ProfileInfo) -> None:
    for fc in prof.filters:
        spec = CATALOG.get(fc.path)
        if spec is None:
            continue
        for name, val in fc.settings.items():
            p = spec.param(name)
            if p is not None and p.kind in ("float", "int"):
                test.assertGreaterEqual(val, p.min, f"{fc.path}.{name}")
                test.assertLessEqual(val, p.max, f"{fc.path}.{name}")


class MoveValueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.outer = CATALOG[RADIAL_TABLET].param("OuterRadius")
        self.coef = CATALOG[RADIAL_TABLET].param("SmoothingCoefficient")

    def test_log_from_zero_goes_to_seed_and_down_snaps_to_min(self) -> None:
        move = adapt.Move("OuterRadius", "log", 0.2, 0.3)
        self.assertAlmostEqual(adapt.move_value(self.outer, move, 0.0, +1, 1.0), 0.3)
        self.assertEqual(adapt.move_value(self.outer, move, 0.15, -1, 1.0), 0.0)

    def test_one_minus_approaches_but_respects_soft_max(self) -> None:
        move = adapt.Move("SmoothingCoefficient", "one_minus", 0.2, soft_max=0.99)
        v = 0.9
        for _ in range(100):
            v = adapt.move_value(self.coef, move, v, +1, 1.0)
        self.assertEqual(v, 0.99)

    def test_tiny_gain_still_moves_one_grid_step(self) -> None:
        move = adapt.Move("OuterRadius", "log", 0.2, 0.3)
        self.assertAlmostEqual(adapt.move_value(self.outer, move, 1.0, +1, 0.01), 1.05)
        self.assertAlmostEqual(adapt.move_value(self.outer, move, 1.0, -1, 0.01), 0.95)

    def test_no_room_returns_current(self) -> None:
        move = adapt.Move("OuterRadius", "log", 0.2, 0.3, soft_max=3.0)
        self.assertEqual(adapt.move_value(self.outer, move, 3.0, +1, 1.0), 3.0)
        self.assertEqual(adapt.move_value(self.outer, move, 0.0, -1, 1.0), 0.0)

    def test_int_param_moves_by_whole_units(self) -> None:
        samples = CATALOG[NOISE_REDUCTION].param("Samples")
        move = adapt.Move("Samples", "linear", 1.0, soft_max=15)
        self.assertEqual(adapt.move_value(samples, move, 10, +1, 0.125), 11)
        self.assertEqual(adapt.move_value(samples, move, 10, -1, 1.0), 9)

    def test_fmt(self) -> None:
        self.assertEqual(adapt.fmt(2.0), "2")
        self.assertEqual(adapt.fmt(0.125), "0.125")
        self.assertEqual(adapt.fmt(True), "on")
        self.assertEqual(adapt.fmt(-0.0), "0")


class StepTest(unittest.TestCase):
    def test_smoother_and_responsive_move_radial_coherently(self) -> None:
        prof = profile(USER_RADIAL)
        _, smoother, res = run(prof, [Intent.SMOOTHER])
        self.assertGreater(value(smoother, RADIAL_TABLET, "OuterRadius"), 0.6)
        self.assertGreater(value(smoother, RADIAL_TABLET, "SmoothingCoefficient"), 0.88)
        changes = [r for r in res[0].reasons if r.key == "adapt.change"]
        self.assertEqual({r.params["param"] for r in changes}, {"OuterRadius", "SmoothingCoefficient"})
        self.assertTrue(all("old" in r.params and "new" in r.params for r in changes))
        _, faster, _ = run(prof, [Intent.RESPONSIVE])
        self.assertLess(value(faster, RADIAL_TABLET, "OuterRadius"), 0.6)
        self.assertLess(value(faster, RADIAL_TABLET, "SmoothingCoefficient"), 0.88)

    def test_precise_moves_only_the_deadzone(self) -> None:
        _, prof, _ = run(profile(USER_RADIAL), [Intent.PRECISE])
        self.assertGreater(value(prof, RADIAL_TABLET, "InnerRadius"), 0.04)
        self.assertEqual(value(prof, RADIAL_TABLET, "OuterRadius"), 0.6)

    def test_other_settings_and_keys_are_untouched(self) -> None:
        _, prof, _ = run(profile(USER_RADIAL), [Intent.SMOOTHER])
        self.assertEqual(value(prof, RADIAL_TABLET, "SoftKneeScale"), 1.0)
        self.assertEqual(value(prof, RADIAL_TABLET, "SmoothingLeakCoefficient"), 0.0)

    def test_deterministic(self) -> None:
        seq = [Intent.SMOOTHER, Intent.RESPONSIVE, Intent.PRECISE, Intent.SMOOTHER]
        self.assertEqual(run(profile(USER_RADIAL), seq)[1], run(profile(USER_RADIAL), seq)[1])

    def test_undo_and_keep_are_not_step_intents(self) -> None:
        with self.assertRaises(ValueError):
            adapt.step(AdaptState(), profile(USER_RADIAL), Intent.UNDO, catalog=CATALOG)

    def test_inner_radius_never_exceeds_outer(self) -> None:
        radial = FilterConfig(RADIAL_TABLET, True, {"OuterRadius": 0.3, "InnerRadius": 0.28,
                                                    "SmoothingCoefficient": 0.9})
        _, prof, results = run(profile(radial), [Intent.RESPONSIVE] * 4 + [Intent.PRECISE] * 6)
        self.assertLessEqual(value(prof, RADIAL_TABLET, "InnerRadius"), value(prof, RADIAL_TABLET, "OuterRadius"))
        self.assertTrue(any(r.key == "adapt.inner_capped" for res in results for r in res.reasons))

    def test_secondary_filter_used_when_primary_is_at_its_limit(self) -> None:
        radial = FilterConfig(RADIAL_TABLET, True, {"OuterRadius": 0.0, "SmoothingCoefficient": 0.5})
        hawku = FilterConfig(HAWKU, True, {"Latency": 6.0, "Frequency": 1000.0})
        _, prof, _ = run(profile(radial, hawku), [Intent.RESPONSIVE])
        self.assertLess(value(prof, HAWKU, "Latency"), 6.0)
        self.assertEqual(value(prof, RADIAL_TABLET, "OuterRadius"), 0.0)

    def test_everything_at_limit_reports_it(self) -> None:
        hawku = FilterConfig(HAWKU, True, {"Latency": 0.0})
        _, _, res = run(profile(hawku), [Intent.RESPONSIVE])
        self.assertIsNone(res[0].changes)
        self.assertEqual(res[0].reasons[0].key, "adapt.limit.less")

    def test_hawku_rest_reuses_smoothing_and_says_so(self) -> None:
        _, prof, res = run(profile(FilterConfig(HAWKU, True, {"Latency": 4.0})), [Intent.PRECISE])
        self.assertGreater(value(prof, HAWKU, "Latency"), 4.0)
        self.assertIn("adapt.shared_axis", [r.key for r in res[0].reasons])

    def test_resampler_second_tier_raises_prediction_once_latency_is_zero(self) -> None:
        tr = FilterConfig(RESAMPLER, True, {"frameShift": 0.3, "latency": 0.0, "followRadius": 0.0})
        _, prof, _ = run(profile(tr), [Intent.RESPONSIVE])
        self.assertGreater(value(prof, RESAMPLER, "frameShift"), 0.3)
        _, prof, res = run(profile(tr), [Intent.RESPONSIVE] * 20)   # never more prediction than the default
        self.assertEqual(value(prof, RESAMPLER, "frameShift"), 0.5)
        self.assertEqual(res[-1].reasons[0].key, "adapt.limit.less")
        tr = replace(tr, settings={**tr.settings, "frameShift": 0.5})
        tr2 = replace(tr, settings={**tr.settings, "latency": 2.0})
        _, prof2, _ = run(profile(tr2), [Intent.RESPONSIVE])
        self.assertLess(value(prof2, RESAMPLER, "latency"), 2.0)
        self.assertEqual(value(prof2, RESAMPLER, "frameShift"), 0.5)

    def test_every_supported_family_moves_or_reports_limit_within_bounds(self) -> None:
        paths = (RADIAL_TABLET, RADIAL_SCREEN, HAWKU, ANTICHATTER, RESAMPLER, NOISE_REDUCTION,
                 CHATTER_RAW, CHATTER_SMOOTH)
        for path in paths:
            for intent in (Intent.SMOOTHER, Intent.RESPONSIVE, Intent.PRECISE, Intent.LOOSER):
                with self.subTest(path=path, intent=intent):
                    fc = FilterConfig(path, True, CATALOG[path].defaults())
                    _, prof, results = run(profile(fc), [intent] * 60)
                    assert_in_bounds(self, prof)
                    self.assertTrue(results[0].reasons)

    def test_long_runs_stay_inside_hard_bounds(self) -> None:
        _, prof, _ = run(profile(USER_RADIAL), [Intent.SMOOTHER] * 200 + [Intent.PRECISE] * 200)
        assert_in_bounds(self, prof)
        self.assertLessEqual(value(prof, RADIAL_TABLET, "SmoothingCoefficient"), 0.99)
        _, prof, _ = run(profile(USER_RADIAL), [Intent.RESPONSIVE] * 200 + [Intent.LOOSER] * 200)
        assert_in_bounds(self, prof)
        self.assertEqual(value(prof, RADIAL_TABLET, "OuterRadius"), 0.0)

    def test_unsupported_and_unknown_filters_are_skipped_with_reason(self) -> None:
        saturn = FilterConfig(SATURN_MULTI, True, {})
        unknown = FilterConfig("Some.Plugin.Filter", True, {"X": 1})
        _, prof, res = run(profile(saturn, unknown, USER_RADIAL), [Intent.SMOOTHER])
        keys = {r.key for r in res[0].skipped}
        self.assertEqual(keys, {"adapt.skip.saturn", "adapt.skip.unknown"})
        self.assertEqual(next(f for f in prof.filters if f.path == "Some.Plugin.Filter").settings, {"X": 1})


class ConvergenceTest(unittest.TestCase):
    def test_alternating_feedback_shrinks_steps_and_converges(self) -> None:
        seq = [Intent.SMOOTHER, Intent.SMOOTHER] + [Intent.RESPONSIVE, Intent.SMOOTHER] * 3
        state, prof, results = run(profile(USER_RADIAL), seq)
        self.assertTrue(results[-1].converged)
        self.assertTrue(adapt.is_converged(state))
        self.assertLessEqual(state.axis("motion").gain, adapt.CONVERGE_MAX_GAIN)
        self.assertIn("adapt.converged", [r.key for r in results[-1].reasons])
        steps = [abs(float(r.params["new"]) - float(r.params["old"])) for res in results for r in res.reasons
                 if r.key == "adapt.change" and r.params["param"] == "SmoothingCoefficient"]
        self.assertLessEqual(steps[-1], steps[0])

    def test_same_direction_never_converges(self) -> None:
        state, _, results = run(profile(USER_RADIAL), [Intent.SMOOTHER] * 8)
        self.assertFalse(adapt.is_converged(state))
        self.assertEqual(state.axis("motion").gain, 1.0)
        self.assertFalse(any(r.converged for r in results))

    def test_gain_regrows_after_a_streak(self) -> None:
        state, _, _ = run(profile(USER_RADIAL), [Intent.SMOOTHER, Intent.RESPONSIVE, Intent.RESPONSIVE,
                                                 Intent.RESPONSIVE])
        self.assertGreater(state.axis("motion").gain, 0.5)

    def test_axes_are_independent(self) -> None:
        state, _, _ = run(profile(USER_RADIAL), [Intent.SMOOTHER, Intent.RESPONSIVE] * 3 + [Intent.PRECISE])
        self.assertEqual(state.axis("rest").gain, 1.0)
        self.assertEqual(state.change_count, 7)

    def test_accept_records_points_and_detects_repeats(self) -> None:
        prof = profile(USER_RADIAL)
        state = adapt.accept(AdaptState(), prof)
        self.assertFalse(adapt.repeated_acceptance(state))
        state = adapt.accept(state, prof)
        self.assertTrue(adapt.repeated_acceptance(state))
        self.assertIn((RADIAL_TABLET, "OuterRadius", 0.6), state.accepted[-1].values)


class PolicyTest(unittest.TestCase):
    def test_tune_only_never_adds(self) -> None:
        policy = TuningPolicy(PolicyMode.TUNE_ONLY, frozenset({RADIAL_TABLET}))
        _, prof, res = run(profile(), [Intent.SMOOTHER], policy)
        self.assertIsNone(res[0].changes)
        self.assertEqual(res[0].reasons[0].key, "adapt.nothing_to_tune")
        self.assertEqual(prof.filters, ())

    def test_installed_adds_an_installed_filter_in_chain_order(self) -> None:
        tr = FilterConfig(RESAMPLER, True, {"frameShift": 0.5})
        policy = TuningPolicy(PolicyMode.INSTALLED, frozenset({RADIAL_TABLET}))
        tr_only = profile(replace(tr, settings={"frameShift": 0.5, "followRadius": 2.0}))
        _, prof, res = run(tr_only, [Intent.PRECISE], policy)   # followRadius at max: nothing to move
        self.assertEqual(res[0].reasons[0].key, "adapt.limit.more")
        _, prof, res = run(profile(), [Intent.PRECISE], policy)
        self.assertEqual([f.path for f in prof.filters], [RADIAL_TABLET])
        self.assertEqual(res[0].reasons[0].key, "adapt.added")
        assert_in_bounds(self, prof)
        prof = replace(prof, filters=adapt.insert_in_chain((tr,), prof.filters[0]))
        self.assertEqual([f.path for f in prof.filters], [RESAMPLER, RADIAL_TABLET])

    def test_installed_reenables_a_disabled_filter_tune_only_does_not(self) -> None:
        off = FilterConfig(HAWKU, False, {"Latency": 4.0})
        _, prof, res = run(profile(off), [Intent.SMOOTHER], TuningPolicy(PolicyMode.INSTALLED))
        self.assertTrue(prof.filters[0].enabled)
        _, prof, res = run(profile(off), [Intent.SMOOTHER], TuningPolicy(PolicyMode.TUNE_ONLY))
        self.assertFalse(prof.filters[0].enabled)
        self.assertIn("adapt.skip.disabled", [r.key for r in res[0].skipped])

    def test_allow_new_only_suggests_missing_plugins(self) -> None:
        _, prof, res = run(profile(), [Intent.SMOOTHER], TuningPolicy(PolicyMode.ALLOW_NEW, frozenset()))
        self.assertIsNone(res[0].changes)
        self.assertEqual(len(res[0].suggestions), 1)
        self.assertEqual(res[0].suggestions[0].path, RADIAL_TABLET)
        self.assertEqual(prof.filters, ())

    def test_less_with_no_filters_is_already_direct(self) -> None:
        _, _, res = run(profile(), [Intent.RESPONSIVE], TuningPolicy(PolicyMode.ALLOW_NEW))
        self.assertEqual(res[0].reasons[0].key, "adapt.nothing_to_reduce")
        self.assertEqual(res[0].suggestions, ())

    def test_policy_predicates(self) -> None:
        p = TuningPolicy(PolicyMode.ALLOW_NEW, frozenset({HAWKU}))
        self.assertTrue(p.may_add(HAWKU))
        self.assertFalse(p.may_add(RADIAL_TABLET))
        self.assertTrue(p.may_suggest(RADIAL_TABLET))
        self.assertFalse(p.may_suggest(HAWKU))
        self.assertFalse(TuningPolicy(PolicyMode.INSTALLED).may_suggest(RADIAL_TABLET))

    def test_insert_in_chain(self) -> None:
        chain = (FilterConfig(RESAMPLER), FilterConfig(HAWKU))
        out = adapt.insert_in_chain(chain, FilterConfig(RADIAL_TABLET))
        self.assertEqual([f.path for f in out], [RESAMPLER, RADIAL_TABLET, HAWKU])
        out = adapt.insert_in_chain((FilterConfig("X.Unknown"),), FilterConfig(HAWKU))
        self.assertEqual(out[-1].path, HAWKU)


class AreaTest(unittest.TestCase):
    def test_nudge_keeps_aspect_center_rotation(self) -> None:
        rect, reasons = adapt.nudge_area(USER_AREA, CTL472, +1)
        self.assertAlmostEqual(rect.width, 71.66 * 1.02, places=2)
        self.assertAlmostEqual(rect.width / rect.height, 71.66 / 41.44, places=3)
        self.assertEqual((rect.x, rect.y, rect.rotation), (USER_AREA.x, USER_AREA.y, 180.0))
        self.assertEqual(reasons[0].key, "adapt.area.change")
        small, _ = adapt.nudge_area(USER_AREA, CTL472, -1)
        self.assertLess(small.width, USER_AREA.width)

    def test_growth_is_capped_by_the_tablet(self) -> None:
        rect = Rect(140.0, 80.0, 76.0, 47.5)
        for _ in range(20):
            new, reasons = adapt.nudge_area(rect, CTL472, +1)
            if new is None:
                self.assertEqual(reasons[0].key, "adapt.area.at_max")
                break
            rect = new
            self.assertLessEqual(rect.x + rect.width / 2, 152.0 + 1e-9)
            self.assertLessEqual(rect.y + rect.height / 2, 95.0 + 1e-9)
        else:
            self.fail("area kept growing")

    def test_rotated_90_uses_swapped_extent(self) -> None:
        self.assertAlmostEqual(adapt.max_area_scale(Rect(80, 40, 76, 47.5, 90), CTL472), 47.5 / 40, places=6)

    def test_minimum_and_unknown(self) -> None:
        self.assertIsNone(adapt.nudge_area(Rect(10.1, 5, 50, 50), CTL472, -1)[0])
        self.assertEqual(adapt.nudge_area(Rect(0, 0, 0, 0), CTL472, +1)[1][0].key, "adapt.area.unknown")

    def test_area_steps_converge_like_filters(self) -> None:
        seq = [Intent.AREA_BIGGER, Intent.AREA_SMALLER] * 3
        state, prof, results = run(profile(USER_RADIAL), seq)
        self.assertTrue(adapt.axis_converged(state.axis("area")))
        self.assertAlmostEqual(prof.tablet_area.width / prof.tablet_area.height, 71.66 / 41.44, places=2)
        pcts = [abs(float(r.reasons[0].params["pct"])) for r in results]
        self.assertLess(pcts[-1], pcts[0])


FAMILY_PATHS = (RADIAL_TABLET, RADIAL_SCREEN, HAWKU, ANTICHATTER, RESAMPLER, NOISE_REDUCTION, CHATTER_RAW,
                CHATTER_SMOOTH)


def run_level(prof: ProfileInfo, intents, level, policy=None, tablet=CTL472):
    state = AdaptState()
    results = []
    for intent in intents:
        res = adapt.step(state, prof, intent, policy, CATALOG, tablet, level=level)
        results.append(res)
        state, prof = res.state, apply(prof, res)
    return state, prof, results


def assert_within_soft_limits(test: unittest.TestCase, before: ProfileInfo, after: ProfileInfo) -> None:
    """Every value the engine changed is inside the catalog range AND the soft limits of its moves."""
    for old, new in zip(before.filters, after.filters):
        spec = CATALOG.get(new.path)
        if spec is None:
            continue
        moves = {m.param: m for tiers in adapt.FAMILIES.get(new.path, {}).values() for tier in tiers for m in tier}
        for name, val in new.settings.items():
            if name not in moves or old.settings.get(name) == val:
                continue
            lo, hi = adapt._limits(spec.param(name), moves[name])
            test.assertGreaterEqual(val, lo, f"{new.path}.{name}")
            test.assertLessEqual(val, hi, f"{new.path}.{name}")


class StepLevelTest(unittest.TestCase):
    def test_levels_and_cycle(self) -> None:
        self.assertEqual(adapt.STEP_LEVELS, ("normal", "fine", "very_fine"))
        self.assertEqual(adapt.step_level(None), "normal")
        self.assertEqual([adapt.next_step_level(x) for x in adapt.STEP_LEVELS], ["fine", "very_fine", "normal"])
        with self.assertRaises(ValueError):
            adapt.step_level("ultra")
        with self.assertRaises(ValueError):
            adapt.step(AdaptState(), profile(USER_RADIAL), Intent.SMOOTHER, catalog=CATALOG, level="ultra")
        self.assertEqual([adapt.area_step_pct(x) for x in adapt.STEP_LEVELS], [2.0, 1.0, 0.5])

    def test_normal_level_is_the_original_behaviour(self) -> None:
        seq = [Intent.SMOOTHER, Intent.SMOOTHER, Intent.RESPONSIVE, Intent.PRECISE, Intent.AREA_BIGGER,
               Intent.LOOSER, Intent.SMOOTHER, Intent.AREA_SMALLER]
        self.assertEqual(run(profile(USER_RADIAL), seq)[:2], run_level(profile(USER_RADIAL), seq, "normal")[:2])

    def test_fine_grids(self) -> None:
        latency = CATALOG[HAWKU].param("Latency")                     # catalog step 0.5 ms
        self.assertEqual([adapt.grid_step(latency, x) for x in adapt.STEP_LEVELS], [0.5, 0.1, 0.05])
        threshold = CATALOG[NOISE_REDUCTION].param("DistanceThreshold")   # 0.05 mm
        self.assertEqual([adapt.grid_step(threshold, x) for x in adapt.STEP_LEVELS], [0.05, 0.01, 0.005])
        samples = CATALOG[NOISE_REDUCTION].param("Samples")           # ints keep whole steps
        self.assertEqual([adapt.grid_step(samples, x) for x in adapt.STEP_LEVELS], [1.0, 1.0, 1.0])
        coef = CATALOG[RADIAL_TABLET].param("SmoothingCoefficient")    # 0.001: the absolute floor 0.0001 wins
        self.assertEqual(adapt.grid_step(coef, "very_fine"), 0.0001)
        self.assertGreaterEqual(min(adapt.grid_step(CATALOG[p].param(n), "very_fine")
                                    for p in FAMILY_PATHS for n, _v in CATALOG[p].defaults().items()
                                    if CATALOG[p].param(n).kind == "float" and CATALOG[p].param(n).step),
                                0.0001)

    def test_fine_steps_are_smaller_than_normal_ones(self) -> None:
        latency = CATALOG[HAWKU].param("Latency")
        move = adapt.FAMILIES[HAWKU]["motion"][0][0]
        sizes = [adapt.move_value(latency, move, 4.0, +1, 1.0, x) - 4.0 for x in adapt.STEP_LEVELS]
        self.assertEqual([round(s, 6) for s in sizes], [1.0, 0.4, 0.2])          # Hawku 4 ms -> 5 / 4.4 / 4.2
        down = [4.0 - adapt.move_value(latency, move, 4.0, -1, 1.0, x) for x in adapt.STEP_LEVELS]
        self.assertGreater(down[0], down[1])
        self.assertGreater(down[1], down[2])
        for path in FAMILY_PATHS:
            for intent in (Intent.SMOOTHER, Intent.RESPONSIVE, Intent.PRECISE, Intent.LOOSER):
                with self.subTest(path=path, intent=intent):
                    prof = profile(FilterConfig(path, True, CATALOG[path].defaults()))
                    deltas = {}
                    for level in adapt.STEP_LEVELS:
                        _, after, _ = run_level(prof, [intent], level)
                        old, new = prof.filters[0].settings, after.filters[0].settings
                        deltas[level] = sum(abs(float(new[k]) - float(old[k])) for k in old
                                            if isinstance(old[k], (int, float)) and not isinstance(old[k], bool))
                    self.assertLessEqual(deltas["fine"], deltas["normal"] + 1e-9)
                    self.assertLessEqual(deltas["very_fine"], deltas["fine"] + 1e-9)

    def test_tiny_fine_moves_still_move_one_fine_grid_step(self) -> None:
        latency = CATALOG[HAWKU].param("Latency")
        move = adapt.FAMILIES[HAWKU]["motion"][0][0]
        self.assertAlmostEqual(adapt.move_value(latency, move, 4.0, +1, 0.001, "fine"), 4.1)
        self.assertAlmostEqual(adapt.move_value(latency, move, 4.0, -1, 0.001, "very_fine"), 3.95)

    def test_ints_stay_ints_and_move_whole_units(self) -> None:
        nr = FilterConfig(NOISE_REDUCTION, True, CATALOG[NOISE_REDUCTION].defaults())
        for level in ("fine", "very_fine"):
            _, prof, _ = run_level(profile(nr), [Intent.SMOOTHER] * 3, level)
            samples = value(prof, NOISE_REDUCTION, "Samples")
            self.assertIsInstance(samples, int)
            self.assertEqual(samples, CATALOG[NOISE_REDUCTION].param("Samples").default + 3)

    def test_fine_runs_stay_in_catalog_and_soft_limits(self) -> None:
        for level in ("fine", "very_fine"):
            for path in FAMILY_PATHS:
                for intent in (Intent.SMOOTHER, Intent.RESPONSIVE, Intent.PRECISE, Intent.LOOSER):
                    with self.subTest(level=level, path=path, intent=intent):
                        start = profile(FilterConfig(path, True, CATALOG[path].defaults()))
                        _, prof, results = run_level(start, [intent] * 150, level)
                        assert_in_bounds(self, prof)
                        assert_within_soft_limits(self, start, prof)
                        self.assertTrue(results[0].reasons)
            _, prof, _ = run_level(profile(USER_RADIAL), [Intent.SMOOTHER] * 300, level)
            self.assertLessEqual(value(prof, RADIAL_TABLET, "SmoothingCoefficient"), 0.99)

    def test_values_are_rounded_to_the_fine_grid(self) -> None:
        _, prof, _ = run_level(profile(USER_RADIAL), [Intent.SMOOTHER, Intent.PRECISE, Intent.RESPONSIVE], "fine")
        for name in ("OuterRadius", "InnerRadius", "SmoothingCoefficient"):
            v = value(prof, RADIAL_TABLET, name)
            grid = adapt.grid_step(CATALOG[RADIAL_TABLET].param(name), "fine")
            self.assertAlmostEqual(v / grid, round(v / grid), places=6, msg=name)
            self.assertEqual(v, round(v, 6))

    def test_fine_level_is_deterministic(self) -> None:
        seq = [Intent.SMOOTHER, Intent.RESPONSIVE, Intent.PRECISE, Intent.AREA_BIGGER, Intent.SMOOTHER]
        self.assertEqual(run_level(profile(USER_RADIAL), seq, "very_fine")[:2],
                         run_level(profile(USER_RADIAL), seq, "very_fine")[:2])

    def test_fine_convergence_uses_the_same_gain_rule(self) -> None:
        seq = [Intent.SMOOTHER, Intent.SMOOTHER] + [Intent.RESPONSIVE, Intent.SMOOTHER] * 3
        normal, _, _ = run_level(profile(USER_RADIAL), seq, "normal")
        for level in ("fine", "very_fine"):
            state, _, results = run_level(profile(USER_RADIAL), seq, level)
            self.assertTrue(results[-1].converged, level)
            self.assertEqual(state.axis("motion").gain, normal.axis("motion").gain)   # level multiplies, not replaces
            self.assertIn("adapt.converged", [r.key for r in results[-1].reasons])
            self.assertLess(abs(state.axis("motion").position), abs(normal.axis("motion").position) + 1e-9)

    def test_area_fine_steps(self) -> None:
        for level, pct in (("normal", 2.0), ("fine", 1.0), ("very_fine", 0.5)):
            rect, reasons = adapt.nudge_area(USER_AREA, CTL472, +1, 1.0, level)
            self.assertAlmostEqual(rect.width, round(71.66 * (1 + pct / 100), 3), places=3)
            self.assertAlmostEqual(rect.width / rect.height, 71.66 / 41.44, places=3)
            self.assertEqual((rect.x, rect.y, rect.rotation), (USER_AREA.x, USER_AREA.y, 180.0))
        _, prof, results = run_level(profile(USER_RADIAL), [Intent.AREA_BIGGER, Intent.AREA_SMALLER] * 3, "fine")
        self.assertTrue(adapt.axis_converged(results[-1].state.axis("area")))


class PollTest(unittest.TestCase):
    def test_mapping(self) -> None:
        self.assertEqual(adapt.poll_to_intents("shaky", "jittery"), (Intent.SMOOTHER, Intent.PRECISE))
        self.assertEqual(adapt.poll_to_intents("laggy", "steady"), (Intent.RESPONSIVE,))
        self.assertEqual(adapt.poll_to_intents("good", "steady"), ())
        self.assertEqual(adapt.poll_to_intents(None, None), ())

    def test_rejects_unknown_answers(self) -> None:
        with self.assertRaises(ValueError):
            adapt.poll_to_intents("meh", None)
        with self.assertRaises(ValueError):
            adapt.poll_to_intents(None, "wobbly")


if __name__ == "__main__":
    unittest.main()
