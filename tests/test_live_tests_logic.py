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


    def test_first_target_is_away_from_the_cursor(self) -> None:
        for seed in range(30):
            start = (random.Random(seed).uniform(0, 1920), random.Random(seed + 99).uniform(0, 1080))
            targets = lt.plan_aim_targets(RECT, 108.0, 5, random.Random(seed), start=start)
            self.assertGreaterEqual(math.dist(start, targets[0]), lt.AIM_MIN_JUMP_FRACTION * 1080 - 1e-6)

    def test_minimum_jump_is_guaranteed_by_the_corner_fallback(self) -> None:
        # Allowed area 360 x 260, half diagonal ~222: random tries rarely find 220 px, corners always do.
        for seed in range(10):
            targets = lt.plan_aim_targets((0, 0, 400, 300), 20.0, 100, random.Random(seed), start=(200.0, 150.0),
                                          min_jump_px=220.0)
            points = [(200.0, 150.0)] + targets
            self.assertGreaterEqual(min(math.dist(a, b) for a, b in zip(points, points[1:])), 220.0)
            for x, y in targets:
                self.assertTrue(20 <= x <= 380 and 20 <= y <= 280)

    def test_window_session_never_spawns_under_the_cursor(self) -> None:
        # Activation leaves the cursor inside the previous circle (< 1 radius from its center), so the
        # next center must be more than 2 radii away; the first one more than 1 radius from the cursor.
        for rect in (RECT, (0, 0, 800, 600), (-1280, 0, 1280, 1024), (0, 0, 1024, 768)):
            for mm_per_px in (0.02, 0.05, 0.1, 0.3):
                for seed in range(5):
                    cursor = (rect[0] + rect[2] / 2, rect[1] + rect[3] / 2)
                    s = lt.new_aim_session(rect, mm_per_px, random.Random(seed), cursor=cursor)
                    r = s.radius
                    self.assertGreater(math.dist(cursor, s.targets[0]), 2 * r)
                    for a, b in zip(s.targets, s.targets[1:]):
                        self.assertGreater(math.dist(a, b), 2 * r, (rect, mm_per_px, seed))


def flick(session: lt.AimSession, start: tuple[float, float], t0: float, hz: float = 1000.0,
          speed: float = 3000.0, linger_s: float = 0.15) -> None:
    """Scripted cursor: straight line to each target center at ``speed`` px/s, sampled at ``hz``.

    After an activation it keeps heading to the old center for ``linger_s`` (reaction time).
    """
    session.start(t0, start)
    t, pos = t0, start
    goal, retarget_at = session.current, t0
    end = t0 + 60
    while t < end:
        t += 1 / hz
        if t >= retarget_at and session.current is not None:
            goal = session.current
        step = speed / hz
        dx, dy = goal[0] - pos[0], goal[1] - pos[1]
        dist = math.hypot(dx, dy)
        pos = goal if dist <= step else (pos[0] + dx / dist * step, pos[1] + dy / dist * step)
        before = len(session.hits)
        session.observe(t, *pos)
        if len(session.hits) > before:
            retarget_at = t + linger_s
            if session.done:
                end = t + 0.4                # settle samples after the last activation


class AimSessionTests(unittest.TestCase):
    """Activation on a single target at (100, 0), radius 10, debounce 0.1 s, cursor starting at the origin."""

    def session(self, activation: str = "touch", **kw) -> lt.AimSession:
        s = lt.AimSession([(100.0, 0.0), (400.0, 0.0)], activation=activation, radius_px=10.0, **kw)
        s.start(0.0, (0.0, 0.0))
        return s

    def hit(self, s: lt.AimSession, i: int = 0) -> PenSample:
        return s.hits[i].sample

    def test_click_mode_flow_and_timing(self) -> None:
        session = lt.AimSession([(10.0, 10.0), (50.0, 60.0)], activation="click")
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

    def test_click_mode_ignores_hover(self) -> None:
        s = self.session("click")
        self.assertFalse(s.observe(0.2, 50, 0))
        self.assertFalse(s.observe(0.3, 100, 0))                  # dead center, no button
        self.assertEqual(s.hits, [])
        self.assertTrue(s.press(0.35, 101, 1))
        self.assertEqual((self.hit(s).t, self.hit(s).x), (0.35, 101.0))

    def test_touch_fires_at_the_interpolated_entry(self) -> None:
        s = self.session()
        self.assertFalse(s.observe(0.2, 50, 0))
        self.assertTrue(s.observe(0.3, 95, 0))
        h = self.hit(s)
        self.assertAlmostEqual(h.x, 90.0)
        self.assertAlmostEqual(h.y, 0.0)
        self.assertAlmostEqual(h.t, 0.2 + 0.1 * 40 / 45)
        self.assertEqual(s.current, (400.0, 0.0))
        self.assertAlmostEqual(s.appear_times[0], 0.0)
        self.assertEqual(s.hits[0].target, (100.0, 0.0))

    def test_fast_flick_crossing_between_two_samples(self) -> None:
        s = self.session()
        s.observe(0.15, 0, 0)
        self.assertTrue(s.observe(0.16, 300, 0))                  # both samples outside, circle in between
        self.assertAlmostEqual(self.hit(s).x, 90.0)
        self.assertAlmostEqual(self.hit(s).t, 0.15 + 0.01 * 90 / 300)

    def test_grazing_misses_and_tangent_touches(self) -> None:
        s = self.session()
        s.observe(0.2, 0, 10.001)
        self.assertFalse(s.observe(0.3, 200, 10.001))
        s.observe(0.4, 0, 10.0)
        self.assertTrue(s.observe(0.5, 200, 10.0))
        self.assertAlmostEqual(self.hit(s).x, 100.0)
        self.assertAlmostEqual(self.hit(s).t, 0.45)

    def test_center_mode_needs_the_center_zone(self) -> None:
        s = self.session("center", center_fraction=0.5)           # zone radius 5
        self.assertEqual(s.zone_radius, 5.0)
        s.observe(0.15, 0, 6)
        self.assertFalse(s.observe(0.35, 200, 6))                 # crossed the ring, not the zone
        for t, x, y in ((0.36, 200, 30), (0.37, 0, 30), (0.4, 0, 3)):   # back around, far from the target
            self.assertFalse(s.observe(t, x, y))
        self.assertTrue(s.observe(0.6, 200, 3))                   # 3-4-5: zone entered at x = 96
        self.assertAlmostEqual(self.hit(s).x, 96.0)
        self.assertAlmostEqual(self.hit(s).t, 0.4 + 0.2 * 96 / 200)

    def test_mouse_buttons_are_ignored_in_touch_and_center_modes(self) -> None:
        for mode in ("touch", "center"):
            with self.subTest(mode=mode):
                s = self.session(mode)
                s.observe(0.2, 300, 300)                          # far from the target
                self.assertFalse(s.press(0.3, 100, 0))            # a click right on the center
                self.assertFalse(s.press(0.4, 300, 300))
                self.assertEqual(s.hits, [])
                self.assertEqual(s.current, (100.0, 0.0))

    def test_legacy_click_is_a_cursor_sample_in_touch_mode(self) -> None:
        s = self.session()
        self.assertFalse(s.click(0.2, 50, 0))
        self.assertTrue(s.click(0.3, 100, 0))                     # fires on position, at the edge crossing
        self.assertAlmostEqual(self.hit(s).x, 90.0)

    def test_cursor_inside_at_spawn_must_leave_first(self) -> None:
        s = lt.AimSession([(100.0, 0.0)], radius_px=10.0)
        s.start(0.0, (100.0, 0.0))                                # spawned right under the cursor
        self.assertFalse(s.observe(0.2, 101, 0))
        self.assertFalse(s.observe(0.3, 95, 2))
        self.assertFalse(s.observe(0.4, 120, 0))                  # leaves the circle
        self.assertTrue(s.observe(0.5, 100, 0))                   # comes back
        self.assertAlmostEqual(self.hit(s).x, 110.0)
        self.assertAlmostEqual(self.hit(s).t, 0.45)

    def test_center_mode_spawn_inside_ring_must_leave_the_ring(self) -> None:
        s = lt.AimSession([(100.0, 0.0)], activation="center", radius_px=10.0)
        s.start(0.0, (108.0, 0.0))                                # on the ring, outside the zone
        self.assertFalse(s.observe(0.3, 100, 0))                  # straight to the center: not counted
        self.assertFalse(s.observe(0.4, 130, 0))
        self.assertTrue(s.observe(0.6, 100, 0))

    def test_debounce_ignores_a_pass_through(self) -> None:
        s = self.session()
        self.assertFalse(s.observe(0.04, 200, 0))                 # crossed the whole circle in 40 ms
        self.assertFalse(s.observe(0.2, 200, 0))
        self.assertEqual(s.hits, [])
        self.assertTrue(s.observe(0.3, 100, 0))                   # a real approach later counts
        self.assertAlmostEqual(self.hit(s).t, 0.2 + 0.1 * 90 / 100)

    def test_inside_when_the_debounce_ends_fires_at_its_end(self) -> None:
        s = self.session()
        self.assertFalse(s.observe(0.05, 100, 0))                 # reached it within 50 ms
        self.assertTrue(s.observe(0.2, 101, 0))                   # never stuck: fires at 0.1 s
        h = self.hit(s)
        self.assertAlmostEqual(h.t, lt.AIM_DEBOUNCE_S)
        self.assertAlmostEqual(h.x, 100 + 1 * 0.05 / 0.15)

    def test_segment_straddling_the_debounce_uses_the_part_after_it(self) -> None:
        s = self.session()
        s.observe(0.05, 0, 0)
        self.assertTrue(s.observe(0.25, 200, 0))                  # at 0.1 s the cursor is at x = 50
        self.assertAlmostEqual(self.hit(s).t, 0.1 + 0.15 * 40 / 150)
        s2 = self.session()
        s2.observe(0.05, 0, 0)
        self.assertFalse(s2.observe(0.125, 300, 0))               # at 0.1 s already past the circle

    def test_one_long_segment_can_fire_two_targets(self) -> None:
        s = self.session()
        s.observe(0.2, 0, 0)
        self.assertTrue(s.observe(1.2, 400, 0))
        self.assertTrue(s.done)
        self.assertAlmostEqual(self.hit(s, 0).t, 0.2 + 90 / 400)
        self.assertAlmostEqual(self.hit(s, 1).t, 0.2 + 390 / 400)
        self.assertAlmostEqual(s.appear_times[1], self.hit(s, 0).t)

    def test_teleport_only_tests_the_end_point(self) -> None:
        s = self.session(max_step_px=100.0)
        s.observe(0.2, 0, 0)
        self.assertFalse(s.observe(0.21, 200, 0))                 # pen re-entered range far away
        self.assertTrue(s.observe(0.3, 100, 0))                   # a normal 100 px step
        self.assertAlmostEqual(self.hit(s).x, 110.0)
        self.assertAlmostEqual(self.hit(s).t, 0.21 + 0.09 * 0.9)
        s2 = self.session(max_step_px=100.0)
        s2.observe(0.2, 0, 0)
        self.assertTrue(s2.observe(0.21, 102, 0))                 # teleport landing inside counts
        self.assertEqual((self.hit(s2).t, self.hit(s2).x), (0.21, 102.0))

    def test_unknown_start_position_takes_the_first_sample_at_face_value(self) -> None:
        s = lt.AimSession([(100.0, 0.0)], radius_px=10.0)
        s.start(0.0)
        self.assertFalse(s.observe(0.05, 100, 0))                 # debounced
        s2 = lt.AimSession([(100.0, 0.0)], radius_px=10.0)
        s2.start(0.0)
        self.assertTrue(s2.observe(0.3, 103, 0))
        self.assertEqual(self.hit(s2).t, 0.3)

    def test_bad_samples_are_ignored(self) -> None:
        s = self.session()
        s.observe(0.3, 50, 0)
        self.assertFalse(s.observe(0.2, 100, 0))                  # older than the previous sample
        self.assertFalse(s.observe(math.nan, 100, 0))
        self.assertFalse(s.observe(0.4, math.inf, 0))
        self.assertEqual(len(s.trail), 2)                         # start + the one good sample
        self.assertTrue(s.observe(0.5, 100, 0))

    def test_not_started_or_finished(self) -> None:
        s = lt.AimSession([(100.0, 0.0)], radius_px=10.0)
        self.assertFalse(s.observe(0.5, 100, 0))
        self.assertEqual(lt.AimSession([]).done, True)

    def test_invalid_arguments(self) -> None:
        for kw in ({"activation": "hover"}, {"radius_px": 0.0}, {"radius_px": math.nan},
                   {"center_fraction": 0.0}, {"center_fraction": 1.5}):
            with self.assertRaises(ValueError, msg=str(kw)):
                lt.AimSession([(0.0, 0.0)], **kw)

    def test_scripted_run_hits_every_target_on_the_edge(self) -> None:
        for mode in ("touch", "center"):
            with self.subTest(mode=mode):
                s = lt.new_aim_session(RECT, 0.1, random.Random(7), cursor=(960.0, 540.0), activation=mode)
                flick(s, (960.0, 540.0), 10.0)
                self.assertTrue(s.done)
                self.assertEqual(len(s.hits), lt.AIM_TARGET_COUNT)
                times = [h.sample.t for h in s.hits]
                self.assertEqual(times, sorted(times))
                for h in s.hits:
                    self.assertAlmostEqual(math.dist((h.sample.x, h.sample.y), h.target), s.zone_radius,
                                           delta=1e-6)

    def test_superhuman_cursor_is_never_stuck(self) -> None:
        # 30 000 px/s with no reaction time: targets are often reached inside the debounce.
        s = lt.new_aim_session(RECT, 0.1, random.Random(2), cursor=(960.0, 540.0))
        flick(s, (960.0, 540.0), 0.0, speed=30000.0, linger_s=0.0)
        self.assertTrue(s.done)
        for h in s.hits:
            self.assertGreaterEqual(h.sample.t - h.appeared_at, lt.AIM_DEBOUNCE_S - 1e-9)


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

    def test_aim_click_mode_end_to_end(self) -> None:
        session = lt.AimSession([(100.0, 100.0), (400.0, 300.0), (200.0, 500.0)], activation="click")
        session.start(0.0)
        session.click(0.6, 103.0, 96.0)
        session.click(1.4, 400.0, 300.0)
        session.click(2.4, 200.0, 510.0)
        report = lt.build_report("aim", 0.1, [], started_at=0.0, aim=session)
        self.assertEqual(report.sample_count, 3)
        self.assertAlmostEqual(report.aim_error_mean_mm, (5 + 0 + 10) / 3 * 0.1)
        self.assertAlmostEqual(report.aim_time_ms_mean, (600 + 800 + 1000) / 3)
        self.assertEqual(report.extras["aim_time_source"], "appear")

    def test_aim_touch_end_to_end(self) -> None:
        # Straight at 3000 px/s to each center, lingering there 150 ms: every target is touched on
        # its edge, then the cursor reaches the center (closest approach 0, no overshoot).
        mm_per_px = 0.1
        session = lt.new_aim_session(RECT, mm_per_px, random.Random(11), cursor=(960.0, 540.0))
        flick(session, (960.0, 540.0), 5.0)
        r = session.radius
        starts = [(960.0, 540.0)] + [h.target for h in session.hits]
        travel = [(math.dist(p, h.target) - r) / 3000.0 for p, h in zip(starts, session.hits)]
        expected = [travel[0]] + [0.15 + t for t in travel[1:]]
        from_trail = lt.build_report("aim", mm_per_px, [], started_at=5.0, aim=session)
        from_samples = lt.build_report("aim", mm_per_px, list(session.trail), started_at=5.0, aim=session)
        for report in (from_trail, from_samples):
            self.assertEqual(report.sample_count, lt.AIM_TARGET_COUNT)
            self.assertEqual(report.extras["trajectory_count"], lt.AIM_TARGET_COUNT)
            self.assertAlmostEqual(report.aim_error_mean_mm, 0.0, delta=1e-6)
            self.assertAlmostEqual(report.extras["activation_error_mean_mm"], r * mm_per_px, delta=1e-6)
            self.assertAlmostEqual(report.extras["overshoot_mean_mm"], 0.0, delta=1e-6)
            # Later approaches start on the previous edge and detour through its center.
            self.assertTrue(0.75 < report.extras["path_efficiency_mean"] <= 1.0)
            self.assertEqual(report.extras["center_reach_rate"], 1.0)
            self.assertGreater(report.extras["center_time_ms_mean"], report.aim_time_ms_mean)
            # The first target is reached from the start point, later ones after the 150 ms linger.
            self.assertAlmostEqual(report.aim_time_ms_mean, 1000 * sum(expected) / len(expected), delta=2.0)

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
        with self.assertRaises(ValueError):
            lt.run_test(None, "aim", 0.05, None, lambda r: None, activation="hover")
        for bad in (0.0, 1.01):
            with self.assertRaises(ValueError):
                lt.run_test(None, "aim", 0.05, None, lambda r: None, center_fraction=bad)

    def test_run_test_defaults_to_touch_activation(self) -> None:
        import inspect
        params = inspect.signature(lt.run_test).parameters
        self.assertEqual(params["activation"].default, "touch")
        self.assertEqual(params["activation"].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertEqual(params["center_fraction"].default, lt.metrics.AIM_CENTER_FRACTION)
        self.assertEqual(lt.AimSession([]).activation, "touch")

    def test_blend(self) -> None:
        self.assertEqual(lt.blend("#000000", "#ffffff", 0.0), "#000000")
        self.assertEqual(lt.blend("#000000", "#ffffff", 1.0), "#ffffff")
        self.assertEqual(lt.blend("#000000", "#fe02fe", 0.5), "#7f017f")
        self.assertEqual(lt.blend("#000000", "#ffffff", 7.0), "#ffffff")

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
        self.assertLessEqual(lt.all_keys(), set(self.load("en")))

    def test_aim_keys_per_activation(self) -> None:
        keys = {mode: lt.kind_keys("aim", mode) for mode in lt.ACTIVATIONS}
        self.assertEqual(keys["touch"]["instructions"], "tests.aim.instructions")
        self.assertEqual(keys["center"]["running"], "tests.aim.running_center")
        self.assertEqual(keys["click"]["instructions"], "tests.aim.instructions_click")
        self.assertEqual(len({k["instructions"] for k in keys.values()}), 3)
        self.assertEqual(lt.kind_keys("rate", "center")["instructions"], "tests.rate.instructions")
        for lang in ("es", "en"):
            data = self.load(lang)
            for mode in ("touch", "center"):
                self.assertIn("clic" if lang == "es" else "click", data[keys[mode]["instructions"]].lower())
                self.assertIn("hover", data[keys[mode]["instructions"]].lower())

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

    def test_snapshot_does_not_consume(self) -> None:
        sampler = PenSampler(position_source=lambda: None, clock=FakeClock())
        for i in range(5):
            sampler.record(i, 0, t=i / 10)
        self.assertEqual([s.x for s in sampler.snapshot()], [0, 1, 2, 3, 4])
        self.assertEqual([s.x for s in sampler.snapshot(3)], [3, 4])
        self.assertEqual(sampler.snapshot(9), [])
        self.assertEqual(len(sampler.drain()), 5)
        self.assertEqual(sampler.snapshot(), [])

    def test_position(self) -> None:
        pos = [(3, 4)]
        self.assertEqual(PenSampler(position_source=lambda: pos[0], clock=FakeClock()).position(), (3.0, 4.0))
        fallback = PenSampler(position_source=lambda: None, clock=FakeClock())
        self.assertIsNone(fallback.position())
        fallback.record(7, 8)
        self.assertEqual(fallback.position(), (7.0, 8.0))

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
