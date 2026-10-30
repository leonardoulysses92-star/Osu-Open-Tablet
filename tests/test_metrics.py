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


if __name__ == "__main__":
    unittest.main()
