"""Companion / live-tuning mode: tune OTD while osu! is open.

Three layers:

* :class:`CompanionCore` (no Tk) wires ``otd.livetune.LiveTuner`` (live
  sends, undo, Keep), ``engine.adapt`` (intent -> parameter steps) and
  ``engine.passive`` (cursor statistics per setting). Fully testable.
* :class:`Overlay`: a tiny always-on-top, click-through, non-activating
  window that shows the last change and fades out (hidden by exclusive
  fullscreen, hence the beeps from ``gui.hotkeys``).
* :class:`CompanionWindow`: a normal window with a compact shortcut panel
  (small buttons that show their key combination, the step-size selector,
  the last change and a "Shortcuts" editor with Detect / Reset), a quick
  "how did that map feel?" poll, policy selector, live numbers, Undo/Keep.
  Hotkeys and step size are saved in the assistant's config.

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
from .widgets import (FONT_FAMILY, Card, Columns, ScrollFrame, WrapLabel, apply_theme, button, check,
                      dark_title_bar, font, frame, label, px, radio, segmented, surface_color)

BG, SURFACE, FG, MUTED, ACCENT, WARN = COLORS.bg, COLORS.surface, COLORS.text, COLORS.muted, COLORS.accent, COLORS.warn
FONT = FONT_FAMILY
TICK_MS = 40
SAMPLE_DRAIN_S = 0.5
HINT_REFRESH_S = 2.0
WATCH_INTERVAL_S = 3.0
COLUMNS_BREAKPOINT = 820          # px of content width for the two lower columns
INTENT_ACTIONS = ("smoother", "responsive", "precise", "looser", "area_bigger", "area_smaller")

Translate = Callable[..., str]


def say(reason: Reason, translate: Translate = default_t) -> str:
    """Translate a Reason, also translating ``axis``/``strength``/``level`` parameter values."""
    params = dict(reason.params)
    if "axis" in params:
        params["axis"] = translate(f"adapt.axis.{params['axis']}")
    if "strength" in params:
        params["strength"] = translate(f"passive.strength.{params['strength']}")
    if "level" in params:
        params["level"] = translate(f"companion.step.{params['level']}")
    return translate(reason.key, **params)


def action_label(action: str, level: str = adapt.DEFAULT_STEP_LEVEL, translate: Translate = default_t) -> str:
    """Plain name of an action; the area actions show the percentage of the current step level."""
    return translate(f"companion.action.{action}", pct=adapt.fmt(adapt.area_step_pct(level)))


def step_hint(level: str, translate: Translate = default_t) -> str:
    """"Fine (about 0.4x, area 1%)" for the status line."""
    return translate("companion.step.with_hint", level=translate(f"companion.step.{level}"),
                     hint=translate(f"companion.step.hint.{level}", pct=adapt.fmt(adapt.area_step_pct(level))))


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
    step_level: str = adapt.DEFAULT_STEP_LEVEL


class CompanionCore:
    """Intent handling on top of a :class:`LiveTuner`; the adaptation state is the tuner's meta.

    ``step_level`` (normal / fine / very_fine) is a view setting, not part of the undo history: undo
    restores values, never the step size.
    """

    def __init__(self, tuner: LiveTuner, *, policy: TuningPolicy | None = None,
                 catalog: Mapping[str, FilterSpec] | None = None, monitor: PassiveMonitor | None = None,
                 display: DisplaySpec | None = None, step_level: str | None = None) -> None:
        self.tuner = tuner
        self.step_level = adapt.step_level(step_level)
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

    def set_step_level(self, level: str) -> Feedback:
        """Choose the step size of the next moves (raises ValueError for an unknown level)."""
        self.step_level = adapt.step_level(level)
        fb = Feedback("step", True, "step", (Reason("companion.step.now", {"level": self.step_level}),),
                      converged=adapt.is_converged(self.state))
        self.last = fb
        return fb

    def cycle_step(self) -> Feedback:
        """Normal -> fine -> very fine -> normal (the "step" hotkey)."""
        return self.set_step_level(adapt.next_step_level(self.step_level))

    def handle(self, action: str) -> Feedback:
        """Run one action: an intent name, "undo", "keep" or "step" (next step size)."""
        if action == "undo":
            fb = self.undo()
        elif action == "keep":
            fb = self.keep()
        elif action == "step":
            fb = self.cycle_step()
        else:
            fb = self._intent(Intent(action))
        self.last = fb
        return fb

    def _intent(self, intent: Intent) -> Feedback:
        action = intent.value
        if intent is Intent.AREA_BIGGER and self.tuner.tablet is None:
            return Feedback(action, False, "limit", (Reason("companion.area.need_tablet"),))
        was_converged = adapt.is_converged(self.state)
        res = adapt.step(self.state, self.tuner.profile(), intent, self.policy, self.catalog, self.tuner.tablet,
                         level=self.step_level)
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
            accepted=len(st.accepted), step_level=self.step_level)


CHANGE_KEYS = ("adapt.change", "adapt.area.change", "adapt.enabled", "adapt.added")


def last_change(fb: Feedback, translate: Translate = default_t) -> str | None:
    """The value change of ``fb`` in one line ("Radial Follow · OuterRadius: 0.6 -> 0.62 mm"), or None."""
    changes = [say(r, translate) for r in fb.reasons if r.key in CHANGE_KEYS]
    return "; ".join(changes) if changes and fb.ok else None


def overlay_lines(fb: Feedback, status: CompanionStatus, translate: Translate = default_t) -> list[str]:
    """Up to three short lines for the overlay; the last one always names the step size in use."""
    lines = [action_label(fb.action, status.step_level, translate)]
    if fb.reasons:
        lines.append(say(fb.reasons[0], translate))
    tail = translate("companion.overlay.count", n=status.changes)
    tail += " · " + translate("companion.overlay.step", level=translate(f"companion.step.{status.step_level}"))
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
                 on_close: Callable[[], None] | None = None, step_level: str | None = None,
                 config: Any = None, save_config: Callable[[], Any] | None = None) -> None:
        args = companion_args_from_session(session) if session is not None else {}
        # The assistant's own config (appconfig.AppConfig): saved hotkeys and step size survive restarts.
        self.config = config if config is not None else getattr(session, "app_config", None)
        if save_config is None and session is not None and hasattr(session, "save_app_config"):
            save_config = session.save_app_config
        self._persist = save_config
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
        if step_level is None:
            saved = getattr(self.config, "companion_step", None)
            step_level = saved if saved in adapt.STEP_LEVELS else adapt.DEFAULT_STEP_LEVEL
        self.core = CompanionCore(self.tuner, policy=TuningPolicy(PolicyMode(policy), frozenset(available)),
                                  display=display, step_level=step_level)
        self.watcher = watcher if watcher is not None else OsuWatcher(interval_s=WATCH_INTERVAL_S)
        self.beeper = beeper if beeper is not None else hk.Beeper()
        self._actions: queue.Queue[str] = queue.Queue()
        if bindings is None:
            saved_keys = getattr(self.config, "companion_hotkeys", None)
            bindings = hk.clean_bindings(saved_keys) if saved_keys is not None else hk.DEFAULT_BINDINGS
        self.hotkeys = hk.HotkeyManager(dict(bindings), self._actions.put,
                                        backend=hotkey_backend, use_default_backend=hotkey_backend is None)
        self._hotkeys_enabled = enable_hotkeys
        self._detecting: str | None = None
        self._last_change = ""
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
        self.root.minsize(px(self.root, 560), px(self.root, 420))
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        # A fixed size from the start: a toplevel that follows its requested size lets wrapping labels and
        # the reflowing grids feed back on each other (the window kept growing and froze). The content
        # scrolls vertically and reflows to the width instead.
        width = min(px(self.root, 900), self.root.winfo_screenwidth() - 40)
        height = min(px(self.root, 820), self.root.winfo_screenheight() - 80)
        self.root.geometry(f"{max(width, 560)}x{max(height, 420)}")
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
        # Starts narrow and re-wraps to the width it is given, so long lines never widen the window.
        lbl = tk.Label(parent, text=self.t(key) if key else "", textvariable=var, bg=surface_color(parent), fg=color,
                       font=font(role), justify="left", anchor="w", wraplength=px(parent, min(wrap, 300)))
        lbl.pack(fill="x", anchor="w")
        lbl.bind("<Configure>", self._rewrap, add="+")
        return lbl

    @staticmethod
    def _rewrap(event: Any) -> None:
        wrap = max(int(event.width) - 8, 80)
        try:
            if abs(float(str(event.widget.cget("wraplength"))) - wrap) > 1:
                event.widget.configure(wraplength=wrap)
        except (tk.TclError, ValueError):
            pass

    def _button(self, parent: tk.Misc, text: str, command: Callable[[], None], accent: bool = False,
                big: bool = False) -> ttk.Button:
        return button(parent, text, command, kind="primary" if accent else "tile" if big else "secondary")

    def _section(self, parent: tk.Misc, key: str) -> ttk.Frame:
        card = Card(parent, self.t(key), padding=14)
        card.pack(fill="x", pady=(0, px(parent, 10)))
        return card.body

    def _hint(self, action: str) -> str:
        """The key combination of ``action`` as the player reads it ("Ctrl+Alt+Up"), or "no key"."""
        text = self.hotkeys.bindings.get(action, "")
        return hk.display_hotkey(text) if text else self.t("companion.hotkeys.none")

    def _action_text(self, action: str) -> str:
        return action_label(action, self.core.step_level, self.t)

    # Compact shortcut panel: what each key does, 4 small buttons per row (click = the same as the hotkey).
    PANEL_ROWS = (("smoother", "responsive", "precise", "looser"), ("area_smaller", "area_bigger", "undo", "keep"))

    def _build(self) -> None:
        # Everything scrolls vertically and reflows to the window width (nothing is cut on the right).
        self.scroll = ScrollFrame(self.root)
        self.scroll.pack(fill="both", expand=True)
        outer = ttk.Frame(self.scroll.body, padding=(px(self.root, 16), px(self.root, 10)))
        outer.pack(fill="both", expand=True)
        self.body = outer
        self._label(outer, "companion.title", 11)
        self.var_game = tk.StringVar()
        self.var_live = tk.StringVar()
        self._label(outer, var=self.var_game, size=9, color=MUTED)
        self._label(outer, var=self.var_live, size=9, color=WARN)
        if self.dry_run:
            self._label(outer, "companion.dry_run", 9, WARN)
        if self.session is not None and not self.own_root:
            self._button(outer, self.t("companion.open_coach"), self.open_coach).pack(anchor="w", pady=(4, 0))
        self._build_panel(outer)
        self._build_keys_editor(outer)

        # two columns on a wide window, one below the breakpoint
        cols = Columns(outer, breakpoint=px(outer, COLUMNS_BREAKPOINT), gap=10)
        self.columns = cols
        cols.pack(fill="x")
        left = frame(cols)
        right = frame(cols)
        cols.add(left)
        cols.add(right)

        live = self._section(left, "companion.section.live")
        self.var_values = tk.StringVar()
        self.var_last = tk.StringVar()
        self.var_hints = tk.StringVar()
        self._label(live, var=self.var_values, size=9)
        self._label(live, var=self.var_last, size=9, color=ACCENT)
        self._label(live, var=self.var_hints, size=9, color=MUTED)
        self._button(live, self.t("companion.action.revert"), self.revert_all).pack(anchor="w", pady=(6, 0))

        poll = self._section(left, "companion.section.poll")
        self.var_motion = tk.StringVar(value="")
        self.var_rest = tk.StringVar(value="")
        for var, options, prefix in ((self.var_motion, adapt.POLL_MOTION, "motion"),
                                     (self.var_rest, adapt.POLL_REST, "rest")):
            line = segmented(poll)
            line.pack(fill="x", pady=(0, px(outer, 4)))
            for opt in options:
                ttk.Radiobutton(line, text=self.t(f"companion.poll.{prefix}.{opt}"), variable=var, value=opt,
                                style="Segment.Toolbutton", takefocus=True).pack(side="left", expand=True, fill="x")
        self._button(poll, self.t("companion.poll.send"), self.send_poll).pack(fill="x", pady=(px(outer, 2), 0))

        pol = self._section(right, "companion.section.policy")
        self.var_policy = tk.StringVar(value=self.core.policy.mode.value)
        for mode in PolicyMode:   # a radio with no text plus a wrapping label: long choices never widen the card
            row = frame(pol)
            row.pack(fill="x", anchor="w")
            radio(row, "", mode.value, self.var_policy, command=self._policy_changed).pack(side="left", anchor="n")
            text = WrapLabel(row, self.t(f"companion.policy.{mode.value}"), "Body")
            text.pack(side="left", fill="x", expand=True)
            text.bind("<Button-1>", lambda _e, m=mode.value: (self.var_policy.set(m), self._policy_changed()))

        opts = self._section(right, "companion.section.options")
        self.var_sound = tk.BooleanVar(value=self.beeper.enabled)
        check(opts, self.t("companion.option.sound"), self.var_sound, command=self._sound_changed).pack(anchor="w")
        self.var_keys = tk.StringVar()
        self._label(opts, var=self.var_keys, size=9, color=MUTED)
        self._label(outer, "companion.limits", 8, MUTED)

    def _build_panel(self, outer: tk.Misc) -> None:
        """The compact shortcut strip: step selector, "Shortcuts" button, 8 small buttons, one status line."""
        card = Card(outer, self.t("companion.section.keys"), padding=12)
        card.pack(fill="x", pady=(px(outer, 8), px(outer, 8)))
        self.panel = card
        bar = frame(card.body)
        bar.pack(fill="x", pady=(0, px(outer, 4)))
        label(bar, self.t("companion.step.title"), "Small").pack(side="left", padx=(0, px(outer, 6)))
        self.var_step_level = tk.StringVar(value=self.core.step_level)
        seg = segmented(bar)
        seg.pack(side="left")
        self.step_buttons: dict[str, ttk.Radiobutton] = {}
        for level in adapt.STEP_LEVELS:
            rb = ttk.Radiobutton(seg, text=self.t(f"companion.step.{level}"), variable=self.var_step_level,
                                 value=level, style="Segment.Toolbutton", takefocus=True,
                                 command=lambda: self.set_step(self.var_step_level.get()))
            rb.pack(side="left")
            self.step_buttons[level] = rb
        self.keys_toggle = button(bar, self.t("companion.hotkeys.button"), self.toggle_bindings, kind="ghost")
        self.keys_toggle.pack(side="right")
        self.panel_grid = grid = frame(card.body)
        grid.pack(fill="x")
        self.intent_buttons: dict[str, ttk.Button] = {}
        for row in self.PANEL_ROWS:
            for action in row:
                b = button(grid, "", lambda a=action: self.do(a), kind="primary" if action == "keep" else "secondary")
                self.intent_buttons[action] = b
        self.keep_button = self.intent_buttons["keep"]
        self._panel_cols = 0
        self.var_step = tk.StringVar()
        self.var_change = tk.StringVar()
        self._label(card.body, var=self.var_step, size=9, color=ACCENT, bold=True).pack_configure(
            pady=(px(outer, 4), 0))
        self._label(card.body, var=self.var_change, size=9)
        self._refresh_buttons()
        self._layout_panel(4)
        card.body.bind("<Configure>", lambda e: self._layout_panel(self._panel_columns_for(e.width)), add="+")

    def _panel_columns_for(self, width: int) -> int:
        """4 buttons per row when they fit at their natural width, else 2."""
        gap = px(self.body, 2)
        need = 4 * max(b.winfo_reqwidth() for b in self.intent_buttons.values()) + 8 * gap
        return 4 if width >= need else 2

    def _layout_panel(self, cols: int) -> None:
        if cols == self._panel_cols:
            return
        self._panel_cols = cols
        gap = px(self.body, 2)
        order = [a for row in self.PANEL_ROWS for a in row]
        for i, action in enumerate(order):
            self.intent_buttons[action].grid(row=i // cols, column=i % cols, sticky="nsew", padx=gap, pady=gap)
        for c in range(4):
            self.panel_grid.columnconfigure(c, weight=1 if c < cols else 0, uniform="key" if c < cols else "")

    def _build_keys_editor(self, outer: tk.Misc) -> None:
        """The hotkey editor (hidden until "Shortcuts" is pressed): Detect, type, apply, reset."""
        card = Card(outer, self.t("companion.hotkeys.title"), padding=12)
        self.keys_frame = card
        self.keys_open = False
        WrapLabel(card.body, self.t("companion.hotkeys.intro"), "Small").pack(fill="x")
        self.keys_rows = rows = frame(card.body)
        rows.pack(fill="x", pady=(px(outer, 4), px(outer, 4)))
        self.binding_vars: dict[str, tk.StringVar] = {}
        self.binding_labels: dict[str, ttk.Label] = {}
        self.binding_entries: dict[str, ttk.Entry] = {}
        self.detect_buttons: dict[str, ttk.Button] = {}
        for action in hk.ACTIONS:
            self.binding_labels[action] = label(rows, self._action_text(action), "Small")
            var = tk.StringVar(value=self._display(self.hotkeys.bindings.get(action, "")))
            self.binding_entries[action] = ttk.Entry(rows, textvariable=var, width=16)
            self.detect_buttons[action] = button(rows, self.t("companion.hotkeys.detect"),
                                                 lambda a=action: self.toggle_detect(a), kind="ghost")
            self.binding_vars[action] = var
        self._editor_groups = 0
        self._layout_editor(1)
        line = frame(card.body)
        line.pack(fill="x")
        button(line, self.t("companion.hotkeys.apply"), self.apply_bindings, kind="secondary").pack(side="left")
        button(line, self.t("companion.hotkeys.reset"), self.reset_bindings, kind="ghost").pack(
            side="left", padx=(px(outer, 6), 0))
        self.keys_status = WrapLabel(card.body, "", "Small")
        self.keys_status.pack(fill="x", pady=(px(outer, 4), 0))
        WrapLabel(card.body, self.t("companion.hotkeys.notes"), "Small").pack(fill="x")
        card.body.bind("<Configure>", lambda e: self._layout_editor(self._editor_groups_for(e.width)), add="+")

    def _editor_groups_for(self, width: int) -> int:
        """Two groups of rows side by side when they fit, else one column."""
        gap = px(self.body, 12)
        need = (max(w.winfo_reqwidth() for w in self.binding_labels.values())
                + max(w.winfo_reqwidth() for w in self.binding_entries.values())
                + max(w.winfo_reqwidth() for w in self.detect_buttons.values()) + px(self.body, 8))
        return 2 if width >= 2 * need + gap else 1

    def _layout_editor(self, groups: int) -> None:
        if groups == self._editor_groups:
            return
        self._editor_groups = groups
        per = (len(hk.ACTIONS) + groups - 1) // groups
        pad = px(self.body, 4)
        for i, action in enumerate(hk.ACTIONS):
            r, c = i % per, (i // per) * 3
            self.binding_labels[action].grid(row=r, column=c, sticky="w", padx=(px(self.body, 12) if c else 0, pad))
            self.binding_entries[action].grid(row=r, column=c + 1, sticky="ew", pady=1)
            self.detect_buttons[action].grid(row=r, column=c + 2, sticky="w", padx=(px(self.body, 2), 0))
        for c in range(6):
            self.keys_rows.columnconfigure(c, weight=1 if c in (1, 4) and c < 3 * groups else 0)

    @staticmethod
    def _display(text: str) -> str:
        return hk.display_hotkey(text) if text else ""

    def _refresh_buttons(self) -> None:
        """Button texts follow the current bindings and step level (the area shows its percentage)."""
        for action, b in self.intent_buttons.items():
            b.configure(text=f"{self._action_text(action)}\n{self._hint(action)}")
        for action, lbl in getattr(self, "binding_labels", {}).items():
            lbl.configure(text=self._action_text(action))

    def toggle_bindings(self) -> None:
        """Show or hide the hotkey editor (the "Shortcuts" button). The window scrolls; it never resizes itself."""
        if self.keys_open:
            self.stop_detect()
            self.keys_open = False
            self.keys_frame.pack_forget()
            self.keys_toggle.configure(text=self.t("companion.hotkeys.button"))
            return
        self.keys_open = True
        self.keys_frame.pack(fill="x", after=self.panel, pady=(0, px(self.body, 8)))
        self.keys_toggle.configure(text=self.t("companion.hotkeys.close"))

    # -------------------------------------------------------------- hotkeys
    def _keys_say(self, key: str, **params: Any) -> None:
        self.keys_status.configure(text=self.t(key, **params) if key else "")

    def toggle_detect(self, action: str) -> None:
        if self._detecting == action:
            self.stop_detect()
            self._keys_say("")
        else:
            self.start_detect(action)

    def start_detect(self, action: str) -> None:
        """Read the next key combination pressed in THIS window (Tk bindings) for ``action``.

        The global hotkeys are paused meanwhile, because a combination registered by this program is
        delivered as a hotkey and would never reach the window.
        """
        if not self.keys_open:
            self.toggle_bindings()
        if self._detecting is None:
            self.hotkeys.stop()
            self.root.bind("<KeyPress>", self.feed_key)
        self._detecting = action
        try:
            self.keys_frame.focus_set()        # so typing does not go into an entry and Space/Enter press nothing
        except tk.TclError:
            pass
        self._keys_say("companion.hotkeys.listening", action=self._action_text(action))
        self._refresh_detect_buttons()

    def stop_detect(self, restart: bool = True) -> None:
        if self._detecting is None:
            return
        self._detecting = None
        try:
            self.root.unbind("<KeyPress>")
        except tk.TclError:
            pass
        if restart:
            self._start_hotkeys()
        self._refresh_detect_buttons()

    def _start_hotkeys(self) -> None:
        if self._hotkeys_enabled and self.hotkeys.available and not self._closed:
            self.hotkeys.start()

    def _refresh_detect_buttons(self) -> None:
        for action, b in self.detect_buttons.items():
            b.configure(text=self.t("companion.hotkeys.cancel" if action == self._detecting
                                    else "companion.hotkeys.detect"))

    def feed_key(self, event: Any) -> str | None:
        """One Tk key press while detecting: a free combination is taken and applied at once."""
        action = self._detecting
        if action is None:
            return None
        combo, status = hk.combo_from_tk_event(event)
        if status == "escape":
            self.stop_detect()
            self._keys_say("")
        elif status == "no_modifier":
            self._keys_say("companion.hotkeys.need_modifier")
        elif status == "unknown":
            self._keys_say("companion.hotkeys.unknown_key")
        elif status == "ok" and combo:
            wanted = {a: v.get().strip() for a, v in self.binding_vars.items()}
            other = hk.conflict_with(wanted, action, combo)
            if other is not None:
                self._keys_say("companion.hotkeys.taken", combo=hk.display_hotkey(combo),
                               other=self._action_text(other))
                return "break"
            self.binding_vars[action].set(hk.display_hotkey(combo))
            self.stop_detect(restart=False)
            if self.apply_bindings():
                self._keys_say("companion.hotkeys.saved", action=self._action_text(action),
                               combo=hk.display_hotkey(combo))
        return "break"

    def apply_bindings(self) -> bool:
        """Use the combinations in the editor; nothing changes while one is invalid or used twice."""
        self.stop_detect(restart=False)
        wanted = {a: v.get().strip() for a, v in self.binding_vars.items()}
        good, errors = hk.parse_bindings(wanted)
        if errors:
            dup = [a for a, e in errors.items() if e.startswith("duplicate")]
            bad = [a for a in errors if a not in dup]
            names = ", ".join(self._action_text(a) for a in (bad or dup))
            self._keys_say("companion.hotkeys.invalid" if bad else "companion.hotkeys.duplicate", actions=names)
            self._start_hotkeys()
            return False
        chosen = {a: good[a].text if a in good else "" for a in hk.ACTIONS}
        self.hotkeys.set_bindings(chosen)
        self._start_hotkeys()
        for action, var in self.binding_vars.items():
            var.set(self._display(chosen[action]))
        self._save(lambda cfg: setattr(cfg, "companion_hotkeys", dict(chosen)))
        self._keys_say("companion.hotkeys.applied")
        self._refresh_buttons()
        self._refresh()
        return True

    def reset_bindings(self) -> None:
        """Back to the default combinations."""
        for action, var in self.binding_vars.items():
            var.set(self._display(hk.DEFAULT_BINDINGS.get(action, "")))
        if self.apply_bindings():
            self._keys_say("companion.hotkeys.reset_done")

    def _save(self, change: Callable[[Any], None]) -> None:
        """Change the assistant's config and save it; a failure never breaks the companion."""
        if self.config is None:
            return
        try:
            change(self.config)
            if self._persist is not None:
                self._persist()
        except Exception:            # saving is a convenience: the choice still applies to this session
            pass

    # --------------------------------------------------------------- actions
    def do(self, action: str) -> Feedback:
        """Run an action (button or hotkey) and give feedback."""
        fb = self.core.handle(action)
        if action == "step":
            self._step_changed()
        self._feedback(fb)
        return fb

    def set_step(self, level: str) -> Feedback:
        """The step selector: choose normal / fine / very fine."""
        fb = self.core.set_step_level(level)
        self._step_changed()
        self._feedback(fb)
        return fb

    def _step_changed(self) -> None:
        level = self.core.step_level
        self.var_step_level.set(level)
        self._save(lambda cfg: setattr(cfg, "companion_step", level))
        self._refresh_buttons()

    def _feedback(self, fb: Feedback) -> None:
        self.beeper.play(fb.sound)
        lines = [say(r, self.t) for r in fb.reasons] + [say(s.reason, self.t) for s in fb.suggestions]
        self.var_last.set("\n".join(lines))
        change = last_change(fb, self.t)
        if change is not None:
            self._last_change = change
        elif fb.action in ("undo", "revert") and fb.ok:
            self._last_change = self.t("companion.last.undone")
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

    def _policy_changed(self) -> None:
        self.core.set_policy(self.var_policy.get())

    def _sound_changed(self) -> None:
        self.beeper.enabled = bool(self.var_sound.get())

    # ------------------------------------------------------------------ loop
    def _tick(self) -> None:
        if self._closed:
            return
        if self._job is not None:            # called directly (tests): never leave a second timer behind
            try:
                self.root.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None
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
            bad = ", ".join(self._action_text(a) for a in
                            list(self.hotkeys.conflicts) + list(self.hotkeys.errors))
            keys = self.t("companion.hotkeys.conflict", actions=bad)
        else:
            keys = self.t("companion.hotkeys.ok")
        self.var_keys.set(keys)
        level = st.step_level
        step = step_hint(level, self.t)
        if self.hotkeys.bindings.get("step"):
            self.var_step.set(self.t("companion.step.status", step=step, combo=self._hint("step")))
        else:
            self.var_step.set(self.t("companion.step.status_nokey", step=step))
        if self.var_step_level.get() != level:
            self.var_step_level.set(level)
        self.var_change.set(self.t("companion.last.change", text=self._last_change) if self._last_change
                            else self.t("companion.last.none"))
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
