"""Display-free tests for gui.live_tests logic and gui.pen_sampler."""
from __future__ import annotations

import json
import math
import random
import string
import time
import unittest
from pathlib import Path

from osu_tablet_assistant.gui import live_tests as lt
from osu_tablet_assistant.gui.pen_sampler import PenSampler
from osu_tablet_assistant.models import DisplaySpec, PenSample

LOCALES = Path(lt.__file__).resolve().parent.parent / "locales"
RECT = (0, 0, 1920, 1080)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class WindowRectTests(unittest.TestCase):
    def test_primary_when_none(self) -> None:
        self.assertEqual(lt.window_rect(None, (2560, 1440)), (0, 0, 2560, 1440))

    def test_center_based_display(self) -> None:
        display = DisplaySpec(1920, 1080, offset_x=2880.0, offset_y=540.0)
        self.assertEqual(lt.window_rect(display, (1920, 1080)), (1920, 0, 1920, 1080))

    def test_display_left_of_primary_has_negative_origin(self) -> None:
        display = DisplaySpec(1280, 1024, offset_x=-640.0, offset_y=512.0)
        self.assertEqual(lt.window_rect(display, (1920, 1080)), (-1280, 0, 1280, 1024))


class TrackingCourseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.course = lt.make_tracking_course((100, 50, 1920, 1080), 8.0)

    def test_constant_speed(self) -> None:
        dt = 0.01
        speeds = []
        for i in range(800):
            (x0, y0), (x1, y1) = self.course.position(i * dt), self.course.position((i + 1) * dt)
            speeds.append(math.hypot(x1 - x0, y1 - y0) / dt)
        self.assertAlmostEqual(min(speeds), self.course.speed, delta=0.01 * self.course.speed)
        self.assertAlmostEqual(max(speeds), self.course.speed, delta=0.01 * self.course.speed)

    def test_closed_loops_and_bounds(self) -> None:
        x0, y0 = self.course.position(0.0)
        x1, y1 = self.course.position(4.0)       # 2 loops in 8 s
        self.assertAlmostEqual(math.hypot(x1 - x0, y1 - y0), 0.0, delta=1.0)
        for i in range(0, 800, 7):
            x, y = self.course.position(i / 100)
            self.assertTrue(100 + 960 - 0.36 * 1920 <= x <= 100 + 960 + 0.36 * 1920)
            self.assertTrue(50 + 540 - 0.36 * 1080 <= y <= 50 + 540 + 0.36 * 1080)

    def test_negative_time_stays_at_start(self) -> None:
        self.assertEqual(self.course.position(-1.0), self.course.position(0.0))

    def test_sample_path_uses_caller_clock(self) -> None:
        path = self.course.sample_path(1000.0, 8.0, 240.0)
        self.assertEqual(len(path), 8 * 240 + 1)
        self.assertEqual(path[0].t, 1000.0)
        self.assertAlmostEqual(path[-1].t, 1008.0)
        self.assertEqual((path[10].x, path[10].y), self.course.position(10 / 240))


class AimPlanTests(unittest.TestCase):
    def test_seeded_plan_is_reproducible(self) -> None:
        a = lt.plan_aim_targets(RECT, 100.0, 20, random.Random(5))
        b = lt.plan_aim_targets(RECT, 100.0, 20, random.Random(5))
        c = lt.plan_aim_targets(RECT, 100.0, 20, random.Random(6))
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(len(a), 20)

    def test_margin_and_minimum_jump(self) -> None:
        rect = (-1920, 100, 1920, 1080)
        targets = lt.plan_aim_targets(rect, 120.0, 200, random.Random(1))
        for x, y in targets:
            self.assertTrue(-1920 + 120 <= x <= -120)
            self.assertTrue(100 + 120 <= y <= 100 + 1080 - 120)
        jumps = [math.dist(a, b) for a, b in zip(targets, targets[1:])]
        self.assertGreaterEqual(min(jumps), lt.AIM_MIN_JUMP_FRACTION * 1080 - 1e-6)

    def test_radius_is_clamped(self) -> None:
        self.assertEqual(lt.aim_radius_px(1.0), lt.AIM_RADIUS_PX[0])
        self.assertEqual(lt.aim_radius_px(0.001), lt.AIM_RADIUS_PX[1])
        self.assertAlmostEqual(lt.aim_radius_px(0.05), 60.0)
        self.assertAlmostEqual(lt.aim_radius_px(0.1), 30.0)


class AimSessionTests(unittest.TestCase):
    def test_flow_and_timing(self) -> None:
        session = lt.AimSession([(10.0, 10.0), (50.0, 60.0)])
        self.assertIsNone(session.current)
        self.assertFalse(session.click(0.5, 10, 10))              # nothing shown yet
        session.start(1.0)
        self.assertEqual(session.current, (10.0, 10.0))
        self.assertFalse(session.click(1.05, 10, 10))             # debounced
        self.assertTrue(session.click(1.5, 12, 9))
        self.assertEqual(session.current, (50.0, 60.0))
        self.assertTrue(session.click(2.25, 49, 61))
        self.assertTrue(session.done)
        self.assertIsNone(session.current)
        self.assertFalse(session.click(3.0, 0, 0))
        self.assertEqual(session.appear_times, [1.0, 1.5])
        self.assertEqual([c.t for c, _ in session.clicks], [1.5, 2.25])
        self.assertEqual(session.clicks[1][1], (50.0, 60.0))


class BuildReportTests(unittest.TestCase):
    def test_rate(self) -> None:
        samples = [PenSample(10.0 + i / 125.0, i, 2 * i) for i in range(500)]
        report = lt.build_report("rate", 0.05, samples, started_at=10.0)
        self.assertEqual(report.test_kind, "rate")
        self.assertAlmostEqual(report.effective_rate_hz, 125.0, delta=0.5)

    def test_stillness_ignores_first_half_second(self) -> None:
        rng = random.Random(2)
        settle = [PenSample(5.0 + i / 1000, 300 + i * 0.1, 300) for i in range(400)]     # 0.4 s of movement
        still = [PenSample(5.4 + i / 1000, 500 + rng.gauss(0, 0.3), 400 + rng.gauss(0, 0.3)) for i in range(4600)]
        with_settle = lt.build_report("stillness", 0.05, settle + still, started_at=5.0)
        expected = math.sqrt(2) * 0.3 * 0.05
        self.assertAlmostEqual(with_settle.jitter_rms_mm, expected, delta=0.1 * expected)
        self.assertLess(with_settle.duration_s, 4.5)

    def test_tracking_end_to_end(self) -> None:
        course = lt.make_tracking_course(RECT, 8.0)
        rng = random.Random(4)
        lag = 0.05
        samples = []
        for i in range(8 * 133):
            t = 100.0 + i / 133.0
            x, y = course.position(t - 100.0 - lag)
            samples.append(PenSample(t, x + rng.gauss(0, 0.5), y + rng.gauss(0, 0.5)))
        report = lt.build_report("tracking", 0.05, samples, started_at=100.0, course=course)
        self.assertAlmostEqual(report.lag_ms_estimate, 50.0, delta=4.0)
        self.assertLess(report.path_error_mean_mm, 0.1)
        self.assertEqual(report.sample_count, 7 * 133)           # first second discarded

    def test_aim_end_to_end(self) -> None:
        session = lt.AimSession([(100.0, 100.0), (400.0, 300.0), (200.0, 500.0)])
        session.start(0.0)
        session.click(0.6, 103.0, 96.0)
        session.click(1.4, 400.0, 300.0)
        session.click(2.4, 200.0, 510.0)
        report = lt.build_report("aim", 0.1, [], started_at=0.0, aim=session)
        self.assertEqual(report.sample_count, 3)
        self.assertAlmostEqual(report.aim_error_mean_mm, (5 + 0 + 10) / 3 * 0.1)
        self.assertAlmostEqual(report.aim_time_ms_mean, (600 + 800 + 1000) / 3)
        self.assertEqual(report.extras["aim_time_source"], "appear")

    def test_no_data_gives_empty_metrics(self) -> None:
        course = lt.make_tracking_course(RECT, 8.0)
        self.assertIsNone(lt.build_report("rate", 0.05, [], started_at=0.0).effective_rate_hz)
        self.assertIsNone(lt.build_report("stillness", 0.05, [], started_at=0.0).jitter_rms_mm)
        self.assertIsNone(lt.build_report("tracking", 0.05, [], started_at=0.0, course=course).lag_ms_estimate)
        self.assertIsNone(lt.build_report("aim", 0.05, [], started_at=0.0, aim=lt.AimSession([])).aim_error_mean_mm)

    def test_invalid_arguments(self) -> None:
        with self.assertRaises(ValueError):
            lt.build_report("bogus", 0.05, [], started_at=0.0)
        with self.assertRaises(ValueError):
            lt.build_report("tracking", 0.05, [], started_at=0.0)
        with self.assertRaises(ValueError):
            lt.build_report("aim", 0.05, [], started_at=0.0)


class RunTestValidationTests(unittest.TestCase):
    def test_rejects_bad_arguments_before_touching_tk(self) -> None:
        with self.assertRaises(ValueError):
            lt.run_test(None, "nope", 0.05, None, lambda r: None)
        for bad in (0.0, -1.0, math.nan):
            with self.assertRaises(ValueError):
                lt.run_test(None, "rate", bad, None, lambda r: None)

    def test_translate_fallback_returns_key_or_translation(self) -> None:
        self.assertEqual(lt.resolve_translate(lambda key, **p: key.upper())("a.b"), "A.B")
        self.assertIsInstance(lt.resolve_translate(None)("tests.common.start_hint"), str)


class LocaleTests(unittest.TestCase):
    def load(self, lang: str) -> dict[str, str]:
        return json.loads((LOCALES / lang / "tests.json").read_text(encoding="utf-8"))

    def test_same_keys_in_both_languages(self) -> None:
        es, en = self.load("es"), self.load("en")
        self.assertEqual(set(es), set(en))
        for data in (es, en):
            self.assertTrue(all(k.startswith("tests.") for k in data))
            self.assertTrue(all(isinstance(v, str) and v.strip() for v in data.values()))

    def test_every_used_key_exists(self) -> None:
        self.assertLessEqual(lt.all_keys(), set(self.load("es")))

    def test_placeholders_match_between_languages(self) -> None:
        es, en = self.load("es"), self.load("en")
        for key in es:
            self.assertEqual(sorted(_fields(es[key])), sorted(_fields(en[key])), key)


def _fields(text: str) -> list[str]:
    return [name for _, name, _, _ in string.Formatter().parse(text) if name]


class PenSamplerTests(unittest.TestCase):
    def test_records_on_change_and_heartbeat(self) -> None:
        clock = FakeClock()
        pos = [(1.0, 1.0)]
        sampler = PenSampler(position_source=lambda: pos[0], clock=clock, heartbeat_s=0.05)
        self.assertTrue(sampler.threaded)
        sampler.poll()                       # first sample
        clock.now = 0.01
        sampler.poll()                       # unchanged, heartbeat not due
        clock.now = 0.02
        pos[0] = (2.0, 1.0)
        sampler.poll()                       # changed
        clock.now = 0.08
        sampler.poll()                       # unchanged but heartbeat due
        got = sampler.drain()
        self.assertEqual([(s.t, s.x, s.y) for s in got], [(0.0, 1.0, 1.0), (0.02, 2.0, 1.0), (0.08, 2.0, 1.0)])
        self.assertEqual(sampler.drain(), [])

    def test_none_position_is_skipped(self) -> None:
        sampler = PenSampler(position_source=lambda: None, clock=FakeClock())
        self.assertIsNone(sampler.poll())
        self.assertEqual(sampler.drain(), [])

    def test_record_for_event_driven_fallback(self) -> None:
        clock = FakeClock()
        sampler = PenSampler(position_source=lambda: None, clock=clock)
        clock.now = 1.0
        sampler.record(5, 6)
        sampler.record(5, 6, t=1.01)         # duplicate within heartbeat
        sampler.record(7, 6, t=1.02)
        self.assertEqual([(s.t, s.x) for s in sampler.drain()], [(1.0, 5.0), (1.02, 7.0)])

    def test_thread_collects_and_stops(self) -> None:
        counter = iter(range(10 ** 9))
        sampler = PenSampler(position_source=lambda: (float(next(counter)), 0.0), poll_hz=2000.0)
        sampler.start()
        sampler.start()                      # second start is a no-op
        self.assertTrue(sampler.running)
        time.sleep(0.1)
        sampler.stop()
        sampler.stop()                       # idempotent
        self.assertFalse(sampler.running)
        got = sampler.drain()
        self.assertGreater(len(got), 10)
        self.assertEqual([s.t for s in got], sorted(s.t for s in got))
        time.sleep(0.02)
        self.assertEqual(sampler.drain(), [])    # nothing after stop


class ImportTests(unittest.TestCase):
    def test_modules_import_without_display(self) -> None:
        import osu_tablet_assistant.engine.metrics  # noqa: F401
        import osu_tablet_assistant.gui.live_tests  # noqa: F401
        import osu_tablet_assistant.gui.pen_sampler  # noqa: F401


if __name__ == "__main__":
    unittest.main()
