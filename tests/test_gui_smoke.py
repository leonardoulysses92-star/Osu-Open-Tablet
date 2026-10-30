"""The real window builds every page without errors (hidden; skipped without a display).

Uses the integration harness: a temp copy of the fixture settings, a fake OTD
controller and a fake pen, so nothing outside the temp folder is touched.
"""
from __future__ import annotations

import time
import tkinter as tk
import unittest
from tkinter import ttk

from osu_tablet_assistant.gui import i18n, wizard
from osu_tablet_assistant.gui.app import App
from osu_tablet_assistant.gui.session import CANDIDATE_KINDS, STEP_ORDER, Step
from test_integration import Harness


def _display_available() -> bool:
    try:
        root = tk.Tk()
    except tk.TclError:
        return False
    root.destroy()
    return True


class _Preset:
    def __init__(self, pid: str, label: str) -> None:
        self.id, self.label, self.summary = pid, label, "summary"


@unittest.skipUnless(_display_available(), "no display")
class GuiSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(i18n.set_language, "es")
        self.harness = Harness(self, dry_run=False)
        self.session = self.harness.session

    def app(self) -> App:
        app = App(self.session)
        app.root.withdraw()
        self.addCleanup(app.root.destroy)
        app.root.update_idletasks()
        return app

    def measured(self) -> None:
        s = self.harness.drive_to_testing(self)
        for cid in ("balanced", "keep-current"):
            cand = s.candidate(cid)
            s.apply_candidate(cand, confirmed=True)
            for kind in CANDIDATE_KINDS:
                s.run_measurement(None, kind, cand)

    def test_every_step_renders_in_both_languages(self) -> None:
        self.measured()
        s = self.session
        s.advance()
        self.assertIs(s.step, Step.RESULT)
        app = self.app()
        steps = list(STEP_ORDER)
        for lang in i18n.SUPPORTED:
            for step in steps:
                s.step = step
                i18n.set_language(lang)
                app._lang_var.set(lang)
                app.wizard.show_current()
                app.root.update()
                self.assertIsInstance(app.wizard.page, wizard.PAGES[step])
        self.assertIsNotNone(app.wizard.page.view)
        app._lang_var.set("en" if i18n.current_language() == "es" else "es")
        app._on_language()   # rebuilds the current page in the other language
        app.root.update()

    def test_testing_table_shows_words_repeat_button_and_no_score_column(self) -> None:
        s = self.harness.drive_to_testing(self)
        cand = s.candidate("balanced")
        s.apply_candidate(cand, confirmed=True)
        for kind in CANDIDATE_KINDS[:1]:
            s.run_measurement(None, kind, cand)          # partial: incomplete
        app = self.app()
        page = app.wizard.page
        self.assertIsInstance(page, wizard.TestingPage)
        self.assertEqual(tuple(page._tree["columns"]), ("label", "status", "result"))
        page._tree.selection_set("balanced")
        page._show_detail()
        self.assertEqual(page._btn_test.cget("text"), i18n.t("gui.testing.test"))
        self.assertIn("incompleta", page._tree.set("balanced", "result"))
        for kind in CANDIDATE_KINDS:
            s.run_measurement(None, kind, cand)          # complete, stillness measured twice
        page._refresh_rows()
        page._show_detail()
        self.assertEqual(page._btn_test.cget("text"), i18n.t("gui.testing.repeat"))
        grades = {i18n.t(f"gui.testing.grade.{g}") for g in ("excellent", "good", "normal", "weak")}
        self.assertIn(page._tree.set("balanced", "result"), grades)
        self.assertTrue(page._tree.set("balanced", "status").startswith("Probada"))
        s.set_rating("balanced", 4)
        page._refresh_rows()
        self.assertTrue(page._tree.set("balanced", "result").endswith("★4"))
        self.assertEqual(page._tree.set("balanced", "label")[:1], "★")   # the only complete option wins
        for lang in i18n.SUPPORTED:
            i18n.set_language(lang)
            page._refresh_rows()
            page._show_detail()

    def test_result_sections_extension_point(self) -> None:
        self.measured()
        self.session.advance()
        built: list[str] = []

        def hook(view: wizard.ResultsView) -> None:
            view.add_section("Community presets", lambda body: built.append(
                ttk.Label(body, text="cmp").winfo_class()))

        wizard.RESULT_SECTIONS.append(hook)
        self.addCleanup(wizard.RESULT_SECTIONS.remove, hook)
        app = self.app()
        view = app.wizard.page.view
        self.assertEqual(built, ["TLabel"])
        builtin = len(view.builtin_sections)        # community comparison (+ suggestions if allowed)
        self.assertGreaterEqual(builtin, 1)
        first = view.add_section("First", lambda body: None, index=0)
        self.assertIs(view.sections.pack_slaves()[0], first)
        self.assertEqual(len(view.sections.pack_slaves()), builtin + 2)

    def test_preset_slot_hidden_without_presets_and_navigable_with_them(self) -> None:
        s = self.session
        self.harness.drive_to_testing(self)
        while s.step is not Step.QUESTIONS:
            s.back()
        app = self.app()
        self.assertNotIn("gui.step.preset", app.wizard.visible_steps())
        s.starting_presets = [_Preset("p1", "Preset one")]
        app.wizard.show_current()
        keys = app.wizard.visible_steps()
        self.assertEqual(keys.index("gui.step.preset"), keys.index("gui.step.questions") + 1)
        app.wizard.on_next()                       # QUESTIONS -> slot, session unchanged
        self.assertIsInstance(app.wizard.page, wizard.PresetPage)
        self.assertIs(s.step, Step.QUESTIONS)
        # Leaving QUESTIONS refreshed the list with the real community presets usable here.
        self.assertTrue(all(p.id.startswith("community:") for p in s.starting_presets))
        pid = s.starting_presets[0].id
        app.wizard.page._var.set(pid)
        app.wizard.on_back()                       # slot -> QUESTIONS
        self.assertIsNone(app.wizard.slot)
        app.wizard.on_next()
        app.wizard.on_next()                       # slot -> BASELINE (worker thread)
        deadline = time.monotonic() + 10
        while not isinstance(app.wizard.page, wizard.BaselinePage) and time.monotonic() < deadline:
            app.root.update()
            time.sleep(0.01)
        self.assertIs(s.step, Step.BASELINE)
        self.assertIsInstance(app.wizard.page, wizard.BaselinePage)
        self.assertEqual(s.starting_preset_id, pid)
        app.wizard.on_back()                     # BASELINE -> back onto the slot
        self.assertIsInstance(app.wizard.page, wizard.PresetPage)
        s.ensure_candidates()                    # the chosen starting preset is measured first
        self.assertEqual(s.candidates[0].id, pid)

    def test_app_wires_the_companion_and_keep_marks_settings_written(self) -> None:
        from unittest import mock
        self.addCleanup(setattr, wizard, "COMPANION_LAUNCHER", None)
        wizard.COMPANION_LAUNCHER = None
        self.measured()
        self.session.advance()
        app = self.app()
        self.assertEqual(wizard.COMPANION_LAUNCHER, app._open_companion)
        self.assertIsNotNone(app.wizard.page.view.companion_button)
        view = app.wizard.page.view
        with mock.patch("osu_tablet_assistant.gui.companion.open_companion") as opened:
            app._open_companion(view)
        args, kwargs = opened.call_args
        self.assertIs(args[1], self.session)
        self.assertFalse(self.session._wrote_settings)
        kwargs["on_keep"]({})                     # companion "Keep" replaced settings.json
        self.assertTrue(self.session._wrote_settings)
        app._release_companion(mock.Mock(widget=app.root))   # what closing the window triggers
        self.assertIsNone(wizard.COMPANION_LAUNCHER)

    def test_companion_extension_point_and_dialogs(self) -> None:
        self.measured()
        self.session.advance()
        called: list[object] = []
        self.addCleanup(setattr, wizard, "COMPANION_LAUNCHER", None)
        wizard.COMPANION_LAUNCHER = called.append
        app = self.app()
        view = app.wizard.page.view
        self.assertIsNotNone(view.companion_button)
        view.companion_button.invoke()
        self.assertEqual(called, [view])
        self.assertTrue(view.builtin_sections)                     # community comparison card
        dialog = wizard.SaveDialog(app.wizard.page)
        self.assertTrue(dialog.name.get())
        dialog.destroy()
        settings = app.wizard.open_settings()
        self.assertIs(app.wizard.open_settings(), settings)
        settings.url.set("http://insecure.example/")
        self.assertFalse(settings.apply())
        settings.url.set("https://example.org/data/")
        self.assertTrue(settings.apply())
        self.assertEqual(self.session.app_config.update_url, "https://example.org/data/")
        settings.destroy()
        wizard.COMPANION_LAUNCHER = None
        app.wizard.show_current()
        self.assertIsNone(app.wizard.page.view.companion_button)

    def test_error_banner_shows_and_clears(self) -> None:
        app = self.app()
        app.wizard.show_error(RuntimeError("boom"))
        self.assertTrue(app.wizard._error.visible)
        app.wizard.clear_error()
        self.assertFalse(app.wizard._error.visible)


if __name__ == "__main__":
    unittest.main()
