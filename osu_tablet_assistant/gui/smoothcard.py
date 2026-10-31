"""The "Smoothing" card of the Play Coach's prepare screen: try installed smoothing filters, the player decides.

It lists the offered catalog filters (``CoachCore.smoothing_options``): for each installed one its name, one
plain sentence of what it does, how it feels, the estimated extra delay and a "Try" switch; filters that are
not installed are shown greyed with "not installed" (nothing is ever installed). Compact by default (title,
one summary chip and "Show"), like the click-buttons card; ``state`` is a dict owned by the window that
remembers whether it is open across screen refreshes. Built from the shared ``widgets`` helpers.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Sequence

from .coachcore import SmoothOption, fmt_num
from .widgets import Card, Chip, WrapLabel, button, check, frame, label, px

Translate = Callable[..., str]


def summary_text(options: Sequence[SmoothOption], translate: Translate) -> tuple[str, str]:
    """The chip next to the title: what is being tried, or how many installed filters are off."""
    trying = [o.name for o in options if o.trying]
    if trying:
        return translate("play.smooth.summary.trying", names=", ".join(trying)), "accent"
    off = [o for o in options if o.installed and not o.on]
    if not off and not any(o.installed for o in options):
        return translate("play.smooth.summary.none"), "neutral"
    return translate("play.smooth.summary.off", n=len(off)), "neutral"


class SmoothingCard(Card):
    """Card with one row per offered smoothing filter (see the module docstring)."""

    def __init__(self, parent: tk.Misc, options: Sequence[SmoothOption], translate: Translate,
                 on_try: Callable[[str], None], on_stop: Callable[[str], None], state: dict | None = None) -> None:
        super().__init__(parent, translate("play.smooth.title"), padding=12)
        self.t, self.options, self.on_try, self.on_stop = translate, list(options), on_try, on_stop
        self.state = state if state is not None else {}
        self.summary = frame(self.header)
        self.summary.pack(side="left", padx=(px(self, 10), 0))
        self.open_button = button(self.header, "", self.toggle_open, kind="ghost")
        self.open_button.pack(side="right")
        # A 1 px child that is always packed lets the closed card give its height back (see ClickKeysCard).
        ttk.Frame(self.body, style="Card.TFrame", height=1).pack(fill="x")
        self.details = frame(self.body)
        self.vars: dict[str, tk.BooleanVar] = {}
        self.switches: dict[str, ttk.Checkbutton] = {}
        self._built = False                     # the rows are built when the card is first opened (cheaper refreshes)
        self._show(bool(self.state.get("open")))

    @property
    def is_open(self) -> bool:
        return bool(self.state.get("open"))

    def toggle_open(self) -> None:
        self._show(not self.is_open)

    def _show(self, opened: bool) -> None:
        self.state["open"] = opened
        if opened and not self._built:
            self._built = True
            self._build_details()
        if opened:
            self.details.pack(fill="x")
        else:
            self.details.pack_forget()
        for child in self.summary.winfo_children():
            child.destroy()
        text, kind = summary_text(self.options, self.t)
        Chip(self.summary, text, kind).pack(side="left")
        self.open_button.configure(text=self.t("play.smooth.hide" if opened else "play.smooth.show"))

    def _build_details(self) -> None:
        box = self.details
        WrapLabel(box, self.t("play.smooth.intro"), "Body").pack(fill="x")
        WrapLabel(box, self.t("play.smooth.players"), "Small").pack(fill="x", pady=(px(self, 2), px(self, 6)))
        for option in (o for o in self.options if o.installed and (o.trying or not o.on)):
            self._row(box, option)
        for option in (o for o in self.options if o.installed and o.on and not o.trying):   # not ours to switch
            WrapLabel(box, self.t("play.smooth.already_on_row", name=option.name), "Small").pack(
                fill="x", pady=(px(self, 2), 0))
        missing = [o for o in self.options if not o.installed]
        for option in missing:
            row = label(box, self.t("play.smooth.not_installed_row", name=option.name), "Small")
            row.state(["disabled"])                  # greyed: there is nothing to switch
            row.pack(anchor="w", pady=(px(self, 2), 0))
        if missing:
            WrapLabel(box, self.t("play.smooth.not_installed_hint"), "Small").pack(fill="x", pady=(px(self, 2), 0))
        WrapLabel(box, self.t("play.smooth.save_note"), "Small").pack(fill="x", pady=(px(self, 8), 0))

    def _row(self, parent: tk.Misc, option: SmoothOption) -> None:
        row = frame(parent)
        row.pack(fill="x", pady=(px(self, 6), 0))
        top = frame(row)
        top.pack(fill="x")
        label(top, option.name, "Strong").pack(side="left")
        delay = self.t("play.smooth.delay", ms=fmt_num(option.delay_ms, self.t)) if option.delay_ms is not None \
            else self.t("play.smooth.delay_unknown")
        Chip(top, delay, "neutral").pack(side="left", padx=(px(self, 8), 0))
        var = tk.BooleanVar(value=option.on)
        self.vars[option.path] = var
        switch = check(top, self.t("play.smooth.try"), var, command=lambda p=option.path: self._switched(p))
        switch.pack(side="right")
        self.switches[option.path] = switch
        WrapLabel(row, self.t(f"play.smooth.what.{option.text_id}"), "Small").pack(fill="x")
        WrapLabel(row, self.t(f"play.smooth.feel.{option.text_id}"), "Muted").pack(fill="x")
        if option.trying:
            WrapLabel(row, self.t("play.smooth.decided_note" if option.decided else "play.smooth.trying_note"),
                      "Accent").pack(fill="x")

    def _switched(self, path: str) -> None:
        """The owner applies it and refreshes the screen (this card may be rebuilt: last thing done here)."""
        if self.vars[path].get():
            self.on_try(path)
        else:
            self.on_stop(path)
