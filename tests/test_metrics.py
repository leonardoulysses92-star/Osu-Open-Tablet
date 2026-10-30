"""Tests for engine.metrics using synthetic pen signals."""
from __future__ import annotations

import math
import random
import unittest

from osu_tablet_assistant.engine import metrics
from osu_tablet_assistant.models import PenSample

MM_PER_PX = 0.05


def figure8(t: float) -> tuple[float, float]:
    """Smooth 1:2 Lissajous, two loops in 8 s."""
    w = 2 * math.pi / 4.0
    return 960 + 300 * math.sin(w * t), 540 + 200 * math.sin(2 * w * t)


def target_samples(t0: float = -1.0, t1: float = 9.0, hz: float = 240.0) -> list[PenSample]:
    n = int((t1 - t0) * hz)
    return [PenSample(t0 + i / hz, *figure8(t0 + i / hz)) for i in range(n + 1)]


def pen_following(lag: float, noise: float, seed: int = 1, hz: float = 133.0,
                  duration: float = 8.0, offset: tuple[float, float] = (0.0, 0.0)) -> list[PenSample]:
    rng = random.Random(seed)
    out = []
    for i in range(int(duration * hz)):
        t = i / hz
        x, y = figure8(t - lag)
        out.append(PenSample(t, x + offset[0] + rng.gauss(0, noise), y + offset[1] + rng.gauss(0, noise)))
    return out


class EffectiveRateTests(unittest.TestCase):
    def test_regular_stream(self) -> None:
        samples = [PenSample(i / 133.0, 100 + i, 200 + 2 * i) for i in range(400)]
        self.assertAlmostEqual(metrics.effective_rate(samples), 133.0, delta=0.5)

    def test_timestamp_jitter_and_heartbeats(self) -> None:
        rng = random.Random(3)
        samples = []
        for i in range(500):
            t = i / 200.0 + rng.uniform(-0.0006, 0.0006)
            samples.append(PenSample(t, 50 + i, 80 - i))
            samples.append(PenSample(t + 0.0013, 50 + i, 80 - i))  # duplicate position
        self.assertAlmostEqual(metrics.effective_rate(samples), 200.0, delta=6.0)

    def test_gaps_are_ignored(self) -> None:
        samples = [PenSample(i / 100.0, i, 0) for i in range(100)]
        samples += [PenSample(5.0 + i / 100.0, 1000 + i, 0) for i in range(100)]
        self.assertAlmostEqual(metrics.effective_rate(samples), 100.0, delta=0.5)

    def test_unsorted_input(self) -> None:
        samples = [PenSample(i / 100.0, i, 0) for i in range(50)]
        random.Random(0).shuffle(samples)
        self.assertAlmostEqual(metrics.effective_rate(samples), 100.0, delta=0.5)

    def test_degenerate(self) -> None:
        self.assertIsNone(metrics.effective_rate([]))
        self.assertIsNone(metrics.effective_rate([PenSample(0.0, 1, 1)]))
        self.assertIsNone(metrics.effective_rate([PenSample(1.0, i, 0) for i in range(10)]))
        self.assertIsNone(metrics.effective_rate([PenSample(i / 10, 5, 5) for i in range(10)]))
        self.assertIsNone(metrics.effective_rate([PenSample(math.nan, 1, 1)] * 5))

    def test_rate_report(self) -> None:
        samples = [PenSample(i / 133.0, i, 0) for i in range(266)]
        report = metrics.rate_report(samples)
        self.assertEqual(report.test_kind, "rate")
        self.assertEqual(report.sample_count, 266)
        self.assertAlmostEqual(report.duration_s, 265 / 133.0)
        self.assertAlmostEqual(report.effective_rate_hz, 133.0, delta=0.5)
        self.assertIsNone(metrics.rate_report([]).effective_rate_hz)


class StillnessTests(unittest.TestCase):
    def noisy(self, sigma: float, drift: tuple[float, float] = (0.0, 0.0), seed: int = 7,
              hz: float = 1000.0, duration: float = 4.5) -> list[PenSample]:
        rng = random.Random(seed)
        return [PenSample(i / hz, 500 + drift[0] * i / hz + rng.gauss(0, sigma),
                          300 + drift[1] * i / hz + rng.gauss(0, sigma))
                for i in range(int(duration * hz))]

    def test_known_jitter(self) -> None:
        report = metrics.stillness_report(self.noisy(0.4), MM_PER_PX)
        expected = math.sqrt(2) * 0.4 * MM_PER_PX
        self.assertEqual(report.test_kind, "stillness")
        self.assertAlmostEqual(report.jitter_rms_mm, expected, delta=0.06 * expected)
        self.assertAlmostEqual(report.extras["rms_x_mm"], 0.4 * MM_PER_PX, delta=0.005)
        self.assertGreater(report.extras["max_excursion_mm"], report.jitter_rms_mm)
        self.assertAlmostEqual(report.duration_s, 4.499)
        for field in ("effective_rate_hz", "path_error_mean_mm", "smoothness_jerk", "lag_ms_estimate",
                      "aim_error_mean_mm"):
            self.assertIsNone(getattr(report, field))

    def test_drift_is_removed(self) -> None:
        plain = metrics.stillness_report(self.noisy(0.4), MM_PER_PX)
        drifting = metrics.stillness_report(self.noisy(0.4, drift=(6.0, -4.0)), MM_PER_PX)
        self.assertAlmostEqual(drifting.jitter_rms_mm, plain.jitter_rms_mm, delta=0.003)
        self.assertAlmostEqual(drifting.extras["drift_mm"], math.hypot(6, 4) * 4.5 * MM_PER_PX,
                               delta=0.05 * math.hypot(6, 4) * 4.5 * MM_PER_PX)

    def test_scales_with_mm_per_px(self) -> None:
        samples = self.noisy(0.4)
        a = metrics.stillness_report(samples, 0.05).jitter_rms_mm
        b = metrics.stillness_report(samples, 0.10).jitter_rms_mm
        self.assertAlmostEqual(b, 2 * a, places=9)

    def test_on_change_sampling_is_time_weighted(self) -> None:
        # Pen sits at 100 for 4 s, then 1 px away for 0.5 s: only two raw samples.
        samples = [PenSample(0.0, 100, 100), PenSample(0.5, 100, 100), PenSample(4.0, 101, 100),
                   PenSample(4.5, 101, 100)]
        report = metrics.stillness_report(samples, 1.0)
        self.assertLess(report.jitter_rms_mm, 0.5)
        self.assertEqual(report.extras["distinct_pixel_positions"], 2)

    def test_quantization_hint(self) -> None:
        quantized = [PenSample(s.t, round(s.x), round(s.y)) for s in self.noisy(0.3)]
        report = metrics.stillness_report(quantized, MM_PER_PX)
        self.assertLess(report.extras["distinct_pixel_positions"], 12)
        perfect = metrics.stillness_report([PenSample(i / 100, 5, 5) for i in range(100)], MM_PER_PX)
        self.assertEqual(perfect.jitter_rms_mm, 0.0)
        self.assertEqual(perfect.extras["distinct_pixel_positions"], 1)

    def test_degenerate_never_raises(self) -> None:
        cases = [[], [PenSample(0, 1, 1)], [PenSample(1.0, i, i) for i in range(10)],
                 [PenSample(0.0, 1, 1), PenSample(1.0, 2, 2)], [PenSample(math.nan, 1, 1)] * 4]
        for samples in cases:
            report = metrics.stillness_report(samples, MM_PER_PX)
            self.assertIsNone(report.jitter_rms_mm)
            self.assertEqual(report.test_kind, "stillness")
        self.assertIsNone(metrics.stillness_report(self.noisy(0.4), 0.0).jitter_rms_mm)
        self.assertIsNone(metrics.stillness_report(self.noisy(0.4), math.nan).jitter_rms_mm)


class TrackingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = target_samples()

    def test_known_lag_and_noise(self) -> None:
        sigma = 0.3
        report = metrics.tracking_report(pen_following(0.040, sigma), self.target, MM_PER_PX)
        self.assertEqual(report.test_kind, "tracking")
        self.assertAlmostEqual(report.lag_ms_estimate, 40.0, delta=3.0)
        # Mean of a 2-D Gaussian radius (Rayleigh) is sigma * sqrt(pi / 2).
        expected = sigma * math.sqrt(math.pi / 2) * MM_PER_PX
        self.assertAlmostEqual(report.path_error_mean_mm, expected, delta=0.15 * expected)
        self.assertGreater(report.path_error_p95_mm, report.path_error_mean_mm)
        # Not compensating the lag is much worse.
        self.assertGreater(report.extras["path_error_zero_lag_mean_mm"], 5 * report.path_error_mean_mm)
        self.assertFalse(report.extras["lag_at_search_limit"])

    def test_various_lags(self) -> None:
        for lag in (0.0, 0.015, 0.1, 0.25, -0.03):
            with self.subTest(lag=lag):
                report = metrics.tracking_report(pen_following(lag, 0.2, seed=5), self.target, MM_PER_PX)
                self.assertAlmostEqual(report.lag_ms_estimate, lag * 1000, delta=3.0)

    def test_constant_offset_is_path_error(self) -> None:
        report = metrics.tracking_report(pen_following(0.02, 0.0, offset=(10.0, 0.0)), self.target, MM_PER_PX)
        self.assertAlmostEqual(report.path_error_mean_mm, 10 * MM_PER_PX, delta=0.05 * 10 * MM_PER_PX)
        self.assertAlmostEqual(report.path_error_p95_mm, 10 * MM_PER_PX, delta=0.05 * 10 * MM_PER_PX)

    def test_jerk_is_normalized_to_target(self) -> None:
        clean = metrics.tracking_report(pen_following(0.03, 0.0), self.target, MM_PER_PX)
        noisy = metrics.tracking_report(pen_following(0.03, 0.3), self.target, MM_PER_PX)
        very_noisy = metrics.tracking_report(pen_following(0.03, 1.0), self.target, MM_PER_PX)
        self.assertAlmostEqual(clean.smoothness_jerk, 1.0, delta=0.25)
        self.assertGreater(noisy.smoothness_jerk, 20 * clean.smoothness_jerk)
        self.assertGreater(very_noisy.smoothness_jerk, 2 * noisy.smoothness_jerk)

    def test_jerk_independent_of_pixel_scale(self) -> None:
        a = metrics.tracking_report(pen_following(0.03, 0.3), self.target, 0.05)
        b = metrics.tracking_report(pen_following(0.03, 0.3), self.target, 0.20)
        self.assertAlmostEqual(a.smoothness_jerk, b.smoothness_jerk, places=9)
        self.assertAlmostEqual(b.path_error_mean_mm, 4 * a.path_error_mean_mm, places=9)

    def test_straight_target_has_no_jerk_ratio(self) -> None:
        line = [PenSample(i / 100.0, 100 + 50 * i / 100.0, 100) for i in range(-100, 1000)]
        pen = [PenSample(i / 133.0, 100 + 50 * i / 133.0, 100) for i in range(1000)]
        report = metrics.tracking_report(pen, line, MM_PER_PX)
        self.assertIsNotNone(report.path_error_mean_mm)
        self.assertIsNone(report.smoothness_jerk)

    def test_pen_not_overlapping_target(self) -> None:
        pen = [PenSample(100 + i / 100.0, 5, 5) for i in range(200)]
        report = metrics.tracking_report(pen, self.target, MM_PER_PX)
        self.assertIsNone(report.path_error_mean_mm)
        self.assertIsNone(report.lag_ms_estimate)

    def test_degenerate_never_raises(self) -> None:
        pen = pen_following(0.03, 0.2)
        cases = [([], self.target), (pen[:1], self.target), (pen[:5], self.target), (pen, []),
                 (pen, self.target[:1]), ([PenSample(1.0, i, i) for i in range(20)], self.target),
                 (pen, [PenSample(2.0, 1, 1)] * 5), ([PenSample(math.nan, 0, 0)] * 20, self.target)]
        for samples, tgt in cases:
            report = metrics.tracking_report(samples, tgt, MM_PER_PX)
            self.assertEqual(report.test_kind, "tracking")
            self.assertIsNone(report.path_error_mean_mm)
            self.assertIsNone(report.smoothness_jerk)
        self.assertIsNone(metrics.tracking_report(pen, self.target, -1.0).path_error_mean_mm)


class AimTests(unittest.TestCase):
    def clicks(self) -> list[tuple[PenSample, tuple[float, float]]]:
        offsets = [(3, 4), (0, 0), (-6, 8), (0, -5)]
        return [(PenSample(1.0 + 0.8 * i, 500 + dx, 400 + dy), (500.0, 400.0)) for i, (dx, dy) in enumerate(offsets)]

    def test_miss_distance(self) -> None:
        report = metrics.aim_report(self.clicks(), 0.1)
        self.assertEqual(report.test_kind, "aim")
        self.assertEqual(report.sample_count, 4)
        self.assertAlmostEqual(report.aim_error_mean_mm, (5 + 0 + 10 + 5) / 4 * 0.1)
        self.assertAlmostEqual(report.extras["aim_error_median_mm"], 0.5)
        self.assertAlmostEqual(report.extras["bias_x_mm"], (3 + 0 - 6 + 0) / 4 * 0.1)
        self.assertAlmostEqual(report.extras["bias_y_mm"], (4 + 0 + 8 - 5) / 4 * 0.1)

    def test_time_from_inter_click_intervals(self) -> None:
        report = metrics.aim_report(self.clicks(), 0.1)
        self.assertAlmostEqual(report.aim_time_ms_mean, 800.0)
        self.assertEqual(report.extras["aim_time_source"], "inter_click")
        self.assertAlmostEqual(report.duration_s, 2.4)

    def test_time_from_appearance(self) -> None:
        appear = [0.5, 1.2, 2.1, 2.9]
        report = metrics.aim_report(self.clicks(), 0.1, appear_times=appear)
        self.assertEqual(report.extras["aim_time_source"], "appear")
        self.assertAlmostEqual(report.aim_time_ms_mean, (500 + 600 + 500 + 500) / 4)
        self.assertAlmostEqual(report.extras["aim_time_ms_median"], 500.0)
        self.assertAlmostEqual(report.duration_s, 3.4 - 0.5)

    def test_mismatched_appear_times_fall_back(self) -> None:
        report = metrics.aim_report(self.clicks(), 0.1, appear_times=[0.0])
        self.assertEqual(report.extras["aim_time_source"], "inter_click")

    def test_single_click_has_error_but_no_time(self) -> None:
        report = metrics.aim_report(self.clicks()[:1], 0.1)
        self.assertAlmostEqual(report.aim_error_mean_mm, 0.5)
        self.assertIsNone(report.aim_time_ms_mean)

    def test_degenerate_never_raises(self) -> None:
        for clicks in ([], [(PenSample(math.nan, 1, 1), (0.0, 0.0))]):
            report = metrics.aim_report(clicks, 0.1)
            self.assertEqual(report.test_kind, "aim")
            self.assertEqual(report.sample_count, 0)
            self.assertIsNone(report.aim_error_mean_mm)
            self.assertIsNone(report.aim_time_ms_mean)
        self.assertIsNone(metrics.aim_report(self.clicks(), 0.0).aim_error_mean_mm)
        weird = [PenSample(0.0, 1, 1)], [PenSample(1.0, 1, 1)] * 3, [PenSample(math.nan, 0, 0)] * 4
        for samples in weird:
            report = metrics.aim_report(self.clicks(), 0.1, samples=samples, radius_px=math.nan,
                                        center_fraction=-1.0, settle_s=math.inf)
            self.assertAlmostEqual(report.aim_error_mean_mm, (5 + 0 + 10 + 5) / 4 * 0.1)
            self.assertEqual(report.extras["trajectory_count"], 0)
            self.assertNotIn("center_reach_rate", report.extras)

    def test_without_trajectory_falls_back_to_activation_point(self) -> None:
        report = metrics.aim_report(self.clicks(), 0.1)
        self.assertEqual(report.extras["trajectory_count"], 0)
        self.assertEqual(report.extras["activation_count"], 4)
        self.assertAlmostEqual(report.extras["activation_error_mean_mm"], report.aim_error_mean_mm)
        for key in ("settle_rms_mm", "path_efficiency_mean", "overshoot_mean_mm", "overshoot_max_mm"):
            self.assertIsNone(report.extras[key], key)

    def test_time_statistics(self) -> None:
        times = [0.1, 0.2, 0.3, 0.4, 1.0]
        hits = [(PenSample(10.0 * i + t, 0, 0), (0.0, 0.0)) for i, t in enumerate(times)]
        report = metrics.aim_report(hits, 0.1, appear_times=[10.0 * i for i in range(5)])
        self.assertAlmostEqual(report.aim_time_ms_mean, 400.0)
        self.assertAlmostEqual(report.extras["aim_time_ms_median"], 300.0)
        self.assertAlmostEqual(report.extras["aim_time_ms_p95"], 400.0 + 0.8 * 600.0)


class CircleGeometryTests(unittest.TestCase):
    C = (0.0, 0.0)

    def test_entry_crossing(self) -> None:
        f = metrics.segment_circle_entry((-20.0, 0.0), (20.0, 0.0), self.C, 5.0)
        self.assertAlmostEqual(f, 15 / 40)
        f = metrics.segment_circle_entry((-10.0, 3.0), (10.0, 3.0), self.C, 5.0)   # 3-4-5: enters at x = -4
        self.assertAlmostEqual(f, 6 / 20)

    def test_very_fast_flick_through_the_circle(self) -> None:
        # Both samples far outside, the whole circle lies between them.
        f = metrics.segment_circle_entry((-1e6, 1.0), (1e6, 1.0), self.C, 5.0)
        self.assertIsNotNone(f)
        x = -1e6 + f * 2e6
        self.assertAlmostEqual(x, -math.sqrt(24.0), delta=1e-6)

    def test_tangent_touches_and_grazing_misses(self) -> None:
        self.assertEqual(metrics.segment_circle_entry((-10.0, 5.0), (10.0, 5.0), self.C, 5.0), 0.5)
        self.assertIsNone(metrics.segment_circle_entry((-10.0, 5.0001), (10.0, 5.0001), self.C, 5.0))
        diag = 5.0 / math.sqrt(2)      # tangent at 45 degrees, rounding-prone
        f = metrics.segment_circle_entry((diag - 10, diag + 10), (diag + 10, diag - 10), self.C, 5.0)
        self.assertAlmostEqual(f, 0.5, delta=1e-6)

    def test_end_on_boundary_counts(self) -> None:
        self.assertEqual(metrics.segment_circle_entry((-15.0, 0.0), (-5.0, 0.0), self.C, 5.0), 1.0)

    def test_no_entry_cases(self) -> None:
        self.assertIsNone(metrics.segment_circle_entry((-20.0, 0.0), (-6.0, 0.0), self.C, 5.0))   # stops short
        self.assertIsNone(metrics.segment_circle_entry((-6.0, 0.0), (-20.0, 0.0), self.C, 5.0))   # moving away
        self.assertIsNone(metrics.segment_circle_entry((-6.0, 0.0), (-6.0, 0.0), self.C, 5.0))    # not moving
        self.assertIsNone(metrics.segment_circle_entry((10.0, -10.0), (10.0, 10.0), self.C, 5.0))  # passes by

    def test_start_inside_is_zero(self) -> None:
        self.assertEqual(metrics.segment_circle_entry((1.0, 1.0), (50.0, 0.0), self.C, 5.0), 0.0)
        self.assertEqual(metrics.segment_circle_entry((5.0, 0.0), (50.0, 0.0), self.C, 5.0), 0.0)

    def test_exit(self) -> None:
        self.assertAlmostEqual(metrics.segment_circle_exit((0.0, 0.0), (10.0, 0.0), self.C, 5.0), 0.5)
        self.assertAlmostEqual(metrics.segment_circle_exit((-4.0, 3.0), (6.0, 3.0), self.C, 5.0), 0.8)
        self.assertIsNone(metrics.segment_circle_exit((0.0, 0.0), (1.0, 1.0), self.C, 5.0))
        self.assertIsNone(metrics.segment_circle_exit((0.0, 0.0), (5.0, 0.0), self.C, 5.0))     # ends on the edge
        self.assertEqual(metrics.segment_circle_exit((5.0, 0.0), (9.0, 0.0), self.C, 5.0), 0.0)  # edge, outward
        self.assertIsNone(metrics.segment_circle_exit((1.0, 1.0), (1.0, 1.0), self.C, 5.0))


class HoverAimReportTests(unittest.TestCase):
    """Hand-computed trajectory: target (100, 0), radius 20, center zone 15 (fraction 0.75), 0.1 mm/px.

    The cursor waits at the origin, bends through (36, 48) and heads straight
    for the center, touches the circle at (84, 12) at t = 0.100 s, then slides
    along y = 12 at 1000 px/s to (120, 12) (enters the center zone at x = 91,
    passes 12 px from the center at x = 100, leaves the circle at x = 116) and
    stops there.
    """
    TARGET = (100.0, 0.0)
    PATH = [PenSample(0.0, 0, 0), PenSample(0.05, 36, 48), PenSample(0.10, 84, 12), PenSample(0.136, 120, 12),
            PenSample(0.2, 120, 12), PenSample(0.5, 120, 12)]

    def report(self, path=None, **kw):
        hit = (PenSample(0.10, 84.0, 12.0), self.TARGET)
        args = {"appear_times": [0.0], "samples": self.PATH if path is None else path, "radius_px": 20.0,
                "center_fraction": 0.75}
        args.update(kw)
        return metrics.aim_report([hit], 0.1, **args)

    def test_hand_computed_values(self) -> None:
        r = self.report()
        ex = r.extras
        self.assertEqual(r.sample_count, 1)
        self.assertEqual(ex["trajectory_count"], 1)
        self.assertAlmostEqual(r.aim_time_ms_mean, 100.0)
        self.assertAlmostEqual(r.aim_error_mean_mm, 1.2)                  # closest approach 12 px
        self.assertAlmostEqual(ex["activation_error_mean_mm"], 2.0)       # touched at the edge, 20 px
        self.assertAlmostEqual(ex["bias_x_mm"], 0.0)
        self.assertAlmostEqual(ex["bias_y_mm"], 1.2)
        # On-target window x = 84 -> 116 at y = 12: |e|^2 + e.d + |d|^2 / 3 = 400 - 512 + 1024 / 3.
        self.assertAlmostEqual(ex["settle_rms_mm"], math.sqrt(688 / 3) * 0.1)
        self.assertAlmostEqual(ex["path_efficiency_mean"], math.sqrt(7200) / 120)   # = 1 / sqrt(2)
        self.assertAlmostEqual(ex["overshoot_mean_mm"], 2.0)              # stopped at x = 120
        self.assertAlmostEqual(ex["overshoot_max_mm"], 2.0)
        self.assertAlmostEqual(ex["center_time_ms_mean"], 107.0)          # x = 91 reached at 0.107 s
        self.assertAlmostEqual(ex["center_reach_rate"], 1.0)
        self.assertEqual(ex["aim_time_source"], "appear")

    def test_closest_approach_stops_when_the_cursor_leaves(self) -> None:
        back = self.PATH[:4] + [PenSample(0.2, 100, 2), PenSample(0.5, 100, 2)]    # returns close later
        self.assertAlmostEqual(self.report(back).aim_error_mean_mm, 1.2)
        self.assertAlmostEqual(self.report(back).extras["overshoot_mean_mm"], 2.0)
        self.assertAlmostEqual(self.report(back, radius_px=None).aim_error_mean_mm, 0.2)   # no circle, no cut
        self.assertNotIn("center_reach_rate", self.report(back, radius_px=None).extras)

    def test_settle_window_length(self) -> None:
        short = self.report(settle_s=0.01)           # window ends at x = 94
        self.assertAlmostEqual(short.aim_error_mean_mm, math.sqrt(36 + 144) * 0.1)
        self.assertAlmostEqual(short.extras["overshoot_mean_mm"], 2.0)     # own 120 ms window
        self.assertAlmostEqual(self.report(overshoot_s=0.01).extras["overshoot_mean_mm"], 0.0)
        self.assertAlmostEqual(self.report(overshoot_s=0.018).extras["overshoot_mean_mm"], 0.2)   # x = 102

    def test_overshoot_ignores_the_move_to_the_next_target(self) -> None:
        # Rests at x = 120, then 150 ms after activation heads on in the same direction.
        onward = self.PATH[:4] + [PenSample(0.25, 120, 12), PenSample(0.35, 400, 12)]
        self.assertAlmostEqual(self.report(onward).extras["overshoot_mean_mm"], 2.0)
        self.assertAlmostEqual(self.report(onward, overshoot_s=0.2).extras["overshoot_mean_mm"], 16.0)   # x = 260

    def test_center_zone_not_reached(self) -> None:
        r = self.report(center_fraction=0.5)         # zone 10 < 12 px pass distance
        self.assertEqual(r.extras["center_reach_rate"], 0.0)
        self.assertIsNone(r.extras["center_time_ms_mean"])
        self.assertIsNone(r.extras["center_time_ms_median"])

    def test_straight_approach_is_efficient(self) -> None:
        path = [PenSample(0.0, 0, 0), PenSample(0.08, 80, 0), PenSample(0.1, 100, 0), PenSample(0.5, 100, 0)]
        r = metrics.aim_report([(PenSample(0.08, 80.0, 0.0), self.TARGET)], 0.1, appear_times=[0.0],
                               samples=path, radius_px=20.0)
        self.assertAlmostEqual(r.extras["path_efficiency_mean"], 1.0)
        self.assertAlmostEqual(r.aim_error_mean_mm, 0.0)
        self.assertAlmostEqual(r.extras["overshoot_mean_mm"], 0.0)
        self.assertAlmostEqual(r.extras["center_time_ms_mean"], 0.08 * 1000 + 13.0)   # zone 7 px: x = 93

    def test_activation_outside_trajectory_falls_back(self) -> None:
        hits = [(PenSample(0.10, 84.0, 12.0), self.TARGET), (PenSample(9.0, 500.0, 3.0), (500.0, 0.0))]
        r = metrics.aim_report(hits, 0.1, appear_times=[0.0, 0.10], samples=self.PATH, radius_px=20.0)
        self.assertEqual(r.extras["trajectory_count"], 1)
        self.assertAlmostEqual(r.aim_error_mean_mm, (1.2 + 0.3) / 2)

    def test_scales_with_mm_per_px(self) -> None:
        a = metrics.aim_report([(PenSample(0.10, 84.0, 12.0), self.TARGET)], 0.1, appear_times=[0.0],
                               samples=self.PATH, radius_px=20.0)
        b = metrics.aim_report([(PenSample(0.10, 84.0, 12.0), self.TARGET)], 0.2, appear_times=[0.0],
                               samples=self.PATH, radius_px=20.0)
        self.assertAlmostEqual(b.aim_error_mean_mm, 2 * a.aim_error_mean_mm)
        self.assertAlmostEqual(b.extras["settle_rms_mm"], 2 * a.extras["settle_rms_mm"])
        self.assertAlmostEqual(b.extras["path_efficiency_mean"], a.extras["path_efficiency_mean"])
        self.assertAlmostEqual(b.aim_time_ms_mean, a.aim_time_ms_mean)


if __name__ == "__main__":
    unittest.main()
