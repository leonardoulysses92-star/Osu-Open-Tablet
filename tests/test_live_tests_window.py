"""Opt-in smoke test of the real aim test window, driven by a scripted cursor.

It opens a small topmost window for a few seconds, so it only runs when
``OSU_TABLET_GUI_TESTS=1`` is set. The OS cursor is never touched: the
sampler reads a scripted position that flies to each target.
"""
from __future__ import annotations

import math
import os
import random
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from osu_tablet_assistant.gui import live_tests as lt
from osu_tablet_assistant.gui.pen_sampler import PenSampler
from osu_tablet_assistant.models import DisplaySpec

ENABLED = os.environ.get("OSU_TABLET_GUI_TESTS") == "1"
DISPLAY = DisplaySpec(640, 480, offset_x=320.0, offset_y=240.0)   # desktop (0, 0) - (640, 480)


class ScriptedCursor:
    """Moves at ``speed`` px/s toward the current target, pausing ``linger_s`` after each activation."""

    def __init__(self, speed: float = 2500.0, linger_s: float = 0.18) -> None:
        self.window: lt._TestWindow | None = None
        self.pos = (320.0, 240.0)
        self.speed, self.linger_s = speed, linger_s
        self._t = time.perf_counter()
        self._goal: tuple[float, float] | None = None
        self._hits = 0
        self._hold_until = 0.0

    def __call__(self) -> tuple[float, float]:
        now = time.perf_counter()
        dt, self._t = now - self._t, now
        aim = self.window.aim if self.window is not None else None
        if aim is not None:
            if len(aim.hits) != self._hits:
                self._hits = len(aim.hits)
                self._hold_until = now + self.linger_s
            if now >= self._hold_until and aim.current is not None:
                self._goal = aim.current
        if self._goal is not None:
            dx, dy = self._goal[0] - self.pos[0], self._goal[1] - self.pos[1]
            dist, step = math.hypot(dx, dy), self.speed * dt
            self.pos = self._goal if dist <= step else (self.pos[0] + dx / dist * step,
                                                        self.pos[1] + dy / dist * step)
        return self.pos


@unittest.skipUnless(ENABLED, "set OSU_TABLET_GUI_TESTS=1 to open real test windows")
class AimWindowSmokeTests(unittest.TestCase):
    def run_window(self, activation: str) -> tuple[object, list[BaseException], int]:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        errors: list[BaseException] = []
        root.report_callback_exception = lambda exc, val, tb: errors.append(val)
        result: dict[str, object] = {}
        cursor = ScriptedCursor()
        sampler = PenSampler(position_source=cursor)

        def done(report: object) -> None:
            result["report"] = report
            root.quit()

        with mock.patch.object(lt, "COUNTDOWN_S", 1):
            win = lt._TestWindow(root, "aim", 0.1, DISPLAY, done, root.quit, lambda k, **p: k,
                                 random.Random(4), sampler, activation)
            cursor.window = win
            ignored = []

            def click_on_target() -> None:     # a pen tap / mouse click right on the center
                if win.aim is not None and win.aim.current is not None:
                    before = len(win.aim.hits)
                    x, y = win.aim.current
                    win._on_click(SimpleNamespace(x_root=x, y_root=y))
                    ignored.append(len(win.aim.hits) - before)

            root.after(50, win._begin_countdown)
            root.after(1300, click_on_target)
            root.after(30000, root.quit)
            root.mainloop()
        root.destroy()
        return result.get("report"), errors, sum(ignored)

    def test_touch_and_center_runs_complete_without_errors(self) -> None:
        for mode in ("touch", "center"):
            with self.subTest(mode=mode):
                report, errors, click_hits = self.run_window(mode)
                self.assertEqual(errors, [])
                self.assertIsNotNone(report, "window did not finish")
                self.assertEqual(report.sample_count, lt.AIM_TARGET_COUNT)
                self.assertEqual(click_hits, 0)
                self.assertGreater(report.extras["trajectory_count"], 0)
                self.assertIsNotNone(report.aim_time_ms_mean)
                self.assertIsNotNone(report.aim_error_mean_mm)

    def test_escape_cancels(self) -> None:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        cancelled = []
        win = lt._TestWindow(root, "aim", 0.1, DISPLAY, lambda r: None, lambda: cancelled.append(1),
                             lambda k, **p: k, random.Random(1), PenSampler(position_source=lambda: (0.0, 0.0)))
        root.after(100, win.cancel)
        root.after(300, root.quit)
        root.mainloop()
        root.destroy()
        self.assertEqual(cancelled, [1])


if __name__ == "__main__":
    unittest.main()
