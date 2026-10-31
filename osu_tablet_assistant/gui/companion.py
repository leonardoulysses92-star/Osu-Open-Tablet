"""Companion / live-tuning mode: tune OTD while osu! is open.

Three layers:

* :class:`CompanionCore` (no Tk) wires ``otd.livetune.LiveTuner`` (live
  sends, undo, Keep), ``engine.adapt`` (intent -> parameter steps) and
  ``engine.passive`` (cursor statistics per setting). Fully testable.
* :class:`Overlay`: a tiny always-on-top, click-through, non-activating
  window that shows the last change and fades out (hidden by exclusive
  fullscreen, hence the beeps from ``gui.hotkeys``).
* :class:`CompanionWindow`: a normal window with big buttons, a quick
  "how did that map feel?" poll, policy selector, live numbers, Undo/Keep.

Integration (wizard)::

    from osu_tablet_assistant.gui.companion import open_companion
    open_companion(root, session)          # session: gui.session.Session

or with explicit dependencies (all optional except settings/tablet_name)::

    CompanionWindow(parent, settings=live_settings_dict, tablet_name="Wacom CTL-472",
                    tablet=tablet_spec, display=display_spec, controller=otd_controller,
                    config_dir=config_dir, translate=i18n.t, available_filters=paths)
"""
from __future__ import annotations

import copy
import queue
import sys
import time
import tkinter as tk
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from ..engine import adapt
from ..engine.adapt import AdaptState, Intent, PolicyMode, Suggestion, TuningPolicy
from ..engine.passive import PassiveMonitor
from ..knowledge.filters import FilterSpec, load_catalog
from ..models import DisplaySpec, Reason, TabletSpec
from ..otd.livetune import (LiveTuner, OsuWatcher, SendOutcome, controller_sender, dry_run_sender)
from . import hotkeys as hk
from .i18n import t as default_t
from .palette import COLORS, mix
from .widgets import (FONT_FAMILY, Card, apply_theme, button, check, dark_title_bar, font, frame, label, px, radio,
                      segmented, surface_color)

BG, SURFACE, FG, MUTED, ACCENT, WARN = COLORS.bg, COLORS.surface, COLORS.text, COLORS.muted, COLORS.accent, COLORS.warn
FONT = FONT_FAMILY
TICK_MS = 40
SAMPLE_DRAIN_S = 0.5
HINT_REFRESH_S = 2.0
WATCH_INTERVAL_S = 3.0
INTENT_ACTIONS = ("smoother", "responsive", "precise", "looser", "area_bigger", "area_smaller")

Translate = Callable[..., str]


def say(reason: Reason, translate: Translate = default_t) -> str:
    """Translate a Reason, also translating ``axis``/``strength`` parameter values."""
    params = dict(reason.params)
    if "axis" in params:
        params["axis"] = translate(f"adapt.axis.{params['axis']}")
    if "strength" in params:
        params["strength"] = translate(f"passive.strength.{params['strength']}")
    return translate(reason.key, **params)


# --------------------------------------------------------------------------
# Core logic (no Tk)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Feedback:
    action: str
    ok: bool
    sound: str
    reasons: tuple[Reason, ...] = ()
    suggestions: tuple[Suggestion, ...] = ()
    skipped: tuple[Reason, ...] = ()
    converged: bool = False
    sent: SendOutcome | None = None


@dataclass(frozen=True)
class LiveValue:
    filter_name: str
    param: str
    value: Any
    unit: str


@dataclass(frozen=True)
class CompanionStatus:
    changes: int
    dirty: bool
    pending: bool
    converged: bool
    positions: dict[str, float]
    gains: dict[str, float]
    values: tuple[LiveValue, ...]
    area: tuple[float, float, float]           # width mm, height mm, rotation
    accepted: int


class CompanionCore:
    """Intent handling on top of a :class:`LiveTuner`; the adaptation state is the tuner's meta."""

    def __init__(self, tuner: LiveTuner, *, policy: TuningPolicy | None = None,
                 catalog: Mapping[str, FilterSpec] | None = None, monitor: PassiveMonitor | None = None,
                 display: DisplaySpec | None = None) -> None:
        self.tuner = tuner
        self.policy = policy or TuningPolicy()
        self.catalog = dict(catalog) if catalog is not None else load_catalog()
        self.display = display
        if not isinstance(tuner.meta, AdaptState):
            tuner.replace_meta(AdaptState())
        self.monitor = monitor if monitor is not None else PassiveMonitor(self.mm_per_px())
        self.last: Feedback | None = None

    @property
    def state(self) -> AdaptState:
        return self.tuner.meta

    def mm_per_px(self) -> float:
        """Tablet mm per screen px of the live profile (for passive stats)."""
        prof = self.tuner.profile()
        width_px = prof.display.width if prof.display.width > 0 else (
            self.display.width_px if self.display else 1920.0)
        width_mm = prof.tablet_area.width if prof.tablet_area.width > 0 else (
            self.tuner.tablet.width_mm if self.tuner.tablet else 100.0)
        return width_mm / width_px

    def set_policy(self, mode: PolicyMode | str) -> None:
        self.policy = TuningPolicy(PolicyMode(mode), self.policy.installed)

    def handle(self, action: str) -> Feedback:
        """Run one action: an intent name, "undo" or "keep"."""
        if action == "undo":
            fb = self.undo()
        elif action == "keep":
            fb = self.keep()
        else:
            fb = self._intent(Intent(action))
        self.last = fb
        return fb

    def _intent(self, intent: Intent) -> Feedback:
        action = intent.value
        if intent is Intent.AREA_BIGGER and self.tuner.tablet is None:
            return Feedback(action, False, "limit", (Reason("companion.area.need_tablet"),))
        was_converged = adapt.is_converged(self.state)
        res = adapt.step(self.state, self.tuner.profile(), intent, self.policy, self.catalog, self.tuner.tablet)
        if not res.moved:
            return Feedback(action, False, "limit", res.reasons, res.suggestions, res.skipped, res.converged)
        self.monitor.mark_change()
        pr = self.tuner.propose(res.changes, meta=res.state)   # type: ignore[arg-type]
        if not pr.accepted:
            return Feedback(action, False, "error", pr.reasons + res.reasons, res.suggestions, res.skipped)
        self.monitor.set_scale(self.mm_per_px())
        failed = pr.sent is not None and not pr.sent.ok
        converged = adapt.is_converged(self.state)
        sound = "error" if failed else ("converged" if converged and not was_converged else action)
        reasons = res.reasons + pr.reasons + ((Reason(pr.sent.message_key),) if failed and pr.sent else ())
        return Feedback(action, not failed, sound, reasons, res.suggestions, res.skipped, converged, pr.sent)

    def undo(self) -> Feedback:
        self.monitor.mark_change()
        pr = self.tuner.undo()
        self.monitor.set_scale(self.mm_per_px())
        failed = pr.sent is not None and not pr.sent.ok
        sound = "undo" if pr.accepted and not failed else "error"
        return Feedback("undo", pr.accepted and not failed, sound, pr.reasons, sent=pr.sent,
                        converged=adapt.is_converged(self.state))

    def keep(self) -> Feedback:
        kr = self.tuner.keep()
        if kr.ok:
            self.tuner.replace_meta(adapt.accept(self.state, self.tuner.profile()))
        params = {"path": str(kr.backup_path or ""), "detail": kr.detail}
        return Feedback("keep", kr.ok, "keep" if kr.ok else "error", (Reason(kr.message_key, params),),
                        converged=adapt.is_converged(self.state))

    def revert_all(self) -> Feedback:
        self.monitor.mark_change()
        pr = self.tuner.revert_all()
        self.monitor.set_scale(self.mm_per_px())
        return Feedback("revert", pr.accepted, "undo" if pr.accepted else "limit", pr.reasons, sent=pr.sent)

    def poll(self, motion: str | None, rest: str | None) -> list[Feedback]:
        """Quick-poll answer -> intents; all-good answers record the point as accepted."""
        intents = adapt.poll_to_intents(motion, rest)
        if not intents:
            self.tuner.replace_meta(adapt.accept(self.state, self.tuner.profile()))
            reasons: tuple[Reason, ...] = (Reason("companion.poll.accepted"),)
            if adapt.repeated_acceptance(self.state) or adapt.is_converged(self.state):
                reasons += (Reason("companion.poll.stop_here"),)
            fb = Feedback("poll", True, "keep", reasons, converged=adapt.is_converged(self.state))
            self.last = fb
            return [fb]
        return [self.handle(i.value) for i in intents]

    def pump(self) -> Feedback | None:
        """Flush a rate-limited send; returns Feedback only when a send failed (auto-reverted)."""
        out = self.tuner.pump()
        if out is None or out.ok:
            return None
        self.monitor.set_scale(self.mm_per_px())
        fb = Feedback("send", False, "error", (Reason(out.message_key), Reason("companion.reverted")), sent=out)
        self.last = fb
        return fb

    def status(self) -> CompanionStatus:
        prof = self.tuner.profile()
        values = []
        for path, name, value in adapt.tunable_values(prof):
            spec = self.catalog.get(path)
            param = spec.param(name) if spec else None
            values.append(LiveValue(spec.display_name if spec else path, name, value, param.unit if param else ""))
        st = self.state
        return CompanionStatus(
            changes=self.tuner.change_count, dirty=self.tuner.dirty, pending=self.tuner.pending,
            converged=adapt.is_converged(st),
            positions={a: st.axis(a).position for a in adapt.AXES},
            gains={a: st.axis(a).gain for a in adapt.AXES}, values=tuple(values),
            area=(prof.tablet_area.width, prof.tablet_area.height, prof.tablet_area.rotation),
            accepted=len(st.accepted))


def overlay_lines(fb: Feedback, status: CompanionStatus, translate: Translate = default_t) -> list[str]:
    """Up to three short lines for the overlay."""
    lines = [translate(f"companion.action.{fb.action}")]
    if fb.reasons:
        lines.append(say(fb.reasons[0], translate))
    tail = translate("companion.overlay.count", n=status.changes)
    if status.converged:
        tail += " · " + translate("companion.overlay.converged")
    elif status.dirty:
        tail += " · " + translate("companion.overlay.unsaved")
    lines.append(tail)
    return lines


# --------------------------------------------------------------------------
# Overlay
# --------------------------------------------------------------------------
WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x8, 0x20, 0x80
WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x80000, 0x08000000
GWL_EXSTYLE = -20


def make_click_through(win: tk.Misc) -> bool:
    """Give a Tk toplevel the no-activate / tool / click-through / topmost ex-styles (Windows)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style |= WS_EX_TOPMOST | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_NOACTIVATE
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        # HWND_TOPMOST, SWP_NOSIZE|SWP_NOMOVE|SWP_NOACTIVATE|SWP_FRAMECHANGED
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x1 | 0x2 | 0x10 | 0x20)
        return True
    except (AttributeError, OSError):
        return False


class Overlay:
    """Fading always-on-top status box. Stays mapped at alpha 0, so showing it never takes focus."""

    def __init__(self, master: tk.Misc, hold_ms: int = 2200, fade_ms: int = 600) -> None:
        self.hold_ms, self.fade_ms = hold_ms, fade_ms
        self.win = tk.Toplevel(master)
        self.win.overrideredirect(True)
        # A glass panel: surface fill and a hairline tinted with the accent (no thick neon frame).
        self.win.configure(bg=SURFACE, highlightthickness=1, highlightbackground=mix(COLORS.border_strong, ACCENT, 0.45),
                           highlightcolor=mix(COLORS.border_strong, ACCENT, 0.45))
        self.win.attributes("-topmost", True)
        self._set_alpha(0.0)
        self.label = tk.Label(self.win, text="", bg=SURFACE, fg=FG, font=(FONT, 11), justify="left", padx=14, pady=10)
        self.label.pack()
        sw = self.win.winfo_screenwidth()
        self.win.geometry(f"+{max(sw - 420, 0)}+24")
        self.win.update_idletasks()
        self.click_through = make_click_through(self.win)
        self._job: str | None = None
        self._alpha = 0.0

    def _set_alpha(self, value: float) -> None:
        try:
            self.win.attributes("-alpha", value)
        except tk.TclError:
            pass
        self._alpha = value

    def show(self, lines: Iterable[str]) -> None:
        self.label.configure(text="\n".join(lines))
        self._set_alpha(0.92)
        self._cancel()
        self._job = self.win.after(self.hold_ms, self._fade)

    def _fade(self) -> None:
        step = 0.92 / max(self.fade_ms / 50, 1)
        alpha = max(self._alpha - step, 0.0)
        self._set_alpha(alpha)
        self._job = self.win.after(50, self._fade) if alpha > 0 else None

    def _cancel(self) -> None:
        if self._job is not None:
            try:
                self.win.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def destroy(self) -> None:
        self._cancel()
        try:
            self.win.destroy()
        except tk.TclError:
            pass


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------
class CompanionWindow:
    """The companion window. Every OS-facing dependency can be injected."""

    def __init__(self, parent: tk.Misc | None, session: Any = None, *,
                 settings: dict | None = None, tablet_name: str | None = None,
                 tablet: TabletSpec | None = None, display: DisplaySpec | None = None,
                 controller: Any = None, config_dir: Path | None = None,
                 translate: Translate | None = None, available_filters: Iterable[str] | None = None,
                 policy: str = PolicyMode.TUNE_ONLY.value, dry_run: bool | None = None,
                 sender: Callable[[dict], Any] | None = None, tuner_options: Mapping[str, Any] | None = None,
                 sampler: Any = None, passive: bool = True, watcher: OsuWatcher | None = None,
                 enable_hotkeys: bool = True, bindings: Mapping[str, str] | None = None,
                 hotkey_backend: hk.HotkeyBackend | None = None, beeper: hk.Beeper | None = None,
                 overlay: bool = True, on_keep: Callable[[dict], None] | None = None,
                 on_close: Callable[[], None] | None = None) -> None:
        args = companion_args_from_session(session) if session is not None else {}
        settings = settings if settings is not None else args.get("settings")
        tablet_name = tablet_name or args.get("tablet_name")
        if settings is None or not tablet_name:
            raise ValueError("CompanionWindow needs settings and tablet_name (or a session)")
        tablet = tablet or args.get("tablet")
        display = display or args.get("display")
        controller = controller if controller is not None else args.get("controller")
        config_dir = config_dir or args.get("config_dir")
        available = set(available_filters if available_filters is not None else args.get("available_filters", ()))
        dry_run = bool(args.get("dry_run", False) if dry_run is None else dry_run)
        self.t: Translate = translate or default_t
        self.session = session
        self.on_keep, self.on_close = on_keep, on_close
        self.dry_run = dry_run

        if sender is None:
            sender = dry_run_sender if dry_run or controller is None else controller_sender(controller)
        self.tuner = LiveTuner(settings, tablet_name, sender, config_dir=config_dir, tablet=tablet,
                               can_keep=not dry_run, **dict(tuner_options or {}))
        self.core = CompanionCore(self.tuner, policy=TuningPolicy(PolicyMode(policy), frozenset(available)),
                                  display=display)
        self.watcher = watcher if watcher is not None else OsuWatcher(interval_s=WATCH_INTERVAL_S)
        self.beeper = beeper if beeper is not None else hk.Beeper()
        self._actions: queue.Queue[str] = queue.Queue()
        self.hotkeys = hk.HotkeyManager(dict(bindings or hk.DEFAULT_BINDINGS), self._actions.put,
                                        backend=hotkey_backend, use_default_backend=hotkey_backend is None)
        self.sampler = sampler
        if self.sampler is None and passive:
            from .pen_sampler import PenSampler
            self.sampler = PenSampler(poll_hz=1000.0)
        self._last_drain = self._last_hints = 0.0
        self._closed = False

        self.own_root = parent is None
        self.root = tk.Tk() if parent is None else tk.Toplevel(parent)
        if parent is None:
            apply_theme(self.root)
        self.root.title(self.t("companion.title"))
        self.root.configure(bg=BG)
        dark_title_bar(self.root)
        self.root.minsize(880, 600)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self.overlay = Overlay(self.root) if overlay else None
        if enable_hotkeys and self.hotkeys.available:
            self.hotkeys.start()
        if self.sampler is not None and getattr(self.sampler, "threaded", False):
            self.sampler.start()
        self.watcher.poll(force=True)
        self._refresh()
        self._job: str | None = self.root.after(TICK_MS, self._tick)

    # ----------------------------------------------------------------- build
    # role of each (size, colour) pair the window uses -> named font of the shared type scale
    _FONT_ROLE = {16: "title", 11: "heading", 10: "body", 9: "small", 8: "small"}

    def _label(self, parent: tk.Misc, key: str | None = None, size: int = 10, color: str = FG,
               bold: bool = False, var: tk.StringVar | None = None, wrap: int = 500) -> tk.Label:
        role = self._FONT_ROLE.get(size, "body")
        if bold and role in ("body", "small"):
            role = "strong" if role == "body" else "small_bold"
        lbl = tk.Label(parent, text=self.t(key) if key else "", textvariable=var, bg=surface_color(parent), fg=color,
                       font=font(role), justify="left", anchor="w", wraplength=px(parent, wrap))
        lbl.pack(fill="x", anchor="w")
        return lbl

    def _button(self, parent: tk.Misc, text: str, command: Callable[[], None], accent: bool = False,
                big: bool = False) -> ttk.Button:
        return button(parent, text, command, kind="primary" if accent else "tile" if big else "secondary")

    def _section(self, parent: tk.Misc, key: str) -> ttk.Frame:
        card = Card(parent, self.t(key), padding=14)
        card.pack(fill="x", pady=(0, px(parent, 10)))
        return card.body

    def _hint(self, action: str) -> str:
        text = self.hotkeys.bindings.get(action, "")
        return text.replace("+", " + ").title() if text else ""

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=(px(self.root, 20), px(self.root, 14)))
        outer.pack(fill="both", expand=True)
        self.body = outer
        self._label(outer, "companion.title", 16)
        self.var_game = tk.StringVar()
        self.var_live = tk.StringVar()
        self._label(outer, var=self.var_game, color=MUTED)
        self._label(outer, var=self.var_live, color=WARN)
        if self.dry_run:
            self._label(outer, "companion.dry_run", color=WARN)
        if self.session is not None and not self.own_root:
            self._button(outer, self.t("companion.open_coach"), self.open_coach).pack(anchor="w", pady=(6, 0))
        cols = frame(outer)
        cols.pack(fill="both", expand=True, pady=(px(outer, 10), 0))
        left = frame(cols)
        right = frame(cols)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, px(outer, 10)))
        right.grid(row=0, column=1, sticky="nsew")
        cols.columnconfigure(0, weight=1, uniform="col")
        cols.columnconfigure(1, weight=1, uniform="col")
        gap = px(outer, 3)

        feel = self._section(left, "companion.section.feel")
        grid = frame(feel)
        grid.pack(fill="x")
        self.intent_buttons: dict[str, ttk.Button] = {}
        for i, action in enumerate(("smoother", "responsive", "precise", "looser")):
            text = f"{self.t(f'companion.action.{action}')}\n{self._hint(action)}"
            b = self._button(grid, text, lambda a=action: self.do(a), big=True)
            b.grid(row=i // 2, column=i % 2, sticky="nsew", padx=gap, pady=gap)
            self.intent_buttons[action] = b
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        self._label(feel, "companion.feel.help", 9, MUTED)

        area = self._section(left, "companion.section.area")
        row = frame(area)
        row.pack(fill="x")
        for action in ("area_smaller", "area_bigger"):
            b = self._button(row, f"{self.t(f'companion.action.{action}')}  ({self._hint(action)})",
                             lambda a=action: self.do(a))
            b.pack(side="left", expand=True, fill="x", padx=gap)
            self.intent_buttons[action] = b
        self._label(area, "companion.area.help", 9, MUTED)

        save = frame(left)
        save.pack(fill="x", pady=(0, px(outer, 10)))
        self._button(save, f"{self.t('companion.action.undo')}  ({self._hint('undo')})",
                     lambda: self.do("undo")).pack(side="left", expand=True, fill="x", padx=gap)
        self.keep_button = self._button(save, f"{self.t('companion.action.keep')}  ({self._hint('keep')})",
                                        lambda: self.do("keep"), accent=True)
        self.keep_button.pack(side="left", expand=True, fill="x", padx=gap)
        self._button(save, self.t("companion.action.revert"), self.revert_all).pack(
            side="left", expand=True, fill="x", padx=gap)

        poll = self._section(left, "companion.section.poll")
        self.var_motion = tk.StringVar(value="")
        self.var_rest = tk.StringVar(value="")
        for var, options, prefix in ((self.var_motion, adapt.POLL_MOTION, "motion"),
                                     (self.var_rest, adapt.POLL_REST, "rest")):
            line = segmented(poll)
            line.pack(fill="x", pady=(0, px(outer, 6)))
            for opt in options:
                ttk.Radiobutton(line, text=self.t(f"companion.poll.{prefix}.{opt}"), variable=var, value=opt,
                                style="Segment.Toolbutton", takefocus=True).pack(side="left", expand=True, fill="x")
        self._button(poll, self.t("companion.poll.send"), self.send_poll).pack(fill="x", pady=(px(outer, 2), 0))

        live = self._section(right, "companion.section.live")
        self.var_values = tk.StringVar()
        self.var_last = tk.StringVar()
        self.var_hints = tk.StringVar()
        self._label(live, var=self.var_values, size=10, wrap=380)
        self._label(live, var=self.var_last, size=10, color=ACCENT, wrap=380)
        self._label(live, var=self.var_hints, size=9, color=MUTED, wrap=380)

        pol = self._section(right, "companion.section.policy")
        self.var_policy = tk.StringVar(value=self.core.policy.mode.value)
        for mode in PolicyMode:
            radio(pol, self.t(f"companion.policy.{mode.value}"), mode.value, self.var_policy,
                  command=self._policy_changed).pack(anchor="w")

        opts = self._section(right, "companion.section.options")
        self.var_sound = tk.BooleanVar(value=self.beeper.enabled)
        check(opts, self.t("companion.option.sound"), self.var_sound, command=self._sound_changed).pack(anchor="w")
        self.var_keys = tk.StringVar()
        self._label(opts, var=self.var_keys, size=9, color=MUTED, wrap=380)
        self.binding_vars: dict[str, tk.StringVar] = {}
        self.keys_frame = keys = frame(opts)
        self._button(opts, self.t("companion.hotkeys.edit"), self.toggle_bindings).pack(fill="x", pady=(4, 0))
        for i, action in enumerate(hk.ACTIONS):
            label(keys, self.t(f"companion.action.{action}"), "Small").grid(row=i, column=0, sticky="w")
            var = tk.StringVar(value=self.hotkeys.bindings.get(action, ""))
            ttk.Entry(keys, textvariable=var, width=22).grid(row=i, column=1, sticky="ew", padx=4, pady=1)
            self.binding_vars[action] = var
        keys.columnconfigure(1, weight=1)
        self._button(keys, self.t("companion.hotkeys.apply"), self.apply_bindings).grid(
            row=len(hk.ACTIONS), column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self._label(outer, "companion.limits", 8, MUTED, wrap=860)

    def toggle_bindings(self) -> None:
        """Show or hide the hotkey editor."""
        if self.keys_frame.winfo_ismapped():
            self.keys_frame.pack_forget()
        else:
            self.keys_frame.pack(fill="x", pady=(4, 0))

    # --------------------------------------------------------------- actions
    def do(self, action: str) -> Feedback:
        """Run an action (button or hotkey) and give feedback."""
        fb = self.core.handle(action)
        self._feedback(fb)
        return fb

    def _feedback(self, fb: Feedback) -> None:
        self.beeper.play(fb.sound)
        lines = [say(r, self.t) for r in fb.reasons] + [say(s.reason, self.t) for s in fb.suggestions]
        self.var_last.set("\n".join(lines))
        status = self.core.status()
        if self.overlay is not None:
            self.overlay.show(overlay_lines(fb, status, self.t))
        if fb.action == "keep" and fb.ok and self.on_keep is not None:
            self.on_keep(self.tuner.settings)
        self._refresh(status)

    def send_poll(self) -> None:
        motion, rest = self.var_motion.get() or None, self.var_rest.get() or None
        if motion is None and rest is None:
            return
        for fb in self.core.poll(motion, rest):
            self._feedback(fb)
        self.var_motion.set("")
        self.var_rest.set("")

    def open_coach(self) -> None:
        """Hand over to the Play Coach, starting from the settings that are live right now."""
        from .coach import open_coach
        parent = self.root.master
        live = self.tuner.settings
        self.destroy()
        open_coach(parent, self.session, settings=live, on_keep=self.on_keep)

    def revert_all(self) -> None:
        self._feedback(self.core.revert_all())

    def apply_bindings(self) -> None:
        if not self.hotkeys.available:
            return
        self.hotkeys.rebind({a: v.get().strip() for a, v in self.binding_vars.items()})
        self._refresh()

    def _policy_changed(self) -> None:
        self.core.set_policy(self.var_policy.get())

    def _sound_changed(self) -> None:
        self.beeper.enabled = bool(self.var_sound.get())

    # ------------------------------------------------------------------ loop
    def _tick(self) -> None:
        if self._closed:
            return
        try:
            while True:
                self.do(self._actions.get_nowait())
        except queue.Empty:
            pass
        fb = self.core.pump()
        if fb is not None:
            self._feedback(fb)
        now = time.monotonic()
        if self.sampler is not None and now - self._last_drain >= SAMPLE_DRAIN_S:
            self._last_drain = now
            self.core.monitor.feed(self.sampler.drain())
        if now - self._last_hints >= HINT_REFRESH_S:
            self._last_hints = now
            self.watcher.poll()
            self._refresh()
        self._job = self.root.after(TICK_MS, self._tick)

    def _refresh(self, status: CompanionStatus | None = None) -> None:
        st = status or self.core.status()
        proc = self.watcher.process
        self.var_game.set(self.t("companion.game.running", flavor=self.t(f"companion.flavor.{proc.flavor}"))
                          if proc else self.t("companion.game.not_running"))
        if st.dirty:
            live = self.t("companion.live.unsaved", n=st.changes)
        else:
            live = self.t("companion.live.saved", n=st.changes)
        if st.converged:
            live += "  " + self.t("companion.live.converged")
        self.var_live.set(live)
        lines = [f"{v.filter_name} · {v.param}: {adapt.fmt(v.value)} {v.unit}".rstrip() for v in st.values]
        lines.append(self.t("companion.live.area", w=adapt.fmt(st.area[0]), h=adapt.fmt(st.area[1]),
                            rot=adapt.fmt(st.area[2])))
        lines.append(self.t("companion.live.levels", motion=f"{st.positions['motion']:+.2f}",
                            rest=f"{st.positions['rest']:+.2f}", area=f"{st.positions['area']:+.2f}"))
        if not st.values:
            lines.insert(0, self.t("companion.live.nothing_tunable"))
        self.var_values.set("\n".join(lines))
        hints = self.core.monitor.hints()
        self.var_hints.set("\n".join([say(h, self.t) for h in hints] + [self.t("companion.hints.caveat")])
                           if hints else self.t("companion.hints.none"))
        if not self.hotkeys.available:
            keys = self.t("companion.hotkeys.unavailable")
        elif self.hotkeys.conflicts or self.hotkeys.errors:
            bad = ", ".join(self.t(f"companion.action.{a}") for a in
                            list(self.hotkeys.conflicts) + list(self.hotkeys.errors))
            keys = self.t("companion.hotkeys.conflict", actions=bad)
        else:
            keys = self.t("companion.hotkeys.ok")
        self.var_keys.set(keys)
        self.keep_button.configure(state="disabled" if self.dry_run else "normal")

    # --------------------------------------------------------------- closing
    def close(self) -> None:
        """Ask what to do with unsaved live changes, then shut down."""
        if self.tuner.dirty and not self.dry_run:
            answer = messagebox.askyesnocancel(self.t("companion.close.title"), self.t("companion.close.body"),
                                               parent=self.root)
            if answer is None:
                return
            if answer:
                fb = self.core.keep()
                self._feedback(fb)
                if not fb.ok:
                    return
            else:
                self.core.revert_all()
                self.tuner.flush()
        self.destroy()

    def destroy(self) -> None:
        """Stop threads and remove the windows (no questions asked)."""
        if self._closed:
            return
        self._closed = True
        if self._job is not None:
            try:
                self.root.after_cancel(self._job)
            except tk.TclError:
                pass
        self.hotkeys.stop()
        if self.sampler is not None and hasattr(self.sampler, "stop"):
            self.sampler.stop()
        self.beeper.close()
        if self.overlay is not None:
            self.overlay.destroy()
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        if self.on_close is not None:
            self.on_close()


def companion_args_from_session(session: Any) -> dict[str, Any]:
    """Constructor arguments taken from a ``gui.session.Session`` (public attributes only).

    The settings are the ones live in OTD right now: the applied final
    settings, else the candidate currently under test, else the original.
    """
    base = getattr(session, "settings", None)
    profile = getattr(session, "profile", None)
    tablet_name = profile.tablet_name if profile is not None else None
    live = base
    try:
        if getattr(session, "applied", None) is not None:
            live = session.final_settings()
        elif getattr(session, "live_candidate_id", None) and base is not None and tablet_name:
            cand = session.candidate(session.live_candidate_id)
            live = session.services.apply_changes(base, tablet_name, cand.changes)
    except Exception:   # fall back to what was read from settings.json
        live = base
    services = getattr(session, "services", None)
    controller = None
    if services is not None and hasattr(services, "make_controller"):
        try:
            controller = services.make_controller(getattr(session, "install_dir", None),
                                                  getattr(session, "config_dir", None))
        except Exception:
            controller = None
    return {"settings": copy.deepcopy(live) if live is not None else None, "tablet_name": tablet_name,
            "tablet": getattr(session, "tablet", None), "display": getattr(session, "display", None),
            "controller": controller, "config_dir": getattr(session, "config_dir", None),
            "available_filters": set(getattr(session, "filter_paths", ()) or ()),
            "dry_run": bool(getattr(session, "dry_run", False))}


def open_companion(parent: tk.Misc | None, session: Any = None, **kwargs: Any) -> CompanionWindow:
    """Open the companion window (see the module docstring for the integration call)."""
    return CompanionWindow(parent, session, **kwargs)
