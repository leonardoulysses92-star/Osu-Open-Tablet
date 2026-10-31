"""osu_tablet_assistant.keys: key names, Windows virtual-key codes, Tk events and the player's choice."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from osu_tablet_assistant import keys as k
from osu_tablet_assistant.gui import playwatch


def key_event(keysym: str, keycode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(keysym=keysym, keycode=keycode, num="??")


def button_event(num: int) -> SimpleNamespace:
    return SimpleNamespace(keysym="??", keycode=0, num=num)


class NameTests(unittest.TestCase):
    def test_defaults_are_z_and_x_only(self) -> None:
        self.assertEqual(k.DEFAULT_CLICK_KEYS, ("z", "x"))
        self.assertEqual(playwatch.DEFAULT_CLICK_KEYS, ("z", "x"))
        self.assertNotIn("mouse1", k.DEFAULT_CLICK_KEYS)

    def test_virtual_key_codes(self) -> None:
        expected = {"z": 0x5A, "a": 0x41, "5": 0x35, "space": 0x20, "enter": 0x0D, "tab": 0x09, "shift": 0x10,
                    "ctrl": 0x11, "alt": 0x12, "capslock": 0x14, "f1": 0x70, "f12": 0x7B, "left": 0x25,
                    "up": 0x26, "right": 0x27, "down": 0x28, "semicolon": 0xBA, "comma": 0xBC, "period": 0xBE,
                    "slash": 0xBF, "quote": 0xDE, "lbracket": 0xDB, "rbracket": 0xDD, "minus": 0xBD,
                    "equal": 0xBB, "backslash": 0xDC, "num0": 0x60, "num9": 0x69, "mouse1": 0x01,
                    "mouse2": 0x02, "mouse3": 0x04, "mouse4": 0x05, "mouse5": 0x06}
        for name, code in expected.items():
            self.assertEqual(k.key_code(name), code, name)
            self.assertEqual(playwatch.key_code(name), code, name)      # still importable from playwatch
        self.assertIsNone(k.key_code("f13"))
        self.assertIsNone(k.key_code("f13-nope"))
        self.assertIsNone(k.key_code(""))

    def test_aliases_and_canonical_names(self) -> None:
        self.assertEqual(k.canonical("Z"), "z")
        self.assertEqual(k.canonical(";"), "semicolon")
        self.assertEqual(k.canonical("Mouse 4"), "mouse4")
        self.assertEqual(k.canonical("numpad5"), "num5")
        self.assertEqual(k.canonical("Caps Lock"), "capslock")
        self.assertEqual(k.canonical("return"), "enter")
        self.assertIsNone(k.canonical("nonsense"))
        self.assertIsNone(k.canonical(5))

    def test_normalize(self) -> None:
        self.assertEqual(k.normalize_keys(["Z", "x", "z", "bogus", "Mouse 4"]), ("z", "x", "mouse4"))
        self.assertEqual(k.normalize_keys([]), ("z", "x"))
        self.assertEqual(k.normalize_keys(["bogus"]), ("z", "x"))
        self.assertEqual(k.normalize_keys([], default=()), ())
        self.assertEqual(len(k.normalize_keys(list("abcdefghij"))), k.MAX_KEYS)

    def test_effective_keys_and_the_pen_tip(self) -> None:
        self.assertEqual(k.effective_keys(["z", "x"]), ("z", "x"))
        self.assertEqual(k.effective_keys(["z", "x"], pen_clicks=True), ("z", "x", "mouse1"))
        self.assertEqual(k.effective_keys(["z", "mouse1"]), ("z",))          # mouse1 only through the switch
        self.assertEqual(k.effective_keys([], pen_clicks=True), ("mouse1",))
        self.assertEqual(k.effective_keys([]), ("z", "x"))

    def test_display_names(self) -> None:
        self.assertEqual(k.display_name("z"), "Z")
        self.assertEqual(k.display_name("mouse1"), "Mouse 1")
        self.assertEqual(k.display_name("semicolon"), ";")
        self.assertEqual(k.display_name("num3"), "Num 3")
        self.assertEqual(k.display_name("f5"), "F5")
        self.assertEqual(k.display_list(("z", "x")), "Z, X")

    def test_typed_by_hand(self) -> None:
        self.assertEqual(k.parse_text("z, x, mouse4"), (("z", "x", "mouse4"), ()))
        self.assertEqual(k.parse_text("a  s ;"), (("a", "s", "semicolon"), ()))
        self.assertEqual(k.parse_text("z, banana"), (("z",), ("banana",)))
        self.assertEqual(k.parse_text(""), ((), ()))


class TkEventTests(unittest.TestCase):
    def test_keyboard_keysyms(self) -> None:
        cases = {"z": "z", "Z": "z", "7": "7", "space": "space", "Return": "enter", "semicolon": ";",
                 "colon": ";", "comma": ",", "period": ".", "slash": "/", "apostrophe": "'", "bracketleft": "[",
                 "bracketright": "]", "minus": "-", "equal": "=", "backslash": "\\", "Left": "left",
                 "Down": "down", "F5": "f5", "F12": "f12", "KP_4": "num4", "Caps_Lock": "capslock", "Tab": "tab"}
        for keysym, name in cases.items():
            self.assertEqual(k.name_from_tk_event(key_event(keysym), vk_fallback=False), k.canonical(name), keysym)

    def test_escape_and_lone_modifiers_are_ignored(self) -> None:
        for keysym in ("Escape", "Shift_L", "Shift_R", "Control_L", "Alt_R", "Win_L", "Num_Lock"):
            self.assertIsNone(k.name_from_tk_event(key_event(keysym, 0x10), vk_fallback=True), keysym)

    def test_mouse_buttons(self) -> None:
        self.assertEqual(k.name_from_tk_event(button_event(1)), "mouse1")
        self.assertEqual(k.name_from_tk_event(button_event(3)), "mouse2")       # Tk right = Windows button 2
        self.assertEqual(k.name_from_tk_event(button_event(2)), "mouse3")       # Tk middle
        self.assertEqual(k.name_from_tk_event(button_event(4)), "mouse4")
        self.assertEqual(k.name_from_tk_event(button_event(9)), "mouse5")
        self.assertIsNone(k.name_from_tk_event(button_event(7)))

    def test_windows_keycode_fallback(self) -> None:
        odd = key_event("dead_acute", 0xDE)          # a layout whose keysym is not in the table
        self.assertIsNone(k.name_from_tk_event(odd, vk_fallback=False))
        self.assertEqual(k.name_from_tk_event(odd, vk_fallback=True), "quote")
        self.assertIsNone(k.name_from_tk_event(key_event("??", 0x10), vk_fallback=True))   # lone shift


class KeyChoiceTests(unittest.TestCase):
    def test_make_validates(self) -> None:
        self.assertEqual(k.KeyChoice.make(["Z", "bogus"]), k.KeyChoice(("z",), False))
        self.assertEqual(k.KeyChoice.make([]), k.KeyChoice(("z", "x"), False))
        self.assertEqual(k.KeyChoice.make(["z", "mouse1"]), k.KeyChoice(("z",), True))
        self.assertEqual(k.KeyChoice.make([], pen_clicks=True), k.KeyChoice((), True))

    def test_add_remove_reset_and_pen(self) -> None:
        choice = k.KeyChoice()
        self.assertEqual(choice.effective, ("z", "x"))
        choice = choice.add("mouse4").add("z").add("bogus").add("mouse1")
        self.assertEqual(choice.keys, ("z", "x", "mouse4"))
        choice = choice.remove("z").remove("x")
        self.assertEqual(choice.keys, ("mouse4",))
        self.assertEqual(choice.remove("mouse4"), choice)                      # never zero ways to click
        pen = choice.with_pen(True)
        self.assertEqual(pen.effective, ("mouse4", "mouse1"))
        self.assertEqual(pen.remove("mouse4").effective, ("mouse1",))
        self.assertEqual(pen.reset(), k.KeyChoice())

    def test_listening_feed(self) -> None:
        choice = k.KeyChoice()
        choice, outcome = choice.feed("a")
        self.assertEqual((choice.keys, outcome), (("z", "x", "a"), "added"))
        self.assertEqual(choice.feed("a")[1], "known")
        self.assertEqual(choice.feed("mouse1"), (choice, "left_click"))
        self.assertEqual(choice.feed(None), (choice, "ignored"))
        for name in "bcd":
            choice, _ = choice.feed(name)
        self.assertEqual(choice.feed("e")[1], "full")
        self.assertEqual(len(choice.keys), k.MAX_KEYS)


if __name__ == "__main__":
    unittest.main()
