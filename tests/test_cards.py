"""The coach's two foldable cards on their own: click buttons (keycard) and smoothing (smoothcard)."""
from __future__ import annotations

import tkinter as tk
import unittest
from types import SimpleNamespace

from osu_tablet_assistant.gui import i18n
from osu_tablet_assistant.gui.coachcore import SmoothOption
from osu_tablet_assistant.gui.keycard import ClickKeysCard
from osu_tablet_assistant.gui.smoothcard import SmoothingCard, summary_text
from osu_tablet_assistant.keys import KeyChoice
from support import display_available

HAWKU = "TabletDriverFilters.Hawku.Smoothing"
RADIAL = "RadialFollow.RadialFollowSmoothingTabletSpace"


def option(path: str = HAWKU, name: str = "Hawku", **kw) -> SmoothOption:
    values = dict(path=path, name=name, text_id="hawku", installed=True, on=False, was_on=False, trying=False,
                  decided=False, delay_ms=4.0)
    values.update(kw)
    return SmoothOption(**values)


class SummaryTextTests(unittest.TestCase):
    def test_what_the_chip_says(self) -> None:
        t = i18n.t
        self.addCleanup(i18n.set_language, "es")
        i18n.set_language("en")
        self.assertEqual(summary_text([option(trying=True, on=True)], t), ("Trying: Hawku", "accent"))
        self.assertEqual(summary_text([option(), option(RADIAL, "Radial")], t), ("Switched off: 2", "neutral"))
        self.assertEqual(summary_text([option(installed=False)], t), ("None installed", "neutral"))


@unittest.skipUnless(display_available(), "no display")
class CardTests(unittest.TestCase):
    def setUp(self) -> None:
        from osu_tablet_assistant.gui.widgets import apply_theme
        self.addCleanup(i18n.set_language, "es")
        i18n.set_language("es")
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        apply_theme(self.root)

    def test_keycard_changes_reach_the_owner(self) -> None:
        changes: list[KeyChoice] = []
        state: dict = {}
        card = ClickKeysCard(self.root, KeyChoice(), i18n.t, changes.append, state=state)
        card.pack()
        self.assertFalse(card.is_open)
        card.toggle_open()
        self.assertTrue(state["open"])
        card.manual_var.set("a, s")
        card.apply_manual()
        self.assertEqual(changes[-1].keys, ("a", "s"))
        card.manual_var.set("a, nonsense")
        card.apply_manual()                                 # an unknown word changes nothing
        self.assertEqual(card.choice.keys, ("a", "s"))
        card.start_listening()
        self.assertEqual(card.feed_event(SimpleNamespace(keysym="d", keycode=0x44, num=0, widget=None)), "break")
        self.assertEqual(card.choice.keys, ("a", "s", "d"))
        card.feed_event(SimpleNamespace(keysym="Escape", num=0, widget=None))
        self.assertFalse(card.listening)
        card.reset()
        self.assertEqual(card.choice, KeyChoice())

    def test_smoothing_card_rows_and_switches(self) -> None:
        tried, stopped = [], []
        state = {"open": True}
        options = [option(), option(RADIAL, "Radial", text_id="radial_tablet", installed=False)]
        card = SmoothingCard(self.root, options, i18n.t, tried.append, stopped.append, state=state)
        card.pack()
        self.root.update_idletasks()
        self.assertEqual(set(card.switches), {HAWKU})       # not installed: shown, but nothing to switch
        card.vars[HAWKU].set(True)
        card._switched(HAWKU)
        card.vars[HAWKU].set(False)
        card._switched(HAWKU)
        self.assertEqual((tried, stopped), ([HAWKU], [HAWKU]))
        card.toggle_open()
        self.assertFalse(state["open"])


if __name__ == "__main__":
    unittest.main()
