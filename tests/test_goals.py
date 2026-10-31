"""Welcome paths (guided / live tuning / Play Coach), launchers, Simple vs Advanced mode."""
from __future__ import annotations

import tkinter as tk
import unittest
from unittest import mock

from osu_tablet_assistant import appconfig
from osu_tablet_assistant.gui import i18n, wizard
from osu_tablet_assistant.gui.app import App
from osu_tablet_assistant.gui.session import GOALS, STEP_ORDER, SessionError, Step
from test_integration import Harness


def _display_available() -> bool:
    try:
        root = tk.Tk()
    except tk.TclError:
        return False
    root.destroy()
    return True


class GoalSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = Harness(self, dry_run=False).session

    def go_to_display(self) -> None:
        s = self.session
        s.accepted_disclaimer = True
        s.advance()
        s.detect()
        s.advance()
        s.select_choice(next(c for c in s.tablet_choices() if c.profile is not None))
        s.advance()
        s.detect_display()
        self.assertIs(s.step, Step.DISPLAY)

    def test_default_is_guided_with_every_step(self) -> None:
        self.assertEqual(self.session.goal, "guided")
        self.assertEqual(self.session.goal_steps(), list(STEP_ORDER))
        self.assertFalse(self.session.at_goal_launch)

    def test_live_and_coach_stop_after_the_display(self) -> None:
        for goal in ("live", "coach"):
            self.session.set_goal(goal)
            self.assertEqual([s.value for s in self.session.goal_steps()],
                             ["welcome", "detect", "tablet", "display"])

    def test_unknown_goal_is_refused_with_a_translatable_error(self) -> None:
        with self.assertRaises(SessionError) as ctx:
            self.session.set_goal("teleport")
        self.assertEqual(ctx.exception.key, "core.error.goal")
        for lang in i18n.SUPPORTED:
            i18n.set_language(lang)
            self.addCleanup(i18n.set_language, "es")
            self.assertNotEqual(i18n.t(ctx.exception.key), ctx.exception.key)

    def test_the_last_step_launches_instead_of_advancing(self) -> None:
        self.go_to_display()
        self.assertFalse(self.session.at_goal_launch)
        self.session.set_goal("coach")
        self.assertTrue(self.session.at_goal_launch)
        with self.assertRaises(SessionError):
            self.session.advance()
        self.assertIs(self.session.step, Step.DISPLAY)
        self.session.set_goal("guided")
        self.assertIs(self.session.advance(), Step.QUESTIONS)

    def test_advanced_flag_follows_the_saved_config(self) -> None:
        self.assertFalse(self.session.advanced)
        self.session.app_config.advanced_ui = True
        self.assertTrue(self.session.advanced)
        self.assertTrue(appconfig.from_dict({"advanced_ui": True}).advanced_ui)
        self.assertFalse(appconfig.from_dict({"advanced_ui": "yes"}).advanced_ui)
        self.assertIn("advanced_ui", appconfig.AppConfig().__dict__)


@unittest.skipUnless(_display_available(), "no display")
class GoalGuiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(i18n.set_language, "es")
        self.addCleanup(setattr, wizard, "COMPANION_LAUNCHER", None)
        self.addCleanup(setattr, wizard, "COACH_LAUNCHER", None)
        wizard.COMPANION_LAUNCHER = wizard.COACH_LAUNCHER = None
        self.harness = Harness(self, dry_run=False)
        self.session = self.harness.session

    def app(self) -> App:
        app = App(self.session)
        app.root.withdraw()
        self.addCleanup(app.root.destroy)
        app.root.update_idletasks()
        return app

    def texts(self, widget: tk.Misc) -> str:
        out = []
        for w in widget.winfo_children():
            try:
                out.append(str(w.cget("text")))
            except tk.TclError:
                pass
            out.append(self.texts(w))
        return " ".join(out)

    def to_display(self, app: App) -> None:
        s = self.session
        s.accepted_disclaimer = True
        s.advance()
        s.detect()
        s.advance()
        s.select_choice(next(c for c in s.tablet_choices() if c.profile is not None))
        s.advance()
        s.detect_display()
        app.wizard.show_current()

    def test_welcome_offers_three_paths_in_both_languages(self) -> None:
        for lang in i18n.SUPPORTED:
            i18n.set_language(lang)
            app = self.app()
            body = self.texts(app.wizard.page)
            for goal in GOALS:
                self.assertIn(i18n.t(f"gui.welcome.goal.{goal}.title"), body)
            self.assertNotIn("gui.welcome", body)

    def test_choosing_a_path_shortens_the_stepper(self) -> None:
        app = self.app()
        self.assertEqual(len(app.wizard.visible_steps()), len(STEP_ORDER))
        app.wizard.page._goal.set("coach")
        app.wizard.page._choose()
        self.assertEqual(self.session.goal, "coach")
        self.assertEqual(len(app.wizard.visible_steps()), 4)

    def test_the_whole_card_is_clickable_and_the_chosen_one_is_highlighted(self) -> None:
        from osu_tablet_assistant.gui.widgets import COLORS
        app = self.app()
        page = app.wizard.page
        page._goal_cards["live"].event_generate("<Button-1>")
        app.root.update()
        self.assertEqual(self.session.goal, "live")
        self.assertEqual(str(page._goal_cards["live"].cget("highlightbackground")), COLORS.accent)
        self.assertNotEqual(str(page._goal_cards["guided"].cget("highlightbackground")), COLORS.accent)

    def test_coach_path_opens_the_coach_from_the_display_step(self) -> None:
        app = self.app()
        self.session.set_goal("coach")
        self.to_display(app)
        self.assertIn(i18n.t("gui.nav.open_coach"), str(app.wizard._next.cget("text")))
        opened: list[wizard.LaunchContext] = []
        wizard.COACH_LAUNCHER = opened.append
        app.wizard.on_next()
        self.assertEqual(len(opened), 1)
        self.assertIs(opened[0].session, self.session)
        self.assertIs(self.session.step, Step.DISPLAY)

    def test_live_path_opens_the_companion(self) -> None:
        app = self.app()
        self.session.set_goal("live")
        self.to_display(app)
        opened: list[object] = []
        wizard.COMPANION_LAUNCHER = opened.append
        app.wizard.on_next()
        self.assertEqual(len(opened), 1)
        self.assertIn(i18n.t("gui.nav.open_live"), str(app.wizard._next.cget("text")))

    def test_missing_launcher_shows_an_error_with_the_next_action(self) -> None:
        app = self.app()
        self.session.set_goal("coach")
        self.to_display(app)
        wizard.COACH_LAUNCHER = None
        app.wizard.on_next()
        self.assertTrue(app.wizard._error.visible)

    def test_a_launcher_that_raises_does_not_take_the_wizard_down(self) -> None:
        app = self.app()
        self.session.set_goal("coach")
        self.to_display(app)

        def boom(_ctx: object) -> None:
            raise RuntimeError("boom")

        wizard.COACH_LAUNCHER = boom
        app.wizard.on_next()
        self.assertTrue(app.wizard._error.visible)

    def test_app_wires_the_coach_launcher_and_the_result_button(self) -> None:
        app = self.app()
        self.assertEqual(wizard.COACH_LAUNCHER, app._open_coach)
        with mock.patch("osu_tablet_assistant.gui.coach.open_coach") as opened:
            app._open_coach(wizard.LaunchContext(self.session, app.root))
        self.assertIs(opened.call_args.args[1], self.session)

    def test_simple_mode_hides_the_technical_cards_and_advanced_shows_them(self) -> None:
        app = self.app()
        simple = self.texts(app.wizard.page)
        self.assertNotIn(i18n.t("gui.data.title"), simple)
        app._advanced_var.set(True)
        app._on_advanced()
        self.assertTrue(self.session.advanced)
        self.assertIn(i18n.t("gui.data.title"), self.texts(app.wizard.page))
        app._advanced_var.set(False)
        app._on_advanced()
        self.assertNotIn(i18n.t("gui.data.title"), self.texts(app.wizard.page))


if __name__ == "__main__":
    unittest.main()
