"""engine.recommend: candidates, scoring, ranking and refinement."""
from __future__ import annotations

import itertools
import unittest

from osu_tablet_assistant.engine import recommend as rec
from osu_tablet_assistant.engine.recommend import (generate_candidates, rank, refine,
                                                   score_reports)
from osu_tablet_assistant.knowledge.filters import load_catalog
from osu_tablet_assistant.models import (Candidate, CandidateResult, DisplaySpec, FilterConfig,
                                         MetricsReport, Priority, ProfileChanges, ProfileInfo,
                                         Rect, SkillLevel, TabletSpec, UserPreferences)

TABLET = TabletSpec("CTL-472-like", 152.0, 95.0, 133.0)
FAST_TABLET = TabletSpec("fast", 216.0, 135.0, 266.0)
DISPLAY_60 = DisplaySpec(1920, 1080, 960, 540, refresh_hz=60)
DISPLAY_240 = DisplaySpec(1920, 1080, 960, 540, refresh_hz=240)
ALL = set(load_catalog())
HAWKU_ONLY = {rec.HAWKU}
NOISY = [MetricsReport("stillness", 200, 2.0, jitter_rms_mm=0.2)]
CLEAN = [MetricsReport("stillness", 200, 2.0, jitter_rms_mm=0.005)]


def _by_id(cands) -> dict[str, Candidate]:
    return {c.id: c for c in cands}


def _paths(cand: Candidate) -> list[str]:
    return [f.path for f in cand.changes.filters]


def _keys(cand: Candidate) -> list[str]:
    return [r.key for r in cand.reasons]


class GenerateTests(unittest.TestCase):
    def test_always_has_reference_and_three_profiles(self) -> None:
        for plugins in (set(), HAWKU_ONLY, ALL):
            ids = [c.id for c in generate_candidates(TABLET, DISPLAY_60, UserPreferences(),
                                                     plugins, None, None)]
            self.assertEqual(ids, ["no-filter", "low-latency", "balanced", "smooth"])

    def test_no_filter_is_a_clean_baseline_with_same_area(self) -> None:
        cands = _by_id(generate_candidates(TABLET, DISPLAY_60, UserPreferences(), ALL, NOISY, None))
        base = cands["no-filter"]
        self.assertEqual(base.changes.filters, ())
        self.assertEqual(base.est_added_latency_ms, 0.0)
        self.assertTrue(base.changes.lock_aspect_ratio)
        for cand in cands.values():
            self.assertEqual(cand.changes.tablet_area, base.changes.tablet_area)

    def test_without_plugins_nothing_is_added(self) -> None:
        for cand in generate_candidates(TABLET, DISPLAY_240, UserPreferences(notices_jitter=True),
                                        set(), NOISY, None)[1:]:
            self.assertEqual(cand.changes.filters, ())
            self.assertIn("engine.reason.nothing_to_add", _keys(cand))

    def test_unknown_paths_are_ignored(self) -> None:
        cands = generate_candidates(TABLET, DISPLAY_60, UserPreferences(),
                                    {"Not.A.Real.Filter"}, None, None)
        self.assertTrue(all(c.changes.filters == () for c in cands))

    def test_hawku_only_levels_and_frequency(self) -> None:
        cands = _by_id(generate_candidates(FAST_TABLET, DISPLAY_60, UserPreferences(),
                                           HAWKU_ONLY, CLEAN, None))
        latencies = {}
        for cid in ("low-latency", "balanced", "smooth"):
            (fc,) = cands[cid].changes.filters
            self.assertEqual(fc.path, rec.HAWKU)
            # Hawku's Frequency is its own timer rate, not the report rate: keep 1000.
            self.assertEqual(fc.settings["Frequency"], 1000.0)
            latencies[cid] = fc.settings["Latency"]
            self.assertAlmostEqual(cands[cid].est_added_latency_ms, fc.settings["Latency"])
            self.assertIn("engine.reason.frequency_default", _keys(cands[cid]))
        self.assertEqual(latencies, {"low-latency": 4.0, "balanced": 10.0, "smooth": 20.0})

    def test_frequency_always_left_at_default_saturn_included(self) -> None:
        # 2026-10-30 audit: "Saturn Frequency = report rate" is doubtful, so it is no longer forced.
        catalog = load_catalog()
        for tablet in (TABLET, FAST_TABLET):
            for cand in generate_candidates(tablet, DISPLAY_240, UserPreferences(notices_jitter=True),
                                            ALL, NOISY, None):
                for fc in cand.changes.filters:
                    param = catalog[fc.path].param("Frequency")
                    if param is not None:
                        self.assertEqual(fc.settings["Frequency"], param.default, fc.path)
        for path in (rec.SATURN_MULTI, rec.HAWKU, rec.ANTICHATTER, rec.RESAMPLER):
            self.assertEqual(rec._make_filter(catalog[path], {}).settings["Frequency"], 1000.0)
        self.assertFalse(hasattr(rec, "FREQUENCY_IS_REPORT_RATE"))

    def test_priority_changes_strength(self) -> None:
        def balanced_latency(**kw) -> float:
            cands = _by_id(generate_candidates(TABLET, DISPLAY_60, UserPreferences(**kw),
                                               HAWKU_ONLY, None, None))
            return cands["balanced"].changes.filters[0].settings["Latency"]
        self.assertLess(balanced_latency(priority=Priority.LATENCY), balanced_latency())
        self.assertGreater(balanced_latency(priority=Priority.SMOOTHNESS), balanced_latency())
        self.assertLess(balanced_latency(notices_input_lag=True), balanced_latency())

    def test_noisy_baseline_adds_noise_filter_first(self) -> None:
        cands = _by_id(generate_candidates(TABLET, DISPLAY_60, UserPreferences(), ALL, NOISY, None))
        for cid in ("low-latency", "balanced", "smooth"):
            self.assertIn(rec.CHATTER_SMOOTH, _paths(cands[cid]))
            self.assertIn("engine.reason.jitter_measured_high", _keys(cands[cid]))
        self.assertEqual(_paths(cands["balanced"])[0], rec.CHATTER_SMOOTH)
        avail = {rec.NOISE_REDUCTION, rec.HAWKU}
        cands = _by_id(generate_candidates(TABLET, DISPLAY_60, UserPreferences(), avail, NOISY, None))
        self.assertEqual(_paths(cands["balanced"]), [rec.NOISE_REDUCTION, rec.HAWKU])

    def test_clean_baseline_adds_no_noise_filter(self) -> None:
        cands = generate_candidates(TABLET, DISPLAY_60, UserPreferences(), ALL, CLEAN, None)
        for cand in cands:
            self.assertFalse(set(_paths(cand)) & set(rec.NOISE_PREFERENCE))
        self.assertIn("engine.reason.jitter_measured_low", _keys(cands[2]))

    def test_reported_jitter_without_measurements(self) -> None:
        cands = _by_id(generate_candidates(TABLET, DISPLAY_60, UserPreferences(notices_jitter=True),
                                           ALL, None, None))
        self.assertIn(rec.CHATTER_SMOOTH, _paths(cands["balanced"]))

    def test_noise_without_noise_filter_uses_antichatter(self) -> None:
        avail = {rec.HAWKU, rec.ANTICHATTER, rec.RADIAL_TABLET}
        cands = _by_id(generate_candidates(TABLET, DISPLAY_60, UserPreferences(), avail, NOISY, None))
        self.assertEqual(_paths(cands["balanced"]), [rec.ANTICHATTER])
        self.assertIn("engine.reason.noise_filter_missing", _keys(cands["balanced"]))

    def test_resampler_for_fast_monitor_or_low_rate(self) -> None:
        cands = _by_id(generate_candidates(TABLET, DISPLAY_240, UserPreferences(), ALL, CLEAN, None))
        # Research order: smoothing first, resampler after it (OTD runs the list in order).
        self.assertEqual(_paths(cands["balanced"]), [rec.RADIAL_TABLET, rec.RESAMPLER])
        self.assertIn("engine.reason.prediction_policy_unknown", _keys(cands["balanced"]))
        cands = _by_id(generate_candidates(TABLET, DISPLAY_60, UserPreferences(), ALL, CLEAN, None))
        self.assertNotIn(rec.RESAMPLER, _paths(cands["balanced"]))
        slow = CLEAN + [MetricsReport("rate", 100, 1.0, effective_rate_hz=60.0)]
        cands = _by_id(generate_candidates(TABLET, DISPLAY_60, UserPreferences(), ALL, slow, None))
        self.assertIn(rec.RESAMPLER, _paths(cands["balanced"]))
        self.assertIn("engine.reason.rate_low", _keys(cands["balanced"]))

    def test_resampler_replaces_second_async_smoother(self) -> None:
        avail = {rec.RESAMPLER, rec.HAWKU}
        cands = _by_id(generate_candidates(TABLET, DISPLAY_240, UserPreferences(), avail, CLEAN, None))
        self.assertEqual(_paths(cands["smooth"]), [rec.RESAMPLER])
        self.assertEqual(cands["smooth"].changes.filters[0].settings["latency"], 20.0)

    def test_values_in_bounds_ordered_and_deterministic(self) -> None:
        catalog = load_catalog()
        order = {p: i for i, p in enumerate(rec.RESEARCH_ORDER)}
        for tablet, display, baseline, prio, skill, jitter in itertools.product(
                (TABLET, FAST_TABLET, TabletSpec("zero-rate", 100, 60, 0.0)),
                (DISPLAY_60, DISPLAY_240), (None, NOISY, CLEAN), Priority, SkillLevel,
                (False, True)):
            prefs = UserPreferences(priority=prio, skill_level=skill, notices_jitter=jitter,
                                    notices_input_lag=not jitter)
            cands = generate_candidates(tablet, display, prefs, ALL, baseline, None)
            self.assertEqual(cands, generate_candidates(tablet, display, prefs, ALL, baseline, None))
            for cand in cands:
                self.assertGreaterEqual(cand.est_added_latency_ms, 0.0)
                idx = [order[p] for p in _paths(cand)]
                self.assertEqual(idx, sorted(idx))
                for fc in cand.changes.filters:
                    spec = catalog[fc.path]
                    self.assertEqual(set(fc.settings), {p.name for p in spec.params})
                    for p in spec.params:
                        self.assertEqual(p.clamp(fc.settings[p.name]), fc.settings[p.name])
                    if fc.path in (rec.RADIAL_TABLET, rec.RADIAL_SCREEN):
                        self.assertLessEqual(fc.settings["InnerRadius"], fc.settings["OuterRadius"])

    def test_current_rotation_is_kept(self) -> None:
        def area(rotation: float) -> Rect:
            current = ProfileInfo("t", "abs", Rect(1920, 1080, 960, 540),
                                  Rect(70, 40, 76, 47.5, rotation), True, ())
            cands = _by_id(generate_candidates(TabletSpec("narrow", 100.0, 160.0), DISPLAY_60,
                                               UserPreferences(), set(), None, current))
            kept = "engine.reason.area_rotation_kept" in _keys(cands["balanced"])
            self.assertEqual(kept, bool(rotation))
            return cands["no-filter"].changes.tablet_area
        flat, flipped, turned = area(0.0), area(180.0), area(90.0)
        self.assertEqual(flat.rotation, 0.0)
        self.assertEqual(flipped.rotation, 180.0)
        self.assertEqual((flipped.width, flipped.x, flipped.y), (flat.width, 50.0, 80.0))
        self.assertEqual(turned.rotation, 90.0)
        # 100 mm wide tablet: unrotated the area is limited to 100 mm, turned 90 deg
        # its width runs along the 160 mm side and the wrist default (80 mm) fits.
        self.assertLessEqual(flat.width, 100.0)
        self.assertLessEqual(turned.height, 100.0)
        self.assertLessEqual(turned.width, 160.0)

    def test_keep_current(self) -> None:
        current = ProfileInfo(
            "CTL-472", "OpenTabletDriver.Desktop.Output.AbsoluteMode",
            Rect(1920, 1080, 960, 540), Rect(70, 40, 76, 47.5), False,
            (FilterConfig(rec.HAWKU, True, {"Latency": 80.0, "Frequency": 1000.0}),
             FilterConfig("Some.Plugin.Unknown", True, {"A": 1})))
        cands = _by_id(generate_candidates(TABLET, DISPLAY_60, UserPreferences(), ALL, None, current))
        keep = cands["keep-current"]
        self.assertEqual(keep.changes.filters, current.filters)          # untouched
        self.assertEqual(keep.changes.tablet_area, current.tablet_area)
        self.assertFalse(keep.changes.lock_aspect_ratio)
        self.assertAlmostEqual(keep.est_added_latency_ms, 80.0)
        self.assertIn("engine.reason.latency_unknown_filter", _keys(keep))


def _tracking(lag: float, err: float) -> MetricsReport:
    return MetricsReport("tracking", 500, 5.0, path_error_mean_mm=err, path_error_p95_mm=2 * err,
                         smoothness_jerk=1.0, lag_ms_estimate=lag)


class ScoreTests(unittest.TestCase):
    def test_neutral_and_rating_bounds(self) -> None:
        prefs = UserPreferences()
        self.assertEqual(score_reports([], prefs, None), rec.NEUTRAL_SCORE)
        self.assertEqual(score_reports([], prefs, 5), rec.NEUTRAL_SCORE + 10)
        self.assertEqual(score_reports([], prefs, 1), rec.NEUTRAL_SCORE - 10)
        self.assertEqual(score_reports([], prefs, 99), rec.NEUTRAL_SCORE + 10)

    def test_lower_jitter_scores_higher(self) -> None:
        prefs = UserPreferences()
        self.assertGreater(score_reports(CLEAN, prefs, None), score_reports(NOISY, prefs, None))

    def test_missing_metrics_are_renormalized(self) -> None:
        prefs = UserPreferences()
        only_jitter = score_reports(NOISY, prefs, None)
        with_empty = score_reports(NOISY + [MetricsReport("tracking", 0, 0.0)], prefs, None)
        self.assertEqual(only_jitter, with_empty)
        nan = MetricsReport("stillness", 1, 1.0, jitter_rms_mm=float("nan"))
        self.assertEqual(score_reports([nan], prefs, None), rec.NEUTRAL_SCORE)

    def test_priority_shifts_the_tradeoff(self) -> None:
        responsive = [_tracking(lag=5, err=1.0),
                      MetricsReport("stillness", 100, 2.0, jitter_rms_mm=0.15)]
        steady = [_tracking(lag=40, err=1.0),
                  MetricsReport("stillness", 100, 2.0, jitter_rms_mm=0.01)]
        fast = UserPreferences(priority=Priority.LATENCY)
        smooth = UserPreferences(priority=Priority.SMOOTHNESS)
        self.assertGreater(score_reports(responsive, fast, None), score_reports(steady, fast, None))
        self.assertGreater(score_reports(steady, smooth, None), score_reports(responsive, smooth, None))

    def test_rank_sets_scores_best_first_stable(self) -> None:
        results = [CandidateResult("a", tuple(NOISY)), CandidateResult("b", tuple(CLEAN)),
                   CandidateResult("c", tuple(NOISY)), CandidateResult("d", tuple(CLEAN), 1)]
        ranked = rank(results, UserPreferences())
        self.assertEqual([r.candidate_id for r in ranked], ["b", "d", "a", "c"])
        self.assertTrue(all(r.score is not None for r in results))
        self.assertEqual(rank([], UserPreferences()), [])


class RefineTests(unittest.TestCase):
    def _balanced_hawku(self) -> Candidate:
        cands = _by_id(generate_candidates(TABLET, DISPLAY_60, UserPreferences(), HAWKU_ONLY,
                                           CLEAN, None))
        return cands["balanced"]

    def test_no_filter_cannot_be_refined(self) -> None:
        cands = _by_id(generate_candidates(TABLET, DISPLAY_60, UserPreferences(), ALL, None, None))
        self.assertIsNone(refine(cands["no-filter"], [CandidateResult("no-filter")],
                                 UserPreferences(), ALL))

    def test_unavailable_filter_cannot_be_refined(self) -> None:
        best = self._balanced_hawku()
        self.assertIsNone(refine(best, [CandidateResult(best.id)], UserPreferences(), set()))

    def test_first_step_follows_priority(self) -> None:
        best = self._balanced_hawku()
        down = refine(best, [CandidateResult(best.id)], UserPreferences(), HAWKU_ONLY)
        up = refine(best, [CandidateResult(best.id)],
                    UserPreferences(priority=Priority.SMOOTHNESS), HAWKU_ONLY)
        self.assertEqual(down.changes.filters[0].settings["Latency"], 7.5)
        self.assertEqual(up.changes.filters[0].settings["Latency"], 12.5)
        self.assertEqual(down.id, "balanced|Latency:10>7.5")
        self.assertEqual(down.changes.tablet_area, best.changes.tablet_area)
        self.assertEqual(down.reasons[0].key, "engine.reason.refine_less_smoothing")
        self.assertIn("refined", down.tags)
        self.assertAlmostEqual(down.est_added_latency_ms, 7.5)

    def test_refined_latency_does_not_read_frequency_as_report_rate(self) -> None:
        avail = {rec.RESAMPLER, rec.HAWKU}
        cands = _by_id(generate_candidates(TABLET, DISPLAY_240, UserPreferences(), avail, CLEAN, None))
        best = cands["balanced"]           # resampler with its own smoothing latency
        (fc,) = best.changes.filters
        self.assertEqual(fc.settings["Frequency"], 1000.0)
        nxt = refine(best, [CandidateResult(best.id)], UserPreferences(), avail)
        old, new = fc.settings["latency"], nxt.changes.filters[0].settings["latency"]
        # 133 Hz report interval term is unchanged, only the EMA latency moves.
        self.assertAlmostEqual(nxt.est_added_latency_ms,
                               round(best.est_added_latency_ms + new - old, 1))

    def test_moves_away_from_worse_neighbour(self) -> None:
        best = self._balanced_hawku()
        results = [CandidateResult(best.id), CandidateResult("balanced|Latency:10>7.5")]
        nxt = refine(best, results, UserPreferences(), HAWKU_ONLY)
        self.assertEqual(nxt.changes.filters[0].settings["Latency"], 12.5)

    def test_respects_catalog_bounds(self) -> None:
        best = self._balanced_hawku()
        fc = best.changes.filters[0]
        at_min = FilterConfig(fc.path, True, dict(fc.settings, Latency=0.0))
        cand = Candidate("x", "l", "s", ProfileChanges(filters=(at_min,)))
        nxt = refine(cand, [CandidateResult("x")], UserPreferences(), HAWKU_ONLY)
        self.assertGreater(nxt.changes.filters[0].settings["Latency"], 0.0)
        at_max = FilterConfig(fc.path, True, dict(fc.settings, Latency=50.0))
        cand = Candidate("y", "l", "s", ProfileChanges(filters=(at_max,)))
        nxt = refine(cand, [CandidateResult("y")],
                     UserPreferences(priority=Priority.SMOOTHNESS), HAWKU_ONLY)
        self.assertLess(nxt.changes.filters[0].settings["Latency"], 50.0)

    def test_stops_after_max_steps(self) -> None:
        best = self._balanced_hawku()
        results = [CandidateResult(best.id)] + [
            CandidateResult(f"balanced|Latency:10>{v}") for v in (1, 2, 3, 4)]
        self.assertEqual(len(results) - 1, rec.MAX_REFINE_STEPS)
        self.assertIsNone(refine(best, results, UserPreferences(), HAWKU_ONLY))

    def test_hill_climb_converges_without_repeating(self) -> None:
        """Synthetic player whose ideal Hawku latency is about 14 ms."""
        prefs = UserPreferences()

        def measure(cand: Candidate) -> tuple[MetricsReport, ...]:
            latency = cand.changes.filters[0].settings["Latency"]
            return (MetricsReport("stillness", 100, 2.0, jitter_rms_mm=0.6 / (1 + latency)),
                    _tracking(lag=latency, err=1.0))

        best = self._balanced_hawku()
        known = {best.id: best}
        results = [CandidateResult(best.id, measure(best))]
        seen = [best.changes.filters[0].settings["Latency"]]
        for _ in range(rec.MAX_REFINE_STEPS + 2):
            winner = known[rank(results, prefs)[0].candidate_id]
            nxt = refine(winner, results, prefs, HAWKU_ONLY)
            if nxt is None:
                break
            value = nxt.changes.filters[0].settings["Latency"]
            self.assertNotIn(value, seen)
            self.assertNotIn(nxt.id, known)
            spec_param = load_catalog()[rec.HAWKU].param("Latency")
            self.assertTrue(spec_param.min <= value <= spec_param.max)
            seen.append(value)
            known[nxt.id] = nxt
            results.append(CandidateResult(nxt.id, measure(nxt)))
        else:
            self.fail("refine did not converge")
        self.assertLessEqual(len(results) - 1, rec.MAX_REFINE_STEPS)

    def test_radial_follow_knob_keeps_inner_below_outer(self) -> None:
        avail = {rec.RADIAL_TABLET}
        cands = _by_id(generate_candidates(TABLET, DISPLAY_60, UserPreferences(), avail, NOISY, None))
        best = cands["low-latency"]
        self.assertEqual(_paths(best), [rec.RADIAL_TABLET])
        nxt = refine(best, [CandidateResult(best.id)], UserPreferences(), avail)
        s = nxt.changes.filters[0].settings
        self.assertLess(s["OuterRadius"], best.changes.filters[0].settings["OuterRadius"])
        self.assertLessEqual(s["InnerRadius"], s["OuterRadius"])


if __name__ == "__main__":
    unittest.main()
