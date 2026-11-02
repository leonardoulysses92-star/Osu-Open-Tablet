"""The "Your playing style" card of the Play Coach report: the player's habits in friendly words.

It shows what ``engine.rulebook.describe`` built from the habits pooled over plays (``CoachCore.style``): one
headline that is always visible and, opened, three short lists: what the setup does to suit the player ("por
eso el área sube un 3 %"), habits seen that need no change, and habits that are "todavía no seguro" (too few
plays). The words describe; they never ask the player to play differently. A privacy line and
"Olvidar mis hábitos" close it. Open state lives in ``state`` (a dict owned by the window), like the smoothing
card; by default it opens when the setup is adapting to something. Built from the shared ``widgets`` helpers.
"""
from __future__ import annotations

import tkinter as tk
from typing import Callable, Sequence

from ..engine.rulebook import StyleItem, StyleSummary
from ..models import Reason
from .coachcore import say
from .widgets import Card, Chip, WrapLabel, button, frame, label, px

Translate = Callable[..., str]
SECTIONS = (("acted", "play.style.acted_title"), ("fine", "play.style.fine_title"),
            ("unsure", "play.style.unsure_title"))


def style_sections(summary: StyleSummary, translate: Translate) -> list[tuple[str, list[tuple[str, str, str]]]]:
    """``[(section title, [(habit, what the setup does, confidence word), ...]), ...]``, empty sections left out."""
    out = []
    for name, title in SECTIONS:
        items: Sequence[StyleItem] = getattr(summary, name)
        if items:
            out.append((translate(title), [(say(i.text, translate),
                                            say(i.accommodation, translate) if i.accommodation is not None else "",
                                            translate(f"play.style.conf.{i.confidence}")) for i in items]))
    return out


class StyleCard(Card):
    """Card with the player's habits (see the module docstring)."""

    def __init__(self, parent: tk.Misc, summary: StyleSummary, translate: Translate, on_forget: Callable[[], None],
                 state: dict | None = None, warnings: Sequence[Reason] = ()) -> None:
        super().__init__(parent, translate("play.style.title"), padding=12)
        self.t, self.summary, self.on_forget, self.warnings = translate, summary, on_forget, tuple(warnings)
        self.state = state if state is not None else {}
        Chip(self.header, translate("play.style.plays", n=summary.plays), "neutral").pack(
            side="left", padx=(px(self, 10), 0))
        self.open_button = button(self.header, "", self.toggle_open, kind="ghost")
        self.open_button.pack(side="right")
        headline = summary.headline or Reason("play.style.headline.learning")
        WrapLabel(self.body, say(headline, translate), "Strong").pack(fill="x")
        self.details = frame(self.body)
        self._built = False
        self.is_open = False
        self._show(bool(self.state.get("open", bool(summary.acted))))    # only a choice of the player is remembered

    def toggle_open(self) -> None:
        self.state["open"] = not self.is_open
        self._show(self.state["open"])

    def _show(self, opened: bool) -> None:
        self.is_open = opened
        if opened and not self._built:
            self._built = True
            self._build_details()
        if opened:
            self.details.pack(fill="x")
        else:
            self.details.pack_forget()
        self.open_button.configure(text=self.t("play.style.hide" if opened else "play.style.show"))

    def _build_details(self) -> None:
        box = self.details
        for title, items in style_sections(self.summary, self.t):
            label(box, title, "Accent").pack(anchor="w", pady=(px(self, 8), 0))
            for text, accommodation, sure in items:
                row = frame(box)
                row.pack(fill="x", pady=(px(self, 4), 0))
                top = frame(row)
                top.pack(fill="x")
                WrapLabel(top, text, "Body").pack(side="left", fill="x", expand=True)
                Chip(top, sure, "neutral").pack(side="right", padx=(px(self, 8), 0))
                if accommodation:
                    WrapLabel(row, accommodation, "Muted").pack(fill="x")
        for warning in self.warnings:
            WrapLabel(box, say(warning, self.t), "Small").pack(fill="x", pady=(px(self, 6), 0))
        WrapLabel(box, self.t("play.style.privacy"), "Small").pack(fill="x", pady=(px(self, 8), 0))
        button(box, self.t("play.style.forget"), self.on_forget, kind="ghost").pack(anchor="w", pady=(px(self, 4), 0))
