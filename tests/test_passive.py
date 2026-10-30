"""Tests for osu_tablet_assistant.engine.passive (pure statistics on PenSample streams)."""
from __future__ import annotations

import math
import random
import unittest

from osu_tablet_assistant.engine import passive
from osu_tablet_assistant.gui import i18n
from osu_tablet_assistant.models import PenSample

MM_PER_PX = 0.0373   # 71.66 mm over 1920 px


def rest_stream(t0: float, seconds: float, jitter_px: float, seed: int, rate: float = 500.0,
                drift_px_s: float = 0.0, x0: float = 500.0) -> list[PenSample]:
    rng = random.Random(seed)
    n = int(seconds * rate)
    return [PenSample(t0 + i / rate, x0 + drift_px_s * i / rate + rng.gauss(0, jitter_px),
                      300 + rng.gauss(0, jitter_px)) for i in range(n)]


def circle_stream(t0: float, seconds: float, noise_px: float, seed: int, rate: float = 500.0,
                  radius_px: float = 200.0, rev_s: float = 0.5) -> list[PenSample]:
    rng = random.Random(seed)
    out = []
    for i in range(int(seconds * rate)):
        t = i / rate
        a = 2 * math.pi * t / rev_s
        out.append(PenSample(t0 + t, 960 + radius_px * math.cos(a) + rng.gauss(0, noise_px),
                             540 + radius_px * math.sin(a) + rng.gauss(0, noise_px)))
    return out


def session(t0: float, jitter_px: float, noise_px: float, seed: int, blocks: int = 6) -> list[PenSample]:
    """Alternating 1 s rests (at different spots) and 2 s circles, like hovering between patterns."""
    out: list[PenSample] = []
    t = t0
    for b in range(blocks):
        out += rest_stream(t, 1.0, jitter_px, seed + b, x0=300 + 40 * b)
        t += 1.05
        out += circle_stream(t, 2.0, noise_px, seed + 100 + b)
        t += 2.05
    return out


class BuildingBlocksTest(unittest.TestCase):
    def test_dedupe(self) -> None:
        s = [PenSample(0, 1, 1), PenSample(0.1, 1, 1), PenSample(0.2, 2, 1), PenSample(0.3, 1, 1)]
        self.assertEqual([p.t for p in passive.dedupe(s)], [0, 0.2, 0.3])

    def test_rest_jitter_matches_known_noise(self) -> None:
        stats = passive.window_stats(rest_stream(0, 2.0, 1.0, 1), MM_PER_PX)
        self.assertGreaterEqual(len(stats.rest_units), 1)
        self.assertAlmostEqual(stats.rest_jitter_mm, math.sqrt(2) * MM_PER_PX, delta=0.3 * MM_PER_PX)
        self.assertFalse(stats.below_resolution)

    def test_linear_drift_is_not_counted_as_jitter(self) -> None:
        stats = passive.window_stats(rest_stream(0, 1.0, 0.0, 1, drift_px_s=5.0), MM_PER_PX)
        self.assertLess(stats.rest_jitter_mm, 1e-6)

    def test_perfectly_still_is_below_resolution(self) -> None:
        still = [PenSample(i * 0.05, 100, 100) for i in range(40)]   # sampler heartbeats only
        stats = passive.window_stats(still, MM_PER_PX)
        self.assertTrue(stats.below_resolution)
        self.assertAlmostEqual(stats.rest_jitter_mm, 0.0, places=9)

    def test_noisy_path_is_rougher_than_clean_path(self) -> None:
        clean = passive.window_stats(circle_stream(0, 3.0, 0.0, 1), MM_PER_PX)
        noisy = passive.window_stats(circle_stream(0, 3.0, 1.5, 1), MM_PER_PX)
        self.assertIsNotNone(clean.roughness)
        self.assertGreater(noisy.roughness, clean.roughness * 3)
        self.assertGreaterEqual(len(clean.path_units), passive.MIN_PATH_UNITS)

    def test_moving_rate(self) -> None:
        stats = passive.window_stats(circle_stream(0, 2.0, 0.0, 1, rate=250.0), MM_PER_PX)
        self.assertAlmostEqual(stats.rate_hz, 250.0, delta=5)
        self.assertIsNone(passive.window_stats(rest_stream(0, 1.0, 0.0, 1), MM_PER_PX).rate_hz)

    def test_empty_and_tiny_inputs(self) -> None:
        empty = passive.window_stats([], MM_PER_PX)
        self.assertEqual(empty.sample_count, 0)
        self.assertIsNone(empty.rest_jitter_mm)
        self.assertEqual(passive.roughness_units([PenSample(0, 0, 0)], MM_PER_PX), [])
        self.assertEqual(passive.detrended_rms_px([PenSample(0, 0, 0)]), 0.0)

    def test_cohens_d(self) -> None:
        self.assertIsNone(passive.cohens_d([1.0], [2.0, 3.0]))
        self.assertEqual(passive.cohens_d([1.0, 1.0], [1.0, 1.0]), 0.0)
        self.assertEqual(passive.cohens_d([1.0, 1.0], [2.0, 2.0]), 10.0)
        self.assertAlmostEqual(passive.cohens_d([1, 2, 3], [2, 3, 4]), 1.0)


class CompareTest(unittest.TestCase):
    def test_less_jitter_and_noise_after_a_change_is_better(self) -> None:
        before = passive.window_stats(session(0, 1.5, 1.5, 10), MM_PER_PX)
        after = passive.window_stats(session(100, 0.4, 0.3, 20), MM_PER_PX)
        verdicts = {c.metric: c.verdict for c in passive.compare_windows(before, after)}
        self.assertEqual(verdicts["jitter"], "better")
        self.assertEqual(verdicts["path"], "better")
        self.assertIn("rate", verdicts)

    def test_same_conditions_are_similar(self) -> None:
        before = passive.window_stats(session(0, 1.0, 1.0, 30, blocks=10), MM_PER_PX)
        after = passive.window_stats(session(100, 1.0, 1.0, 40, blocks=10), MM_PER_PX)
        verdicts = {c.metric: c.verdict for c in passive.compare_windows(before, after)}
        self.assertEqual(verdicts["jitter"], "similar")

    def test_short_windows_are_not_enough_data(self) -> None:
        before = passive.window_stats(rest_stream(0, 0.5, 1.0, 1), MM_PER_PX)
        after = passive.window_stats(rest_stream(10, 0.5, 0.2, 2), MM_PER_PX)
        verdicts = {c.metric: c.verdict for c in passive.compare_windows(before, after)}
        self.assertEqual(verdicts, {"jitter": "not_enough_data", "path": "not_enough_data"})

    def test_hint_keys_exist_in_both_languages(self) -> None:
        known = set(i18n.load_catalog("en")) & set(i18n.load_catalog("es"))
        for metric in ("jitter", "path"):
            for verdict in ("better", "worse", "similar", "not_enough_data"):
                self.assertIn(f"passive.{metric}.{verdict}", known)
        self.assertIn("passive.rate.info", known)
        for s in ("small", "medium", "large"):
            self.assertIn(f"passive.strength.{s}", known)
        before = passive.window_stats(session(0, 1.5, 1.5, 10), MM_PER_PX)
        after = passive.window_stats(session(100, 0.4, 0.3, 20), MM_PER_PX)
        for reason in passive.hints(passive.compare_windows(before, after)):
            self.assertIn(reason.key, known)


class MonitorTest(unittest.TestCase):
    def test_windows_around_a_change(self) -> None:
        mon = passive.PassiveMonitor(MM_PER_PX, max_window_s=60)
        self.assertEqual(mon.compare(), ())
        mon.feed(session(0, 1.5, 1.5, 10))
        before = mon.mark_change()
        self.assertGreater(before.sample_count, 0)
        self.assertEqual(mon.current().sample_count, 0)
        mon.feed(session(100, 0.4, 0.3, 20))
        verdicts = {c.metric: c.verdict for c in mon.compare()}
        self.assertEqual(verdicts["jitter"], "better")
        self.assertTrue(mon.hints())

    def test_old_samples_are_dropped(self) -> None:
        mon = passive.PassiveMonitor(MM_PER_PX, max_window_s=5)
        mon.feed(rest_stream(0, 20, 0.5, 1, rate=100))
        cur = mon.current()
        self.assertLessEqual(cur.duration_s, 5.0 + 1e-6)

    def test_scale_change_applies_to_next_window(self) -> None:
        mon = passive.PassiveMonitor(MM_PER_PX)
        mon.feed(rest_stream(0, 1, 1.0, 1))
        self.assertEqual(mon.mark_change().mm_per_px, MM_PER_PX)
        mon.set_scale(0.05)
        self.assertEqual(mon.current().mm_per_px, 0.05)


if __name__ == "__main__":
    unittest.main()
