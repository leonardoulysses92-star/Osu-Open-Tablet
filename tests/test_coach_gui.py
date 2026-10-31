"""The Play Coach window and the TabletMap canvas build and run through all phases (skipped without a display)."""
from __future__ import annotations

import tkinter as tk
import unittest

from osu_tablet_assistant.engine.playtypes import HIT, TOO_FAR, CellStat, ObjectResult
from osu_tablet_assistant.gui import hotkeys as hk
from osu_tablet_assistant.gui import i18n
from osu_tablet_assistant.models import PenSample
from test_coach import CTL472, DISPLAY, USER_AREA, Env


def _display_available() -> bool:
    try:
        root = tk.Tk()
    except tk.TclError:
        return False
    root.destroy()
    return True


@unittest.skipUnless(_display_available(), "no display")
class CoachWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        from osu_tablet_assistant.gui import coach
        self.coach = coach
        self.addCleanup(i18n.set_language, "es")
        self.env = Env(self)

    def window(self):
        win = self.coach.CoachWindow(None, core=self.env.core, enable_hotkeys=False,
                                     beeper=hk.Beeper(enabled=False))
        self.addCleanup(win.destroy)
        win.root.update()
        return win

    def texts(self, widget: tk.Misc) -> str:
        out = []
        for w in widget.winfo_children():
            try:
                out.append(str(w.cget("text")))
            except tk.TclError:
                pass
            out.append(self.texts(w))
        return " ".join(out)

    def test_full_flow_in_both_languages(self) -> None:
        for lang in i18n.SUPPORTED:
            i18n.set_language(lang)
            env = self.env = Env(self)
            win = self.window()
            self.assertEqual(env.core.phase, "prepare")
            self.assertIn(i18n.t("play.prepare.start"), self.texts(win.body))
            win.start_observing()
            self.assertEqual(env.core.phase, "observe")
            win._tick_observe()
            win.root.update()
            win.stop_observing()
            win.root.update()
            self.assertEqual(env.core.phase, "report")
            body = self.texts(win.body)
            self.assertIn(i18n.t("play.report.apply"), body)
            self.assertIn(i18n.t("play.report.skip"), body)
            win.apply()
            win.root.update()
            self.assertEqual(env.core.phase, "prepare")
            self.assertEqual(len(env.sent), 1)
            win.destroy()

    def test_no_untranslated_keys_are_shown(self) -> None:
        win = self.window()
        win.start_observing()
        win.stop_observing()
        win.root.update()
        shown = self.texts(win.body)
        self.assertNotIn("play.", shown)

    def test_start_is_disabled_until_a_map_is_known(self) -> None:
        self.env = Env(self, title="osu!")
        win = self.window()
        win.start_observing()
        self.assertEqual(self.env.core.phase, "prepare")
        self.assertIn(i18n.t("play.prepare.pick_map"), self.texts(win.body))

    def test_hotkey_toggles_observation(self) -> None:
        win = self.window()
        win._actions.put("coach_toggle")
        win._handle_actions()
        self.assertEqual(self.env.core.phase, "observe")
        win._actions.put("coach_toggle")
        win._handle_actions()
        self.assertEqual(self.env.core.phase, "report")

    def test_minimap_option_creates_and_removes_the_window(self) -> None:
        win = self.window()
        win._mini_var.set(True)
        win._toggle_mini()
        self.assertIsNotNone(win.mini)
        win._mini_var.set(False)
        win._toggle_mini()
        self.assertIsNone(win.mini)

    def test_close_asks_before_dropping_unsaved_changes(self) -> None:
        from unittest import mock
        win = self.window()
        win.start_observing()
        win.stop_observing()
        win.apply()
        with mock.patch.object(self.coach.messagebox, "askyesnocancel", return_value=None) as ask:
            win.close()
        self.assertTrue(ask.called)
        self.assertFalse(win._closed)
        with mock.patch.object(self.coach.messagebox, "askyesnocancel", return_value=False):
            win.close()
        self.assertTrue(win._closed)
        self.assertEqual(self.env.tuner.profile().tablet_area, USER_AREA)      # live changes dropped


@unittest.skipUnless(_display_available(), "no display")
class TabletMapTests(unittest.TestCase):
    def test_draws_every_layer_without_errors(self) -> None:
        from osu_tablet_assistant.gui.tabletmap import TabletMap
        root = tk.Tk()
        self.addCleanup(root.destroy)
        from osu_tablet_assistant.gui.widgets import apply_theme
        apply_theme(root)
        for compact in (False, True):
            canvas = TabletMap(root, compact=compact)
            canvas.pack(fill="both", expand=True)
            root.update()
            canvas.set_setup(CTL472, USER_AREA, DISPLAY)
            canvas.set_cells([CellStat(r, c, 8, 2, 0.5, -0.4, 1.2, -0.6) for r in range(3) for c in range(4)])
            canvas.add_results([ObjectResult(i, 0.0, HIT if i % 3 else TOO_FAR, err_mm=1.0, cursor_u=0.3 + i / 100,
                                             cursor_v=0.5) for i in range(20)], 3.0)
            canvas.update_pen([PenSample(i * 0.01, 900 + i * 5, 500) for i in range(30)])
            root.update()
            self.assertGreater(len(canvas.find_all()), 20)
            canvas.clear_play()
            root.update()


if __name__ == "__main__":
    unittest.main()
