"""engine.playobserve on simulated players: alignment, reach, edges, feel, determinism, speed."""
from __future__ import annotations

import time
import unittest
from dataclasses import replace

from osu_tablet_assistant.engine import playobserve as po
from osu_tablet_assistant.engine.playtypes import HIT, NO_CLICK, SHORT, PlayCapture
from playsim import AREA, Player, make_map, simulate

MAP = make_map(seed=3, n=150)


def analyze(player: Player, seed: int = 0, beatmap=MAP, **kw):
    capture = simulate(beatmap, player, seed=seed, **kw)
    return capture, po.analyze_play(beatmap, capture)


class AlignmentTests(unittest.TestCase):
    def test_offset_is_recovered_for_random_start_times(self) -> None:
        for seed in range(6):
            capture, report = analyze(Player(), seed=seed)
            first_s = MAP.judged_objects[0].time_ms / 1000.0
            expected_ms = (capture.samples[0].t + 1.0 - first_s) * 1000.0 + 10.0   # t0 + the player's lateness
            self.assertAlmostEqual(report.alignment.offset_ms, expected_ms, delta=8.0, msg=f"seed {seed}")
            self.assertGreaterEqual(report.alignment.quality, 0.6)
            self.assertTrue(report.trusted)
            self.assertEqual(report.alignment.time_scale, 1.0)
            self.assertFalse(report.alignment.flip_y)

    def test_double_time_and_half_time_are_detected(self) -> None:
        for scale in (1.5, 0.75):
            _c, report = analyze(Player(scale=scale), seed=2)
            self.assertEqual(report.alignment.time_scale, scale)
            self.assertGreaterEqual(report.alignment.quality, 0.6, msg=str(scale))

    def test_hard_rock_flip_is_detected(self) -> None:
        _c, report = analyze(Player(hr=True), seed=4)
        self.assertTrue(report.alignment.flip_y)
        _c, normal = analyze(Player(hr=False), seed=4)
        self.assertFalse(normal.alignment.flip_y)

    def test_unrelated_clicks_are_not_trusted(self) -> None:
        capture = simulate(MAP, Player(), seed=1)
        import random
        rng = random.Random(9)
        t_start, t_end = capture.clicks[0].t, capture.clicks[-1].t
        noise = tuple(replace(c, t=rng.uniform(t_start, t_end)) for c in capture.clicks)
        report = po.analyze_play(MAP, replace(capture, clicks=noise))
        self.assertLess(report.alignment.quality, 0.6)
        self.assertFalse(report.trusted)
        self.assertIn("play.warn.align_low", [w.key for w in report.warnings])

    def test_estimate_alignment_matches_the_report(self) -> None:
        capture, report = analyze(Player(), seed=1)
        self.assertEqual(po.estimate_alignment(MAP, capture).offset_ms, report.alignment.offset_ms)


class JudgingTests(unittest.TestCase):
    def test_clean_player_has_no_false_alarms(self) -> None:
        _c, report = analyze(Player(miss_rate=0.0, aim_noise_mm=0.3, time_sd_ms=10))
        self.assertLess(report.miss_rate, 0.05)
        self.assertEqual([e.zone for e in report.edges if e.kind != "ok"], [])
        self.assertLess(report.mean_err_ratio, 0.5)
        self.assertAlmostEqual(report.reach.gain_x, 1.0, delta=0.06)
        self.assertAlmostEqual(report.reach.gain_y, 1.0, delta=0.06)

    def test_misses_are_no_clicks(self) -> None:
        _c, report = analyze(Player(miss_rate=0.25), seed=5)
        no_click = sum(1 for r in report.results if r.verdict == NO_CLICK) / len(report.results)
        self.assertAlmostEqual(no_click, 0.25, delta=0.08)
        self.assertGreater(report.miss_rate, 0.2)

    def test_hits_have_small_error_and_correct_object_position(self) -> None:
        _c, report = analyze(Player(miss_rate=0.0), seed=6)
        hits = [r for r in report.results if r.verdict == HIT]
        self.assertGreater(len(hits), 100)
        self.assertTrue(all(0.0 <= r.fx <= 1.0 and 0.0 <= r.fy <= 1.0 for r in report.results))
        self.assertLess(sum(r.err_mm for r in hits) / len(hits), 1.0)

    def test_gain_below_one_is_recovered_inside_the_interval(self) -> None:
        _c, report = analyze(Player(gain_x=0.92, bias_x_mm=0.5, miss_rate=0.0, aim_noise_mm=0.25), seed=7)
        reach = report.reach
        self.assertLessEqual(reach.ci_x[0], 0.92)
        self.assertGreaterEqual(reach.ci_x[1], 0.92)
        self.assertAlmostEqual(reach.gain_x, 0.92, delta=0.04)
        self.assertAlmostEqual(reach.gain_y, 1.0, delta=0.05)
        self.assertAlmostEqual(reach.bias_x_mm, 0.5, delta=0.5)
        self.assertGreater(reach.n, 100)

    def test_undershoot_at_the_sides_is_reported_as_short(self) -> None:
        _c, report = analyze(Player(gain_x=0.92, miss_rate=0.0), seed=8)
        by_zone = {e.zone: e for e in report.edges}
        self.assertEqual(by_zone["left"].kind, SHORT)
        self.assertEqual(by_zone["right"].kind, SHORT)
        self.assertGreater(by_zone["left"].severity, 0.1)
        self.assertNotEqual(by_zone["top"].kind, SHORT)

    def test_vertical_undershoot_is_top_and_bottom(self) -> None:
        _c, report = analyze(Player(gain_y=0.9, miss_rate=0.0), seed=9)
        by_zone = {e.zone: e for e in report.edges}
        self.assertEqual(by_zone["top"].kind, SHORT)
        self.assertEqual(by_zone["bottom"].kind, SHORT)

    def test_corner_cells_are_filled(self) -> None:
        # the default grid is now the finer 6 x 8 ("Fine"); 3 x 4 is still available ("Normal")
        _c, report = analyze(Player(), seed=1)
        self.assertEqual((report.grid, len(report.cells)), ((6, 8), 48))
        self.assertEqual(sum(c.n for c in report.cells), report.n_judged)
        coarse = po.analyze_play(MAP, simulate(MAP, Player(), seed=1), rows=3, cols=4)
        self.assertEqual(len(coarse.cells), 12)
        self.assertEqual(sum(c.n for c in coarse.cells), coarse.n_judged)

    def test_lag_shows_as_negative_along_error_on_fast_jumps(self) -> None:
        fast = make_map(seed=5, n=160, gap_ms=95.0)
        _c, laggy = analyze(Player(lag_ms=25, miss_rate=0.0, aim_noise_mm=0.1), beatmap=fast, seed=2)
        _c, sharp = analyze(Player(lag_ms=0, miss_rate=0.0, aim_noise_mm=0.1), beatmap=fast, seed=2)
        self.assertIsNotNone(laggy.feel.lag_mm)
        self.assertLess(laggy.feel.lag_mm, sharp.feel.lag_mm - 0.5)

    def test_rest_jitter_is_measured(self) -> None:
        slow = make_map(seed=2, n=80, gap_ms=900.0)
        _c, shaky = analyze(Player(jitter_px=2.0, miss_rate=0.0), beatmap=slow, seed=3)
        _c, calm = analyze(Player(jitter_px=0.0, miss_rate=0.0), beatmap=slow, seed=3)
        self.assertGreater(shaky.feel.jitter_mm, calm.feel.jitter_mm + 0.03)
        self.assertGreater(shaky.feel.n_slow, 5)


class RobustnessTests(unittest.TestCase):
    def test_deterministic(self) -> None:
        capture = simulate(MAP, Player(gain_x=0.93), seed=3)
        self.assertEqual(po.analyze_play(MAP, capture, seed=5), po.analyze_play(MAP, capture, seed=5))

    def test_partial_capture_only_judges_what_already_happened(self) -> None:
        capture, full = analyze(Player(gain_x=0.93, miss_rate=0.0), seed=3)
        part_capture = simulate(MAP, Player(gain_x=0.93, miss_rate=0.0), seed=3, t0=123.0, stop_after_s=14.0)
        part = po.analyze_play(MAP, part_capture)
        self.assertTrue(part.partial)
        self.assertGreater(part.alignment.quality, 0.6)
        self.assertLess(part.n_judged, full.n_judged)
        self.assertGreater(part.n_judged, 20)
        self.assertAlmostEqual(part.alignment.offset_ms, 123010.0, delta=15.0)

    def test_live_mode_matches_the_full_alignment_and_is_faster(self) -> None:
        big = make_map(seed=7, n=400, gap_ms=300.0)
        capture = simulate(big, Player(hz=500.0), seed=2)
        full, live = po.analyze_play(big, capture), po.analyze_play(big, capture, detail=False)
        self.assertEqual(live.alignment, full.alignment)
        self.assertEqual(live.results, full.results)
        self.assertEqual((live.cells, live.edges, live.reach, live.feel), ((), (), None, None))
        self.assertLess(min(self._time(big, capture, False) for _ in range(3)),
                        min(self._time(big, capture, True) for _ in range(3)))

    def test_samples_with_odd_ordering_still_work(self) -> None:
        capture = simulate(MAP, Player(), seed=1)
        shuffled = tuple(reversed(capture.samples[:50])) + capture.samples[50:]
        self.assertTrue(po.analyze_play(MAP, replace(capture, samples=shuffled)).trusted)

    def test_early_capture_does_not_crash_and_says_not_enough(self) -> None:
        capture = simulate(MAP, Player(), seed=1, stop_after_s=2.0)
        report = po.analyze_play(MAP, capture)
        self.assertFalse(report.trusted)
        for bad in (PlayCapture((), (), AREA, capture.display), replace(capture, clicks=()),
                    replace(capture, samples=capture.samples[:1])):
            r = po.analyze_play(MAP, bad)
            self.assertEqual(r.results, ())
            self.assertIn("play.warn.not_enough", [w.key for w in r.warnings])

    def test_empty_beatmap_and_bad_display_never_raise(self) -> None:
        capture = simulate(MAP, Player(), seed=1)
        self.assertEqual(po.analyze_play(replace(MAP, objects=()), capture).results, ())
        bad = replace(capture, display=replace(capture.display, width_px=0))
        self.assertEqual(po.analyze_play(MAP, bad).results, ())

    def test_nan_samples_are_ignored(self) -> None:
        capture = simulate(MAP, Player(), seed=1)
        from osu_tablet_assistant.models import PenSample
        dirty = capture.samples + (PenSample(float("nan"), 1.0, 1.0), PenSample(5.0, float("inf"), 2.0))
        report = po.analyze_play(MAP, replace(capture, samples=dirty))
        self.assertTrue(report.trusted)

    def test_speed_bound_5000_samples_600_objects(self) -> None:
        big = make_map(seed=7, n=600, gap_ms=90.0)
        capture = simulate(big, Player(hz=1000.0), seed=1, t0=10.0, stop_after_s=5.0)
        capture = replace(capture, samples=capture.samples[:5000])
        po.analyze_play(big, capture)      # warm caches
        best = min(self._time(big, capture) for _ in range(3))
        self.assertLess(best, 0.08, f"{best * 1000:.0f} ms")
        full = simulate(big, Player(hz=100.0), seed=1)     # 600 objects, long play, fewer samples
        best_full = min(self._time(big, full) for _ in range(3))
        self.assertLess(best_full, 0.2, f"{best_full * 1000:.0f} ms")

    @staticmethod
    def _time(beatmap, capture, detail: bool = True) -> float:
        start = time.perf_counter()
        po.analyze_play(beatmap, capture, detail=detail)
        return time.perf_counter() - start


if __name__ == "__main__":
    unittest.main()
