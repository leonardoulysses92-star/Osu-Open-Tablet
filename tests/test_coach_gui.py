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

    def test_replay_buttons_and_the_check_line(self) -> None:
        from pathlib import Path
        from unittest import mock
        from test_coach import ReplayRoundTests
        helper = ReplayRoundTests()
        win = self.window()
        self.assertIn(i18n.t("play.prepare.use_replay"), self.texts(win.body))
        path = helper.osr(self.env)
        with mock.patch.object(self.coach.filedialog, "askopenfilename", return_value=str(path)):
            win.pick_replay("round")
        win.root.update()
        self.assertEqual(self.env.core.phase, "report")
        self.assertIn(i18n.t("play.replay.used"), self.texts(win.body))
        self.assertNotIn(i18n.t("play.report.verify_replay"), self.texts(win.body))
        win.play_again()
        win.start_observing()
        win.stop_observing()
        self.assertIn(i18n.t("play.report.verify_replay"), self.texts(win.body))
        with mock.patch.object(self.coach.filedialog, "askopenfilename", return_value=str(path)):
            win.pick_replay("verify")
        win.root.update()
        self.assertIn("%", self.texts(win.body))
        self.assertNotIn("play.replay", self.texts(win.body))
        with mock.patch.object(self.coach.filedialog, "askopenfilename", return_value=""):
            win.pick_replay("round")                      # cancelling the dialog changes nothing

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


def key(keysym: str, keycode: int = 0):
    from types import SimpleNamespace
    return SimpleNamespace(keysym=keysym, keycode=keycode, num="??", widget=None)


def click(num: int):
    from types import SimpleNamespace
    return SimpleNamespace(keysym="??", keycode=0, num=num, widget=None)


class FakeSession:
    """Just what the coach uses of a wizard session for the click keys: the config and its save."""

    def __init__(self, fail: bool = False) -> None:
        from osu_tablet_assistant.appconfig import AppConfig
        self.app_config, self.saves, self.fail = AppConfig(), 0, fail

    def save_app_config(self):
        self.saves += 1
        if self.fail:
            raise OSError("disk full")


@unittest.skipUnless(_display_available(), "no display")
class ClickKeysCardTests(unittest.TestCase):
    def setUp(self) -> None:
        from osu_tablet_assistant.gui import coach
        self.coach = coach
        self.addCleanup(i18n.set_language, "es")
        i18n.set_language("es")
        self.env = Env(self)

    def window(self, session=None):
        win = self.coach.CoachWindow(None, session, core=self.env.core, enable_hotkeys=False,
                                     beeper=hk.Beeper(enabled=False))
        self.addCleanup(win.destroy)
        win.root.update()
        return win

    def chips(self, win) -> list[str]:
        from osu_tablet_assistant.gui.widgets import Chip
        return [w.cget("text") for w in win.keys_card.chips.winfo_children() if isinstance(w, Chip)]

    def test_detect_mode_adds_chips_from_this_window_only_and_esc_ends_it(self) -> None:
        session = FakeSession()
        win = self.window(session)
        self.assertEqual(self.chips(win), ["Z", "X"])
        card = win.keys_card
        card.start_listening()
        self.assertTrue(card.listening)
        self.assertTrue(win.root.bind("<KeyPress>"))                 # a binding on the coach window, nothing global
        self.assertIn(i18n.t("play.keys.listening"), card.status.cget("text"))
        self.assertEqual(card.feed_event(key("a", 0x41)), "break")
        card.feed_event(key("semicolon"))
        card.feed_event(click(3))                                    # right button -> mouse 2
        card.feed_event(click(1))                                    # left button is not taken (pen switch)
        card.feed_event(key("Shift_L"))                              # a lone modifier is ignored
        self.assertEqual(self.chips(win), ["Z", "X", "A", ";", "Mouse 2"])
        self.assertEqual(self.env.core.keys, ("z", "x", "a", "semicolon", "mouse2"))
        self.assertIs(win.keys_card, card)                           # no rebuild while listening
        card.feed_event(key("Escape"))
        win.root.update()
        self.assertFalse(card.listening)
        self.assertEqual(win.root.bind("<KeyPress>"), "")             # listening really stopped
        self.assertEqual(self.chips(win), ["Z", "X", "A", ";", "Mouse 2"])  # the rebuilt card keeps them
        self.assertEqual(session.app_config.click_keys, ["z", "x", "a", "semicolon", "mouse2"])
        self.assertGreaterEqual(session.saves, 1)
        self.assertIn("A, ;, Mouse 2", self.texts(win.body))         # the honest text names the chosen keys

    def test_closing_the_card_gives_back_its_height_and_the_window_size(self) -> None:
        win = self.window(FakeSession())
        card, top = win.keys_card, win.root
        closed_h = card.winfo_reqheight()
        window_h = top.winfo_height()
        card.toggle_open()
        top.update()
        self.assertGreater(card.winfo_reqheight(), closed_h)         # the open card needs room
        card.toggle_open()
        top.update()
        self.assertEqual(card.winfo_reqheight(), closed_h)           # ...and gives it back: no empty box left
        self.assertLessEqual(abs(top.winfo_height() - window_h), 2)  # the window shrinks back as well

    def test_reset_remove_pen_tip_and_typing(self) -> None:
        session = FakeSession(fail=True)                             # saving fails: the coach carries on
        win = self.window(session)
        win.keys_card.set_choice(win.keys_card.choice.add("c"))
        win.root.update()
        self.assertEqual(self.env.core.keys, ("z", "x", "c"))
        win.keys_card.remove("z")
        win.root.update()
        self.assertEqual(self.env.core.keys, ("x", "c"))
        win.keys_card.reset()
        win.root.update()
        self.assertEqual(self.env.core.keys, ("z", "x"))
        card = win.keys_card
        card.pen_var.set(True)
        card._pen_toggled()
        win.root.update()
        self.assertEqual(self.env.core.keys, ("z", "x", "mouse1"))   # the pen tip counts only now
        self.assertIn("Mouse 1", self.chips(win))
        self.assertIn(i18n.t("play.prep.pen_tip"), self.texts(win.body))
        card = win.keys_card
        card.manual_var.set("z, banana")
        card.apply_manual()
        self.assertIn("banana", card.status.cget("text"))
        self.assertEqual(self.env.core.keys, ("z", "x", "mouse1"))   # nothing changes while a word is unknown
        card.manual_var.set("k, mouse4")
        card.apply_manual()
        win.root.update()
        self.assertEqual(self.env.core.keys, ("k", "mouse4", "mouse1"))
        self.assertTrue(session.app_config.pen_clicks)
        win.keys_card.pen_var.set(False)
        win.keys_card._pen_toggled()
        self.assertEqual(self.env.core.keys, ("k", "mouse4"))

    def test_the_card_is_one_compact_line_until_opened_and_stays_open_across_refreshes(self) -> None:
        from osu_tablet_assistant.gui.widgets import Chip
        win = self.window()
        card = win.keys_card
        self.assertFalse(card.is_open)
        self.assertFalse(card.details.winfo_ismapped())
        self.assertEqual([w.cget("text") for w in card.summary.winfo_children() if isinstance(w, Chip)], ["Z", "X"])
        card.toggle_open()
        win.root.update()
        self.assertTrue(card.details.winfo_ismapped())
        card.remove("x")                                             # a change refreshes the whole screen ...
        win.root.update()
        self.assertIsNot(win.keys_card, card)
        self.assertTrue(win.keys_card.is_open)                       # ... and the card is still open
        self.assertNotIn("play.", self.texts(win.body))              # every text is translated
        for lang in i18n.SUPPORTED:
            i18n.set_language(lang)
            win.show_phase("prepare")
            self.assertNotIn("play.", self.texts(win.body))

    def test_analysis_detail_choice(self) -> None:
        session = FakeSession()
        win = self.window(session)
        card = win.keys_card
        self.assertEqual(card.detail_var.get(), "fine")
        card.detail_var.set("very_fine")
        card.on_detail("very_fine")
        self.assertEqual((self.env.core.grid, session.app_config.analysis_detail), ((9, 12), "very_fine"))

    def test_no_click_hint_offers_the_key_card(self) -> None:
        from osu_tablet_assistant.gui import playwatch
        from test_coach import FakeRecorder

        class Silent(FakeRecorder):
            def live(self):
                return playwatch.LiveView((1.0, 1.0), (), 0, None, 20.0, 1e6, 20.0)

        self.env.core._recorder_factory = lambda area: Silent(area, 1)   # type: ignore[assignment]
        win = self.window()
        win.start_observing()
        win._tick_observe()
        win.root.update()
        self.assertEqual(win._observe_var.get(), "No he visto ningún clic con Z, X: ¿usas otros botones?")
        self.assertIsNotNone(win._hint_button)
        win._hint_button.invoke()
        win.root.update()
        self.assertEqual(self.env.core.phase, "prepare")
        self.assertTrue(win.keys_card.listening)
        self.assertEqual(self.env.core.state.rounds, ())              # what was recorded is not a round

    def texts(self, widget: tk.Misc) -> str:
        out = []
        for w in widget.winfo_children():
            try:
                out.append(str(w.cget("text")))
            except tk.TclError:
                pass
            out.append(self.texts(w))
        return " ".join(out)


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

    def test_fine_grids_draw_every_cell_and_arrows_only_where_there_is_data(self) -> None:
        from osu_tablet_assistant.gui.tabletmap import TabletMap
        from osu_tablet_assistant.gui.widgets import apply_theme
        root = tk.Tk()
        self.addCleanup(root.destroy)
        apply_theme(root)
        canvas = TabletMap(root)
        canvas.pack(fill="both", expand=True)
        root.update()
        canvas.set_setup(CTL472, USER_AREA, DISPLAY)
        cells = [CellStat(r, c, 4, 1, 0.0, 0.0, 1.0, 0.0, 1.0, -1.0, 0.3, (r + c) % 2 == 0)
                 for r in range(9) for c in range(12)]
        canvas.set_cells(cells)
        self.assertEqual(canvas._grid, (9, 12))                       # read from the cells
        root.update()
        rectangles = [i for i in canvas.find_all() if canvas.type(i) == "rectangle"]
        arrows = [i for i in canvas.find_all() if canvas.type(i) == "line" and canvas.itemcget(i, "arrow") == "last"]
        self.assertGreaterEqual(len(rectangles), 108)
        self.assertEqual(len(arrows), 54)                             # only the cells with enough data
        before = set(canvas.find_all())
        canvas.update_pen([PenSample(i * 0.01, 900 + i * 5, 500) for i in range(30)])
        after = set(canvas.find_all())
        self.assertTrue(before <= after)                              # a pen update keeps the cells
        self.assertTrue(canvas.find_withtag("trail"))


if __name__ == "__main__":
    unittest.main()
