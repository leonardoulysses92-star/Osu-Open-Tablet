"""The wizard: a shell (stepper, Back/Next) plus one page per session step.

Pages only render ``Session`` state and forward user actions to it; anything
that may take time (talking to OTD, generating candidates) runs in a worker
thread through ``run_in_thread`` so the window never freezes.

Extension points (used by later features, safe to leave empty):

* :data:`SLOTS` - GUI-only steps shown between two session steps when their
  ``visible(session)`` is true. The "Pick a starting preset" slot is
  registered here and stays hidden while the session offers no presets.
* :data:`RESULT_SECTIONS` / :meth:`ResultsView.add_section` - extra cards on
  the result screen. The built-in "Community presets" comparison and the
  plugin suggestions are added the same way first (``view.builtin_sections``).
* :data:`COMPANION_LAUNCHER` - set it to a callable taking the ``ResultsView``
  and the result screen shows a "Companion / live tuning" button that calls it.
"""
from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from ..knowledge.tablets import normalize_name
from ..models import (
    ApplyResult,
    Candidate,
    MapPreference,
    MetricsReport,
    PlayStyle,
    PluginPolicy,
    Priority,
    SkillLevel,
)
from .coachcore import ledger_text
from .i18n import t, tr
from .session import (
    BASELINE_KINDS,
    CANDIDATE_KINDS,
    GOALS,
    Session,
    SessionError,
    Step,
    TabletChoice,
    candidate_label,
    comparison_for,
    describe_changes,
    filter_table,
    metric_lines,
    plain_verdict,
)
from .widgets import (
    COLORS,
    AreaPreview,
    BarChart,
    Card,
    Chip,
    Columns,
    Expander,
    InfoTip,
    Notice,
    ScrollFrame,
    Stepper,
    Tooltip,
    WrapLabel,
    bullets,
    button,
    check,
    font,
    frame,
    label,
    parse_float,
    px,
    radio,
    run_in_thread,
)

_GAP = 16          # vertical space between cards (96-dpi px)
_SHOWN_REASONS = 3  # engine reasons visible before "show all" on the result screen
_PAGE_MAX_WIDTH = 980


def error_text(exc: BaseException) -> tuple[str, str]:
    """User message and technical detail for any exception raised by the session."""
    if isinstance(exc, SessionError):
        return t(exc.key, **exc.params), exc.detail
    return t("core.error.unexpected"), f"{type(exc).__name__}: {exc}"


def error_with_detail(exc: BaseException) -> str:
    """The user message followed by its technical detail, for plain-text contexts."""
    message, detail = error_text(exc)
    return f"{message}\n{t('gui.details.title')}: {detail}" if detail else message


class Page(ttk.Frame):
    """Base class of a wizard page."""

    step: Step | None = None
    next_key = "gui.nav.next"

    def __init__(self, parent: tk.Misc, wizard: "Wizard") -> None:
        super().__init__(parent, padding=(px(parent, 32), px(parent, 20), px(parent, 32), px(parent, 24)))
        self.wizard = wizard
        self.session: Session = wizard.session
        self.build()

    def build(self) -> None:
        """Create the page's widgets."""
        raise NotImplementedError

    def commit(self) -> bool:
        """Write form values into the session; False (after showing why) if invalid."""
        return True

    def next_enabled(self) -> bool:
        """Whether the Next button is usable."""
        return self.session.can_advance()

    def next_label(self) -> str:
        """Text of the Next button."""
        return t(self.next_key)

    # --- layout helpers
    def gap(self, size: int = _GAP) -> int:
        """A vertical gap in real pixels."""
        return px(self, size)

    def title(self, key: str, lead: str | None = None) -> None:
        """Add the page heading and an optional lead paragraph."""
        label(self, t(key), "Title").pack(anchor="w")
        if lead:
            WrapLabel(self, lead, "Muted").pack(fill="x", pady=(px(self, 4), 0))
        ttk.Frame(self, height=self.gap(18)).pack(fill="x")

    def para(self, text: str, muted: bool = False, pady: tuple[int, int] = (0, 8)) -> WrapLabel:
        """Add a wrapped paragraph."""
        widget = WrapLabel(self, text, "Muted" if muted else "Body")
        widget.pack(fill="x", pady=(px(self, pady[0]), px(self, pady[1])))
        return widget

    def card(self, title: str | None = None, subtitle: str | None = None, accent: str | None = None,
             tip: str | None = None, parent: tk.Misc | None = None) -> Card:
        """Add a card (packed full width) and return it; ``tip`` adds a "?" next to the title."""
        box = Card(parent or self, title, subtitle, accent)
        if tip and box.title_label is not None:
            InfoTip(box.header, tip).pack(side="left", padx=(px(self, 8), 0))
        if parent is None:
            box.pack(fill="x", pady=(0, self.gap()))
        return box

    def actions(self, parent: tk.Misc | None = None) -> ttk.Frame:
        """A row for buttons; packed on the page, or left for the caller to place in ``parent``."""
        row = frame(parent or self)
        if parent is None:
            row.pack(fill="x", pady=(0, self.gap()))
        return row

    def alive(self) -> bool:
        """False once the page was destroyed (language change, window closed)."""
        try:
            return bool(self.winfo_exists())
        except tk.TclError:
            return False


def how_to_card(page: Page, compact: bool = False) -> None:
    """Explain the pen tests: no clicks, touch or hover, results measure speed (and precision)."""
    if compact:
        notice = Notice(page)
        notice.pack(fill="x", pady=(0, page.gap()))
        notice.show(t("gui.howto.short"), "info")
        return
    box = page.card(t("gui.howto.title"), accent=COLORS.info)
    bullets(box.body, [t("gui.howto.no_clicks"), t("gui.howto.hover"), t("gui.howto.speed"),
                       t("gui.howto.cancel")]).pack(fill="x")


# ------------------------------------------------------------- community data
def data_status_text(status: dict[str, Any] | None) -> tuple[str, str | None]:
    """Status line and, after 90 days, a staleness warning for the community data."""
    if not status:
        return t("gui.data.status_unknown"), None
    text = t("gui.data.status", date=status["date"], presets=status["presets"], sources=status["sources"],
             origin=t(f"gui.data.origin.{status['origin']}"))
    warning = t("gui.data.stale", days=status["age_days"]) if status.get("stale") else None
    return text, warning


class DataCard(Card):
    """Community data: last update, preset/source counts, "Update community data" (Welcome, Settings)."""

    def __init__(self, parent: tk.Misc, wizard: "Wizard", settings_button: bool = True) -> None:
        super().__init__(parent, t("gui.data.title"), t("gui.data.subtitle"))
        self.wizard = wizard
        self.status = WrapLabel(self.body, "", "Body")
        self.status.pack(fill="x")
        self.notice = Notice(self.body)
        self.notice.pack(fill="x", pady=(px(self, 6), 0))
        row = frame(self.body)
        row.pack(fill="x", pady=(px(self, 10), 0))
        self.update_button = button(row, t("gui.data.update"), self._update)
        self.update_button.pack(side="left")
        if settings_button:
            button(row, t("gui.settings.open"), wizard.open_settings, kind="ghost").pack(
                side="left", padx=(px(self, 8), 0))
        label(self.body, t("gui.data.privacy"), "Small").pack(anchor="w", pady=(px(self, 6), 0))
        self.refresh()

    def refresh(self) -> None:
        """Show the current status."""
        text, warning = data_status_text(self.wizard.session.community_status())
        self.status.configure(text=text)
        if warning:
            self.notice.show(warning, "warn")
        else:
            self.notice.hide()

    def _update(self) -> None:
        self.update_button.configure(state="disabled")
        self.notice.show(t("gui.data.updating"), "info")
        run_in_thread(self, self.wizard.session.update_community_data, self._done, self._failed)

    def _done(self, result: Any) -> None:
        if not self.winfo_exists():
            return
        self.update_button.configure(state="normal")
        self.refresh()
        ok = bool(getattr(result, "ok", False))
        self.notice.show(t(result.key, **result.params), "success" if ok else "error",
                         getattr(result, "detail", ""))

    def _failed(self, exc: BaseException) -> None:
        if self.winfo_exists():
            self.update_button.configure(state="normal")
            message, detail = error_text(exc)
            self.notice.show(message, "error", detail)


# ---------------------------------------------------------------------- welcome
class WelcomePage(Page):
    step = Step.WELCOME

    def build(self) -> None:
        s = self.session
        label(self, t("gui.welcome.eyebrow"), "Eyebrow").pack(anchor="w")
        label(self, t("gui.welcome.title"), "Display").pack(anchor="w", pady=(px(self, 2), px(self, 6)))
        WrapLabel(self, t("gui.welcome.intro"), "Muted").pack(fill="x", pady=(0, self.gap(20)))
        if s.dry_run:
            notice = Notice(self)
            notice.pack(fill="x", pady=(0, self.gap()))
            notice.show(t("gui.welcome.dry_run"), "info")
        self._build_saved_card()
        box = self.card(t("gui.welcome.goal_title"), t("gui.welcome.goal_help"))
        self._goal = tk.StringVar(value=s.goal)
        self._goal_cards: dict[str, tk.Frame] = {}
        for goal in GOALS:
            # the whole card is the click target; the radio inside keeps keyboard and screen-reader access
            row = tk.Frame(box.body, background=COLORS.surface, highlightthickness=2, cursor="hand2",
                           highlightbackground=COLORS.border, highlightcolor=COLORS.accent)
            row.pack(fill="x", pady=(0, px(self, 8)))
            inner = frame(row)
            inner.pack(fill="x", padx=px(self, 12), pady=px(self, 10))
            top = frame(inner)
            top.pack(fill="x")
            radio(top, t(f"gui.welcome.goal.{goal}.title"), goal, self._goal, command=self._choose).pack(side="left")
            if goal == "guided":
                Chip(top, t("gui.welcome.recommended"), "accent").pack(side="left", padx=(px(self, 8), 0))
            text = WrapLabel(inner, t(f"gui.welcome.goal.{goal}.text"), "Muted")
            text.pack(fill="x", padx=(px(self, 24), 0))
            for widget in (row, inner, top, text):
                widget.bind("<Button-1>", lambda _e, g=goal: self._pick_goal(g), add="+")
            self._goal_cards[goal] = row
        self._paint_goals()
        if s.advanced:       # the long explanation and the community data are Advanced-mode content
            steps = self.card(t("gui.welcome.steps_title"))
            steps.body.columnconfigure(1, weight=1)
            for index in range(1, 6):
                badge = tk.Label(steps.body, text=str(index), bg=COLORS.accent_soft, fg=COLORS.accent,
                                 width=2, font=font("small_bold"))
                badge.grid(row=index, column=0, sticky="nw", padx=(0, px(self, 12)), pady=px(self, 4))
                WrapLabel(steps.body, t(f"gui.welcome.step{index}")).grid(
                    row=index, column=1, sticky="we", pady=px(self, 4))
            DataCard(self, self.wizard).pack(fill="x", pady=(0, self.gap()))
        box = self.card(t("gui.welcome.disclaimer_title"), accent=COLORS.warn)
        WrapLabel(box.body, t("gui.welcome.disclaimer")).pack(fill="x")
        self._accepted = tk.BooleanVar(value=s.accepted_disclaimer)
        accept = check(box.body, t("gui.welcome.accept"), self._accepted, command=self._toggle)
        accept.pack(anchor="w", pady=(px(self, 12), 0))
        self.after_idle(accept.focus_set)

    def _build_saved_card(self) -> None:
        """"Welcome back": the saved profile lets the player skip the questions and the baseline."""
        store = self.session.profile_store
        saved = store.latest() if store is not None else None
        if saved is None or self.session.dry_run:
            return
        box = self.card(t("gui.welcome.back_title", tablet=saved.tablet_name), t("gui.welcome.back_help"),
                        accent=COLORS.good)
        last = saved.last
        if last is not None:
            WrapLabel(box.body, t("gui.welcome.back_last", date=last.when[:10],
                                  source=t(f"gui.welcome.source.{last.source}")), "Body").pack(fill="x")
        if saved.coach:
            c = saved.coach[0]
            gain = c.get("gain_pct")
            WrapLabel(box.body, t("gui.welcome.back_coach", date=c["when"][:10], rounds=c["rounds"])
                      + (" " + t("gui.welcome.back_gain", pct=f"{gain:.0f}") if gain else ""), "Small").pack(fill="x")
        self._resume_notice = Notice(box.body)
        self._resume_notice.pack(fill="x", pady=(px(self, 6), 0))
        row = frame(box.body)
        row.pack(fill="x", pady=(px(self, 8), 0))
        button(row, t("gui.welcome.back_resume"), self._resume).pack(side="left")
        label(row, t("gui.welcome.back_resume_note"), "Small").pack(side="left", padx=(px(self, 10), 0))

    def _resume(self) -> None:
        if not self.session.accepted_disclaimer:
            self._resume_notice.show(t("core.error.accept_first"), "warn")
            return
        self.wizard.resume()

    def _pick_goal(self, goal: str) -> None:
        self._goal.set(goal)
        self._choose()

    def _paint_goals(self) -> None:
        """Accent border on the chosen path."""
        for goal, card in self._goal_cards.items():
            card.configure(highlightbackground=COLORS.accent if goal == self._goal.get() else COLORS.border)

    def _choose(self) -> None:
        self.session.set_goal(self._goal.get())
        self._paint_goals()
        self.wizard.update_nav()
        self.wizard.refresh_stepper()

    def _toggle(self) -> None:
        self.session.accepted_disclaimer = self._accepted.get()
        self.wizard.update_nav()


# ----------------------------------------------------------------------- detect
class DetectPage(Page):
    step = Step.DETECT

    def build(self) -> None:
        self.title("gui.detect.title", t("gui.detect.intro"))
        self._status = Notice(self)
        self._status.pack(fill="x", pady=(0, self.gap()))
        self._summary_card = self.card(t("gui.detect.summary_title"))
        self._summary = frame(self._summary_card.body)
        self._summary.pack(fill="x")
        self._summary_card.pack_forget()
        self._otd_notice = Notice(self)
        self._otd_notice.pack(fill="x", pady=(0, self.gap()))
        self._locations_card = self.card(t("gui.detect.locations_title"), t("gui.detect.locations_help"))
        row = frame(self._locations_card.body)
        row.pack(fill="x")
        self._location_box = ttk.Combobox(row, state="readonly", width=70)
        self._location_box.pack(side="left", fill="x", expand=True)
        button(row, t("gui.detect.use_location"), self._use_location).pack(side="left", padx=(px(self, 8), 0))
        self._locations_card.pack_forget()
        row = self.actions()
        button(row, t("gui.detect.retry"), self._start).pack(side="left")
        button(row, t("gui.detect.pick_config"), lambda: self._pick(True), kind="ghost").pack(
            side="left", padx=(px(self, 8), 0))
        button(row, t("gui.detect.pick_install"), lambda: self._pick(False), kind="ghost").pack(
            side="left", padx=(px(self, 4), 0))
        label(self, "\U0001f512  " + t("gui.detect.safe_note"), "Small").pack(anchor="w")
        self._details = Expander(self, t("gui.details.log"))
        self._details.pack(fill="x", pady=(px(self, 8), 0))
        if self.session.settings is None:
            self.after(50, self._start)
        else:
            self._render()

    def _pick(self, config: bool) -> None:
        chosen = filedialog.askdirectory(parent=self, title=t(
            "gui.detect.pick_config_title" if config else "gui.detect.pick_install_title"))
        if not chosen:
            return
        if config:
            self.wizard.config_hint = Path(chosen)
        else:
            self.wizard.install_hint = Path(chosen)
        self._start()

    def _start(self) -> None:
        self._status.show(t("gui.detect.searching"), "info")
        self._summary_card.pack_forget()
        self.wizard.set_busy(True)
        run_in_thread(
            self, lambda: self.session.detect(self.wizard.config_hint, self.wizard.install_hint),
            self._found, self._failed)

    def _found(self, _result: Any) -> None:
        self.wizard.set_busy(False)
        self._render()

    def _failed(self, exc: BaseException) -> None:
        self.wizard.set_busy(False)
        message, detail = error_text(exc)
        self._status.show(f"{message}\n{t('gui.detect.failed_hint')}", "error",
                          detail + "\n" + "\n".join(self.session.log))
        self._summary_card.pack_forget()
        self._details.set(detail + "\n" + "\n".join(self.session.log))
        self.wizard.update_nav()

    def _render(self) -> None:
        s = self.session
        self._status.show(t("gui.detect.found"), "success")
        for child in self._summary.winfo_children():
            child.destroy()
        names = ", ".join(p.tablet_name for p in s.profiles) or t("gui.detect.no_profiles")
        self._summary.columnconfigure(1, weight=1)
        for row, (key, value) in enumerate((
                ("gui.detect.k.config", str(s.config_dir)), ("gui.detect.k.install", str(s.install_dir or "-")),
                ("gui.detect.k.profiles", names), ("gui.detect.k.plugins", str(len(s.plugins))))):
            label(self._summary, t(key), "Muted").grid(row=row, column=0, sticky="nw",
                                                        padx=(0, px(self, 16)), pady=px(self, 3))
            WrapLabel(self._summary, value).grid(row=row, column=1, sticky="we", pady=px(self, 3))
        self._summary_card.pack(fill="x", pady=(0, self.gap()), after=self._status)
        if s.otd_notice:
            key, params = s.otd_notice
            self._otd_notice.show(t(key, **params), "info")
        else:
            self._otd_notice.hide()
        self._show_locations()
        lines = [f"config_dir: {s.config_dir}", f"install_dir: {s.install_dir}"]
        lines += [f"profile: {p.tablet_name} | {p.output_mode_path} | area "
                  f"{p.tablet_area.width:g}x{p.tablet_area.height:g} mm" for p in s.profiles]
        lines += [f"plugin: {p.name} {p.version or ''}".strip() for p in s.plugins]
        lines.append(f"filters available: {len(s.filter_paths)}")
        lines += [f"location: {loc.kind} {loc.path} {[p.name for p in loc.settings_files]}" for loc in s.locations]
        self._details.set("\n".join(lines + s.log))
        self.wizard.update_nav()

    def _show_locations(self) -> None:
        """Offer every OTD settings location found (installed and portable) when there is a choice."""
        locations = list(self.session.locations)
        if len(locations) < 2 and not any(loc.kind == "portable" for loc in locations):
            self._locations_card.pack_forget()
            return
        self._locations = locations
        values = [t(f"otd.location.{loc.kind}", path=str(loc.path)) for loc in locations]
        self._location_box.configure(values=values)
        current = next((i for i, loc in enumerate(locations) if loc.path == self.session.config_dir), None)
        if current is not None:
            self._location_box.current(current)
        self._locations_card.pack(fill="x", pady=(0, self.gap()), after=self._summary_card)

    def _use_location(self) -> None:
        index = self._location_box.current()
        if index < 0:
            return
        self.wizard.config_hint = self._locations[index].path
        self._start()


# ----------------------------------------------------------------------- tablet
class TabletPage(Page):
    step = Step.TABLET

    def build(self) -> None:
        self._choices: list[TabletChoice] = self.session.tablet_choices()
        self.title("gui.tablet.title", t("gui.tablet.intro"))
        box = self.card(t("gui.tablet.list_title"), t("gui.tablet.approx_note"), tip=t("gui.tip.tablet_list"))
        search = frame(box.body)
        search.pack(fill="x", pady=(0, px(self, 10)))
        label(search, t("gui.tablet.search")).pack(side="left")
        self._filter = tk.StringVar()
        self._filter.trace_add("write", lambda *_: self._fill())
        entry = ttk.Entry(search, textvariable=self._filter)
        entry.pack(side="left", fill="x", expand=True, padx=(px(self, 8), 0))
        holder = frame(box.body)
        holder.pack(fill="both", expand=True)
        self._tree = ttk.Treeview(holder, columns=("name", "area", "rate", "source"),
                                  show="headings", height=8, selectmode="browse")
        for col, key, width in (("name", "gui.tablet.col.name", 240), ("area", "gui.tablet.col.area", 130),
                                ("rate", "gui.tablet.col.rate", 90), ("source", "gui.tablet.col.source", 190)):
            self._tree.heading(col, text=t(key))
            self._tree.column(col, width=px(self, width), anchor="w", stretch=col in ("name", "source"))
        bar = ttk.Scrollbar(holder, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=bar.set)
        self._tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self._tree.bind("<<TreeviewSelect>>", self._on_select)
        self._summary = Notice(self)
        self._summary.pack(fill="x", pady=(0, self.gap()))
        self._link = frame(self)
        label(self._link, t("gui.tablet.link_profile"), "Strong").pack(side="left")
        self._link_box = ttk.Combobox(self._link, state="readonly", width=34,
                                      values=[p.tablet_name for p in self.session.profiles])
        self._link_box.pack(side="left", padx=px(self, 8))
        self._link_box.bind("<<ComboboxSelected>>", self._on_link)
        self._build_custom()
        self._fill()
        self._show_summary()

    def _build_custom(self) -> None:
        body = self.card(t("gui.tablet.custom_title"), t("gui.tablet.custom_help")).body
        self._vars = {k: tk.StringVar(value=v) for k, v in (
            ("name", ""), ("width", ""), ("height", ""), ("rate", "133"), ("pressure", "0"))}
        fields = (("name", "gui.tablet.custom.name", None), ("width", "gui.tablet.custom.width", "gui.tip.area"),
                  ("height", "gui.tablet.custom.height", "gui.tip.area"),
                  ("rate", "gui.tablet.custom.rate", "gui.tip.report_rate"),
                  ("pressure", "gui.tablet.custom.pressure", None))
        for col in (1, 3):
            body.columnconfigure(col, weight=1)
        for index, (key, text, tip) in enumerate(fields):
            row, col = index // 2, (index % 2) * 2
            cell = frame(body)
            cell.grid(row=row, column=col, sticky="w", padx=(0 if col == 0 else px(self, 16), px(self, 8)),
                      pady=px(self, 4))
            label(cell, t(text)).pack(side="left")
            if tip:
                InfoTip(cell, t(tip)).pack(side="left", padx=(px(self, 6), 0))
            ttk.Entry(body, textvariable=self._vars[key], width=24 if key == "name" else 10).grid(
                row=row, column=col + 1, sticky="we" if key == "name" else "w", pady=px(self, 4))
        row = frame(body)
        row.grid(row=3, column=0, columnspan=4, sticky="we", pady=(px(self, 10), 0))
        button(row, t("gui.tablet.custom.use"), self._use_custom).pack(side="left")
        self._custom_error = label(row, "", "Error")
        self._custom_error.pack(side="left", padx=px(self, 12))

    def _fill(self) -> None:
        needle = self._filter.get().strip().casefold()
        self._tree.delete(*self._tree.get_children())
        current = normalize_name(self.session.tablet.name) if self.session.tablet else None
        for index, choice in enumerate(self._choices):
            if needle and needle not in choice.label.casefold():
                continue
            spec = choice.tablet
            source = t("gui.tablet.source.profile" if choice.profile or spec is None
                       else f"gui.tablet.source.{spec.source.value}")
            self._tree.insert("", "end", iid=str(index), values=(
                choice.label, f"{spec.width_mm:g} x {spec.height_mm:g} mm" if spec else "?",
                f"{spec.max_report_rate_hz:g}" if spec else "?", source))
            if current and normalize_name(choice.label) == current:
                self._tree.selection_set(str(index))
                self._tree.see(str(index))

    def _on_select(self, _event: tk.Event) -> None:
        selected = self._tree.selection()
        if not selected:
            return
        choice = self._choices[int(selected[0])]
        if not self.session.select_choice(choice):
            self._vars["name"].set(choice.label)
            self._custom_error.configure(text=t("gui.tablet.unknown_profile"))
        else:
            self._custom_error.configure(text="")
        self._show_summary()
        self.wizard.update_nav()

    def _on_link(self, _event: tk.Event) -> None:
        self.session.link_profile(self.session.profiles[self._link_box.current()])
        self._show_summary()
        self.wizard.update_nav()

    def _use_custom(self) -> None:
        try:
            self.session.set_custom_tablet(
                self._vars["name"].get(), parse_float(self._vars["width"].get()),
                parse_float(self._vars["height"].get()), parse_float(self._vars["rate"].get()),
                int(parse_float(self._vars["pressure"].get())))
        except (ValueError, SessionError):
            self._custom_error.configure(text=t("gui.tablet.custom.invalid"))
            return
        self._custom_error.configure(text="")
        self._show_summary()
        self.wizard.update_nav()

    def _show_summary(self) -> None:
        s = self.session
        if s.tablet is None:
            self._summary.show(t("gui.tablet.none_selected"), "info")
            self._link.pack_forget()
            return
        text = t("gui.tablet.selected", name=s.tablet.name, w=f"{s.tablet.width_mm:g}",
                 h=f"{s.tablet.height_mm:g}", rate=f"{s.tablet.max_report_rate_hz:g}")
        if s.profile is not None:
            self._summary.show(text + "\n" + t("gui.tablet.profile_linked"), "success")
        else:
            key = "gui.tablet.pick_profile" if s.profiles else "gui.tablet.no_profiles_at_all"
            self._summary.show(text + "\n" + t(key), "warn")
        if s.profile is None and s.profiles:
            self._link.pack(fill="x", pady=(0, self.gap()), after=self._summary)
        else:
            self._link.pack_forget()


# ---------------------------------------------------------------------- display
class DisplayPage(Page):
    step = Step.DISPLAY

    def next_label(self) -> str:
        """Live tuning and the coach end here: "Next" opens the tool."""
        return t(f"gui.nav.open_{self.session.goal}") if self.session.at_goal_launch else super().next_label()

    def build(self) -> None:
        self.title("gui.display.title", t("gui.display.intro"))
        s = self.session
        if s.display is None:
            s.detect_display()
        d = s.display
        self._vars = {
            "width": tk.StringVar(value=str(d.width_px) if d else ""),
            "height": tk.StringVar(value=str(d.height_px) if d else ""),
            "left": tk.StringVar(value=f"{d.offset_x - d.width_px / 2:g}" if d else "0"),
            "top": tk.StringVar(value=f"{d.offset_y - d.height_px / 2:g}" if d else "0"),
            "refresh": tk.StringVar(value=f"{d.refresh_hz:g}" if d else "60"),
        }
        box = self.card(t("gui.display.values"))
        body = box.body
        body.columnconfigure(2, weight=1)
        for index, key in enumerate(("width", "height", "left", "top", "refresh")):
            label(body, t(f"gui.display.{key}"), "Strong").grid(row=index, column=0, sticky="w",
                                                                 pady=px(self, 6))
            ttk.Entry(body, textvariable=self._vars[key], width=10).grid(
                row=index, column=1, sticky="w", padx=px(self, 14), pady=px(self, 6))
            WrapLabel(body, t(f"gui.display.{key}_help"), "Muted").grid(
                row=index, column=2, sticky="we", pady=px(self, 6))
        row = self.actions(body)
        row.grid(row=5, column=0, columnspan=3, sticky="w", pady=(px(self, 10), 0))
        button(row, t("gui.display.redetect"), self._redetect).pack(side="left")
        self._message = Notice(self)
        self._message.pack(fill="x")
        tip = Notice(self)
        tip.pack(fill="x", pady=(self.gap(), 0))
        tip.show(t("gui.display.multi_monitor"), "info")

    def _redetect(self) -> None:
        try:
            found = self.session.detect_display()
        except SessionError as exc:
            message, detail = error_text(exc)
            self._message.show(message, "error", detail)
            return
        if found is None:
            self._message.show(t("gui.display.detect_failed"), "warn")
            return
        self._message.hide()
        self._vars["width"].set(str(found.width_px))
        self._vars["height"].set(str(found.height_px))
        self._vars["left"].set(f"{found.offset_x - found.width_px / 2:g}")
        self._vars["top"].set(f"{found.offset_y - found.height_px / 2:g}")

    def next_enabled(self) -> bool:
        return True

    def commit(self) -> bool:
        try:
            self.session.set_display(
                int(parse_float(self._vars["width"].get())), int(parse_float(self._vars["height"].get())),
                parse_float(self._vars["left"].get()), parse_float(self._vars["top"].get()),
                parse_float(self._vars["refresh"].get()))
        except (ValueError, SessionError):
            self._message.show(t("gui.display.invalid"), "error")
            return False
        return True


# ---------------------------------------------------------------- questionnaire
_QUESTION_TIPS = {"play_style": "gui.tip.area", "priority": "gui.tip.smoothing",
                  "notices_jitter": "gui.tip.jitter", "notices_input_lag": "gui.tip.latency",
                  "plugin_policy": "gui.tip.plugins"}


class QuestionsPage(Page):
    step = Step.QUESTIONS

    def build(self) -> None:
        self.title("gui.questions.title", t("gui.questions.intro"))
        prefs = self.session.prefs
        self._radio("play_style", prefs.play_style, PlayStyle)
        self._radio("map_preference", prefs.map_preference, MapPreference)
        self._radio("priority", prefs.priority, Priority)
        self._radio("skill_level", prefs.skill_level, SkillLevel)
        self._check("notices_jitter", prefs.notices_jitter)
        self._check("notices_input_lag", prefs.notices_input_lag)
        profile = self.session.profile
        self._keep = tk.BooleanVar(value=prefs.preferred_area_width_mm is not None)
        if profile is not None:
            box = self._block("keep_area")
            check(box.body, t("gui.questions.keep_area.label", width=f"{profile.tablet_area.width:.1f}"),
                  self._keep, command=self._on_keep).pack(anchor="w")
        self._policy_block()

    def _policy_block(self) -> None:
        """Which plugins the assistant may use (it never installs any)."""
        box = self._block("plugin_policy")
        var = tk.StringVar(value=self.session.prefs.plugin_policy.value)
        for policy in PluginPolicy:
            radio(box.body, t(f"core.pref.plugin_policy.{policy.value}"), policy.value, var).pack(anchor="w")
            WrapLabel(box.body, t(f"gui.questions.plugin_policy.{policy.value}_help"), "Small").pack(
                fill="x", padx=(px(self, 26), 0), pady=(0, px(self, 4)))
        installed = ", ".join(sorted(p.name for p in self.session.plugins)) or t("gui.questions.plugin_policy.none")
        label(box.body, t("gui.questions.plugin_policy.installed_list", plugins=installed), "Small").pack(
            anchor="w", pady=(px(self, 6), 0))
        var.trace_add("write", lambda *_: self.session.set_plugin_policy(PluginPolicy(var.get())))

    def commit(self) -> bool:
        try:   # the "starting preset" step depends on the answers (policy, tablet)
            self.session.refresh_starting_presets()
        except SessionError as exc:
            self.wizard.show_error(exc)
        return True

    def _block(self, name: str) -> Card:
        tip = _QUESTION_TIPS.get(name)
        return self.card(t(f"gui.questions.{name}.title"), t(f"gui.questions.{name}.why"),
                         tip=t(tip) if tip else None)

    def _radio(self, name: str, current: Any, enum: type) -> None:
        box = self._block(name)
        var = tk.StringVar(value=current.value)
        for member in enum:
            radio(box.body, t(f"core.pref.{name}.{member.value}"), member.value, var).pack(anchor="w")
        var.trace_add("write", lambda *_: setattr(self.session.prefs, name, enum(var.get())))

    def _check(self, name: str, current: bool) -> None:
        box = self._block(name)
        var = tk.BooleanVar(value=current)
        check(box.body, t(f"gui.questions.{name}.label"), var).pack(anchor="w")
        var.trace_add("write", lambda *_: setattr(self.session.prefs, name, var.get()))

    def _on_keep(self) -> None:
        profile = self.session.profile
        self.session.prefs.preferred_area_width_mm = (
            profile.tablet_area.width if profile is not None and self._keep.get() else None)


# ------------------------------------------------------------- starting preset
def starting_presets(session: Session) -> list[Any]:
    """Presets the session offers as a starting point (``session.starting_presets``, if any).

    Each item needs ``id`` and ``label`` (text, already translated) and may
    have ``summary``. The recommender's "community presets" feature fills
    this; while it is empty the step stays hidden.
    """
    return list(getattr(session, "starting_presets", None) or ())


class PresetPage(Page):
    """GUI-only step: pick a starting preset (stored as ``session.starting_preset_id``)."""

    step = None

    def build(self) -> None:
        self.title("gui.preset.title", t("gui.preset.intro"))
        box = self.card(t("gui.preset.list_title"))
        self._var = tk.StringVar(value=getattr(self.session, "starting_preset_id", None) or "")
        radio(box.body, t("gui.preset.none"), "", self._var).pack(anchor="w")
        for preset in starting_presets(self.session):
            radio(box.body, str(preset.label), str(preset.id), self._var).pack(
                anchor="w", pady=(px(self, 6), 0))
            summary = getattr(preset, "summary", "")
            if summary:
                WrapLabel(box.body, str(summary), "Muted").pack(fill="x", padx=(px(self, 26), 0))
        self.para(t("gui.preset.note"), muted=True, pady=(4, 0))

    def next_enabled(self) -> bool:
        return True

    def commit(self) -> bool:
        self.session.starting_preset_id = self._var.get() or None  # type: ignore[attr-defined]
        return True


@dataclass(frozen=True)
class StepSlot:
    """A GUI-only step shown right after session step ``after`` while ``visible(session)``."""
    key: str
    after: Step
    page: type[Page]
    visible: Callable[[Session], bool]


SLOTS: list[StepSlot] = [
    StepSlot("gui.step.preset", Step.QUESTIONS, PresetPage, lambda s: bool(starting_presets(s))),
]


# --------------------------------------------------------------------- baseline
_TEST_TIPS = {"rate": "gui.tip.report_rate", "stillness": "gui.tip.jitter"}


class BaselinePage(Page):
    step = Step.BASELINE

    def build(self) -> None:
        self.title("gui.baseline.title", t("gui.baseline.intro"))
        how_to_card(self)
        self._rows: dict[str, tuple[ttk.Button, Chip, WrapLabel]] = {}
        for kind in BASELINE_KINDS:
            tip = _TEST_TIPS.get(kind)
            box = self.card(t(f"gui.test.{kind}.name"), t(f"gui.test.{kind}.desc"), tip=t(tip) if tip else None)
            chip = Chip(box.header, "")
            chip.pack(side="right")
            row = frame(box.body)
            row.pack(fill="x")
            run = button(row, t("gui.baseline.run"), lambda k=kind: self._run(k), kind="primary")
            run.pack(side="left")
            result = WrapLabel(row, "", "Muted")
            result.pack(side="left", fill="x", expand=True, padx=(px(self, 14), 0))
            self._rows[kind] = (run, chip, result)
            self._show_status(kind)
        self.para(t("gui.baseline.skip_note"), muted=True)
        self._message = Notice(self)
        self._message.pack(fill="x")

    def _show_status(self, kind: str) -> None:
        report = self.session.baseline_report(kind)
        run, chip, result = self._rows[kind]
        if report:
            chip.set("✓ " + t("gui.baseline.done"), "success")
            result.configure(text=" · ".join(metric_lines([report])))
            run.configure(text=t("gui.baseline.rerun"), style="Secondary.TButton")
        else:
            chip.set(t("gui.baseline.not_done"))
            result.configure(text="")
            run.configure(text=t("gui.baseline.run"), style="Primary.TButton")

    def _set_enabled(self, enabled: bool) -> None:
        for run, _chip, _result in self._rows.values():
            run.configure(state="normal" if enabled else "disabled")
        self.wizard.set_busy(not enabled)

    def _run(self, kind: str) -> None:
        self._message.hide()
        self._set_enabled(False)

        def finished(_report: MetricsReport | None = None) -> None:
            if self.alive():
                self._set_enabled(True)
                self._show_status(kind)

        try:
            self.session.run_measurement(self.wizard.winfo_toplevel(), kind, on_done=finished,
                                         on_cancel=finished)
        except SessionError as exc:
            finished()
            message, detail = error_text(exc)
            self._message.show(message, "error", detail)


# ---------------------------------------------------------------------- testing
class TestingPage(Page):
    """Candidate loop: apply, measure, rate, refine."""

    step = Step.TESTING
    next_key = "gui.nav.see_result"

    def build(self) -> None:
        s = self.session
        self._testing_id: str | None = None
        self._rating = tk.IntVar(value=0)
        self.title("gui.testing.title", t("gui.testing.intro_dry" if s.dry_run else "gui.testing.intro"))
        if s.can_measure:
            how_to_card(self, compact=True)
        columns = Columns(self, breakpoint=px(self, 880))
        columns.pack(fill="x")
        left = self.card(t("gui.testing.options_title"), parent=columns)
        columns.add(left)
        self._tree = ttk.Treeview(left.body, columns=("label", "status", "result"),
                                  show="headings", height=7, selectmode="browse")
        for col, key, width in (("label", "gui.testing.col.option", 170), ("status", "gui.testing.col.status", 96),
                                ("result", "gui.testing.col.result", 100)):
            self._tree.heading(col, text=t(key))
            self._tree.column(col, width=px(self, width), anchor="w", stretch=col == "label",
                              minwidth=px(self, 40))
        self._tree.pack(fill="both", expand=True)
        for tag, color in (("excellent", COLORS.good), ("good", COLORS.good), ("weak", COLORS.warn),
                           ("incomplete", COLORS.subtle)):
            self._tree.tag_configure(tag, foreground=color)
        self._tree.bind("<<TreeviewSelect>>", lambda _e: self._show_detail())
        self._tie_note = WrapLabel(left.body, "", "Small")
        if s.can_measure:
            WrapLabel(left.body, t("gui.testing.table_help"), "Small").pack(fill="x", pady=(px(self, 8), 0))
        right = self.card(parent=columns)
        columns.add(right)
        head = frame(right.body)
        head.pack(fill="x")
        self._head = label(head, "", "Heading")
        self._head.pack(side="left")
        self._latency_chip = Chip(head, "")
        Tooltip(self._latency_chip, t("gui.tip.latency"))
        self._info = frame(right.body)
        self._info.pack(fill="x", pady=(px(self, 6), 0))
        self._rating_box = frame(right.body)
        if s.can_measure:
            self._rating_box.pack(fill="x", pady=(px(self, 12), 0))
            label(self._rating_box, t("gui.testing.rating_title"), "Strong").pack(anchor="w")
            seg = frame(self._rating_box)
            seg.pack(anchor="w", pady=(px(self, 6), px(self, 4)))
            for n in range(0, 6):
                ttk.Radiobutton(seg, text=str(n) if n else t("gui.testing.rating_none"), value=n,
                                variable=self._rating, command=self._on_rating,
                                style="Segment.Toolbutton", takefocus=True).pack(side="left", padx=(0, 2))
            WrapLabel(self._rating_box, t("gui.testing.rating_help"), "Small").pack(fill="x")
        self._buttons = self.actions()
        self._btn_test = button(self._buttons, t("gui.testing.test"), self._on_test, kind="primary")
        self._btn_use = button(self._buttons, t("gui.testing.use"), self._on_use)
        self._btn_refine = button(self._buttons, t("gui.testing.refine"), self._on_refine)
        self._btn_restore = button(self._buttons, t("gui.testing.restore"), self._on_restore, kind="ghost")
        if not s.can_measure:
            self._btn_use.configure(style="Primary.TButton")
        for btn in ((self._btn_test,) if s.can_measure else ()) + (self._btn_use,) + (
                (self._btn_refine, self._btn_restore) if s.can_measure else ()):
            btn.pack(side="left", padx=(0, px(self, 8)))
        self._status = Notice(self)
        self._status.pack(fill="x", pady=(0, px(self, 8)))
        self._details = Expander(self, t("gui.testing.tech_title"), height=12)
        self._details.pack(fill="x")
        self._refresh_rows()
        first = s.next_candidate() or (s.candidates[0] if s.candidates else None)
        if first is not None:
            self._tree.selection_set(first.id)
        self._show_detail()

    # --- helpers
    def _selected(self) -> Candidate | None:
        selected = self._tree.selection()
        return self.session.candidate(selected[0]) if selected else None

    def _status_of(self, cand: Candidate) -> str:
        s = self.session
        if cand.id == self._testing_id:
            return t("gui.testing.status.testing")
        if s.is_complete(cand.id):
            runs = min(s.run_count(cand.id, kind) for kind in CANDIDATE_KINDS)
            return t("gui.testing.status.done_n", n=runs) if runs > 1 else t("gui.testing.status.done")
        if cand.id in s.results and s.results[cand.id].reports:
            return t("gui.testing.status.partial")
        return t("gui.testing.status.pending")

    def _verdict(self) -> Any:
        """The session verdict (grades, ties); None while nothing is measured."""
        try:
            return self.session.verdict()
        except SessionError:
            return None

    def _result_text(self, cand: Candidate, verdict: Any) -> str:
        """Grade in words, "—" while incomplete, plus ★n only when the player rated it."""
        result = self.session.results.get(cand.id)
        info = verdict.summaries.get(cand.id) if verdict is not None else None
        if info is None or not result or not result.reports:
            text = ""
        elif not info.complete:
            text = t("gui.testing.grade.incomplete")
        else:
            text = t(f"gui.testing.grade.{info.grade}")
        if result and result.user_rating:
            text = f"{text}  ★{result.user_rating}".strip()
        return text

    def _refresh_rows(self) -> None:
        s = self.session
        verdict = self._verdict()
        top = set(verdict.top) if verdict is not None else set()
        keep = self._tree.selection()
        self._tree.delete(*self._tree.get_children())
        for cand in s.candidates:
            star = "★ " if cand.id in top else ""
            info = verdict.summaries.get(cand.id) if verdict is not None else None
            tag = "" if info is None or cand.id not in s.results else (info.grade or "incomplete")
            self._tree.insert("", "end", iid=cand.id, tags=(tag,) if tag else (),
                              values=(star + candidate_label(s, cand), self._status_of(cand),
                                      self._result_text(cand, verdict)))
        if keep and self._tree.exists(keep[0]):
            self._tree.selection_set(keep[0])
        if verdict is not None and verdict.tie:
            self._tie_note.configure(text=t("gui.testing.tie"))
            self._tie_note.pack(fill="x", pady=(px(self, 8), 0))
        else:
            self._tie_note.pack_forget()

    def _show_detail(self) -> None:
        cand = self._selected()
        s = self.session
        if cand is None:
            return
        self._head.configure(text=candidate_label(s, cand))
        if cand.est_added_latency_ms:
            self._latency_chip.set(t("gui.testing.latency_chip", ms=f"{cand.est_added_latency_ms:.1f}"), "warn")
            self._latency_chip.pack(side="right")
        else:
            self._latency_chip.pack_forget()
        for child in self._info.winfo_children():
            child.destroy()
        WrapLabel(self._info, t(cand.summary_key), "Muted").pack(fill="x", pady=(0, px(self, 6)))
        result = s.results.get(cand.id)
        reports = result.reports if result else ()
        measured = bool(reports)
        verdict = self._verdict()
        info = verdict.summaries.get(cand.id) if verdict is not None else None
        if measured and info is not None:
            self._build_verdict(cand, reports, info, verdict)
        else:
            WrapLabel(self._info, t("gui.testing.no_data"), "Muted").pack(fill="x")
        self._rating.set((result.user_rating or 0) if result else 0)
        self._details.set(self._technical_text(cand, reports, info))
        busy = self._testing_id is not None
        done = s.is_complete(cand.id)
        self._btn_test.configure(state="disabled" if busy else "normal",
                                 text=t("gui.testing.repeat" if done else "gui.testing.test"))
        self._btn_use.configure(state="normal" if (measured or not s.can_measure or "saved" in cand.tags)
                                and not busy else "disabled")
        self._btn_refine.configure(state="normal" if s.results_with_reports() and not busy else "disabled")
        self._btn_restore.configure(state="normal" if s.live_candidate_id and not busy else "disabled")
        for seg in self._rating_box.winfo_children():
            for child in seg.winfo_children():
                if isinstance(child, ttk.Radiobutton):
                    child.configure(state="normal" if measured and not busy else "disabled")
        self.wizard.update_nav()

    def _build_verdict(self, cand: Candidate, reports: Any, info: Any, verdict: Any) -> None:
        """Grade + reliability chips, 2-3 plain lines and the tie / repeat hint."""
        row = frame(self._info)
        row.pack(fill="x", pady=(px(self, 4), px(self, 4)))
        if info.complete:
            kind = {"excellent": "success", "good": "success", "normal": "neutral", "weak": "warn"}[info.grade]
            Chip(row, t(f"gui.testing.grade.{info.grade}"), kind).pack(side="left", padx=(0, 6))
        else:
            Chip(row, t("gui.testing.grade.incomplete"), "neutral").pack(side="left", padx=(0, 6))
        rel_kind = {"reliable": "success", "repeat": "warn", "few": "neutral"}[info.reliability]
        rel = Chip(row, t(f"gui.testing.reliability.{info.reliability}"), rel_kind)
        rel.pack(side="left")
        Tooltip(rel, t("gui.testing.reliability_tip"))
        lines = [t(key, **params) for key, params in plain_verdict(reports, cand.est_added_latency_ms)]
        if lines:
            bullets(self._info, lines, role="Body").pack(fill="x")
        if not info.complete:
            WrapLabel(self._info, t("gui.testing.incomplete_hint"), "Small").pack(fill="x", pady=(px(self, 6), 0))
        elif info.tied and info.best:
            WrapLabel(self._info, t("gui.testing.tie_hint"), "Small").pack(fill="x", pady=(px(self, 6), 0))
        elif info.reliability == "repeat":
            WrapLabel(self._info, t("gui.testing.repeat_hint"), "Small").pack(fill="x", pady=(px(self, 6), 0))

    def _technical_text(self, cand: Candidate, reports: Any, info: Any) -> str:
        """Everything the plain view hides: reasons, raw metrics, runs, numeric score."""
        parts: list[str] = []
        if cand.reasons:
            parts += [tr(reason) for reason in cand.reasons]
        if reports:
            parts.append("")
            parts += metric_lines(reports)
        if info is not None and info.objective is not None:
            parts.append("")
            parts.append(t("gui.testing.tech_score", score=f"{info.objective:.1f}", noise=f"{info.noise:.1f}",
                           runs=info.runs))
        return "\n".join(parts) if parts else t("gui.testing.no_data")

    def _fail(self, exc: BaseException) -> None:
        if not self.alive():
            return
        self._testing_id = None
        self.wizard.set_busy(False)
        message, detail = error_text(exc)
        self._status.show(message, "error", detail + "\n" + "\n".join(self.session.log))
        self._refresh_rows()
        self._show_detail()

    def _info_status(self, text: str, kind: str = "info") -> None:
        self._status.show(text, kind)

    # --- actions
    def _on_test(self) -> None:
        cand = self._selected()
        if cand is None or not self._consent():
            return
        self._testing_id = cand.id
        self.wizard.set_busy(True)
        self._info_status(t("gui.testing.applying"))
        self._refresh_rows()
        self._show_detail()
        run_in_thread(self, lambda: self.session.apply_candidate(cand, confirmed=True),
                      lambda result: self._applied(cand, result), self._fail)

    def _consent(self) -> bool:
        if self.wizard.apply_consent:
            return True
        backup = self.session.backup_path
        text = t("gui.testing.consent", backup=str(backup) if backup else t("gui.testing.consent_backup_new"))
        self.wizard.apply_consent = messagebox.askyesno(t("gui.testing.consent_title"), text, parent=self)
        return self.wizard.apply_consent

    def _applied(self, cand: Candidate, result: ApplyResult) -> None:
        if not self.alive():
            return
        if not result.ok:
            self._fail(SessionError(result.message_key, result.detail))
            return
        self._run_kinds(cand, list(CANDIDATE_KINDS))

    def _run_kinds(self, cand: Candidate, kinds: list[str], cancelled: bool = False) -> None:
        if not self.alive():
            return
        if not kinds:
            self._testing_id = None
            self.wizard.set_busy(False)
            self._info_status(t("gui.testing.cancelled" if cancelled else "gui.testing.finished"),
                              "warn" if cancelled else "success")
            self._refresh_rows()
            self._tree.selection_set(cand.id)
            self._show_detail()
            return
        self._info_status(t("gui.testing.running", test=t(f"gui.test.{kinds[0]}.name")))
        try:
            self.session.run_measurement(
                self.wizard.winfo_toplevel(), kinds[0], cand,
                on_done=lambda _r: self._run_kinds(cand, kinds[1:]),
                on_cancel=lambda: self._run_kinds(cand, [], cancelled=True))
        except SessionError as exc:
            self._fail(exc)

    def _on_rating(self) -> None:
        cand = self._selected()
        if cand is not None:
            self.session.set_rating(cand.id, self._rating.get() or None)
            self._refresh_rows()
            self._show_detail()

    def _on_use(self) -> None:
        cand = self._selected()
        if cand is not None:
            self.session.choose(cand.id)
            self._info_status(t("gui.testing.chosen", name=candidate_label(self.session, cand)), "success")
            self._refresh_rows()
            self._show_detail()

    def _on_refine(self) -> None:
        self.wizard.set_busy(True)
        self._info_status(t("gui.testing.refining"))
        run_in_thread(self, self.session.refine, self._refined, self._fail)

    def _refined(self, cand: Candidate | None) -> None:
        if not self.alive():
            return
        self.wizard.set_busy(False)
        if cand is None:
            self._info_status(t("gui.testing.converged"))
            return
        self._info_status(t("gui.testing.refined"), "success")
        self._refresh_rows()
        self._tree.selection_set(cand.id)
        self._show_detail()

    def _on_restore(self) -> None:
        if not messagebox.askyesno(t("gui.testing.restore_title"), t("gui.testing.restore_confirm"), parent=self):
            return
        self.wizard.set_busy(True)
        run_in_thread(self, lambda: self.session.restore_original(confirmed=True),
                      self._restored, self._fail)

    def _restored(self, result: ApplyResult) -> None:
        if not self.alive():
            return
        self.wizard.set_busy(False)
        self._info_status(t(result.message_key), "success" if result.ok else "error")
        self._show_detail()


# ----------------------------------------------------------------------- result
SectionBuilder = Callable[[ttk.Frame], None]
# Each hook gets the ResultsView after its built-in cards exist and may call
# ``view.add_section(...)``.
RESULT_SECTIONS: list[Callable[["ResultsView"], None]] = []

# ============================================================================
# EXTENSION POINT: "Companion / live tuning" button on the Result screen.
# Set this to a callable that takes the ResultsView (``view.session``,
# ``view.best`` and ``view.page`` are available), e.g. from gui/companion.py:
#     wizard.COMPANION_LAUNCHER = lambda view: open_companion(view.page, view.session, view.best)
# While it is None the button is not shown. Its label is "gui.result.companion".
# ============================================================================
COMPANION_LAUNCHER: Callable[["ResultsView"], None] | None = None
# Same idea for the Play Coach (button "Play Coach" on the result screen and the welcome path). Its argument
# has the same ``session`` / ``page`` attributes (a ``ResultsView`` or a ``LaunchContext``).
COACH_LAUNCHER: Callable[["ResultsView"], None] | None = None


@dataclass(frozen=True)
class LaunchContext:
    """What a launcher gets when it is started from the welcome path instead of the result screen."""
    session: Session
    page: tk.Misc


class ResultsView(ttk.Frame):
    """The result screen body: hero card, before/after bars, area preview, filter chain.

    Extra cards go through :meth:`add_section` (directly, or by appending a hook
    to :data:`RESULT_SECTIONS`). They are placed in ``sections``, between the
    comparison and the technical details.
    """

    def __init__(self, parent: Page, session: Session, best: Candidate) -> None:
        super().__init__(parent)
        self.page, self.session, self.best = parent, session, best
        self._build_hero()
        self.status = Notice(self)
        self.status.pack(fill="x", pady=(0, parent.gap()))
        self._build_comparison()
        columns = Columns(self, breakpoint=px(self, 820))
        columns.pack(fill="x")
        area_card = self._build_area(columns)
        if area_card is not None:
            columns.add(area_card)
        columns.add(self._build_filters(columns))
        # ---- EXTENSION POINT: cards added with add_section() live here --------
        # Built-in sections first (community comparison, plugin suggestions), then RESULT_SECTIONS.
        self.sections = ttk.Frame(self)
        self.sections.pack(fill="x")
        self.builtin_sections: list[Card] = []
        for builtin in (self._build_community, self._build_suggestions):
            card = builtin()
            if card is not None:
                self.builtin_sections.append(card)
        for hook in RESULT_SECTIONS:
            hook(self)
        self.report = Expander(self, t("gui.result.full_report"), height=14)
        self.log = Expander(self)
        if parent.session.advanced:      # the technical report and log are Advanced-mode content
            self.report.pack(fill="x", pady=(px(self, 4), 0))
            self.log.pack(fill="x")

    def add_section(self, title: str, builder: SectionBuilder, subtitle: str | None = None,
                    index: int | None = None) -> Card:
        """Add a card titled ``title`` whose body ``builder(body)`` fills; returns the card.

        ``index`` inserts it before the n-th extra section (None = at the end).
        """
        existing = self.sections.pack_slaves()
        box = Card(self.sections, title, subtitle)
        options: dict[str, Any] = {"fill": "x", "pady": (0, self.page.gap())}
        if index is not None and 0 <= index < len(existing):
            options["before"] = existing[index]
        box.pack(**options)
        builder(box.body)
        return box

    # --- built-in cards
    def _build_community(self) -> Card | None:
        """Best result vs the closest community presets: what differs, source, date, confidence."""
        rows = self.session.community_comparison(self.best)
        if not rows:
            return None

        def fill(body: ttk.Frame) -> None:
            for row in rows:
                preset = row.preset
                head = frame(body)
                head.pack(fill="x", pady=(px(self, 8), px(self, 2)))
                who = preset.name if preset.attribution != "user" else t("gui.preset.user_values")
                label(head, who, "Strong").pack(side="left")
                Chip(head, t(f"gui.preset.confidence.{preset.confidence}"),
                     {"high": "success", "medium": "neutral", "low": "warn"}[preset.confidence]).pack(
                    side="left", padx=(px(self, 8), 0))
                WrapLabel(body, t("gui.community.source", source=preset.source_url or t("gui.preset.no_source"),
                                  date=preset.source_date or t("gui.preset.undated", retrieved=preset.retrieved)),
                          "Small").pack(fill="x")
                bullets(body, [t(key, **params) for key, params in row.lines], role="Muted").pack(fill="x")
            WrapLabel(body, t("gui.community.note"), "Small").pack(fill="x", pady=(px(self, 10), 0))

        return self.add_section(t("gui.community.title"), fill, t("gui.community.subtitle"))

    def _build_suggestions(self) -> Card | None:
        """Plugins that are not installed (policy "allow_new"): name, why, where, how. Never installed by us."""
        suggestions = self.session.plugin_suggestions()
        if not suggestions:
            return None

        def fill(body: ttk.Frame) -> None:
            for item in suggestions:
                label(body, f"{item.display_name} ({item.plugin_name})", "Strong").pack(
                    anchor="w", pady=(px(self, 6), 0))
                bullets(body, [tr(item.reason), t("gui.suggest.source", url=item.homepage),
                               t("engine.suggest.install_hint", plugin=item.plugin_name)], role="Small").pack(fill="x")

        return self.add_section(t("gui.suggest.title"), fill, t("gui.suggest.subtitle"))

    def _label_of(self, path: str) -> str:
        try:
            spec = self.session.services.load_filter_catalog().get(path)
        except Exception:  # the catalog only decorates names
            spec = None
        return getattr(spec, "display_name", None) or path

    def _build_hero(self) -> None:
        s, best, gap = self.session, self.best, self.page.gap
        hero = Card(self, accent=COLORS.accent, padding=22)
        hero.pack(fill="x", pady=(0, gap()))
        body = hero.body
        label(body, t("gui.result.eyebrow"), "Eyebrow").pack(anchor="w")
        top = frame(body)
        top.pack(fill="x", pady=(px(self, 2), px(self, 4)))
        label(top, candidate_label(s, best), "Title").pack(side="left")
        result = s.results.get(best.id)
        chips = frame(top)
        chips.pack(side="left", padx=(px(self, 12), 0))
        if s.chosen_id == best.id:
            Chip(chips, t("gui.result.chip_chosen"), "accent").pack(side="left", padx=(0, 6))
        if result is not None and result.reports:
            Chip(chips, "✓ " + t("gui.result.chip_measured"), "success").pack(side="left", padx=(0, 6))
        if result is not None and result.user_rating:
            Chip(chips, t("gui.result.chip_rating", n=result.user_rating)).pack(side="left", padx=(0, 6))
        WrapLabel(body, t(best.summary_key), "Muted").pack(fill="x")
        label(body, t("gui.result.changes_title"), "Strong").pack(anchor="w", pady=(px(self, 14), px(self, 4)))
        changes = describe_changes(s.profile, best.changes, self._label_of, best.est_added_latency_ms,
                                   detail=True)
        bullets(body, [t(key, **params) for key, params in changes]).pack(fill="x")
        if best.reasons:
            label(body, t("gui.result.why_title"), "Strong").pack(anchor="w", pady=(px(self, 14), px(self, 4)))
            reasons = [tr(reason) for reason in best.reasons]
            bullets(body, reasons[:_SHOWN_REASONS], role="Muted").pack(fill="x")
            if len(reasons) > _SHOWN_REASONS:
                more = bullets(body, reasons[_SHOWN_REASONS:], role="Muted")
                hidden = len(reasons) - _SHOWN_REASONS
                toggle = button(body, "▸  " + t("gui.result.more_reasons", n=hidden), lambda: None,
                                kind="ghost")
                toggle.pack(anchor="w", pady=(px(self, 2), 0))

                def flip() -> None:
                    if more.winfo_manager():
                        more.pack_forget()
                        toggle.configure(text="▸  " + t("gui.result.more_reasons", n=hidden))
                    else:
                        more.pack(fill="x", before=toggle)
                        toggle.configure(text="▾  " + t("gui.result.fewer_reasons"))

                toggle.configure(command=flip)
        if s.dry_run:
            WrapLabel(body, t("gui.result.dry_run_note"), "Small").pack(fill="x", pady=(px(self, 12), 0))
        row = frame(body)
        row.pack(fill="x", pady=(px(self, 16), 0))
        self.buttons: list[ttk.Button] = []
        if s.can_measure:
            self.buttons.append(button(row, t("gui.result.apply"), self.page._on_apply, kind="primary"))
            self.buttons.append(button(row, t("gui.result.save_preset"), self.page._on_preset))
        else:
            self.buttons.append(button(row, t("gui.result.save_preset"), self.page._on_preset, kind="primary"))
        self.buttons.append(button(row, t("gui.result.copy"), self.page._on_copy, kind="ghost"))
        if s.can_measure:
            self.buttons.append(button(row, t("gui.result.restore"), self.page._on_restore, kind="ghost"))
        self.companion_button: ttk.Button | None = None
        self.coach_button: ttk.Button | None = None
        launcher = COMPANION_LAUNCHER   # EXTENSION POINT (see COMPANION_LAUNCHER above)
        if launcher is not None:
            self.companion_button = button(row, t("gui.result.companion"), lambda: launcher(self), kind="ghost")
            self.buttons.append(self.companion_button)
        coach_launcher = COACH_LAUNCHER
        if coach_launcher is not None:
            self.coach_button = button(row, t("gui.result.coach"), lambda: coach_launcher(self), kind="ghost")
            self.buttons.append(self.coach_button)
        target = s.output_dir or (s.config_dir / "Presets" if s.config_dir else None)
        if target is not None:
            WrapLabel(body, t("gui.result.save_where", path=str(target)), "Small").pack(
                fill="x", pady=(px(self, 8), 0), after=row)
        for btn in self.buttons:
            btn.pack(side="left", padx=(0, px(self, 8)))

    def _build_comparison(self) -> None:
        rows = comparison_for(self.session, self.best)
        box = Card(self, t("gui.compare.title"), t("gui.compare.subtitle"))
        InfoTip(box.header, t("gui.tip.jitter") + "\n\n" + t("gui.tip.latency")).pack(
            side="left", padx=(px(self, 8), 0))
        box.pack(fill="x", pady=(0, self.page.gap()))
        if not any(r.before is not None or r.after is not None for r in rows):
            WrapLabel(box.body, t("gui.compare.empty"), "Muted").pack(fill="x")
            return
        chart = BarChart(box.body, t("gui.compare.before"), t("gui.compare.after"), t("gui.compare.missing"))
        chart.pack(fill="x")
        chart.set_rows(rows)
        WrapLabel(box.body, t("gui.compare.note"), "Small").pack(fill="x", pady=(px(self, 6), 0))

    def _build_area(self, parent: tk.Misc) -> Card | None:
        s, best = self.session, self.best
        area = best.changes.tablet_area or (s.profile.tablet_area if s.profile is not None else None)
        if s.tablet is None or area is None or area.width <= 0:
            return None
        box = Card(parent, t("gui.area.title"), t("gui.area.subtitle"))
        InfoTip(box.header, t("gui.tip.area")).pack(side="left", padx=(px(self, 8), 0))
        preview = AreaPreview(box.body)
        preview.pack(fill="x")
        current = s.profile.tablet_area if s.profile is not None else None
        display = (s.display.width_px, s.display.height_px) if s.display is not None else None
        preview.set_area(s.tablet.width_mm, s.tablet.height_mm, area, current, display,
                         {"tablet": t("gui.area.tablet"), "current": t("gui.area.current"),
                          "display": t("gui.area.display")})
        return box

    def _build_filters(self, parent: tk.Misc) -> Card:
        s, best = self.session, self.best
        filters = best.changes.filters
        if filters is None and s.profile is not None:
            filters = s.profile.filters
        box = Card(parent, t("gui.filters.title"), t("gui.filters.subtitle"))
        InfoTip(box.header, t("gui.tip.smoothing") + "\n\n" + t("gui.tip.chatter")).pack(
            side="left", padx=(px(self, 8), 0))
        try:
            catalog = s.services.load_filter_catalog()
        except Exception:  # names and units are decoration only
            catalog = {}
        rows = filter_table(filters or (), catalog)
        if not rows:
            WrapLabel(box.body, t("gui.filters.none"), "Muted").pack(fill="x")
            return box
        count = sum(1 + len(params) for _name, _on, params in rows)
        tree = ttk.Treeview(box.body, columns=("value", "unit"), show="tree headings",
                            height=min(count, 12), selectmode="none")
        tree.heading("#0", text=t("gui.filters.col.name"), anchor="w")
        tree.heading("value", text=t("gui.filters.col.value"), anchor="w")
        tree.heading("unit", text=t("gui.filters.col.unit"), anchor="w")
        tree.column("#0", width=px(self, 220), stretch=True)
        tree.column("value", width=px(self, 90), anchor="w", stretch=False)
        tree.column("unit", width=px(self, 60), anchor="w", stretch=False)
        tree.tag_configure("filter", font=font("strong"))
        tree.tag_configure("off", foreground=COLORS.subtle)
        for index, (name, enabled, params) in enumerate(rows):
            parent_id = tree.insert("", "end", text=f"{index + 1}. {name}" + (
                "" if enabled else f"  ({t('gui.value.off')})"), open=True,
                tags=("filter",) if enabled else ("filter", "off"))
            for param, value, unit in params:
                tree.insert(parent_id, "end", text=param, values=(value, unit))
        tree.pack(fill="x")
        notes = []   # osu!-policy / user-decision notes of the filters in this chain
        for cfg in filters or ():
            spec = catalog.get(cfg.path)
            key = getattr(spec, "policy_note", "")
            if key and cfg.enabled:
                notes.append(f"{spec.display_name}: {t(key)}")
        if notes:
            bullets(box.body, notes, role="Small").pack(fill="x", pady=(px(self, 8), 0))
        return box


class ResultPage(Page):
    step = Step.RESULT
    next_key = "gui.nav.finish"

    def build(self) -> None:
        s = self.session
        self.view: ResultsView | None = None
        self.title("gui.result.title")
        try:
            best = s.best_candidate()
        except SessionError as exc:
            message, detail = error_text(exc)
            notice = Notice(self)
            notice.pack(fill="x")
            notice.show(message, "error", detail)
            return
        if best is None:
            box = self.card(accent=COLORS.border_strong)
            label(box.body, t("gui.result.empty_title"), "Heading").pack(anchor="w")
            WrapLabel(box.body, t("gui.result.nothing"), "Muted").pack(fill="x", pady=(px(self, 4), 0))
            row = self.actions(box.body)
            row.pack(fill="x", pady=(px(self, 12), 0))
            button(row, t("gui.result.back_to_testing"), self.wizard.on_back).pack(side="left")
            return
        self.view = ResultsView(self, s, best)
        self.view.pack(fill="x")
        self.view.report.set(s.report_text())
        self._log()

    @property
    def _buttons(self) -> list[ttk.Button]:
        return self.view.buttons if self.view is not None else []

    def _log(self) -> None:
        if self.view is not None:
            self.view.log.set("\n".join(self.session.log) or "-")

    def _enable(self, enabled: bool) -> None:
        for btn in self._buttons:
            btn.configure(state="normal" if enabled else "disabled")
        self.wizard.set_busy(not enabled)

    def _say(self, text: str, error: bool = False, detail: str = "") -> None:
        if self.view is not None:
            self.view.status.show(text, "error" if error else "success", detail)
        self._log()

    def _fail(self, exc: BaseException) -> None:
        if self.alive():
            self._enable(True)
            message, detail = error_text(exc)
            self.session.log.append(detail)
            self._say(message, error=True, detail=detail)

    def _background(self, work: Callable[[], Any], done: Callable[[Any], None]) -> None:
        self._enable(False)
        run_in_thread(self, work, done, self._fail)

    def _on_preset(self) -> None:
        SaveDialog(self)

    def save_as(self, name: str) -> bool:
        """Export under ``name``; asks before replacing an existing file. False if cancelled."""
        try:
            target = self.session.export_target(name)
        except SessionError as exc:
            self._fail(exc)
            return False
        overwrite = target.exists()
        if overwrite and not messagebox.askyesno(
                t("gui.save.overwrite_title"),
                t("gui.save.overwrite", path=str(target), backup=str(target.parent / "Backup")), parent=self):
            return False
        self._background(lambda: self.session.export_preset(confirmed=True, name=name, overwrite=overwrite),
                         self._preset_done)
        return True

    def _preset_done(self, path: Path) -> None:
        if self.alive():
            self._enable(True)
            self._say(t("gui.result.preset_saved", path=str(path)))

    def _on_apply(self) -> None:
        backup = self.session.backup_path
        text = t("gui.result.apply_confirm", backup=str(backup) if backup else t("gui.testing.consent_backup_new"))
        if not messagebox.askyesno(t("gui.result.apply_title"), text, parent=self):
            return
        self._background(lambda: self.session.apply_final(confirmed=True), self._apply_done)

    def _apply_done(self, result: ApplyResult) -> None:
        if self.alive():
            self._enable(True)
            self._say(t(result.message_key), error=not result.ok, detail=result.detail if not result.ok else "")

    def _on_restore(self) -> None:
        if not messagebox.askyesno(t("gui.testing.restore_title"), t("gui.testing.restore_confirm"), parent=self):
            return
        self._background(lambda: self.session.restore_original(confirmed=True), self._apply_done)

    def _on_copy(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.session.report_text())
        self._say(t("gui.result.copied"))

    def next_enabled(self) -> bool:
        return True


class SaveDialog(tk.Toplevel):
    """Name (editable) and folder of the settings file to save; Save asks before overwriting."""

    def __init__(self, page: ResultPage) -> None:
        super().__init__(page)
        self.page, self.session = page, page.session
        self.title(t("gui.save.title"))
        self.transient(page.winfo_toplevel())
        self.configure(background=COLORS.bg)
        body = ttk.Frame(self, padding=px(self, 20))
        body.pack(fill="both", expand=True)
        WrapLabel(body, t("gui.save.intro")).pack(fill="x")
        label(body, t("gui.save.name"), "Strong").pack(anchor="w", pady=(px(self, 12), px(self, 4)))
        default = self.session.default_export_name() if self.session.output_dir else self.session.preset_name()
        self.name = tk.StringVar(value=default)
        entry = ttk.Entry(body, textvariable=self.name, width=60)
        entry.pack(fill="x")
        label(body, t("gui.save.folder"), "Strong").pack(anchor="w", pady=(px(self, 12), px(self, 4)))
        self.folder = WrapLabel(body, "", "Muted")
        self.folder.pack(fill="x")
        row = frame(body)
        row.pack(fill="x", pady=(px(self, 6), 0))
        button(row, t("gui.save.change_folder"), self._pick).pack(side="left")
        button(row, t("gui.save.otd_folder"), self._use_otd, kind="ghost").pack(side="left", padx=(px(self, 8), 0))
        self.error = label(body, "", "Error")
        self.error.pack(anchor="w", pady=(px(self, 8), 0))
        actions = frame(body)
        actions.pack(fill="x", pady=(px(self, 12), 0))
        button(actions, t("gui.save.save"), self._save, kind="primary").pack(side="right")
        button(actions, t("gui.save.cancel"), self.destroy, kind="ghost").pack(side="right", padx=px(self, 8))
        self._show_folder()
        entry.focus_set()
        self.bind("<Return>", lambda _e: self._save())
        self.bind("<Escape>", lambda _e: self.destroy())

    def _show_folder(self) -> None:
        if self.session.output_dir is not None:
            self.folder.configure(text=str(self.session.output_dir))
        else:
            path = self.session.config_dir / "Presets" if self.session.config_dir else "-"
            self.folder.configure(text=t("gui.save.otd_presets", path=str(path)))

    def _pick(self) -> None:
        chosen = filedialog.askdirectory(parent=self, title=t("gui.save.pick_title"),
                                         initialdir=str(self.session.output_dir or Path.home()))
        if chosen:
            self.session.set_output_dir(Path(chosen))
            if self.name.get() == self.session.preset_name():
                self.name.set(self.session.default_export_name())
            self._show_folder()

    def _use_otd(self) -> None:
        self.session.set_output_dir(None)
        self._show_folder()

    def _save(self) -> None:
        name = self.name.get().strip()
        if not name:
            self.error.configure(text=t("gui.save.name_missing"))
            return
        if self.page.save_as(name):
            self.destroy()


class SettingsDialog(tk.Toplevel):
    """The assistant's own settings: output folder, file names, plugin policy, community data."""

    def __init__(self, wizard: "Wizard") -> None:
        super().__init__(wizard)
        self.wizard, self.session = wizard, wizard.session
        cfg = self.session.app_config
        self.title(t("gui.settings.title"))
        self.transient(wizard.winfo_toplevel())
        self.configure(background=COLORS.bg)
        scroll = ScrollFrame(self, max_width=px(self, 760))
        scroll.pack(fill="both", expand=True)
        body = ttk.Frame(scroll.body, padding=px(self, 20))
        body.pack(fill="both", expand=True)
        out = Card(body, t("gui.settings.output_title"), t("gui.settings.output_help"))
        out.pack(fill="x", pady=(0, px(self, 12)))
        self.output = tk.StringVar(value=cfg.output_dir or "")
        ttk.Entry(out.body, textvariable=self.output, state="readonly").pack(fill="x")
        row = frame(out.body)
        row.pack(fill="x", pady=(px(self, 6), 0))
        button(row, t("gui.settings.browse"), self._browse).pack(side="left")
        button(row, t("gui.settings.clear"), lambda: self.output.set(""), kind="ghost").pack(
            side="left", padx=(px(self, 8), 0))
        label(out.body, t("gui.settings.name_template"), "Strong").pack(anchor="w", pady=(px(self, 10), 2))
        self.template = tk.StringVar(value=cfg.preset_name_template)
        ttk.Entry(out.body, textvariable=self.template).pack(fill="x")
        label(out.body, t("gui.settings.name_template_help"), "Small").pack(anchor="w")
        pol = Card(body, t("gui.questions.plugin_policy.title"), t("gui.questions.plugin_policy.why"))
        pol.pack(fill="x", pady=(0, px(self, 12)))
        self.policy = tk.StringVar(value=self.session.prefs.plugin_policy.value)
        for policy in PluginPolicy:
            radio(pol.body, t(f"core.pref.plugin_policy.{policy.value}"), policy.value, self.policy).pack(anchor="w")
        upd = Card(body, t("gui.settings.update_title"), t("gui.settings.update_help"))
        upd.pack(fill="x", pady=(0, px(self, 12)))
        self.on_start = tk.BooleanVar(value=cfg.check_updates_on_start)
        check(upd.body, t("gui.settings.check_on_start"), self.on_start).pack(anchor="w")
        label(upd.body, t("gui.settings.update_url"), "Strong").pack(anchor="w", pady=(px(self, 8), 2))
        self.url = tk.StringVar(value=cfg.update_url)
        ttk.Entry(upd.body, textvariable=self.url).pack(fill="x")
        prof = Card(body, t("gui.settings.profile_title"), t("gui.settings.profile_help"))
        prof.pack(fill="x", pady=(0, px(self, 12)))
        self._profile_button = button(prof.body, t("gui.settings.profile_forget"), self._forget_profile, kind="secondary")
        self._profile_button.pack(anchor="w")
        if self.session.profile_store is None or not self.session.profile_store.profiles:
            self._profile_button.configure(state="disabled")
        ledger = Card(body, t("gui.settings.ledger_title"), t("gui.settings.ledger_help"))
        ledger.pack(fill="x", pady=(0, px(self, 12)))
        WrapLabel(ledger.body, ledger_text(t), "Small").pack(fill="x")
        self.data = DataCard(body, wizard, settings_button=False)
        self.data.pack(fill="x", pady=(0, px(self, 12)))
        self.message = Notice(body)
        self.message.pack(fill="x")
        actions = frame(body)
        actions.pack(fill="x", pady=(px(self, 8), 0))
        button(actions, t("gui.settings.save"), self._save, kind="primary").pack(side="right")
        button(actions, t("gui.settings.close"), self.destroy, kind="ghost").pack(side="right", padx=px(self, 8))
        self.bind("<Escape>", lambda _e: self.destroy())

    def _forget_profile(self) -> None:
        if not messagebox.askyesno(t("gui.settings.profile_title"), t("gui.settings.profile_confirm"), parent=self):
            return
        ok = self.session.forget_profile()
        self._profile_button.configure(state="disabled")
        self.message.show(t("gui.settings.profile_forgotten" if ok else "gui.settings.profile_not_saved"),
                          "success" if ok else "warn")

    def _browse(self) -> None:
        chosen = filedialog.askdirectory(parent=self, title=t("gui.save.pick_title"),
                                         initialdir=self.output.get() or str(Path.home()))
        if chosen:
            self.output.set(chosen)

    def apply(self) -> bool:
        """Copy the form into the session's config and persist it; False (with a message) if invalid."""
        from ..appconfig import from_dict
        cfg = self.session.app_config
        url = self.url.get().strip()
        if from_dict({"update_url": url}).update_url != url:
            self.message.show(t("gui.settings.bad_url"), "error")
            return False
        template = self.template.get().strip() or cfg.preset_name_template
        cfg.preset_name_template = template
        cfg.update_url = url
        cfg.check_updates_on_start = bool(self.on_start.get())
        self.session.set_output_dir(Path(self.output.get()) if self.output.get() else None)
        self.session.set_plugin_policy(PluginPolicy(self.policy.get()))
        return True

    def _save(self) -> None:
        if self.apply():
            self.message.show(t("gui.settings.saved"), "success")


PAGES: dict[Step, type[Page]] = {
    page.step: page for page in (WelcomePage, DetectPage, TabletPage, DisplayPage, QuestionsPage,
                                 BaselinePage, TestingPage, ResultPage)
}


# ----------------------------------------------------------------------- shell
class Wizard(ttk.Frame):
    """Shell around the pages: stepper, error banner, Back/Next."""

    def __init__(self, parent: tk.Misc, session: Session, config_hint: Path | None = None,
                 install_hint: Path | None = None,
                 on_close: Callable[[], None] | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.config_hint = config_hint
        self.install_hint = install_hint
        self.apply_consent = False
        self.slots: list[StepSlot] = list(SLOTS)
        self.slot: StepSlot | None = None          # GUI-only step currently shown
        self._on_close = on_close or self.winfo_toplevel().destroy
        self._busy = False
        self.page: Page | None = None
        top = ttk.Frame(self, padding=(px(self, 24), px(self, 14), px(self, 24), px(self, 4)))
        top.pack(fill="x")
        self.indicator = Stepper(top)
        self.indicator.pack(fill="x")
        tk.Frame(self, height=1, background=COLORS.border).pack(fill="x")
        footer = ttk.Frame(self, padding=(px(self, 24), px(self, 12)))
        footer.pack(side="bottom", fill="x")
        tk.Frame(self, height=1, background=COLORS.border).pack(side="bottom", fill="x")
        self._counter = label(footer, "", "Small")
        self._counter.pack(side="left")
        self._progress = ttk.Progressbar(footer, mode="indeterminate", length=px(self, 120),
                                         style="Accent.Horizontal.TProgressbar")
        self._next = button(footer, "", self.on_next, kind="primary")
        self._back = button(footer, "", self.on_back)
        self._next.pack(side="right")
        self._back.pack(side="right", padx=px(self, 8))
        self._error_holder = ttk.Frame(self, padding=(px(self, 24), 0))
        self._error_holder.pack(side="bottom", fill="x")
        self._error = Notice(self._error_holder)
        self._error.pack(fill="x", pady=(0, px(self, 8)))
        self._scroll = ScrollFrame(self, max_width=px(self, _PAGE_MAX_WIDTH))
        self._scroll.pack(fill="both", expand=True)
        root = self.winfo_toplevel()
        root.bind("<Alt-Right>", lambda _e: self._invoke(self._next), add="+")
        root.bind("<Alt-Left>", lambda _e: self._invoke(self._back), add="+")
        self.show_current()

    # --- rendering
    def visible_steps(self) -> list[str]:
        """i18n keys of the steps shown in the stepper (session steps plus visible slots)."""
        keys = []
        for step in self.session.goal_steps():
            keys.append(f"gui.step.{step.value}")
            keys += [slot.key for slot in self._slots_after(step)]
        return keys

    def _slots_after(self, step: Step) -> list[StepSlot]:
        return [slot for slot in self.slots if slot.after is step and slot.visible(self.session)]

    def show_current(self) -> None:
        """(Re)build the page of the current step (or slot) in the current language."""
        for child in self._scroll.body.winfo_children():
            child.destroy()
        self.clear_error()
        page_cls = self.slot.page if self.slot is not None else PAGES[self.session.step]
        self.page = page_cls(self._scroll.body, self)
        self.page.pack(fill="both", expand=True)
        self._scroll.scroll_top()
        keys = self.visible_steps()
        current = self.slot.key if self.slot is not None else f"gui.step.{self.session.step.value}"
        index = keys.index(current)
        self.indicator.show([t(k) for k in keys], index)
        self._counter.configure(text=t("gui.nav.counter", n=index + 1, total=len(keys)))
        self.update_nav()

    def refresh_stepper(self) -> None:
        """Redraw the step indicator (the chosen goal changes how many steps there are)."""
        keys = self.visible_steps()
        current = self.slot.key if self.slot is not None else f"gui.step.{self.session.step.value}"
        self.indicator.show([t(k) for k in keys], keys.index(current))
        self._counter.configure(text=t("gui.nav.counter", n=keys.index(current) + 1, total=len(keys)))

    def rebuild(self) -> None:
        """Re-render after a language change, keeping what was typed."""
        if self.page is not None:
            self.page.commit()
        self.show_current()

    def update_nav(self) -> None:
        """Refresh Back/Next labels and enabled state."""
        if self.page is None:
            return
        self._back.configure(text="←  " + t("gui.nav.back"))
        self._next.configure(text=self.page.next_label() + "  →")
        can_back = self.session.can_go_back() or self.slot is not None
        self._back.configure(state="normal" if can_back and not self._busy else "disabled")
        self._next.configure(state="normal" if self.page.next_enabled() and not self._busy else "disabled")

    def set_busy(self, busy: bool) -> None:
        """Disable navigation and show a wait cursor and progress bar during long operations."""
        self._busy = busy
        self.configure(cursor="watch" if busy else "")
        if busy:
            self._progress.pack(side="left", padx=px(self, 12))
            self._progress.start(12)
        else:
            self._progress.stop()
            self._progress.pack_forget()
        self.update_nav()

    def show_error(self, exc: BaseException) -> None:
        """Show a failure in the error banner, technical detail expandable (and logged)."""
        message, detail = error_text(exc)
        if detail:
            self.session.log.append(detail)
        self._error.show(message, "error", detail)

    def clear_error(self) -> None:
        """Hide the error banner."""
        self._error.hide()

    def open_settings(self) -> "SettingsDialog":
        """Open (or raise) the Settings window."""
        existing = getattr(self, "_settings", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return existing
        self._settings = SettingsDialog(self)
        return self._settings

    @staticmethod
    def _invoke(btn: ttk.Button) -> None:
        if str(btn.cget("state")) != "disabled":
            btn.invoke()

    # --- navigation
    def on_next(self) -> None:
        """Validate the page, then move on (in a worker: the next step may be slow)."""
        if self.page is None:
            return
        if self.session.step is Step.RESULT:
            self.request_close()
            return
        self.clear_error()
        if not self.page.commit():
            return
        if self.session.at_goal_launch:
            self.launch_goal()
            return
        if self.slot is None:
            slots = self._slots_after(self.session.step)
            if slots and self.session.can_advance():
                self.slot = slots[0]
                self.show_current()
                return
        self.set_busy(True)
        run_in_thread(self, self.session.advance, lambda _s: self._moved(), self._move_failed)

    def resume(self) -> None:
        """Skip the repeated steps using the saved profile (detect OTD, load choices, open the test list)."""
        self.clear_error()
        self.set_busy(True)
        run_in_thread(self, self.session.resume_from_profile, lambda _r: self._moved(), self._resume_failed)

    def _resume_failed(self, exc: BaseException) -> None:
        self.set_busy(False)
        self.show_error(exc)
        self.show_current()      # whatever step the attempt reached; the normal flow continues from there

    def launch_goal(self) -> None:
        """Open the live-tuning companion or the Play Coach on the current session."""
        launcher = COACH_LAUNCHER if self.session.goal == "coach" else COMPANION_LAUNCHER
        if launcher is None:
            self.show_error(SessionError("core.error.no_launcher"))
            return
        try:
            launcher(LaunchContext(self.session, self))
        except Exception as exc:   # the window must never take the wizard down with it
            self.show_error(exc)

    def _moved(self) -> None:
        self.slot = None
        self.set_busy(False)
        self.show_current()

    def _move_failed(self, exc: BaseException) -> None:
        self.set_busy(False)
        self.show_error(exc)

    def on_back(self) -> None:
        """Go back one step (slots included)."""
        if self.page is not None:
            self.page.commit()
        if self.slot is not None:
            self.slot = None
        else:
            self.session.back()
            slots = self._slots_after(self.session.step)   # the slot sits between the two pages
            self.slot = slots[-1] if slots else None
        self.show_current()

    def request_close(self) -> None:
        """Close the app, offering to restore OTD if a test option is still live."""
        if self.session.has_unfinished_changes:
            answer = messagebox.askyesnocancel(
                t("gui.close.title"), t("gui.close.restore_question"), parent=self)
            if answer is None:
                return
            if answer:
                self.set_busy(True)
                run_in_thread(self, lambda: self.session.restore_original(confirmed=True),
                              lambda _r: self._on_close(), self._restore_failed)
                return
        self._on_close()

    def _restore_failed(self, exc: BaseException) -> None:
        self.set_busy(False)
        self.show_error(exc)
