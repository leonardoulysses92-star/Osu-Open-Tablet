"""The "Your click buttons" card of the Play Coach's prepare screen.

The player says which keys/buttons they click with (default Z and X) and whether the pen tip counts. The
logic is in :class:`osu_tablet_assistant.keys.KeyChoice`; this card only shows it, built from the shared
``widgets`` helpers so the app's style applies.

"Detect my buttons" listens with ordinary Tk ``<KeyPress>`` / ``<ButtonPress>`` bindings on THIS window
only, while it has focus, until Esc or "Done". There is no global hook and nothing is polled: keys pressed
in other programs are never seen. The left button is not taken from detection (it is how "Done" is pressed);
the pen tip has its own checkbox.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from ..engine.playtypes import ANALYSIS_DETAILS
from ..keys import KeyChoice, display_name, name_from_tk_event, parse_text
from .widgets import Card, Chip, WrapLabel, button, check, frame, label, px, radio

Translate = Callable[..., str]


class ClickKeysCard(Card):
    """Chips with the chosen keys, detection mode, reset, the pen-tip switch, typing by hand, analysis detail.

    Compact by default (title, the chips and "Change" on one line) so the prepare screen stays short; ``state``
    is a dict owned by the window that remembers whether it is open across screen refreshes.
    """

    def __init__(self, parent: tk.Misc, choice: KeyChoice, translate: Translate,
                 on_change: Callable[[KeyChoice], None], detail: str | None = None,
                 on_detail: Callable[[str], None] | None = None, state: dict | None = None) -> None:
        super().__init__(parent, translate("play.keys.title"), padding=12)
        self.t, self.choice, self.on_change, self.on_detail = translate, choice, on_change, on_detail
        self.state = state if state is not None else {}
        self.listening = False
        self._bound: list[str] = []
        self._manual_open = False
        self.summary = frame(self.header)
        self.summary.pack(side="left", padx=(px(self, 10), 0))
        self.open_button = button(self.header, "", self.toggle_open, kind="ghost")
        self.open_button.pack(side="right")
        self.details = frame(self.body)
        box = self.details
        WrapLabel(box, self.t("play.keys.intro"), "Small").pack(fill="x")
        self.chips = frame(box)
        self.chips.pack(fill="x", pady=(px(self, 4), px(self, 2)))
        self.status = WrapLabel(box, "", "Small")
        self.status.pack(fill="x")
        row = frame(box)
        row.pack(fill="x", pady=(px(self, 4), 0))
        self.detect_button = button(row, self.t("play.keys.detect"), self.toggle_listening, kind="secondary")
        self.detect_button.pack(side="left")
        button(row, self.t("play.keys.reset"), self.reset, kind="ghost").pack(side="left", padx=(px(self, 6), 0))
        self.pen_var = tk.BooleanVar(value=choice.pen_clicks)
        check(box, self.t("play.keys.pen"), self.pen_var, command=self._pen_toggled).pack(
            anchor="w", pady=(px(self, 6), 0))
        WrapLabel(box, self.t("play.keys.pen_note"), "Small").pack(fill="x")
        self._build_manual()
        if detail is not None and on_detail is not None:
            self._build_detail(detail)
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._render()
        self._show_details(bool(self.state.get("open")))

    @property
    def is_open(self) -> bool:
        return bool(self.state.get("open"))

    def toggle_open(self) -> None:
        if not self.is_open:
            self._show_details(True)
            return
        was_listening = self._end_listening()
        self._show_details(False)
        if was_listening:
            self.on_change(self.choice)         # the owner may refresh the screen now (last thing: it may rebuild)

    def _show_details(self, opened: bool) -> None:
        self.state["open"] = opened
        if opened:
            self.details.pack(fill="x")
        else:
            self.details.pack_forget()
        self._render()
        if opened:
            self._fit_window()

    def _fit_window(self) -> None:
        """Make the window tall enough for the open card (the user can still resize it)."""
        try:
            top = self.winfo_toplevel()
            top.update_idletasks()
            need = min(top.winfo_reqheight(), top.winfo_screenheight() - px(self, 80))
            if top.winfo_height() > 1 and top.winfo_height() < need:
                top.geometry(f"{top.winfo_width()}x{need}")
        except tk.TclError:
            pass

    # ---------------------------------------------------------------- pieces
    def _build_manual(self) -> None:
        self.manual_toggle = button(self.details, "", self._toggle_manual, kind="ghost")
        self.manual_toggle.pack(anchor="w", pady=(px(self, 4), 0))
        self.manual = frame(self.details)
        self.manual_var = tk.StringVar(value=", ".join(self.choice.keys))
        line = frame(self.manual)
        line.pack(fill="x")
        self.manual_entry = ttk.Entry(line, textvariable=self.manual_var)
        self.manual_entry.pack(side="left", fill="x", expand=True)
        self.manual_entry.bind("<Return>", lambda _e: self.apply_manual())
        button(line, self.t("play.keys.apply"), self.apply_manual, kind="secondary").pack(
            side="left", padx=(px(self, 6), 0))
        WrapLabel(self.manual, self.t("play.keys.manual_help"), "Small").pack(fill="x")
        self._refresh_manual()

    def _build_detail(self, detail: str) -> None:
        row = frame(self.details)
        row.pack(fill="x", pady=(px(self, 8), 0))
        label(row, self.t("play.detail.title"), "Strong").pack(side="left", padx=(0, px(self, 8)))
        self.detail_var = tk.StringVar(value=detail)
        for level in ANALYSIS_DETAILS:
            radio(row, self.t(f"play.detail.{level}"), level, self.detail_var,
                  command=lambda: self.on_detail(self.detail_var.get())).pack(side="left", padx=(0, px(self, 6)))
        WrapLabel(self.details, self.t("play.detail.note"), "Small").pack(fill="x")

    def _render(self) -> None:
        for child in self.summary.winfo_children():
            child.destroy()
        if not self.is_open:                    # compact: just the chips next to the title
            for name in self.choice.effective:
                Chip(self.summary, display_name(name), "accent").pack(side="left", padx=(0, px(self, 4)))
        self.open_button.configure(text=self.t("play.keys.close" if self.is_open else "play.keys.change"))
        for child in self.chips.winfo_children():
            child.destroy()
        removable = len(self.choice.keys) > 1 or self.choice.pen_clicks
        for key in self.choice.keys:
            Chip(self.chips, display_name(key), "accent").pack(side="left")
            remove = button(self.chips, self.t("play.keys.remove"), lambda k=key: self.remove(k), kind="ghost")
            remove.pack(side="left", padx=(0, px(self, 6)))
            if not removable:
                remove.configure(state="disabled")
        if self.choice.pen_clicks:
            Chip(self.chips, display_name("mouse1"), "neutral").pack(side="left")
        self.detect_button.configure(text=self.t("play.keys.done" if self.listening else "play.keys.detect"))

    def _say(self, key: str, **params: Any) -> None:
        self.status.configure(text=self.t(key, **params) if key else "")

    # --------------------------------------------------------------- changes
    def set_choice(self, choice: KeyChoice) -> None:
        if choice == self.choice:
            return
        self.choice = choice
        self.pen_var.set(choice.pen_clicks)
        self.manual_var.set(", ".join(choice.keys))
        self._render()
        self.on_change(choice)

    def remove(self, key: str) -> None:
        self.set_choice(self.choice.remove(key))

    def reset(self) -> None:
        was_listening = self._end_listening()
        fresh = self.choice.reset()
        if fresh != self.choice:
            self.set_choice(fresh)
        else:
            self._render()
            if was_listening:
                self.on_change(self.choice)

    def _pen_toggled(self) -> None:
        self.set_choice(self.choice.with_pen(bool(self.pen_var.get())))

    def _toggle_manual(self) -> None:
        self._manual_open = not self._manual_open
        self._refresh_manual()

    def _refresh_manual(self) -> None:
        arrow = "▾" if self._manual_open else "▸"
        self.manual_toggle.configure(text=f"{arrow}  {self.t('play.keys.manual')}")
        if self._manual_open:
            self.manual.pack(fill="x", after=self.manual_toggle)
        else:
            self.manual.pack_forget()

    def apply_manual(self) -> None:
        """Use the typed keys; nothing changes while a word is not understood (it is named instead)."""
        known, unknown = parse_text(self.manual_var.get())
        if unknown:
            self._say("play.keys.unknown", words=", ".join(unknown))
            return
        self._say("")
        if known:
            self.set_choice(KeyChoice.make(known, self.choice.pen_clicks or "mouse1" in known))

    # ------------------------------------------------------------- listening
    def toggle_listening(self) -> None:
        if self.listening:
            self.stop_listening()
        else:
            self.start_listening()

    def start_listening(self) -> None:
        """Read the next key / button presses in THIS window only (Tk bindings, while it has focus)."""
        if self.listening:
            return
        if not self.is_open:
            self._show_details(True)
        self.listening = True
        top = self.winfo_toplevel()
        self._bound = ["<KeyPress>", "<ButtonPress>"]
        top.bind("<KeyPress>", self.feed_event)
        top.bind("<ButtonPress>", self.feed_event)
        self.focus_set()                        # so Space/Enter do not press a focused button
        self._say("play.keys.listening")
        self._render()

    def stop_listening(self) -> None:
        if self._end_listening():
            self._render()
            self.on_change(self.choice)         # the owner may refresh the screen now (last thing: it may rebuild)

    def _end_listening(self) -> bool:
        """Stop reading keys without telling the owner; True when it was listening."""
        if not self.listening:
            return False
        self.listening = False
        self._unbind()
        self._say("")
        return True

    def _unbind(self) -> None:
        try:
            top = self.winfo_toplevel()
            for sequence in self._bound:
                top.unbind(sequence)
        except tk.TclError:
            pass
        self._bound = []

    def feed_event(self, event: Any) -> str | None:
        """One Tk key/button event while listening; Esc ends. Returns "break" for keys it handled."""
        if not self.listening:
            return None
        if str(getattr(event, "keysym", "")) == "Escape":
            self.stop_listening()
            return "break"
        name = name_from_tk_event(event)
        choice, outcome = self.choice.feed(name)
        if outcome == "added":
            self.set_choice(choice)
            self._say("play.keys.listening")
        elif outcome == "left_click":
            if not isinstance(getattr(event, "widget", None), (ttk.Button, tk.Button)):
                self._say("play.keys.left_click")
            return None                         # let the click reach the button it was meant for
        elif outcome == "full":
            self._say("play.keys.full")
        return "break" if name is not None else None

    def _on_destroy(self, event: Any) -> None:
        if getattr(event, "widget", None) is self and self.listening:
            self.listening = False
            self._unbind()
