"""engine.judge: stable aggregation of repeated runs, words instead of points, ties.

Also the session side (runs accumulate instead of overwriting) and the
deterministic test seeds that remove one source of measurement noise.
"""
from __future__ import annotations

import random
import unittest

from osu_tablet_assistant.engine import judge, recommend
from osu_tablet_assistant.gui import live_tests
from osu_tablet_assistant.gui.session import CANDIDATE_KINDS
from osu_tablet_assistant.models import CandidateResult, MetricsReport, UserPreferences

PREFS = UserPreferences()


def still(v: float) -> MetricsReport:
    return MetricsReport("stillness", 100, 5.0, jitter_rms_mm=v)


def track(v: float) -> MetricsReport:
    return MetricsReport("tracking", 100, 8.0, path_error_mean_mm=v, path_error_p95_mm=2 * v,
                         smoothness_jerk=1.0, lag_ms_estimate=10.0)


def aim(v: float) -> MetricsReport:
    return MetricsReport("aim", 20, 10.0, aim_error_mean_mm=v, aim_time_ms_mean=400.0)


def result(cid: str, s: float, tr: float, a: float, repeats: int = 1, rating: int | None = None
           ) -> CandidateResult:
    runs = tuple(r for _ in range(repeats) for r in (still(s), track(tr), aim(a)))
    return CandidateResult(cid, judge.aggregate_by_kind(runs), user_rating=rating, runs=runs)


class AggregateTests(unittest.TestCase):
    def test_median_is_stable_with_good_bad_good(self) -> None:
        merged = judge.aggregate_runs([still(0.02), still(0.20), still(0.021)])
        self.assertAlmostEqual(merged.jitter_rms_mm, 0.021)
        self.assertEqual(merged.extras["runs"], 3)
        self.assertEqual(merged.sample_count, 300)

    def test_one_bad_repeat_does_not_overwrite_a_good_one(self) -> None:
        good = judge.aggregate_runs([aim(1.0), aim(1.1)])
        after_bad = judge.aggregate_runs([aim(1.0), aim(1.1), aim(6.0)])
        self.assertLess(abs(after_bad.aim_error_mean_mm - good.aim_error_mean_mm), 0.15)

    def test_extreme_run_dropped_from_four(self) -> None:
        merged = judge.aggregate_runs([still(0.02), still(0.021), still(0.5), still(0.022)])
        self.assertEqual(merged.extras["runs"], 3)
        self.assertAlmostEqual(merged.jitter_rms_mm, 0.021)

    def test_single_run_is_returned_unchanged_and_empty_is_none(self) -> None:
        one = still(0.03)
        merged = judge.aggregate_runs([one])
        self.assertEqual(merged.jitter_rms_mm, 0.03)
        self.assertIsNone(judge.aggregate_runs([]))

    def test_none_and_nan_fields_are_ignored(self) -> None:
        merged = judge.aggregate_runs([still(float("nan")), still(0.04), MetricsReport("stillness", 1, 1.0)])
        self.assertEqual(merged.jitter_rms_mm, 0.04)

    def test_by_kind_keeps_first_appearance_order(self) -> None:
        reports = judge.aggregate_by_kind([still(0.03), aim(1.0), still(0.04), track(0.5)])
        self.assertEqual([r.test_kind for r in reports], ["stillness", "aim", "tracking"])


class GradeAndReliabilityTests(unittest.TestCase):
    def test_grade_thresholds(self) -> None:
        self.assertEqual(judge.grade_for(judge.GRADE_EXCELLENT), "excellent")
        self.assertEqual(judge.grade_for(judge.GRADE_GOOD), "good")
        self.assertEqual(judge.grade_for(judge.GRADE_NORMAL), "normal")
        self.assertEqual(judge.grade_for(judge.GRADE_NORMAL - 0.1), "weak")
        self.assertIsNone(judge.grade_for(None))

    def test_reliability_levels(self) -> None:
        one = [still(0.03), track(0.6), aim(1.0)]
        self.assertEqual(judge.reliability(one[:2], CANDIDATE_KINDS), "few")
        self.assertEqual(judge.reliability(one, CANDIDATE_KINDS), "repeat")
        steady = one + [still(0.031), track(0.62), aim(1.02)]
        self.assertEqual(judge.reliability(steady, CANDIDATE_KINDS), "reliable")
        erratic = one + [still(0.03), track(0.6), aim(4.0)]
        self.assertEqual(judge.reliability(erratic, CANDIDATE_KINDS), "repeat")


class RankAndTieTests(unittest.TestCase):
    def rank(self, *results: CandidateResult) -> list[CandidateResult]:
        return recommend.rank(list(results), PREFS, required_kinds=CANDIDATE_KINDS)

    def test_grade_does_not_depend_on_other_candidates(self) -> None:
        a = result("a", 0.03, 0.6, 1.0)
        alone = judge.summarize(self.rank(a), PREFS, CANDIDATE_KINDS).summaries["a"]
        b = result("b", 0.01, 0.2, 0.4)
        together = judge.summarize(self.rank(result("a", 0.03, 0.6, 1.0), b), PREFS,
                                   CANDIDATE_KINDS).summaries["a"]
        self.assertEqual(alone.grade, together.grade)
        self.assertEqual(alone.objective, together.objective)

    def test_incomplete_never_beats_complete(self) -> None:
        full_weak = result("full", 0.12, 2.0, 4.0)
        partial = CandidateResult("partial", (still(0.005),), runs=(still(0.005),))
        ranked = self.rank(partial, full_weak)
        self.assertEqual([r.candidate_id for r in ranked], ["full", "partial"])
        verdict = judge.summarize(ranked, PREFS, CANDIDATE_KINDS)
        self.assertFalse(verdict.summaries["partial"].complete)
        self.assertIsNone(verdict.summaries["partial"].grade)
        self.assertEqual(verdict.top, ("full",))

    def test_outlier_repeat_does_not_flip_clearly_different_options(self) -> None:
        good = lambda: [r for _ in range(3) for r in (still(0.02), track(0.5), aim(0.9))]   # noqa: E731
        bad = lambda: [r for _ in range(3) for r in (still(0.10), track(2.0), aim(3.5))]    # noqa: E731
        runs_g, runs_b = good(), bad()
        runs_g[2] = aim(8.0)                       # one terrible aim run on the good option
        ranked = self.rank(
            CandidateResult("bad", judge.aggregate_by_kind(runs_b), runs=tuple(runs_b)),
            CandidateResult("good", judge.aggregate_by_kind(runs_g), runs=tuple(runs_g)))
        self.assertEqual(ranked[0].candidate_id, "good")
        verdict = judge.summarize(ranked, PREFS, CANDIDATE_KINDS)
        self.assertFalse(verdict.tie)

    def test_close_options_tie_and_all_are_starred(self) -> None:
        ranked = self.rank(result("a", 0.030, 0.60, 1.00), result("b", 0.031, 0.61, 1.01),
                           result("c", 0.120, 2.00, 4.00))
        verdict = judge.summarize(ranked, PREFS, CANDIDATE_KINDS)
        self.assertTrue(verdict.tie)
        self.assertEqual(set(verdict.top), {"a", "b"})
        self.assertTrue(verdict.summaries["a"].best and verdict.summaries["b"].best)
        self.assertFalse(verdict.summaries["c"].best)

    def test_distinct_options_have_a_single_winner(self) -> None:
        ranked = self.rank(result("a", 0.01, 0.2, 0.4), result("b", 0.12, 2.0, 4.0))
        verdict = judge.summarize(ranked, PREFS, CANDIDATE_KINDS)
        self.assertEqual(verdict.top, ("a",))
        self.assertFalse(verdict.tie)

    def test_noise_assumed_for_single_runs_and_measured_for_repeats(self) -> None:
        single = result("a", 0.03, 0.6, 1.0)
        self.assertEqual(judge.score_noise(single.runs, CANDIDATE_KINDS, PREFS), judge.TIE_SINGLE_RUN_POINTS)
        steady = result("a", 0.03, 0.6, 1.0, repeats=3)
        self.assertEqual(judge.score_noise(steady.runs, CANDIDATE_KINDS, PREFS), judge.TIE_MIN_POINTS)
        erratic = tuple(r for v in (0.5, 3.0, 1.0) for r in (still(0.03), track(0.6), aim(v)))
        self.assertGreater(judge.score_noise(erratic, CANDIDATE_KINDS, PREFS), judge.TIE_MIN_POINTS)

    def test_rank_without_required_kinds_keeps_old_behaviour(self) -> None:
        r = CandidateResult("x", (still(0.03),))
        recommend.rank([r], PREFS)
        self.assertTrue(r.complete)
        self.assertIsNotNone(r.objective)

    def test_rating_moves_score_but_not_the_grade(self) -> None:
        plain = self.rank(result("a", 0.03, 0.6, 1.0))[0]
        rated = self.rank(result("a", 0.03, 0.6, 1.0, rating=5))[0]
        self.assertGreater(rated.score, plain.score)
        self.assertEqual(rated.objective, plain.objective)


class DeterministicTestsTests(unittest.TestCase):
    def test_same_seed_for_every_run_of_a_kind(self) -> None:
        rect = (0, 0, 1920, 1080)
        a = live_tests.plan_aim_targets(rect, 100.0, 10, live_tests.test_rng("aim"))
        b = live_tests.plan_aim_targets(rect, 100.0, 10, live_tests.test_rng("aim"))
        self.assertEqual(a, b)
        self.assertNotEqual(a, live_tests.plan_aim_targets(rect, 100.0, 10, random.Random(1)))

    def test_warmup_targets_are_played_but_not_scored(self) -> None:
        rect = (0, 0, 1920, 1080)
        s = live_tests.new_aim_session(rect, 0.1, live_tests.test_rng("aim"), cursor=(960.0, 540.0),
                                       warmup=live_tests.AIM_WARMUP_TARGETS)
        self.assertEqual(len(s.targets), live_tests.AIM_TARGET_COUNT + live_tests.AIM_WARMUP_TARGETS)
        s.start(0.0, (960.0, 540.0))
        now = 0.0
        for target in list(s.targets):
            now += 0.5
            s.observe(now - 0.1, target[0] + 200, target[1] + 200)   # arrive from far away
            s.observe(now, *target)
        self.assertTrue(s.done)
        report = live_tests.build_report("aim", 0.1, [], started_at=0.0, aim=s)
        self.assertEqual(report.sample_count, live_tests.AIM_TARGET_COUNT)


class SessionRepeatTests(unittest.TestCase):
    """Repeating a test accumulates runs (real Session, fake pen/OTD from the integration harness)."""

    def test_repeat_accumulates_and_scores_stay_comparable(self) -> None:
        from tests.test_integration import Harness

        h = Harness(self, dry_run=False)
        s = h.drive_to_testing(self)
        cand = s.candidates[0]
        s.apply_candidate(cand, confirmed=True)
        for _ in range(2):
            for kind in CANDIDATE_KINDS:
                s.run_measurement(None, kind, cand)
        result = s.results[cand.id]
        self.assertEqual(len(result.runs), 2 * len(CANDIDATE_KINDS))
        self.assertEqual({r.test_kind for r in result.reports}, set(CANDIDATE_KINDS))
        self.assertEqual(len(result.reports), len(CANDIDATE_KINDS))
        self.assertTrue(all(s.run_count(cand.id, k) == 2 for k in CANDIDATE_KINDS))
        self.assertTrue(all(r.extras["runs"] == 2 for r in result.reports))
        self.assertTrue(s.is_complete(cand.id))
        verdict = s.verdict()
        self.assertTrue(verdict.summaries[cand.id].complete)
        self.assertEqual(verdict.summaries[cand.id].runs, 2)

    def test_partial_candidate_is_incomplete_in_the_verdict(self) -> None:
        from tests.test_integration import Harness

        h = Harness(self, dry_run=False)
        s = h.drive_to_testing(self)
        cand = s.candidates[0]
        s.apply_candidate(cand, confirmed=True)
        s.run_measurement(None, "stillness", cand)
        info = s.verdict().summaries[cand.id]
        self.assertFalse(info.complete)
        self.assertEqual(info.reliability, "few")
        self.assertIsNone(info.grade)


if __name__ == "__main__":
    unittest.main()
