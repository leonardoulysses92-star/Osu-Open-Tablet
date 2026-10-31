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
from .coachcore import (ANALYSIS_EVERY_S, FREE_ONLY, MANUAL_MOVES, PHASES, CoachCore, CoachFeedback, Readiness,
                        RoundResult, Translate, area_changes, area_summary, ledger_text, say)
from .companion import companion_args_from_session, make_click_through
from .i18n import t as default_t
from .keycard import ClickKeysCard
from .smoothcard import SmoothingCard
from .tabletmap import TabletMap
from .widgets import (COLORS, Card, Chip, Expander, Notice, ScrollFrame, WrapLabel, apply_theme, bullets, button,
                      check, dark_title_bar, frame, icon_image, label, px, surface_color)

TICK_MS = 100
MAP_POLL_S = 1.0       # how often osu!'s window title is read to notice a new map
MAP_NOTE_S = 6.0       # how long "new map recognised" replaces the status line while observing
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
            core_kwargs.setdefault("aspect_lock", not config.free_aspect)
        self.core = core if core is not None else self._core_from_session(session, settings, **core_kwargs)
        self.key_choice = config.key_choice if config is not None else KeyChoice.make(self.core.keys)
        self.core.set_keys(self.key_choice.effective)
        self.keys_card: ClickKeysCard | None = None
        self._keys_state: dict = {"open": False}       # the key card stays open across screen refreshes
        self._smooth_state: dict = {"open": False}     # ... and so do the smoothing card ...
        self._manual_state: dict = {"open": False}     # ... and "Ajustar el área a mano"
        self.smooth_card: SmoothingCard | None = None
        self._area_holder: ttk.Frame | None = None
        self._smooth_holder: ttk.Frame | None = None
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
        self._last_map_poll = 0.0
        self._map_note_until = 0.0             # while observing: the "new map recognised" line stays this long
        self._map_note = ""
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
        self.footer: ttk.Frame | None = None      # the action bar of the prepare and report screens
        self._scroll: ScrollFrame | None = None
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
        for old in (self.body, self.footer, self._scroll):
            if old is not None:
                old.destroy()
        compact = phase == "observe"
        if compact:                                     # the small always-on-top panel: nothing to scroll
            self.footer = self._scroll = None
            self.body = ttk.Frame(self.root, padding=px(self.root, 16))
            self.body.pack(fill="both", expand=True)
        else:
            # The actions live in a bar that never scrolls away (save / play again are always in reach);
            # the rest scrolls, so nothing is cut off on a short screen.
            self.footer = ttk.Frame(self.root, padding=(px(self.root, 16), px(self.root, 8)))
            self.footer.pack(side="bottom", fill="x")
            self._scroll = ScrollFrame(self.root)
            self._scroll.pack(fill="both", expand=True)
            self.body = ttk.Frame(self._scroll.body, padding=px(self.root, 16))
            self.body.pack(fill="both", expand=True)
        self.map, self.keys_card, self._hint_button = None, None, None
        self.smooth_card, self._area_holder, self._smooth_holder = None, None, None
        self.area_card: Card | None = None
        self.manual_buttons: dict[str, Any] = {}
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
        self._check_row(card.body, ready.beatmap is not None, self._map_line())
        if ready.beatmap is None:
            row = frame(card.body)
            row.pack(fill="x", pady=(px(self.root, 4), 0))
            button(row, self.t("play.prepare.pick_map"), self._pick_map, kind="ghost").pack(side="left")
        for issue in ready.issues:
            WrapLabel(card.body, "• " + self._say(issue), "Small").pack(fill="x")
        self.keys_card = ClickKeysCard(self.body, self.key_choice, self.t, self._keys_changed, core.analysis_detail,
                                       self._detail_changed, self._keys_state)
        self.keys_card.pack(fill="x", pady=(0, px(self.root, 8)))
        self._free_var = tk.BooleanVar(value=not core.aspect_lock)
        check(self.body, self.t("play.aspect.label"), self._free_var, command=self._aspect_changed).pack(anchor="w")
        WrapLabel(self.body, self.t("play.aspect.note"), "Small").pack(fill="x", pady=(0, px(self.root, 8)))
        self.map = TabletMap(self.body, height=190)
        self.map.pack(fill="x", pady=(0, px(self.root, 8)))
        self.map.set_setup(core.tablet, core.area, core.display)
        self._area_holder = frame(self.body)
        self._area_holder.pack(fill="x", pady=(0, px(self.root, 8)))
        self._build_area_card(self._area_holder, editable=True)
        self._smooth_holder = frame(self.body)
        self._smooth_holder.pack(fill="x", pady=(0, px(self.root, 8)))
        self._build_smooth_card()
        WrapLabel(self.body, self.t("play.prepare.honest", keys=display_list(core.keys)), "Small").pack(
            fill="x", pady=(0, px(self.root, 10)))
        if core.result is None and core.state.rounds:
            WrapLabel(self.body, self._progress_text(), "Muted").pack(fill="x", pady=(0, px(self.root, 6)))
        self._mini_var = tk.BooleanVar(value=self.mini is not None)
        adv = Expander(self.body, self.t("play.prepare.options"), height=9)
        adv.pack(fill="x", pady=(px(self.root, 10), 0))
        adv.set(ledger_text(self.t, core.keys))
        check(self.body, self.t("play.prepare.minimap"), self._mini_var, command=self._toggle_mini).pack(
            anchor="w", pady=(px(self.root, 2), 0))
        WrapLabel(self.body, self.t("play.prepare.minimap_note"), "Small").pack(fill="x")
        if core.state.rounds:
            self._build_progress_strip(self.body)
        self._build_prepare_footer(ready)

    def _build_prepare_footer(self, ready: Readiness) -> None:
        """Messages, "Start observing" and save/restore: the bar that never scrolls away (rebuilt after changes)."""
        if self.footer is None:
            return
        for child in self.footer.winfo_children():
            child.destroy()
        self.status = Notice(self.footer)               # messages and buttons sit in the bar that never scrolls away
        self.status.pack(fill="x", pady=(0, px(self.root, 6)))
        actions = frame(self.footer)
        actions.pack(fill="x")
        start = button(actions, self.t("play.prepare.start"), self.start_observing, kind="primary")
        start.pack(side="left")
        start.configure(state="normal" if ready.can_start else "disabled")
        button(actions, self.t("play.prepare.use_replay"), lambda: self.pick_replay("round"), kind="ghost").pack(
            side="left", padx=(px(self.root, 8), 0))
        if self.hotkeys.available:
            label(actions, self.t("play.prepare.hotkey", combo=COACH_HOTKEY.replace("+", " + ").title()), "Small").pack(
                side="left", padx=(px(self.root, 12), 0))
        if self.core.state.rounds or self.core.dirty:     # changes by hand can be saved before any round
            self._build_keep_row(self.footer)

    # -- the area in numbers, and by hand
    def _build_area_card(self, parent: tk.Misc, editable: bool, proposed: Any = None) -> None:
        """"Área actual: 80 × 45 mm, centro ..." and, once something changed, "antes → ahora" per value."""
        core = self.core
        card = Card(parent, self.t("play.area.title"), padding=12)
        card.pack(fill="x")
        self.area_card = card
        WrapLabel(card.body, area_summary(core.area, self.t), "Strong").pack(fill="x")
        if proposed is not None and proposed != core.area:
            WrapLabel(card.body, area_summary(proposed, self.t, "play.area.after"), "Body").pack(fill="x")
        changes = area_changes(core.original_area, core.area, self.t)
        if changes:
            WrapLabel(card.body, self.t("play.area.since"), "Small").pack(fill="x", pady=(px(self.root, 4), 0))
            bullets(card.body, changes, role="Small").pack(fill="x")
        WrapLabel(card.body, self.t("play.area.otd_note"), "Small").pack(fill="x", pady=(px(self.root, 2), 0))
        if editable:
            self._build_manual(card.body)

    def _build_manual(self, parent: tk.Misc) -> None:
        """"Ajustar el área a mano": size -/+ 2 %, move 0.5 mm, one axis with the free proportion; undo."""
        fold = Expander(parent, self.t("play.manual.title"), widgets=True, opened=bool(self._manual_state["open"]),
                        on_toggle=lambda opened: self._manual_state.__setitem__("open", opened))
        fold.pack(fill="x", pady=(px(self.root, 6), 0))
        box = fold.content
        WrapLabel(box, self.t("play.manual.intro"), "Small").pack(fill="x", pady=(0, px(self.root, 4)))
        self.manual_buttons: dict[str, Any] = {}
        groups = [("play.manual.size", ("smaller", "bigger")), ("play.manual.move", tuple(MANUAL_MOVES))]
        if not self.core.aspect_lock:
            groups.append(("play.manual.axes", FREE_ONLY))
        for title, actions in groups:
            row = frame(box)
            row.pack(fill="x", pady=(px(self.root, 2), 0))
            label(row, self.t(title), "Strong").pack(side="left", padx=(0, px(self.root, 8)))
            for action in actions:
                btn = button(row, self.t(f"play.manual.{action}"), lambda a=action: self.manual(a), kind="secondary")
                btn.pack(side="left", padx=(0, px(self.root, 4)))
                self.manual_buttons[action] = btn
        if self.core.aspect_lock:
            WrapLabel(box, self.t("play.manual.free_hint"), "Small").pack(fill="x", pady=(px(self.root, 4), 0))
        undo = button(box, self.t("play.manual.undo"), self.undo_prepare, kind="ghost")
        undo.pack(anchor="w", pady=(px(self.root, 4), 0))
        undo.configure(state="normal" if self.core.tuner.can_undo else "disabled")

    def _build_smooth_card(self) -> None:
        if self._smooth_holder is None:
            return
        for child in self._smooth_holder.winfo_children():
            child.destroy()
        self.smooth_card = SmoothingCard(self._smooth_holder, self.core.smoothing_options(), self.t,
                                         self.smooth_try, self.smooth_stop, self._smooth_state)
        self.smooth_card.pack(fill="x")

    def _refresh_prepare(self) -> None:
        """After a change by hand: new numbers, the outline on the tablet map, the smoothing card, the footer
        (the screen is not rebuilt, so it does not scroll back to the top)."""
        core = self.core
        if self._area_holder is not None:
            for child in self._area_holder.winfo_children():
                child.destroy()
            self._build_area_card(self._area_holder, editable=True)
        self._build_smooth_card()
        if self.map is not None:
            self.map.set_setup(core.tablet, core.area, core.display)
        self._build_prepare_footer(core.readiness(refresh=False))

    def manual(self, action: str) -> None:
        """One press of a button of "Ajustar el área a mano"."""
        fb = self.core.manual_area(action)
        if fb.ok:
            self._refresh_prepare()
        self._notify(fb)

    def undo_prepare(self) -> None:
        fb = self.core.undo_last()
        if fb.ok:
            self._refresh_prepare()
        self._notify(fb)

    # -- smoothing the player decides on
    def smooth_try(self, path: str) -> None:
        self._smooth_action(lambda: self.core.try_smoothing(path))

    def smooth_stop(self, path: str) -> None:
        self._smooth_action(lambda: self.core.stop_smoothing(path))

    def smooth_keep(self, path: str) -> None:
        self._smooth_action(lambda: self.core.keep_smoothing(path))

    def _smooth_action(self, act: Callable[[], CoachFeedback]) -> None:
        fb = act()
        if self.core.phase == "prepare":
            self._refresh_prepare()
        elif self.core.phase == "report" and self.core.result is not None:
            self.show_phase("report")
        self._notify(fb)

    def _keys_changed(self, choice: KeyChoice) -> None:
        """The player changed their click buttons: use them, save them, refresh the screen when not listening."""
        self.key_choice = choice
        self.core.set_keys(choice.effective)
        self._save_config(lambda cfg: cfg.set_key_choice(choice))
        if self.keys_card is None or not self.keys_card.listening:
            self.show_phase("prepare")

    def _aspect_changed(self) -> None:
        """Free proportion on/off: the next proposals may (or may not) stretch the area on one axis only."""
        free = bool(self._free_var.get())
        self.core.set_free_aspect(free)
        self._save_config(lambda cfg: setattr(cfg, "free_aspect", free))
        if self._area_holder is not None:                # the one-axis buttons appear / go away
            for child in self._area_holder.winfo_children():
                child.destroy()
            self._build_area_card(self._area_holder, editable=True)

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
        if self.core.phase == "observe":
            return
        self.core.track_map()                  # never start on the previous map when osu! already loaded another
        if not self.core.readiness(refresh=False).can_start:
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
        WrapLabel(self.body, self._map_line(), "Muted").pack(fill="x")
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
        WrapLabel(self.body, self._map_line(), "Muted").pack(fill="x", pady=(0, px(self.root, 4)))
        if result.map_changed:
            WrapLabel(self.body, self.t("play.map.compare_note"), "Small").pack(fill="x", pady=(0, px(self.root, 6)))
        self.map = TabletMap(self.body, height=190)
        self.map.pack(fill="x", pady=(0, px(self.root, 6)))
        self.map.set_setup(core.tablet, core.area, core.display)
        self.map.set_cells(report.cells)
        self.map.add_results(report.results, report.radius_mm)
        if proposal.probe_outcome is not None:          # what happened to the trial this round was played on
            card = Card(self.body, self.t("play.probe.title"), padding=12)
            card.pack(fill="x", pady=(0, px(self.root, 8)))
            WrapLabel(card.body, self._say(proposal.probe_outcome), "Strong").pack(fill="x")
        for verdict in core.smoothing_verdicts():
            self._build_smooth_verdict(verdict)
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
        holder = frame(self.body)
        holder.pack(fill="x", pady=(0, px(self.root, 8)))
        self._build_area_card(holder, editable=False, proposed=proposal.area if proposal.kind != "none" else None)
        self._build_progress_strip(self.body)
        WrapLabel(self.body, self._say(result.convergence.message), "Strong").pack(fill="x", pady=(0, px(self.root, 8)))
        if result.from_replay:
            WrapLabel(self.body, self.t("play.replay.used"), "Small").pack(fill="x", pady=(0, px(self.root, 6)))
        elif result.check is not None:
            self._build_check(result.check)
        done = proposal.kind == "none"
        self.status = Notice(self.footer)               # the whole action bar stays visible while the report scrolls
        self.status.pack(fill="x", pady=(0, px(self.root, 6)))
        actions = frame(self.footer)
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
        row = frame(self.footer)
        row.pack(fill="x", pady=(px(self.root, 4), 0))
        undo = button(row, self.t("play.report.undo"), self.undo, kind="ghost")
        undo.pack(side="left")
        if not result.from_replay and result.check is None:
            button(row, self.t("play.report.verify_replay"), lambda: self.pick_replay("verify"), kind="ghost").pack(
                side="left", padx=(px(self.root, 4), 0))
        undo.configure(state="normal" if core.tuner.can_undo else "disabled")
        if not (done and proposal.verdict == opt.PLATEAU):
            self._build_keep_row(self.footer, only_restore=False)
        else:
            button(row, self.t("play.report.restore"), self.restore, kind="ghost").pack(side="left")
        tech = Expander(self.body, self.t("gui.details.title"), height=8)
        tech.pack(fill="x", pady=(px(self.root, 6), 0))
        tech.set(self._technical_text(report, result))

    def _build_smooth_verdict(self, verdict: Any) -> None:
        """"Con el suavizado: mejor / igual / peor" for one trial, and the player's two choices."""
        card = Card(self.body, self.t("play.smooth.verdict.title"), padding=12)
        card.pack(fill="x", pady=(0, px(self.root, 8)))
        key = "play.smooth.verdict.no_base" if verdict.no_base else f"play.smooth.verdict.{verdict.verdict}"
        WrapLabel(card.body, self.t(key, name=verdict.name), "Strong").pack(fill="x")
        WrapLabel(card.body, self.t("play.smooth.verdict.decide"), "Small").pack(fill="x", pady=(px(self.root, 2), 0))
        row = frame(card.body)
        row.pack(fill="x", pady=(px(self.root, 6), 0))
        button(row, self.t("play.smooth.keep"), lambda p=verdict.path: self.smooth_keep(p), kind="secondary").pack(
            side="left")
        button(row, self.t("play.smooth.remove"), lambda p=verdict.path: self.smooth_stop(p), kind="ghost").pack(
            side="left", padx=(px(self.root, 8), 0))

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
        fb = self.core.skip_proposal()
        self.show_phase("prepare")
        if fb is not None:                     # e.g. a trial that did not help was still put back
            self._notify(fb)

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
            self._tick_map()
            if self.core.phase == "observe":
                self._tick_observe()
        finally:
            if not self._closed:
                self._job = self.root.after(TICK_MS, self._tick)

    def _map_line(self) -> str:
        """The map the analysis is judged against, in words (so the player can tell it was recognised)."""
        beatmap = self.core.beatmap
        return self.t("play.map.now", name=beatmap.display_name) if beatmap is not None \
            else self.t("play.prepare.map_missing")

    def _tick_map(self) -> None:
        """Once a second: did osu! load another map? (it names the map in its title only while playing)"""
        now = time.monotonic()
        if now - self._last_map_poll < MAP_POLL_S:
            return
        self._last_map_poll = now
        if self.core.track_map():
            self._on_map_changed()

    def _on_map_changed(self) -> None:
        core = self.core
        beatmap = core.beatmap
        name = beatmap.display_name if beatmap is not None else None
        if core.phase == "observe":
            self._map_note = self.t("play.map.changed", name=name) if name else self.t("play.map.lost")
            self._map_note_until = time.monotonic() + MAP_NOTE_S
            if self.map is not None:
                self.map.clear_play()                 # the dots belonged to the previous map
            self._observe_var.set(self._map_note)
        elif core.phase in ("prepare", "report") and not (self.keys_card is not None and self.keys_card.listening):
            self.show_phase(core.phase)               # redraw: the map line (and the start button) follow the new map

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
            if time.monotonic() < self._map_note_until:
                text = self._map_note
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
