"""Play Coach: tune area and filters from a real play, round after round.

Flow (see docs/PLAY_COACH.md): **prepare** (is osu! open, which map) -> **observe** (the player plays; the
cursor and the click keys are recorded and judged live) -> **report** (what went wrong, ONE proposed change,
apply and play again) -> repeat until the gain is practically nil.

* :class:`CoachCore` has all the logic and no Tk, so it is fully testable with fakes.
* :class:`CoachWindow` is the Tk front-end: a normal window for prepare/report that shrinks to a small
  always-on-top one while observing, plus an optional click-through mini-map in a screen corner.

Integration (same shape as the companion)::

    from osu_tablet_assistant.gui.coach import open_coach
    open_coach(root, session, on_keep=callback)

Honest limits: the coach reads the OS cursor and ONLY the configured click keys; it does not read
osu!'s memory, so hits and misses are inferred (see ``engine.playobserve``).
"""
from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from ..appconfig import app_data_dir
from ..engine import adapt, optimizer as opt
from ..engine.playtypes import PlayReport
from ..keys import KeyChoice, display_list
from ..models import Reason
from ..otd.livetune import LiveTuner, controller_sender, dry_run_sender
from . import hotkeys as hk
from .coachcore import (ANALYSIS_EVERY_S, PHASES, CoachCore, CoachFeedback, Readiness, RoundResult, Translate,
                        ledger_text, say)
from .companion import companion_args_from_session, make_click_through
from .i18n import t as default_t
from .keycard import ClickKeysCard
from .tabletmap import TabletMap
from .widgets import (COLORS, Card, Chip, Expander, Notice, WrapLabel, apply_theme, bullets, button, check,
                      dark_title_bar, frame, icon_image, label, px, surface_color)

TICK_MS = 100
COACH_HOTKEY = "ctrl+alt+p"

__all__ = ["CoachCore", "CoachWindow", "MiniMap", "Readiness", "RoundResult", "open_coach", "say"]


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------
class MiniMap:
    """Optional click-through map in a corner of the screen (hidden by exclusive full screen)."""

    SIZE = (220, 130)

    def __init__(self, master: tk.Misc, core: CoachCore) -> None:
        self.win = tk.Toplevel(master)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-alpha", 0.85)
        except tk.TclError:
            pass
        w, h = px(master, self.SIZE[0]), px(master, self.SIZE[1])
        x = self.win.winfo_screenwidth() - w - px(master, 16)
        self.win.geometry(f"{w}x{h}+{x}+{px(master, 16)}")
        self.map = TabletMap(self.win, height=self.SIZE[1], compact=True)
        self.map.pack(fill="both", expand=True)
        self.map.set_setup(core.tablet, core.area, core.display)
        make_click_through(self.win)

    def destroy(self) -> None:
        try:
            self.win.destroy()
        except tk.TclError:
            pass


class CoachWindow:
    """The Play Coach window. Every OS-facing dependency can be injected through ``core``."""

    def __init__(self, parent: tk.Misc | None, session: Any = None, *, core: CoachCore | None = None,
                 translate: Translate | None = None, on_keep: Callable[[dict], None] | None = None,
                 on_close: Callable[[], None] | None = None, enable_hotkeys: bool = True,
                 hotkey_backend: hk.HotkeyBackend | None = None, beeper: hk.Beeper | None = None,
                 settings: dict | None = None, on_summary: Callable[[dict], None] | None = None,
                 **core_kwargs: Any) -> None:
        self.t: Translate = translate or default_t
        self.on_keep, self.on_close, self.on_summary = on_keep, on_close, on_summary
        self.session = session
        config = getattr(session, "app_config", None)
        if core is None and config is not None:
            core_kwargs.setdefault("keys", config.effective_click_keys)
            core_kwargs.setdefault("analysis_detail", config.analysis_detail)
        self.core = core if core is not None else self._core_from_session(session, settings, **core_kwargs)
        self.key_choice = config.key_choice if config is not None else KeyChoice.make(self.core.keys)
        self.core.set_keys(self.key_choice.effective)
        self.keys_card: ClickKeysCard | None = None
        self._keys_state: dict = {"open": False}       # the key card stays open across screen refreshes
        self.beeper = beeper if beeper is not None else hk.Beeper()
        self._actions: queue.Queue[str] = queue.Queue()
        self.hotkeys = hk.HotkeyManager({"coach_toggle": COACH_HOTKEY}, self._actions.put,
                                        backend=hotkey_backend, use_default_backend=hotkey_backend is None)
        self._results: queue.Queue[PlayReport | None] = queue.Queue()
        self._analyzing = False
        self._last_analysis = 0.0
        self._interval = ANALYSIS_EVERY_S      # grows when one analysis is slow (it competes with the cursor sampler)
        self._closed = False
        self._job: str | None = None
        self.mini: MiniMap | None = None
        self.own_root = parent is None
        self.root = tk.Tk() if parent is None else tk.Toplevel(parent)
        if parent is None:
            apply_theme(self.root)
        self.root.title(self.t("play.title"))
        self.root.configure(bg=COLORS.bg)
        dark_title_bar(self.root)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._geometry_before_observe: str | None = None
        self.body: ttk.Frame | None = None
        self.map: TabletMap | None = None
        self.status = Notice(self.root)
        self.show_phase("prepare")
        if enable_hotkeys and self.hotkeys.available:
            self.hotkeys.start()
        self._job = self.root.after(TICK_MS, self._tick)

    # ------------------------------------------------------------- construction
    def _core_from_session(self, session: Any, settings: dict | None = None, **kwargs: Any) -> CoachCore:
        """``settings`` overrides the settings taken from the session (e.g. the companion's live ones)."""
        args = companion_args_from_session(session) if session is not None else {}
        settings, name = settings if settings is not None else args.get("settings"), args.get("tablet_name")
        if settings is None or not name:
            raise ValueError("CoachWindow needs a session (or a core)")
        dry_run, controller = bool(args.get("dry_run")), args.get("controller")
        sender = dry_run_sender if dry_run or controller is None else controller_sender(controller)
        tuner = LiveTuner(settings, name, sender, config_dir=args.get("config_dir"), tablet=args.get("tablet"),
                          can_keep=not dry_run)
        display = args.get("display")
        if display is None:
            raise ValueError("CoachWindow needs the display of the session")
        prefs = getattr(session, "prefs", None)
        policy = adapt.TuningPolicy(adapt.PolicyMode(getattr(getattr(prefs, "plugin_policy", None), "value",
                                                              "tune_only")),
                                    frozenset(args.get("available_filters", ())))
        cache = app_data_dir() / "beatmap_index.json"
        return CoachCore(tuner, args.get("tablet"), display, prefs=prefs, policy=policy, cache_path=cache,
                         **kwargs)

    # ----------------------------------------------------------------- phases
    def show_phase(self, phase: str) -> None:
        core = self.core
        core.phase = phase
        if self.body is not None:
            self.body.destroy()
        self.body = ttk.Frame(self.root, padding=px(self.root, 16))
        self.body.pack(fill="both", expand=True)
        self.map, self.keys_card, self._hint_button = None, None, None
        compact = phase == "observe"
        self._set_window_mode(compact)
        {"prepare": self._build_prepare, "observe": self._build_observe, "report": self._build_report}[phase]()

    def _set_window_mode(self, compact: bool) -> None:
        root = self.root
        if compact:
            if self._geometry_before_observe is None:
                self._geometry_before_observe = root.geometry()
            root.attributes("-topmost", True)
            root.minsize(px(root, 320), px(root, 300))
            root.geometry(f"{px(root, 360)}x{px(root, 380)}")
        else:
            root.attributes("-topmost", False)
            root.minsize(px(root, 560), px(root, 560))
            if self._geometry_before_observe is not None:
                root.geometry(self._geometry_before_observe)
                self._geometry_before_observe = None
            elif not root.winfo_viewable():   # tall enough for the click-button card, never taller than the screen
                root.geometry(f"{px(root, 640)}x{min(px(root, 760), root.winfo_screenheight() - px(root, 80))}")

    def _say(self, reason: Reason) -> str:
        return say(reason, self.t)

    def _title_block(self, title_key: str, subtitle_key: str | None = None) -> None:
        label(self.body, self.t(title_key), "Title").pack(anchor="w")
        if subtitle_key:
            WrapLabel(self.body, self.t(subtitle_key), "Muted").pack(fill="x", pady=(px(self.root, 2), px(self.root, 10)))
        self._steps()

    def _steps(self) -> None:
        names = ["play.step.prepare", "play.step.play", "play.step.result"]
        index = PHASES.index(self.core.phase)
        row = frame(self.body)
        row.pack(fill="x", pady=(0, px(self.root, 10)))
        for i, key in enumerate(names):
            Chip(row, f"{i + 1}  {self.t(key)}", "accent" if i == index else "neutral").pack(
                side="left", padx=(0, px(self.root, 6)))

    # -- prepare
    def _build_prepare(self) -> None:
        core = self.core
        self._title_block("play.title", "play.prepare.intro")
        ready = core.readiness()
        card = Card(self.body, self.t("play.prepare.check_title"))
        card.pack(fill="x", pady=(0, px(self.root, 8)))
        self._check_row(card.body, ready.osu_running, self.t("play.prepare.osu_open"))
        self._check_row(card.body, ready.beatmap is not None,
                        ready.beatmap.display_name if ready.beatmap else self.t("play.prepare.map_missing"))
        if ready.beatmap is None:
            row = frame(card.body)
            row.pack(fill="x", pady=(px(self.root, 4), 0))
            button(row, self.t("play.prepare.pick_map"), self._pick_map, kind="ghost").pack(side="left")
        for issue in ready.issues:
            WrapLabel(card.body, "• " + self._say(issue), "Small").pack(fill="x")
        self.keys_card = ClickKeysCard(self.body, self.key_choice, self.t, self._keys_changed, core.analysis_detail,
                                       self._detail_changed, self._keys_state)
        self.keys_card.pack(fill="x", pady=(0, px(self.root, 8)))
        self.map = TabletMap(self.body, height=190)
        self.map.pack(fill="x", pady=(0, px(self.root, 8)))
        self.map.set_setup(core.tablet, core.area, core.display)
        WrapLabel(self.body, self.t("play.prepare.honest", keys=display_list(core.keys)), "Small").pack(
            fill="x", pady=(0, px(self.root, 10)))
        if core.result is None and core.state.rounds:
            WrapLabel(self.body, self._progress_text(), "Muted").pack(fill="x", pady=(0, px(self.root, 6)))
        actions = frame(self.body)
        actions.pack(fill="x")
        start = button(actions, self.t("play.prepare.start"), self.start_observing, kind="primary")
        start.pack(side="left")
        start.configure(state="normal" if ready.can_start else "disabled")
        button(actions, self.t("play.prepare.use_replay"), lambda: self.pick_replay("round"), kind="ghost").pack(
            side="left", padx=(px(self.root, 8), 0))
        if self.hotkeys.available:
            label(actions, self.t("play.prepare.hotkey", combo=COACH_HOTKEY.replace("+", " + ").title()), "Small").pack(
                side="left", padx=(px(self.root, 12), 0))
        self._mini_var = tk.BooleanVar(value=self.mini is not None)
        adv = Expander(self.body, self.t("play.prepare.options"), height=9)
        adv.pack(fill="x", pady=(px(self.root, 10), 0))
        adv.set(ledger_text(self.t, core.keys))
        check(self.body, self.t("play.prepare.minimap"), self._mini_var, command=self._toggle_mini).pack(
            anchor="w", pady=(px(self.root, 2), 0))
        WrapLabel(self.body, self.t("play.prepare.minimap_note"), "Small").pack(fill="x")
        self.status = Notice(self.body)
        self.status.pack(fill="x", pady=(px(self.root, 8), 0))
        if core.state.rounds:
            self._build_progress_strip(self.body)
            self._build_keep_row(self.body)

    def _keys_changed(self, choice: KeyChoice) -> None:
        """The player changed their click buttons: use them, save them, refresh the screen when not listening."""
        self.key_choice = choice
        self.core.set_keys(choice.effective)
        self._save_config(lambda cfg: cfg.set_key_choice(choice))
        if self.keys_card is None or not self.keys_card.listening:
            self.show_phase("prepare")

    def _detail_changed(self, detail: str) -> None:
        self.core.set_analysis_detail(detail)
        self._save_config(lambda cfg: setattr(cfg, "analysis_detail", detail))

    def _save_config(self, change: Callable[[Any], None]) -> None:
        """Change the assistant's config and save it at once; a failure never breaks the coach."""
        config = getattr(self.session, "app_config", None)
        if config is None:
            return
        try:
            change(config)
            self.session.save_app_config()
        except Exception:            # saving is a convenience: the choice still applies to this session
            pass

    def choose_keys(self) -> None:
        """From a "no click seen" hint: back to prepare (dropping what was recorded) and listen for the buttons."""
        if self.core.phase == "observe":
            self.core.abort()
        self.core.result = None
        self.show_phase("prepare")
        if self.keys_card is not None:
            self.keys_card.start_listening()

    def _check_row(self, parent: tk.Misc, ok: bool, text: str) -> None:
        row = frame(parent)
        row.pack(fill="x", pady=1)
        mark = icon_image(row, "success" if ok else "warn", 16, COLORS.good if ok else COLORS.warn,
                          surface_color(row))
        tk.Label(row, image=mark, background=surface_color(row), borderwidth=0).pack(side="left")
        label(row, text, "Body").pack(side="left", padx=(px(self.root, 8), 0))

    def _pick_map(self) -> None:
        path = filedialog.askopenfilename(parent=self.root, title=self.t("play.prepare.pick_map"),
                                          filetypes=[("osu! beatmap", "*.osu"), ("*", "*")])
        if path and not self.core.set_beatmap_file(path):
            messagebox.showerror(self.t("play.title"), self.t("play.prepare.map_unreadable"), parent=self.root)
        self.show_phase("prepare")

    def _toggle_mini(self) -> None:
        if self._mini_var.get() and self.mini is None:
            self.mini = MiniMap(self.root, self.core)
        elif not self._mini_var.get() and self.mini is not None:
            self.mini.destroy()
            self.mini = None

    # -- observe
    def start_observing(self) -> None:
        if self.core.phase == "observe" or not self.core.readiness(refresh=False).can_start:
            return
        if not self.core.start():
            return
        self._last_analysis = time.monotonic()
        self._analyzing = False
        self.show_phase("observe")
        self.beeper.play("converged")

    def _build_observe(self) -> None:
        self._observe_var = tk.StringVar(value=self.t("play.observe.searching"))
        label(self.body, self.t("play.observe.title"), "Heading").pack(anchor="w")
        self.map = TabletMap(self.body, height=200, compact=False)
        self.map.pack(fill="both", expand=True)
        self.map.set_setup(self.core.tablet, self.core.area, self.core.display)
        ttk.Label(self.body, textvariable=self._observe_var, wraplength=px(self.root, 320)).pack(
            fill="x", pady=(px(self.root, 6), px(self.root, 6)))
        button(self.body, self.t("play.observe.stop"), self.stop_observing, kind="primary").pack(anchor="w")

    def stop_observing(self) -> None:
        if self.core.phase != "observe":
            return
        self.core.finish()
        self.show_phase("report")
        self.root.lift()

    # -- report
    def _build_report(self) -> None:
        core, result = self.core, self.core.result
        assert result is not None
        report, proposal = result.report, result.proposal
        self._title_block("play.report.title")
        self.map = TabletMap(self.body, height=190)
        self.map.pack(fill="x", pady=(0, px(self.root, 6)))
        self.map.set_setup(core.tablet, core.area, core.display)
        self.map.set_cells(report.cells)
        self.map.add_results(report.results, report.radius_mm)
        card = Card(self.body, self.t("play.report.found_title"))
        card.pack(fill="x", pady=(0, px(self.root, 8)))
        bullets(card.body, [self._say(r) for r in result.findings[:4]]).pack(fill="x")
        if result.hint is not None:
            WrapLabel(card.body, self._say(result.hint), "Strong").pack(fill="x", pady=(px(self.root, 6), 0))
            button(card.body, self.t("play.keys.open"), self.choose_keys, kind="secondary").pack(
                anchor="w", pady=(px(self.root, 4), 0))
        if proposal.kind != "none":
            card = Card(self.body, self.t("play.report.change_title"), accent=COLORS.accent)
            card.pack(fill="x", pady=(0, px(self.root, 8)))
            WrapLabel(card.body, self._say(proposal.message) if proposal.message else "", "Strong").pack(fill="x")
            bullets(card.body, [self._say(r) for r in proposal.reasons[:3]], role="Small").pack(fill="x")
            WrapLabel(card.body, self.t("play.report.live_note"), "Small").pack(fill="x", pady=(px(self.root, 4), 0))
        elif proposal.message:
            WrapLabel(self.body, self._say(proposal.message), "Muted").pack(fill="x", pady=(0, px(self.root, 8)))
        self._build_progress_strip(self.body)
        WrapLabel(self.body, self._say(result.convergence.message), "Strong").pack(fill="x", pady=(0, px(self.root, 8)))
        if result.from_replay:
            WrapLabel(self.body, self.t("play.replay.used"), "Small").pack(fill="x", pady=(0, px(self.root, 6)))
        elif result.check is not None:
            self._build_check(result.check)
        done = proposal.kind == "none"
        actions = frame(self.body)
        actions.pack(fill="x")
        if done and proposal.verdict == opt.PLATEAU:
            button(actions, self.t("play.report.keep"), self.keep, kind="primary").pack(side="left")
            button(actions, self.t("play.report.play_again"), self.play_again, kind="secondary").pack(
                side="left", padx=(px(self.root, 8), 0))
        elif done:
            button(actions, self.t("play.report.play_again"), self.play_again, kind="primary").pack(side="left")
        else:
            button(actions, self.t("play.report.apply"), self.apply, kind="primary").pack(side="left")
            button(actions, self.t("play.report.skip"), self.skip, kind="secondary").pack(
                side="left", padx=(px(self.root, 8), 0))
        self.status = Notice(self.body)
        self.status.pack(fill="x", pady=(px(self.root, 8), 0))
        row = frame(self.body)
        row.pack(fill="x", pady=(px(self.root, 4), 0))
        undo = button(row, self.t("play.report.undo"), self.undo, kind="ghost")
        undo.pack(side="left")
        if not result.from_replay and result.check is None:
            button(row, self.t("play.report.verify_replay"), lambda: self.pick_replay("verify"), kind="ghost").pack(
                side="left", padx=(px(self.root, 4), 0))
        undo.configure(state="normal" if core.tuner.can_undo else "disabled")
        if not (done and proposal.verdict == opt.PLATEAU):
            self._build_keep_row(self.body, only_restore=False)
        else:
            button(row, self.t("play.report.restore"), self.restore, kind="ghost").pack(side="left")
        tech = Expander(self.body, self.t("gui.details.title"), height=8)
        tech.pack(fill="x", pady=(px(self.root, 6), 0))
        tech.set(self._technical_text(report, result))

    def _build_check(self, check: Any) -> None:
        """How well the coach's inference matched the game's own replay (plain words, one line + one hint)."""
        good = check.agreement >= 0.9
        text = self.t("play.replay.check", pct=f"{check.agreement * 100:.0f}", n=check.compared)
        if check.real_accuracy is not None:
            text += " " + self.t("play.replay.accuracy", acc=f"{check.real_accuracy * 100:.1f}")
        card = Notice(self.body)
        card.pack(fill="x", pady=(0, px(self.root, 6)))
        card.show(text + " " + self.t("play.replay.check_good" if good else "play.replay.check_weak"),
                  "success" if good else "warn")

    def pick_replay(self, mode: str) -> None:
        """Choose an .osr: ``round`` uses it as a round, ``verify`` compares it with the last live round."""
        folder = self.core.replay_dir()
        path = filedialog.askopenfilename(parent=self.root, title=self.t("play.replay.pick"),
                                          initialdir=str(folder) if folder else None,
                                          filetypes=[("osu! replay", "*.osr"), ("*", "*")])
        if not path:
            return
        outcome = self.core.round_from_replay(path) if mode == "round" else self.core.verify_with_replay(path)
        if not outcome.ok:
            messagebox.showwarning(self.t("play.title"), self._say(outcome.problem) if outcome.problem else "",
                                   parent=self.root)
            return
        self.show_phase("report")
        if outcome.problem is not None:           # read fine, but it is of another map file
            self.status.show(self._say(outcome.problem), "warn")

    def _build_progress_strip(self, parent: tk.Misc) -> None:
        marks = self.core.marks()
        if not marks:
            return
        row = frame(parent)
        row.pack(fill="x", pady=(px(self.root, 2), px(self.root, 6)))
        label(row, self.t("play.progress.title"), "Small").pack(side="left", padx=(0, px(self.root, 8)))
        kinds = {"start": "neutral", "better": "success", "same": "neutral", "worse": "warn", "unclear": "neutral"}
        for i, mark in enumerate(marks):
            Chip(row, f"{i + 1} · {self.t(f'play.mark.{mark}')}", kinds[mark]).pack(side="left", padx=(0, 4))

    def _build_keep_row(self, parent: tk.Misc, only_restore: bool = False) -> None:
        row = frame(parent)
        row.pack(fill="x", pady=(px(self.root, 4), 0))
        if not only_restore and self.core.dirty:
            button(row, self.t("play.report.keep"), self.keep, kind="secondary").pack(side="left")
        button(row, self.t("play.report.restore"), self.restore, kind="ghost").pack(
            side="left", padx=(px(self.root, 8), 0))

    def _progress_text(self) -> str:
        return self.t("play.progress.rounds", n=len(self.core.state.rounds))

    def _technical_text(self, report: PlayReport, result: RoundResult) -> str:
        a = report.alignment
        lines = [self.t("play.tech.alignment", quality=f"{a.quality:.2f}", matched=a.matched, scale=f"{a.time_scale:g}",
                        flip="HR" if a.flip_y else "-", offset=f"{a.offset_ms:.0f}", spread=f"{a.spread_ms:.0f}"),
                 self.t("play.tech.judged", n=report.n_judged, total=report.n_objects,
                        miss=f"{report.miss_rate * 100:.0f}")]
        if report.reach is not None:
            r = report.reach
            lines.append(self.t("play.tech.reach", gx=f"{r.gain_x:.3f}", gy=f"{r.gain_y:.3f}",
                                bx=f"{r.bias_x_mm:.2f}", by=f"{r.bias_y_mm:.2f}", n=r.n))
        if report.cost is not None:
            lines.append(self.t("play.tech.cost", cost=f"{report.cost:.3f}",
                                lo=f"{report.cost_ci[0]:.3f}" if report.cost_ci else "-",
                                hi=f"{report.cost_ci[1]:.3f}" if report.cost_ci else "-"))
        lines += [self._say(w) for w in report.warnings]
        lines += [self._say(r) for r in result.proposal.reasons]
        return "\n".join(lines)

    # -- actions
    def play_again(self) -> None:
        self.core.result = None
        self.show_phase("prepare")

    def _notify(self, fb: CoachFeedback) -> None:
        if fb.message is not None:
            kind = "success" if fb.ok else "error"
            text = self._say(fb.message) + (" " + self.t("play.msg.live_only") if fb.live_only and fb.ok else "")
            self.status.show(text, kind)
        self.beeper.play(fb.sound)

    def apply(self) -> None:
        fb = self.core.apply_proposal()
        if fb.ok:
            self.show_phase("prepare")
        self._notify(fb)

    def skip(self) -> None:
        self.core.skip_proposal()
        self.show_phase("prepare")

    def undo(self) -> None:
        fb = self.core.undo_last()
        self._notify(fb)

    def restore(self) -> None:
        if not messagebox.askyesno(self.t("play.title"), self.t("play.restore.confirm"), parent=self.root):
            return
        fb = self.core.restore_original()
        self.show_phase("prepare")
        self._notify(fb)

    def keep(self) -> None:
        fb = self.core.keep()
        self._notify(fb)
        if fb.ok:
            self._report_summary()
            if self.on_keep is not None:
                self.on_keep(self.core.tuner.settings)

    def _report_summary(self) -> None:
        """Tell the owner how the session went (rounds and how much the cost dropped), once per Keep."""
        if self.on_summary is None:
            return
        info = self.core.progress_summary()
        if info is not None:
            self.on_summary(info)

    # ------------------------------------------------------------------- loop
    def _tick(self) -> None:
        if self._closed:
            return
        try:
            self._handle_actions()
            if self.core.phase == "observe":
                self._tick_observe()
        finally:
            if not self._closed:
                self._job = self.root.after(TICK_MS, self._tick)

    def _handle_actions(self) -> None:
        while True:
            try:
                action = self._actions.get_nowait()
            except queue.Empty:
                return
            if action == "coach_toggle":
                if self.core.phase == "observe":
                    self.stop_observing()
                elif self.core.phase == "prepare":
                    self.start_observing()

    def _tick_observe(self) -> None:
        core = self.core
        view = core.live_view()
        if view is not None:
            targets = [self.map] + ([self.mini.map] if self.mini is not None else [])
            for m in targets:
                if m is not None:
                    m.update_pen(view.trail)
        try:
            while True:
                report = self._results.get_nowait()
                self._analyzing = False
                if report is not None and self.map is not None:
                    self.map.add_results(report.results, report.radius_mm)
                    if self.mini is not None:
                        self.mini.map.add_results(report.results, report.radius_mm)
        except queue.Empty:
            pass
        if view is not None:
            text = self.t("play.observe.searching") if core.searching() else self.t(
                "play.observe.running", clicks=view.clicks)
            hint = core.no_clicks_hint(view)
            if hint is not None:
                text = self._say(hint)
                if self._hint_button is None:
                    self._hint_button = button(self.body, self.t("play.keys.open"), self.choose_keys, kind="ghost")
                    self._hint_button.pack(anchor="w", pady=(px(self.root, 4), 0))
            self._observe_var.set(text)
        now = time.monotonic()
        if not self._analyzing and now - self._last_analysis >= self._interval:
            self._analyzing, self._last_analysis = True, now
            threading.Thread(target=self._analysis_worker, name="CoachAnalysis", daemon=True).start()
        if core.should_auto_stop():
            self.stop_observing()

    def _analysis_worker(self) -> None:
        started = time.monotonic()
        try:
            report = self.core.analyze_live()
        except Exception:
            report = None
        # leave at least ~9 analysis-durations of idle time so the cursor sampler thread keeps its CPU
        self._interval = max(ANALYSIS_EVERY_S, 10 * (time.monotonic() - started))
        self._results.put(report)

    # ----------------------------------------------------------------- closing
    def close(self) -> None:
        if self.core.dirty and self.core.tuner.can_keep:
            answer = messagebox.askyesnocancel(self.t("play.close.title"), self.t("play.close.body"), parent=self.root)
            if answer is None:
                return
            if answer:
                fb = self.core.keep()
                self._notify(fb)
                if not fb.ok:
                    return
                self._report_summary()
                if self.on_keep is not None:
                    self.on_keep(self.core.tuner.settings)
            else:
                self.core.tuner.revert_all()
                self.core.tuner.flush()
        self.destroy()

    def destroy(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._job is not None:
            try:
                self.root.after_cancel(self._job)
            except tk.TclError:
                pass
        self.hotkeys.stop()
        self.core.stop_recording()
        self.beeper.close()
        if self.mini is not None:
            self.mini.destroy()
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        if self.on_close is not None:
            self.on_close()


def open_coach(parent: tk.Misc | None, session: Any = None, **kwargs: Any) -> CoachWindow:
    """Open the Play Coach window on a wizard session (see the module docstring)."""
    return CoachWindow(parent, session, **kwargs)
