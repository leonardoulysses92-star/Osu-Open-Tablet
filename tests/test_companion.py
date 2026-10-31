"""Tests for osu_tablet_assistant.gui.companion (core logic + one headless window)."""
from __future__ import annotations

import copy
import json
import re
import sys
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace

from osu_tablet_assistant.appconfig import AppConfig
from osu_tablet_assistant.engine import adapt
from osu_tablet_assistant.engine.adapt import PolicyMode
from osu_tablet_assistant.engine.recommend import RADIAL_TABLET
from osu_tablet_assistant.gui import companion as comp
from osu_tablet_assistant.gui import hotkeys as hk
from osu_tablet_assistant.gui import i18n
from osu_tablet_assistant.models import (ApplyResult, Candidate, DisplaySpec, PenSample, ProfileChanges, Reason,
                                         Rect, TabletSpec)
from osu_tablet_assistant.otd import livetune as lt
from osu_tablet_assistant.otd import settings_io

PACKAGE = Path(comp.__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "settings_sample.json"
CTL472 = TabletSpec("Wacom CTL-472", 152.0, 95.0, 133.0)
DISPLAY = DisplaySpec(1920, 1080, 960, 540, 144.0)


def user_settings() -> dict:
    """Synthetic copy of the user's setup (CTL-472, 71.66x41.44 mm at 180 deg, Radial Follow)."""
    base = json.loads(FIXTURE.read_text(encoding="utf-8"))
    prof = copy.deepcopy(base["Profiles"][0])
    prof["Tablet"] = "Wacom CTL-472"
    prof["AbsoluteModeSettings"]["Tablet"] = {"Width": 71.66, "Height": 41.44, "X": 80.17, "Y": 26.72,
                                              "Rotation": 180.0}
    props = {"OuterRadius": 0.6, "InnerRadius": 0.04, "SmoothingCoefficient": 0.88, "SoftKneeScale": 1.0,
             "SmoothingLeakCoefficient": 0.0}
    prof["Filters"] = [{"Path": RADIAL_TABLET, "Settings": [{"Property": k, "Value": v} for k, v in props.items()],
                        "Enable": True}]
    base["Profiles"] = [prof]
    return base


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Sender:
    def __init__(self, fail_on: set[int] | None = None) -> None:
        self.calls: list[dict] = []
        self.fail_on = fail_on or set()

    def __call__(self, settings: dict) -> ApplyResult:
        self.calls.append(copy.deepcopy(settings))
        if len(self.calls) in self.fail_on:
            return ApplyResult(False, "none", "livetune.error.rpc", "boom")
        return ApplyResult(True, "rpc", "otd.apply.ok_rpc")


def make_core(sender=None, tablet=CTL472, **kw):
    clock = Clock()
    sender = sender or Sender()
    writes: list[dict] = []
    tuner = lt.LiveTuner(user_settings(), "Wacom CTL-472", sender, tablet=tablet, clock=clock,
                         config_dir=Path("C:/fake"), backup=lambda d: Path("C:/fake/Backup/b.json"),
                         writer=lambda d, s: writes.append(s), **kw)
    core = comp.CompanionCore(tuner, display=DISPLAY)
    return core, sender, clock, writes


def radial(settings: dict) -> dict:
    return lt._values(settings["Profiles"][0]["Filters"][0])


class CoreScenarioTest(unittest.TestCase):
    def test_user_session_converges_stays_in_bounds_and_saves_only_on_keep(self) -> None:
        core, sender, clock, writes = make_core()
        seq = ["smoother", "smoother", "responsive", "smoother", "responsive", "smoother", "responsive"]
        sounds = []
        for action in seq:
            fb = core.handle(action)
            self.assertTrue(fb.ok, action)
            sounds.append(fb.sound)
            clock.now += 0.5
            core.pump()
        self.assertTrue(core.status().converged)
        self.assertIn("converged", sounds)
        self.assertEqual(writes, [])
        catalog = core.catalog[RADIAL_TABLET]
        for sent in sender.calls:
            for name, v in radial(sent).items():
                p = catalog.param(name)
                self.assertTrue(p.min <= v <= p.max)
        fb = core.handle("keep")
        self.assertTrue(fb.ok)
        self.assertEqual(fb.sound, "keep")
        self.assertEqual(len(writes), 1)
        self.assertFalse(core.status().dirty)
        self.assertEqual(core.status().accepted, 1)

    def test_undo_everything_restores_identical_settings(self) -> None:
        core, sender, clock, _ = make_core()
        for action in ["smoother", "precise", "area_bigger", "responsive", "looser", "area_smaller"]:
            self.assertTrue(core.handle(action).ok, action)
            clock.now += 0.2
        while core.tuner.can_undo:
            self.assertEqual(core.handle("undo").sound, "undo")
            clock.now += 0.2
        self.assertEqual(core.tuner.settings, user_settings())
        self.assertEqual(sender.calls[-1], user_settings())
        self.assertEqual(core.state, adapt.AdaptState())
        self.assertEqual(core.handle("undo").sound, "error")

    def test_rate_limited_send_flushes_in_pump(self) -> None:
        core, sender, clock, _ = make_core()
        core.handle("smoother")
        core.handle("smoother")
        self.assertEqual(len(sender.calls), 1)
        self.assertIsNone(core.pump())
        clock.now += 0.2
        self.assertIsNone(core.pump())
        self.assertEqual(len(sender.calls), 2)
        self.assertEqual(sender.calls[-1], core.tuner.settings)

    def test_failed_send_reverts_settings_and_adapt_state(self) -> None:
        core, sender, clock, _ = make_core(sender=Sender(fail_on={2}))
        core.handle("smoother")
        state_after_first = core.state
        settings_after_first = core.tuner.settings
        fb = core.handle("smoother")      # queued
        self.assertTrue(fb.ok)
        clock.now += 1
        fb = core.pump()
        self.assertIsNotNone(fb)
        self.assertEqual(fb.sound, "error")
        self.assertEqual(core.tuner.settings, settings_after_first)
        self.assertIs(core.state, state_after_first)

    def test_area_bigger_needs_a_known_tablet(self) -> None:
        core, sender, _, _ = make_core(tablet=None)
        fb = core.handle("area_bigger")
        self.assertFalse(fb.ok)
        self.assertEqual(fb.reasons[0].key, "companion.area.need_tablet")
        self.assertEqual(sender.calls, [])
        self.assertTrue(core.handle("area_smaller").ok)

    def test_area_change_updates_the_passive_scale(self) -> None:
        core, _, _, _ = make_core()
        before = core.monitor.mm_per_px
        core.handle("area_bigger")
        self.assertGreater(core.monitor.mm_per_px, before)
        self.assertAlmostEqual(before, 71.66 / 1920)

    def test_poll_good_accepts_and_bad_answers_move(self) -> None:
        core, sender, clock, _ = make_core()
        fbs = core.poll("good", "steady")
        self.assertEqual(fbs[0].reasons[0].key, "companion.poll.accepted")
        self.assertEqual(sender.calls, [])
        fbs = core.poll("good", "steady")
        self.assertIn("companion.poll.stop_here", [r.key for r in fbs[0].reasons])
        fbs = core.poll("shaky", "jittery")
        self.assertEqual([f.action for f in fbs], ["smoother", "precise"])

    def test_policy_switch(self) -> None:
        core, _, _, _ = make_core()
        core.set_policy("allow_new")
        self.assertIs(core.policy.mode, PolicyMode.ALLOW_NEW)
        with self.assertRaises(ValueError):
            core.set_policy("anything")

    def test_revert_and_status(self) -> None:
        core, _, clock, _ = make_core()
        core.handle("smoother")
        st = core.status()
        self.assertEqual(st.changes, 1)
        self.assertTrue(st.dirty)
        self.assertEqual(st.area, (71.66, 41.44, 180.0))
        self.assertIn("OuterRadius", [v.param for v in st.values])
        clock.now += 1
        self.assertTrue(core.revert_all().ok)
        self.assertEqual(core.tuner.settings, user_settings())

    def test_dry_run_sender_cannot_keep(self) -> None:
        tuner = lt.LiveTuner(user_settings(), "Wacom CTL-472", lt.dry_run_sender, can_keep=False)
        core = comp.CompanionCore(tuner)
        self.assertTrue(core.handle("smoother").ok)
        self.assertEqual(core.handle("keep").reasons[0].key, "livetune.keep.disabled")

    def test_overlay_lines_and_say(self) -> None:
        core, _, _, _ = make_core()
        fb = core.handle("smoother")
        lines = comp.overlay_lines(fb, core.status(), i18n.t)
        self.assertEqual(len(lines), 3)
        text = comp.say(Reason("adapt.limit.more", {"axis": "motion"}), lambda k, **p: f"{k}|{p}")
        self.assertIn("adapt.axis.motion", text)


class StepLevelCoreTest(unittest.TestCase):
    def test_default_is_normal_and_step_cycles(self) -> None:
        core, sender, _, _ = make_core()
        self.assertEqual(core.step_level, "normal")
        self.assertEqual(core.status().step_level, "normal")
        levels = [core.handle("step") for _ in range(3)]
        self.assertEqual([fb.reasons[0].params["level"] for fb in levels], ["fine", "very_fine", "normal"])
        self.assertTrue(all(fb.ok and fb.sound == "step" for fb in levels))
        self.assertEqual(sender.calls, [])                 # choosing a step size sends nothing
        self.assertEqual(core.tuner.change_count, 0)
        with self.assertRaises(ValueError):
            core.set_step_level("ultra")

    def test_fine_moves_are_smaller_and_undo_keeps_the_level(self) -> None:
        sizes = {}
        for level in adapt.STEP_LEVELS:
            core, _, _, _ = make_core()
            core.set_step_level(level)
            self.assertTrue(core.handle("smoother").ok)
            sizes[level] = radial(core.tuner.settings)["OuterRadius"] - 0.6
        self.assertGreater(sizes["normal"], sizes["fine"])
        self.assertGreater(sizes["fine"], sizes["very_fine"])
        self.assertGreater(sizes["very_fine"], 0)
        core.handle("undo")
        self.assertEqual(core.step_level, "very_fine")
        self.assertEqual(core.tuner.settings, user_settings())

    def test_fine_area_step_is_one_percent(self) -> None:
        core, _, _, _ = make_core()
        core.set_step_level("fine")
        fb = core.handle("area_bigger")
        self.assertEqual(fb.reasons[0].params["pct"], "1")
        self.assertIn("1%", comp.action_label("area_bigger", "fine", i18n.t))
        self.assertIn("0.5%", comp.action_label("area_smaller", "very_fine", i18n.t))

    def test_fine_session_converges(self) -> None:
        core, _, clock, _ = make_core()
        core.set_step_level("very_fine")
        sounds = []
        for action in ["smoother", "smoother", "responsive", "smoother", "responsive", "smoother", "responsive"]:
            sounds.append(core.handle(action).sound)
            clock.now += 0.5
            core.pump()
        self.assertTrue(core.status().converged)
        self.assertIn("converged", sounds)

    def test_overlay_names_the_step_and_last_change_line(self) -> None:
        core, _, _, _ = make_core()
        core.set_step_level("fine")
        fb = core.handle("smoother")
        lines = comp.overlay_lines(fb, core.status(), i18n.t)
        self.assertEqual(len(lines), 3)
        self.assertIn(i18n.t("companion.step.fine"), lines[-1])
        change = comp.last_change(fb, i18n.t)
        self.assertIn("->", change)
        self.assertIn("OuterRadius", change)
        self.assertIsNone(comp.last_change(core.handle("step"), i18n.t))


class SessionAdapterTest(unittest.TestCase):
    def test_uses_live_candidate_settings_and_public_attributes(self) -> None:
        cand = Candidate("c1", "l", "s", ProfileChanges(tablet_area=Rect(70, 40, 80, 27, 180)))
        made = []
        services = SimpleNamespace(apply_changes=settings_io.apply_changes,
                                   make_controller=lambda i, c: made.append((i, c)) or "controller")
        session = SimpleNamespace(settings=user_settings(), profile=SimpleNamespace(tablet_name="Wacom CTL-472"),
                                  applied=None, live_candidate_id="c1", candidate=lambda cid: cand,
                                  services=services, tablet=CTL472, display=DISPLAY, install_dir=None,
                                  config_dir=Path("C:/cfg"), filter_paths={RADIAL_TABLET}, dry_run=False)
        args = comp.companion_args_from_session(session)
        self.assertEqual(args["tablet_name"], "Wacom CTL-472")
        self.assertEqual(args["settings"]["Profiles"][0]["AbsoluteModeSettings"]["Tablet"]["Width"], 70.0)
        self.assertEqual(args["controller"], "controller")
        self.assertEqual(args["available_filters"], {RADIAL_TABLET})
        session.live_candidate_id = None
        self.assertEqual(comp.companion_args_from_session(session)["settings"], user_settings())


class LocaleKeysTest(unittest.TestCase):
    def test_every_key_used_by_companion_modules_exists(self) -> None:
        en, es = set(i18n.load_catalog("en")), set(i18n.load_catalog("es"))
        files = [PACKAGE / "gui" / "companion.py", PACKAGE / "engine" / "adapt.py",
                 PACKAGE / "otd" / "livetune.py", PACKAGE / "otd" / "controller.py"]
        pattern = re.compile(r"""["']((?:companion|adapt|livetune|passive)\.[a-z_.]+[a-z_])["']""")
        used = {m for f in files for m in pattern.findall(f.read_text(encoding="utf-8"))}
        self.assertGreater(len(used), 40)
        dynamic = {f"companion.action.{a}" for a in hk.ACTIONS + ("revert", "poll", "send")}
        dynamic |= {f"companion.poll.motion.{o}" for o in adapt.POLL_MOTION}
        dynamic |= {f"companion.poll.rest.{o}" for o in adapt.POLL_REST}
        dynamic |= {f"companion.policy.{m.value}" for m in PolicyMode}
        dynamic |= {f"companion.flavor.{f}" for f in ("stable", "lazer", "unknown")}
        dynamic |= {f"adapt.axis.{a}" for a in adapt.AXES}
        dynamic |= {f"companion.step.{lv}" for lv in adapt.STEP_LEVELS}
        dynamic |= {f"companion.step.hint.{lv}" for lv in adapt.STEP_LEVELS}
        needed = {k for k in used | dynamic if not k.endswith(".")}
        self.assertEqual(sorted(needed - en), [])
        self.assertEqual(sorted(needed - es), [])


class FakeBackend:
    def __init__(self) -> None:
        self.woken = False

    def run(self, hotkeys, on_registered, on_hotkey, stop) -> None:
        on_registered({h: 0 for h in hotkeys})
        stop.wait(5)

    def wake(self) -> None:
        self.woken = True


class FakeSampler:
    threaded = True

    def __init__(self) -> None:
        self.started = self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def drain(self):
        return [PenSample(i * 0.002, 500 + i % 2, 300) for i in range(50)]


def hide(win) -> None:
    """Tests never show a window on the desktop: fully transparent and far off-screen."""
    try:
        win.root.attributes("-alpha", 0.0)
    except tk.TclError:
        pass
    win.root.geometry("+-20000+-20000")


def make_window(root, width: int | None = None, **kw):
    kw.setdefault("sender", Sender())
    kw.setdefault("sampler", FakeSampler())
    kw.setdefault("watcher", lt.OsuWatcher(lambda: []))
    kw.setdefault("hotkey_backend", FakeBackend())
    kw.setdefault("beeper", hk.Beeper(beep=lambda f, d: None, threaded=False))
    kw.setdefault("overlay", False)
    win = comp.open_companion(root, settings=user_settings(), tablet_name="Wacom CTL-472", tablet=CTL472,
                              display=DISPLAY, **kw)
    hide(win)
    if width is not None:
        win.root.geometry(f"{width}x700+-20000+-20000")
    for _ in range(3):
        win.root.update()
    return win


def layout_problems(win) -> list:
    """Mapped widgets past the window's right edge, or text widgets narrower than their content."""
    top = win.root
    right = top.winfo_rootx() + top.winfo_width()
    bad, stack = [], list(top.winfo_children())
    while stack:
        w = stack.pop()
        stack.extend(w.winfo_children())
        if not w.winfo_ismapped():
            continue
        if w.winfo_rootx() + w.winfo_width() > right + 1:
            bad.append(("past right edge", str(w), w.winfo_rootx() + w.winfo_width() - right))
        if w.winfo_class() in ("TButton", "TRadiobutton", "TCheckbutton", "TLabel", "Label", "TEntry") \
                and w.winfo_reqwidth() > w.winfo_width() + 1:
            bad.append(("squeezed", str(w), w.winfo_reqwidth(), w.winfo_width()))
    return bad


ALT = hk.TK_ALT_WIN if sys.platform == "win32" else hk.TK_MOD1


def key(keysym: str, state: int = hk.TK_CONTROL | ALT, keycode: int = 0):
    return SimpleNamespace(keysym=keysym, state=state, keycode=keycode)


class HeadlessWindowTest(unittest.TestCase):
    def test_window_builds_runs_and_closes(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"no display: {exc}")
        root.withdraw()
        sender, sampler, backend = Sender(), FakeSampler(), FakeBackend()
        tones: list = []
        closed: list = []
        try:
            win = comp.open_companion(
                root, settings=user_settings(), tablet_name="Wacom CTL-472", tablet=CTL472, display=DISPLAY,
                config_dir=None, sender=sender, sampler=sampler,
                watcher=lt.OsuWatcher(lambda: [lt.ProcessEntry(9, "osu!.exe", r"C:\osu!\osu!.exe")]),
                hotkey_backend=backend, beeper=hk.Beeper(beep=lambda f, d: tones.append(f), threaded=False),
                available_filters={RADIAL_TABLET}, on_close=lambda: closed.append(True))
            hide(win)
            root.update()
            self.assertTrue(sampler.started)
            self.assertTrue(win.hotkeys.running)
            win.do("smoother")
            win._actions.put("precise")          # as if a global hotkey fired
            win._tick()
            root.update()
            self.assertGreaterEqual(len(sender.calls), 1)
            self.assertTrue(tones)
            self.assertIn("osu!", win.var_game.get())
            win.var_motion.set("good")
            win.var_rest.set("steady")
            win.send_poll()
            win.toggle_bindings()
            root.update()
            win.revert_all()
            root.update()
            win.destroy()
            self.assertTrue(sampler.stopped)
            self.assertTrue(backend.woken)
            self.assertEqual(closed, [True])
        finally:
            root.destroy()

    def test_requires_settings(self) -> None:
        with self.assertRaises(ValueError):
            comp.CompanionWindow(None)


class CompactPanelWindowTest(unittest.TestCase):
    """The compact shortcut panel, the step selector and the shortcut editor (hidden windows only)."""

    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"no display: {exc}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.config = AppConfig()
        self.saves: list[int] = []

    def window(self, width: int | None = None, **kw):
        kw.setdefault("config", self.config)
        kw.setdefault("save_config", lambda: self.saves.append(1))
        win = make_window(self.root, width, **kw)
        self.addCleanup(win.destroy)
        return win

    def test_layout_fits_narrow_and_wide_windows(self) -> None:
        for lang in ("es", "en"):
            i18n.set_language(lang)
            self.addCleanup(i18n.set_language, "es")
            for width, cols, groups in ((640, 2, 1), (900, 4, 2)):
                with self.subTest(lang=lang, width=width):
                    win = self.window(width)
                    self.assertEqual(win._panel_cols, cols)
                    self.assertEqual(layout_problems(win), [])
                    win.set_step("very_fine")
                    win.do("smoother")
                    win.toggle_bindings()
                    for _ in range(3):
                        win.root.update()
                    self.assertTrue(win.keys_frame.winfo_ismapped())
                    self.assertEqual(win._editor_groups, groups)
                    self.assertEqual(layout_problems(win), [])
                    win.destroy()

    def test_panel_shows_what_each_key_does_and_the_step(self) -> None:
        win = self.window()
        smoother = win.intent_buttons["smoother"].cget("text")
        self.assertIn(i18n.t("companion.action.smoother"), smoother)
        self.assertIn("Ctrl+Alt+Up", smoother)
        self.assertIn("Ctrl+Alt+PageUp", win.intent_buttons["area_bigger"].cget("text"))
        self.assertIn(i18n.t("companion.step.normal"), win.var_step.get())
        self.assertIn("Ctrl+Alt+P", win.var_step.get())
        self.assertEqual(win.var_change.get(), i18n.t("companion.last.none"))
        win.intent_buttons["smoother"].invoke()            # a click is the same as the hotkey
        self.assertIn("->", win.var_change.get())
        self.assertEqual(win.tuner.change_count, 1)

    def test_step_selector_and_hotkey_are_saved(self) -> None:
        win = self.window()
        win.set_step("very_fine")
        self.assertEqual((win.core.step_level, self.config.companion_step), ("very_fine", "very_fine"))
        self.assertIn("0.5%", win.intent_buttons["area_bigger"].cget("text"))
        win._actions.put("step")                            # as if Ctrl+Alt+P fired while playing
        win._tick()
        self.assertEqual((win.core.step_level, win.var_step_level.get()), ("normal", "normal"))
        self.assertEqual(self.config.companion_step, "normal")
        self.assertTrue(self.saves)
        self.assertIn("2%", win.intent_buttons["area_bigger"].cget("text"))

    def test_saved_choices_are_used_at_start(self) -> None:
        self.config.companion_step = "fine"
        self.config.companion_hotkeys = {**hk.DEFAULT_BINDINGS, "smoother": "ctrl+shift+up"}
        win = self.window()
        self.assertEqual(win.core.step_level, "fine")
        self.assertEqual(win.hotkeys.bindings["smoother"], "ctrl+shift+up")
        self.assertIn("Ctrl+Shift+Up", win.intent_buttons["smoother"].cget("text"))

    def test_detect_takes_a_free_combination_and_rejects_a_taken_one(self) -> None:
        win = self.window()
        self.assertTrue(win.hotkeys.running)
        win.start_detect("keep")
        self.assertTrue(win.keys_open)
        self.assertFalse(win.hotkeys.running)              # paused: our own combos must reach the window
        self.assertEqual(win.feed_key(key("Up")), "break")
        self.assertIn(i18n.t("companion.action.smoother"), win.keys_status.cget("text"))
        self.assertEqual(win._detecting, "keep")
        win.feed_key(key("j", state=0))
        self.assertEqual(str(win.keys_status.cget("text")), i18n.t("companion.hotkeys.need_modifier"))
        win.feed_key(key("Control_L"))                     # a modifier alone: keep waiting
        self.assertEqual(win._detecting, "keep")
        win.feed_key(key("j"))
        self.assertIsNone(win._detecting)
        self.assertTrue(win.hotkeys.running)
        self.assertEqual(win.hotkeys.bindings["keep"], "ctrl+alt+j")
        self.assertEqual(win.binding_vars["keep"].get(), "Ctrl+Alt+J")
        self.assertEqual(self.config.companion_hotkeys["keep"], "ctrl+alt+j")
        self.assertIn("Ctrl+Alt+J", win.intent_buttons["keep"].cget("text"))
        win.start_detect("undo")
        win.feed_key(key("Escape", state=0))
        self.assertIsNone(win._detecting)
        self.assertTrue(win.hotkeys.running)
        self.assertEqual(win.hotkeys.bindings["undo"], "ctrl+alt+z")

    def test_typed_bindings_invalid_duplicate_and_reset(self) -> None:
        win = self.window()
        win.toggle_bindings()
        win.binding_vars["undo"].set("ctrl+banana")
        self.assertFalse(win.apply_bindings())
        self.assertEqual(win.hotkeys.bindings["undo"], "ctrl+alt+z")
        self.assertTrue(win.hotkeys.running)
        win.binding_vars["undo"].set("Ctrl+Alt+Up")          # same as "smoother"
        self.assertFalse(win.apply_bindings())
        self.assertEqual(self.saves, [])
        win.binding_vars["undo"].set("ctrl+shift+z")
        win.binding_vars["step"].set("")                     # no key for the step size
        self.assertTrue(win.apply_bindings())
        self.assertEqual(self.config.companion_hotkeys["undo"], "ctrl+shift+z")
        self.assertEqual(self.config.companion_hotkeys["step"], "")
        self.assertNotIn("Ctrl+Alt+P", win.var_step.get())
        win.reset_bindings()
        self.assertEqual(self.config.companion_hotkeys, hk.DEFAULT_BINDINGS)
        self.assertEqual(win.hotkeys.bindings, hk.DEFAULT_BINDINGS)
        win.toggle_bindings()
        self.assertFalse(win.keys_open)


if __name__ == "__main__":
    unittest.main()
