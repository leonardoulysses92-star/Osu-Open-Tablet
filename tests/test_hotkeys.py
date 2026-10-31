"""Tests for osu_tablet_assistant.gui.hotkeys. No real hotkey is registered and no sound is played."""
from __future__ import annotations

import queue
import threading
import time
import unittest

from osu_tablet_assistant.gui import hotkeys as hk


class FakeBackend:
    """Registers unless the combination is in ``taken``; ``press`` injects WM_HOTKEY-like ids."""

    def __init__(self, taken: set[str] = frozenset()) -> None:
        self.taken = set(taken)
        self.presses: queue.Queue[int | None] = queue.Queue()
        self.registered: dict[int, hk.Hotkey] = {}
        self.unregistered: list[int] = []
        self.thread_names: list[str] = []

    def run(self, hotkeys, on_registered, on_hotkey, stop) -> None:
        self.thread_names.append(threading.current_thread().name)
        status = {}
        for hid, key in hotkeys.items():
            status[hid] = hk.ERROR_HOTKEY_ALREADY_REGISTERED if key.text in self.taken else 0
            if status[hid] == 0:
                self.registered[hid] = key
        on_registered(status)
        while not stop.is_set():
            try:
                hid = self.presses.get(timeout=0.5)
            except queue.Empty:
                continue
            if hid is None:
                break
            on_hotkey(hid)
        self.unregistered = list(self.registered)
        self.registered = {}

    def wake(self) -> None:
        self.presses.put(None)

    def press(self, text: str) -> None:
        hid = next(h for h, k in self.registered.items() if k.text == text)
        self.presses.put(hid)


def wait_for(predicate, timeout: float = 2.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class ParseTest(unittest.TestCase):
    def test_parse_variants(self) -> None:
        a = hk.parse_hotkey("Ctrl+Alt+Up")
        self.assertEqual((a.modifiers, a.vk, a.text), (hk.MOD_CONTROL | hk.MOD_ALT, 0x26, "ctrl+alt+up"))
        self.assertEqual(hk.parse_hotkey("alt + ctrl + k").text, "ctrl+alt+k")
        self.assertEqual(hk.parse_hotkey("shift+f9").vk, 0x78)
        self.assertEqual(hk.parse_hotkey("ctrl+num5").vk, 0x65)

    def test_parse_errors(self) -> None:
        for bad in ("up", "ctrl+alt", "ctrl+up+down", "ctrl+banana", ""):
            with self.subTest(bad=bad), self.assertRaises(hk.HotkeyError):
                hk.parse_hotkey(bad)

    def test_bindings_report_bad_text_and_duplicates(self) -> None:
        good, errors = hk.parse_bindings({"smoother": "ctrl+alt+up", "responsive": "alt+ctrl+up",
                                          "undo": "nonsense", "keep": ""})
        self.assertEqual(set(good), {"smoother"})
        self.assertEqual(set(errors), {"responsive", "undo"})

    def test_defaults_are_valid_and_cover_every_action(self) -> None:
        good, errors = hk.parse_bindings(hk.DEFAULT_BINDINGS)
        self.assertEqual(errors, {})
        self.assertEqual(set(good), set(hk.ACTIONS))


class Ev:
    """A stand-in for a Tk key event."""

    def __init__(self, keysym: str, state: int = 0, keycode: int = 0) -> None:
        self.keysym, self.state, self.keycode = keysym, state, keycode


CTRL_ALT_WIN = hk.TK_CONTROL | hk.TK_ALT_WIN


class EditorHelpersTest(unittest.TestCase):
    def test_step_action_has_a_free_default(self) -> None:
        self.assertIn("step", hk.ACTIONS)
        self.assertEqual(hk.DEFAULT_BINDINGS["step"], "ctrl+alt+p")
        self.assertIn("step", hk.PATTERNS)

    def test_display(self) -> None:
        self.assertEqual(hk.display_hotkey("ctrl+alt+pageup"), "Ctrl+Alt+PageUp")
        self.assertEqual(hk.display_hotkey("alt+ctrl+k"), "Ctrl+Alt+K")
        self.assertEqual(hk.display_hotkey("shift+f9"), "Shift+F9")
        self.assertEqual(hk.display_hotkey("nonsense"), "nonsense")

    def test_conflict_with(self) -> None:
        b = dict(hk.DEFAULT_BINDINGS)
        self.assertEqual(hk.conflict_with(b, "keep", "Alt+Ctrl+Up"), "smoother")
        self.assertIsNone(hk.conflict_with(b, "smoother", "ctrl+alt+up"))       # its own combination
        self.assertIsNone(hk.conflict_with(b, "keep", "ctrl+shift+k"))
        self.assertIsNone(hk.conflict_with({**b, "undo": "garbage"}, "keep", "ctrl+alt+j"))

    def test_clean_bindings_from_saved_data(self) -> None:
        self.assertEqual(hk.clean_bindings(None), hk.DEFAULT_BINDINGS)
        self.assertEqual(hk.clean_bindings("ctrl+alt+up"), hk.DEFAULT_BINDINGS)
        old_file = {a: v for a, v in hk.DEFAULT_BINDINGS.items() if a != "step"}   # saved before "step" existed
        self.assertEqual(hk.clean_bindings(old_file), hk.DEFAULT_BINDINGS)
        got = hk.clean_bindings({"smoother": "Alt+Ctrl+P", "keep": "bad+key", "undo": "ctrl+alt+up",
                                 "looser": "", "unknown": "ctrl+alt+q", "precise": 5, "area_bigger": "x" * 99})
        self.assertEqual(set(got), set(hk.ACTIONS))
        self.assertEqual(got["smoother"], "ctrl+alt+p")
        self.assertEqual(got["undo"], "ctrl+alt+up")
        self.assertEqual(got["keep"], "ctrl+alt+k")             # invalid -> default
        self.assertEqual(got["looser"], "")                     # explicitly no hotkey
        self.assertEqual(got["precise"], "ctrl+alt+left")
        self.assertEqual(got["step"], "")                       # its default is taken by "smoother": no key
        good, errors = hk.parse_bindings(got)
        self.assertEqual(errors, {})                            # never two actions on one combination
        dup = hk.clean_bindings({"smoother": "ctrl+alt+j", "responsive": "alt+ctrl+j"})
        self.assertEqual(dup["smoother"], "ctrl+alt+j")
        self.assertEqual(dup["responsive"], "ctrl+alt+down")

    def test_combo_from_tk_event_windows(self) -> None:
        f = lambda e: hk.combo_from_tk_event(e, windows=True)   # noqa: E731
        self.assertEqual(f(Ev("Up", CTRL_ALT_WIN)), ("ctrl+alt+up", "ok"))
        self.assertEqual(f(Ev("Prior", CTRL_ALT_WIN)), ("ctrl+alt+pageup", "ok"))
        self.assertEqual(f(Ev("P", CTRL_ALT_WIN | hk.TK_SHIFT)), ("ctrl+alt+shift+p", "ok"))
        self.assertEqual(f(Ev("KP_5", hk.TK_CONTROL)), ("ctrl+num5", "ok"))
        self.assertEqual(f(Ev("exclam", hk.TK_CONTROL | hk.TK_SHIFT, 0x31)), ("ctrl+shift+1", "ok"))  # by keycode
        self.assertEqual(f(Ev("Control_L", hk.TK_CONTROL)), (None, "wait"))
        self.assertEqual(f(Ev("Escape")), (None, "escape"))
        self.assertEqual(f(Ev("k")), (None, "no_modifier"))
        self.assertEqual(f(Ev("k", 0x8)), (None, "no_modifier"))          # 0x8 is Num Lock on Windows
        self.assertEqual(f(Ev("odiaeresis", hk.TK_CONTROL, 0xFFFF)), (None, "unknown"))

    def test_combo_from_tk_event_x11_alt(self) -> None:
        self.assertEqual(hk.combo_from_tk_event(Ev("Up", hk.TK_CONTROL | hk.TK_MOD1), windows=False),
                         ("ctrl+alt+up", "ok"))


class ManagerTest(unittest.TestCase):
    def test_set_bindings_does_not_register_until_start(self) -> None:
        backend = FakeBackend()
        mgr = hk.HotkeyManager(hk.DEFAULT_BINDINGS, lambda a: None, backend=backend)
        mgr.start()
        mgr.set_bindings({**hk.DEFAULT_BINDINGS, "step": ""})
        self.assertFalse(mgr.running)
        self.assertNotIn("step", mgr.hotkeys)
        self.assertTrue(mgr.start()["smoother"])
        mgr.stop()

    def test_registers_on_its_own_thread_and_delivers_actions(self) -> None:
        got: list[str] = []
        backend = FakeBackend()
        mgr = hk.HotkeyManager(hk.DEFAULT_BINDINGS, got.append, backend=backend)
        self.assertEqual(mgr.start(), {a: True for a in hk.ACTIONS})
        self.assertEqual(mgr.conflicts, [])
        self.assertEqual(backend.thread_names, ["hotkeys"])
        backend.press("ctrl+alt+up")
        backend.press("ctrl+alt+z")
        self.assertTrue(wait_for(lambda: len(got) == 2))
        self.assertEqual(got, ["smoother", "undo"])
        mgr.stop()
        self.assertFalse(mgr.running)
        self.assertEqual(len(backend.unregistered), len(hk.ACTIONS))
        mgr.stop()   # idempotent

    def test_conflicts_are_reported(self) -> None:
        backend = FakeBackend(taken={"ctrl+alt+up", "ctrl+alt+down"})
        mgr = hk.HotkeyManager(hk.DEFAULT_BINDINGS, lambda a: None, backend=backend)
        status = mgr.start()
        self.assertFalse(status["smoother"])
        self.assertTrue(status["keep"])
        self.assertEqual(sorted(mgr.conflicts), ["responsive", "smoother"])
        mgr.stop()

    def test_rebind_resolves_a_conflict(self) -> None:
        backend = FakeBackend(taken={"ctrl+alt+up"})
        got: list[str] = []
        mgr = hk.HotkeyManager(hk.DEFAULT_BINDINGS, got.append, backend=backend)
        mgr.start()
        status = mgr.rebind({**hk.DEFAULT_BINDINGS, "smoother": "ctrl+shift+up"})
        self.assertTrue(status["smoother"])
        self.assertEqual(mgr.conflicts, [])
        backend.press("ctrl+shift+up")
        self.assertTrue(wait_for(lambda: got == ["smoother"]))
        mgr.stop()

    def test_callback_errors_do_not_kill_the_thread(self) -> None:
        backend = FakeBackend()
        calls = []

        def boom(action: str) -> None:
            calls.append(action)
            raise RuntimeError("bug")
        mgr = hk.HotkeyManager(hk.DEFAULT_BINDINGS, boom, backend=backend)
        mgr.start()
        backend.press("ctrl+alt+k")
        backend.press("ctrl+alt+k")
        self.assertTrue(wait_for(lambda: len(calls) == 2))
        self.assertTrue(mgr.running)
        mgr.stop()

    def test_without_backend_nothing_happens(self) -> None:
        mgr = hk.HotkeyManager(hk.DEFAULT_BINDINGS, lambda a: None, backend=None, use_default_backend=False)
        self.assertFalse(mgr.available)
        self.assertEqual(mgr.start(), {})
        mgr.stop()


class BeeperTest(unittest.TestCase):
    def test_patterns_differ_per_action(self) -> None:
        for action in hk.ACTIONS:
            self.assertIn(action, hk.PATTERNS)
        self.assertEqual(len({hk.PATTERNS[a] for a in hk.PATTERNS}), len(hk.PATTERNS))

    def test_plays_synchronously_when_not_threaded(self) -> None:
        tones = []
        b = hk.Beeper(beep=lambda f, d: tones.append((f, d)), threaded=False)
        b.play("keep")
        b.play("nope")
        self.assertEqual(tones, list(hk.PATTERNS["keep"]))
        b.enabled = False
        b.play("undo")
        self.assertEqual(len(tones), 3)

    def test_threaded_worker_and_errors(self) -> None:
        tones = []

        def beep(f, d):
            tones.append(f)
            if f == 200:
                raise RuntimeError("no sound device")
        b = hk.Beeper(beep=beep)
        b.play("error")
        b.play("undo")
        self.assertTrue(wait_for(lambda: len(tones) == 2))
        b.close()

    def test_unavailable_sound_disables(self) -> None:
        b = hk.Beeper(beep=None)
        if b._beep is None:
            self.assertFalse(b.enabled)


if __name__ == "__main__":
    unittest.main()
