"""The wizard: a shell (step indicator, Back/Next) plus one page per session step.

Pages only render ``Session`` state and forward user actions to it; anything
that may take time (talking to OTD, generating candidates) runs in a worker
thread through ``run_in_thread`` so the window never freezes.
"""
from __future__ import annotations

import tkinter as tk
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
    Priority,
    SkillLevel,
)
from .i18n import t, tr
from .session import (
    BASELINE_KINDS,
    CANDIDATE_KINDS,
    STEP_ORDER,
    Session,
    SessionError,
    Step,
    TabletChoice,
    metric_lines,
)
from .widgets import Expander, ReadOnlyText, ScrollFrame, StepIndicator, WrapLabel, parse_float, px, run_in_thread

_PAD = 12


def error_text(exc: BaseException) -> tuple[str, str]:
    """User message and technical detail for any exception raised by the session."""
    if isinstance(exc, SessionError):
        return t(exc.key, **exc.params), exc.detail
    return t("core.error.unexpected"), f"{type(exc).__name__}: {exc}"


class Page(ttk.Frame):
    """Base class of a wizard page."""

    step: Step
    next_key = "gui.nav.next"

    def __init__(self, parent: tk.Misc, wizard: "Wizard") -> None:
        super().__init__(parent, padding=(0, 0, 0, _PAD))
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

    def title(self, key: str) -> None:
        """Add the page heading."""
        ttk.Label(self, text=t(key), style="Title.TLabel").pack(anchor="w", pady=(0, 6))

    def para(self, text: str, muted: bool = False, pady: tuple[int, int] = (0, 8)) -> WrapLabel:
        """Add a wrapped paragraph."""
        label = WrapLabel(self, text, style="Muted.TLabel" if muted else "TLabel")
        label.pack(fill="x", pady=pady)
        return label

    def alive(self) -> bool:
        """False once the page was destroyed (language change, window closed)."""
        try:
            return bool(self.winfo_exists())
        except tk.TclError:
            return False


# ---------------------------------------------------------------------- welcome
class WelcomePage(Page):
    step = Step.WELCOME

    def build(self) -> None:
        self.title("gui.welcome.title")
        self.para(t("gui.welcome.intro"))
        self.para(t("gui.welcome.what_happens"))
        box = ttk.LabelFrame(self, text=t("gui.welcome.disclaimer_title"), padding=_PAD)
        box.pack(fill="x", pady=(4, 8))
        WrapLabel(box, t("gui.welcome.disclaimer")).pack(fill="x")
        if self.session.dry_run:
            self.para(t("gui.welcome.dry_run"), muted=True)
        self._accepted = tk.BooleanVar(value=self.session.accepted_disclaimer)
        ttk.Checkbutton(self, text=t("gui.welcome.accept"), variable=self._accepted,
                        command=self._toggle).pack(anchor="w", pady=(6, 0))

    def _toggle(self) -> None:
        self.session.accepted_disclaimer = self._accepted.get()
        self.wizard.update_nav()


# ----------------------------------------------------------------------- detect
class DetectPage(Page):
    step = Step.DETECT

    def build(self) -> None:
        self.title("gui.detect.title")
        self.para(t("gui.detect.intro"))
        self._status = WrapLabel(self, "")
        self._status.pack(fill="x", pady=(0, 6))
        self._summary = WrapLabel(self, "")
        self._summary.pack(fill="x", pady=(0, 6))
        row = ttk.Frame(self)
        row.pack(fill="x", pady=4)
        ttk.Button(row, text=t("gui.detect.retry"), command=self._start).pack(side="left")
        ttk.Button(row, text=t("gui.detect.pick_config"),
                   command=lambda: self._pick(True)).pack(side="left", padx=6)
        ttk.Button(row, text=t("gui.detect.pick_install"),
                   command=lambda: self._pick(False)).pack(side="left")
        self.para(t("gui.detect.safe_note"), muted=True, pady=(8, 4))
        self._details = Expander(self)
        self._details.pack(fill="x", pady=(8, 0))
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
        self._status.configure(text=t("gui.detect.searching"), style="TLabel")
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
        self._status.configure(text=f"{message}\n{t('gui.detect.failed_hint')}", style="Error.TLabel")
        self._summary.configure(text="")
        self._details.set(detail + "\n" + "\n".join(self.session.log))
        self.wizard.update_nav()

    def _render(self) -> None:
        s = self.session
        self._status.configure(text=t("gui.detect.found"), style="Good.TLabel")
        names = ", ".join(p.tablet_name for p in s.profiles) or t("gui.detect.no_profiles")
        self._summary.configure(text=t(
            "gui.detect.summary", config=str(s.config_dir), install=str(s.install_dir or "-"),
            profiles=names, plugins=len(s.plugins)))
        lines = [f"config_dir: {s.config_dir}", f"install_dir: {s.install_dir}"]
        lines += [f"profile: {p.tablet_name} | {p.output_mode_path} | area "
                  f"{p.tablet_area.width:g}x{p.tablet_area.height:g} mm" for p in s.profiles]
        lines += [f"plugin: {p.name} {p.version or ''}".strip() for p in s.plugins]
        lines.append(f"filters available: {len(s.filter_paths)}")
        self._details.set("\n".join(lines + s.log))
        self.wizard.update_nav()


# ----------------------------------------------------------------------- tablet
class TabletPage(Page):
    step = Step.TABLET

    def build(self) -> None:
        self._choices: list[TabletChoice] = self.session.tablet_choices()
        self.title("gui.tablet.title")
        self.para(t("gui.tablet.intro"))
        search = ttk.Frame(self)
        search.pack(fill="x")
        ttk.Label(search, text=t("gui.tablet.search")).pack(side="left")
        self._filter = tk.StringVar()
        self._filter.trace_add("write", lambda *_: self._fill())
        ttk.Entry(search, textvariable=self._filter).pack(side="left", fill="x", expand=True, padx=6)
        holder = ttk.Frame(self)
        holder.pack(fill="both", expand=True, pady=6)
        self._tree = ttk.Treeview(holder, columns=("name", "area", "rate", "source"),
                                  show="headings", height=8, selectmode="browse")
        for col, key, width in (("name", "gui.tablet.col.name", 220), ("area", "gui.tablet.col.area", 120),
                                ("rate", "gui.tablet.col.rate", 80), ("source", "gui.tablet.col.source", 140)):
            self._tree.heading(col, text=t(key))
            self._tree.column(col, width=px(self, width), anchor="w")
        bar = ttk.Scrollbar(holder, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=bar.set)
        self._tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self._tree.bind("<<TreeviewSelect>>", self._on_select)
        self._summary = WrapLabel(self, "")
        self._summary.pack(fill="x", pady=4)
        self._link = ttk.Frame(self)
        ttk.Label(self._link, text=t("gui.tablet.link_profile")).pack(side="left")
        self._link_box = ttk.Combobox(self._link, state="readonly", width=34,
                                      values=[p.tablet_name for p in self.session.profiles])
        self._link_box.pack(side="left", padx=6)
        self._link_box.bind("<<ComboboxSelected>>", self._on_link)
        self.para(t("gui.tablet.approx_note"), muted=True)
        self._build_custom()
        self._fill()
        self._show_summary()

    def _build_custom(self) -> None:
        box = ttk.LabelFrame(self, text=t("gui.tablet.custom_title"), padding=_PAD)
        box.pack(fill="x", pady=(6, 0))
        WrapLabel(box, t("gui.tablet.custom_help"), style="Muted.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="we", pady=(0, 6))
        self._vars = {k: tk.StringVar(value=v) for k, v in (
            ("name", ""), ("width", ""), ("height", ""), ("rate", "133"), ("pressure", "0"))}
        fields = (("name", "gui.tablet.custom.name"), ("width", "gui.tablet.custom.width"),
                  ("height", "gui.tablet.custom.height"), ("rate", "gui.tablet.custom.rate"),
                  ("pressure", "gui.tablet.custom.pressure"))
        for index, (key, label) in enumerate(fields):
            row, col = 1 + index // 2, (index % 2) * 2
            ttk.Label(box, text=t(label)).grid(row=row, column=col, sticky="w", padx=(0, 6), pady=2)
            ttk.Entry(box, textvariable=self._vars[key], width=24 if key == "name" else 10).grid(
                row=row, column=col + 1, sticky="w", padx=(0, 16), pady=2)
        ttk.Button(box, text=t("gui.tablet.custom.use"), command=self._use_custom).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self._custom_error = ttk.Label(box, text="", style="Error.TLabel")
        self._custom_error.grid(row=4, column=2, columnspan=2, sticky="w", pady=(6, 0))

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
            self._summary.configure(text=t("gui.tablet.none_selected"))
            return
        text = t("gui.tablet.selected", name=s.tablet.name, w=f"{s.tablet.width_mm:g}",
                 h=f"{s.tablet.height_mm:g}", rate=f"{s.tablet.max_report_rate_hz:g}")
        if s.profile is not None:
            text += "\n" + t("gui.tablet.profile_linked")
        else:
            text += "\n" + t("gui.tablet.pick_profile" if s.profiles else "gui.tablet.no_profiles_at_all")
        self._summary.configure(text=text)
        if s.profile is None and s.profiles:
            self._link.pack(fill="x", pady=2, after=self._summary)
        else:
            self._link.pack_forget()


# ---------------------------------------------------------------------- display
class DisplayPage(Page):
    step = Step.DISPLAY

    def build(self) -> None:
        self.title("gui.display.title")
        self.para(t("gui.display.intro"))
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
        box = ttk.LabelFrame(self, text=t("gui.display.values"), padding=_PAD)
        box.pack(fill="x", pady=6)
        for index, key in enumerate(("width", "height", "left", "top", "refresh")):
            ttk.Label(box, text=t(f"gui.display.{key}")).grid(row=index, column=0, sticky="w", pady=2)
            ttk.Entry(box, textvariable=self._vars[key], width=10).grid(
                row=index, column=1, sticky="w", padx=8, pady=2)
            WrapLabel(box, t(f"gui.display.{key}_help"), style="Muted.TLabel").grid(
                row=index, column=2, sticky="we", pady=2)
        box.columnconfigure(2, weight=1)
        ttk.Button(self, text=t("gui.display.redetect"), command=self._redetect).pack(anchor="w")
        self._message = WrapLabel(self, "", style="Error.TLabel")
        self._message.pack(fill="x", pady=4)
        self.para(t("gui.display.multi_monitor"), muted=True)

    def _redetect(self) -> None:
        try:
            found = self.session.detect_display()
        except SessionError as exc:
            self._message.configure(text=error_text(exc)[0])
            return
        if found is None:
            self._message.configure(text=t("gui.display.detect_failed"))
            return
        self._message.configure(text="")
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
            self._message.configure(text=t("gui.display.invalid"))
            return False
        return True


# ---------------------------------------------------------------- questionnaire
class QuestionsPage(Page):
    step = Step.QUESTIONS

    def build(self) -> None:
        self.title("gui.questions.title")
        self.para(t("gui.questions.intro"))
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
            self._block("keep_area")
            ttk.Checkbutton(
                self, text=t("gui.questions.keep_area.label", width=f"{profile.tablet_area.width:.1f}"),
                variable=self._keep, command=self._on_keep).pack(anchor="w")

    def _block(self, name: str) -> None:
        ttk.Label(self, text=t(f"gui.questions.{name}.title"), style="Heading.TLabel").pack(
            anchor="w", pady=(10, 0))
        WrapLabel(self, t(f"gui.questions.{name}.why"), style="Muted.TLabel").pack(fill="x", pady=(0, 3))

    def _radio(self, name: str, current: Any, enum: type) -> None:
        self._block(name)
        var = tk.StringVar(value=current.value)
        row = ttk.Frame(self)
        row.pack(fill="x")
        for member in enum:
            ttk.Radiobutton(row, text=t(f"core.pref.{name}.{member.value}"), value=member.value,
                            variable=var).pack(anchor="w")
        var.trace_add("write", lambda *_: setattr(self.session.prefs, name, enum(var.get())))

    def _check(self, name: str, current: bool) -> None:
        self._block(name)
        var = tk.BooleanVar(value=current)
        ttk.Checkbutton(self, text=t(f"gui.questions.{name}.label"), variable=var).pack(anchor="w")
        var.trace_add("write", lambda *_: setattr(self.session.prefs, name, var.get()))

    def _on_keep(self) -> None:
        profile = self.session.profile
        self.session.prefs.preferred_area_width_mm = (
            profile.tablet_area.width if profile is not None and self._keep.get() else None)


# --------------------------------------------------------------------- baseline
class BaselinePage(Page):
    step = Step.BASELINE

    def build(self) -> None:
        self.title("gui.baseline.title")
        self.para(t("gui.baseline.intro"))
        self._rows: dict[str, tuple[ttk.Button, ttk.Label]] = {}
        for kind in BASELINE_KINDS:
            box = ttk.LabelFrame(self, text=t(f"gui.test.{kind}.name"), padding=_PAD)
            box.pack(fill="x", pady=4)
            WrapLabel(box, t(f"gui.test.{kind}.desc")).pack(fill="x")
            row = ttk.Frame(box)
            row.pack(fill="x", pady=(6, 0))
            button = ttk.Button(row, text=t("gui.baseline.run"), command=lambda k=kind: self._run(k))
            button.pack(side="left")
            status = ttk.Label(row, text="", style="Muted.TLabel")
            status.pack(side="left", padx=10)
            self._rows[kind] = (button, status)
            self._show_status(kind)
        self.para(t("gui.baseline.skip_note"), muted=True, pady=(8, 0))
        self._message = WrapLabel(self, "", style="Error.TLabel")
        self._message.pack(fill="x")

    def _show_status(self, kind: str) -> None:
        report = self.session.baseline_report(kind)
        text = "; ".join(metric_lines([report])) if report else t("gui.baseline.not_done")
        self._rows[kind][1].configure(text=text)

    def _set_enabled(self, enabled: bool) -> None:
        for button, _status in self._rows.values():
            button.configure(state="normal" if enabled else "disabled")
        self.wizard.set_busy(not enabled)

    def _run(self, kind: str) -> None:
        self._message.configure(text="")
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
            self._message.configure(text=error_text(exc)[0])


# ---------------------------------------------------------------------- testing
class TestingPage(Page):
    """Candidate loop: apply, measure, rate, refine."""

    step = Step.TESTING
    next_key = "gui.nav.see_result"

    def build(self) -> None:
        s = self.session
        self._testing_id: str | None = None
        self._rating = tk.IntVar(value=0)
        self.title("gui.testing.title")
        self.para(t("gui.testing.intro_dry" if s.dry_run else "gui.testing.intro"))
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        holder = ttk.Frame(body)
        holder.pack(side="left", fill="both", expand=True)
        self._tree = ttk.Treeview(holder, columns=("label", "status", "score", "rating"),
                                  show="headings", height=7, selectmode="browse")
        for col, key, width in (("label", "gui.testing.col.option", 200), ("status", "gui.testing.col.status", 90),
                                ("score", "gui.testing.col.score", 60), ("rating", "gui.testing.col.rating", 60)):
            self._tree.heading(col, text=t(key))
            self._tree.column(col, width=px(self, width), anchor="w")
        self._tree.pack(fill="both", expand=True)
        self._tree.bind("<<TreeviewSelect>>", lambda _e: self._show_detail())
        side = ttk.Frame(body, padding=(_PAD, 0, 0, 0))
        side.pack(side="left", fill="both", expand=True)
        self._head = ttk.Label(side, text="", style="Heading.TLabel")
        self._head.pack(anchor="w")
        self._info = WrapLabel(side, "")
        self._info.pack(fill="x", pady=4)
        self._buttons = ttk.Frame(self)
        self._buttons.pack(fill="x", pady=6)
        self._btn_test = ttk.Button(self._buttons, text=t("gui.testing.test"), command=self._on_test)
        self._btn_use = ttk.Button(self._buttons, text=t("gui.testing.use"), command=self._on_use)
        self._btn_refine = ttk.Button(self._buttons, text=t("gui.testing.refine"), command=self._on_refine)
        self._btn_restore = ttk.Button(self._buttons, text=t("gui.testing.restore"), command=self._on_restore)
        for button in ((self._btn_test,) if s.can_measure else ()) + (self._btn_use,) + (
                (self._btn_refine, self._btn_restore) if s.can_measure else ()):
            button.pack(side="left", padx=(0, 6))
        self._rating_box = ttk.LabelFrame(self, text=t("gui.testing.rating_title"), padding=8)
        if s.can_measure:
            self._rating_box.pack(fill="x", pady=4)
            ttk.Radiobutton(self._rating_box, text=t("gui.testing.rating_none"), value=0,
                            variable=self._rating, command=self._on_rating).pack(side="left", padx=4)
            for n in range(1, 6):
                ttk.Radiobutton(self._rating_box, text=str(n), value=n, variable=self._rating,
                                command=self._on_rating).pack(side="left", padx=4)
            WrapLabel(self._rating_box, t("gui.testing.rating_help"), style="Muted.TLabel").pack(
                side="left", padx=10, fill="x", expand=True)
        self._status = WrapLabel(self, "")
        self._status.pack(fill="x", pady=4)
        self._details = Expander(self)
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
            return t("gui.testing.status.done")
        if cand.id in s.results and s.results[cand.id].reports:
            return t("gui.testing.status.partial")
        return t("gui.testing.status.pending")

    def _refresh_rows(self) -> None:
        s = self.session
        try:
            s.ranked()  # refreshes the scores
            best = s.best_candidate()
        except SessionError:
            best = None
        keep = self._tree.selection()
        self._tree.delete(*self._tree.get_children())
        for cand in s.candidates:
            result = s.results.get(cand.id)
            score = f"{result.score:.1f}" if result and result.score is not None else ""
            rating = str(result.user_rating) if result and result.user_rating else ""
            star = " *" if best is not None and best.id == cand.id else ""
            self._tree.insert("", "end", iid=cand.id,
                              values=(t(cand.label_key) + star, self._status_of(cand), score, rating))
        if keep and self._tree.exists(keep[0]):
            self._tree.selection_set(keep[0])

    def _show_detail(self) -> None:
        cand = self._selected()
        s = self.session
        if cand is None:
            return
        self._head.configure(text=t(cand.label_key))
        lines = [t(cand.summary_key)]
        if cand.est_added_latency_ms:
            lines.append(t("gui.testing.latency", ms=f"{cand.est_added_latency_ms:.1f}"))
        lines += [f"- {tr(reason)}" for reason in cand.reasons]
        self._info.configure(text="\n".join(lines))
        result = s.results.get(cand.id)
        reports = result.reports if result else ()
        measured = bool(reports)
        self._rating.set((result.user_rating or 0) if result else 0)
        self._details.set("\n".join(metric_lines(reports)) if measured else t("gui.testing.no_data"))
        busy = self._testing_id is not None
        self._btn_test.configure(state="disabled" if busy else "normal")
        self._btn_use.configure(state="normal" if (measured or not s.can_measure) and not busy else "disabled")
        self._btn_refine.configure(state="normal" if s.results_with_reports() and not busy else "disabled")
        self._btn_restore.configure(state="normal" if s.live_candidate_id and not busy else "disabled")
        for child in self._rating_box.winfo_children():
            if isinstance(child, ttk.Radiobutton):
                child.configure(state="normal" if measured and not busy else "disabled")
        self.wizard.update_nav()

    def _fail(self, exc: BaseException) -> None:
        if not self.alive():
            return
        self._testing_id = None
        self.wizard.set_busy(False)
        message, detail = error_text(exc)
        self._status.configure(text=message, style="Error.TLabel")
        self._details.set(detail + "\n" + "\n".join(self.session.log))
        self._refresh_rows()
        self._show_detail()

    def _info_status(self, text: str) -> None:
        self._status.configure(text=text, style="TLabel")

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
            self._info_status(t("gui.testing.cancelled" if cancelled else "gui.testing.finished"))
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
            self._info_status(t("gui.testing.chosen", name=t(cand.label_key)))
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
        self._info_status(t("gui.testing.refined"))
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
        self._info_status(t(result.message_key))
        self._show_detail()


# ----------------------------------------------------------------------- result
class ResultPage(Page):
    step = Step.RESULT
    next_key = "gui.nav.finish"

    def build(self) -> None:
        s = self.session
        self.title("gui.result.title")
        best = s.best_candidate()
        if best is None:
            self.para(t("gui.result.nothing"))
            return
        self.para(t("gui.result.best", name=t(best.label_key)))
        self._report = ReadOnlyText(self, height=14)
        self._report.set(s.report_text())
        self._report.pack(fill="both", expand=True, pady=6)
        row = ttk.Frame(self)
        row.pack(fill="x", pady=4)
        self._buttons = [
            ttk.Button(row, text=t("gui.result.save_preset"), command=self._on_preset),
            ttk.Button(row, text=t("gui.result.copy"), command=self._on_copy),
        ]
        if s.can_measure:
            self._buttons.insert(1, ttk.Button(row, text=t("gui.result.apply"), command=self._on_apply))
            self._buttons.append(ttk.Button(row, text=t("gui.result.restore"), command=self._on_restore))
        for button in self._buttons:
            button.pack(side="left", padx=(0, 6))
        if s.dry_run:
            self.para(t("gui.result.dry_run_note"), muted=True)
        self._status = WrapLabel(self, "")
        self._status.pack(fill="x", pady=4)
        self._details = Expander(self)
        self._details.pack(fill="x")
        self._log()

    def _log(self) -> None:
        self._details.set("\n".join(self.session.log) or "-")

    def _enable(self, enabled: bool) -> None:
        for button in self._buttons:
            button.configure(state="normal" if enabled else "disabled")
        self.wizard.set_busy(not enabled)

    def _say(self, text: str, error: bool = False) -> None:
        self._status.configure(text=text, style="Error.TLabel" if error else "Good.TLabel")
        self._log()

    def _fail(self, exc: BaseException) -> None:
        if self.alive():
            self._enable(True)
            message, detail = error_text(exc)
            self.session.log.append(detail)
            self._say(message, error=True)

    def _background(self, work: Callable[[], Any], done: Callable[[Any], None]) -> None:
        self._enable(False)
        run_in_thread(self, work, done, self._fail)

    def _on_preset(self) -> None:
        if not messagebox.askyesno(t("gui.result.preset_title"), t("gui.result.preset_confirm"), parent=self):
            return
        self._background(lambda: self.session.export_preset(confirmed=True), self._preset_done)

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
            self._say(t(result.message_key), error=not result.ok)

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


PAGES: dict[Step, type[Page]] = {
    page.step: page for page in (WelcomePage, DetectPage, TabletPage, DisplayPage, QuestionsPage,
                                 BaselinePage, TestingPage, ResultPage)
}


# ----------------------------------------------------------------------- shell
class Wizard(ttk.Frame):
    """Shell around the pages: step indicator, error line, Back/Next."""

    def __init__(self, parent: tk.Misc, session: Session, config_hint: Path | None = None,
                 install_hint: Path | None = None,
                 on_close: Callable[[], None] | None = None) -> None:
        super().__init__(parent, padding=_PAD)
        self.session = session
        self.config_hint = config_hint
        self.install_hint = install_hint
        self.apply_consent = False
        self._on_close = on_close or self.winfo_toplevel().destroy
        self._busy = False
        self.page: Page | None = None
        self.indicator = StepIndicator(self)
        self.indicator.pack(fill="x", pady=(0, 8))
        self._error = WrapLabel(self, "", style="Error.TLabel")
        footer = ttk.Frame(self)
        footer.pack(side="bottom", fill="x", pady=(8, 0))
        self._back = ttk.Button(footer, command=self.on_back)
        self._next = ttk.Button(footer, command=self.on_next, style="Accent.TButton")
        self._next.pack(side="right")
        self._back.pack(side="right", padx=6)
        self._error.pack(side="bottom", fill="x")
        self._scroll = ScrollFrame(self)
        self._scroll.pack(fill="both", expand=True)
        self.show_current()

    # --- rendering
    def show_current(self) -> None:
        """(Re)build the page of the current step in the current language."""
        for child in self._scroll.body.winfo_children():
            child.destroy()
        self.clear_error()
        self.page = PAGES[self.session.step](self._scroll.body, self)
        self.page.pack(fill="both", expand=True)
        self._scroll.scroll_top()
        self.indicator.show([t(f"gui.step.{s.value}") for s in STEP_ORDER],
                            STEP_ORDER.index(self.session.step))
        self.update_nav()

    def rebuild(self) -> None:
        """Re-render after a language change, keeping what was typed."""
        if self.page is not None:
            self.page.commit()
        self.show_current()

    def update_nav(self) -> None:
        """Refresh Back/Next labels and enabled state."""
        if self.page is None:
            return
        self._back.configure(text=t("gui.nav.back"))
        self._next.configure(text=t(self.page.next_key))
        self._back.configure(state="normal" if self.session.can_go_back() and not self._busy else "disabled")
        self._next.configure(state="normal" if self.page.next_enabled() and not self._busy else "disabled")

    def set_busy(self, busy: bool) -> None:
        """Disable navigation and show a wait cursor during long operations."""
        self._busy = busy
        self.configure(cursor="watch" if busy else "")
        self.update_nav()

    def show_error(self, exc: BaseException) -> None:
        """Show a failure in the error line (technical detail goes to the log)."""
        message, detail = error_text(exc)
        if detail:
            self.session.log.append(detail)
        self._error.configure(text=message)

    def clear_error(self) -> None:
        """Hide the error line."""
        self._error.configure(text="")

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
        self.set_busy(True)
        run_in_thread(self, self.session.advance, lambda _s: self._moved(), self._move_failed)

    def _moved(self) -> None:
        self.set_busy(False)
        self.show_current()

    def _move_failed(self, exc: BaseException) -> None:
        self.set_busy(False)
        self.show_error(exc)

    def on_back(self) -> None:
        """Go back one step."""
        if self.page is not None:
            self.page.commit()
        self.session.back()
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


def configure_styles(root: tk.Misc) -> None:
    """Define the named ttk styles the pages use."""
    import tkinter.font as tkfont
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    elif "clam" in style.theme_names():
        style.theme_use("clam")
    base = tkfont.nametofont("TkDefaultFont")
    size = base.cget("size")
    title = base.copy()
    title.configure(size=size + 4 if size > 0 else size - 6, weight="bold")
    heading = base.copy()
    heading.configure(weight="bold")
    style.configure("Title.TLabel", font=title)
    style.configure("Heading.TLabel", font=heading)
    style.configure("Muted.TLabel", foreground="#666666")
    style.configure("Error.TLabel", foreground="#b00020")
    style.configure("Good.TLabel", foreground="#1b7f3b")
    style.configure("Step.TLabel", foreground="#888888")
    style.configure("StepDone.TLabel", foreground="#1b7f3b")
    style.configure("StepCurrent.TLabel", font=heading)
    style.configure("Accent.TButton", font=heading)
    style.configure("Treeview", rowheight=px(root, 24))
