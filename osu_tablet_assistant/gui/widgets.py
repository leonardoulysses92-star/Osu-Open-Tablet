"""Design tokens, the dark ttk theme and the reusable widgets of the wizard.

Everything visual is defined here so the pages stay consistent:

* ``COLORS``, ``SPACE`` and ``FONT_ROLES`` are the design tokens (the palette
  matches the live pen tests: background ``#14161a``, accent ``#ff66aa``);
* :func:`apply_theme` turns them into named ttk styles;
* the widgets (``Card``, ``Notice``, ``Stepper``, ``BarChart``, ``AreaPreview``,
  ``Tooltip``...) only use those styles and tokens.

The geometry helpers at the end (``stepper_centers``, ``bar_lengths``,
``area_geometry``...) are pure functions with no Tk dependency, unit-tested
headlessly.
"""
from __future__ import annotations

import math
import queue
import threading
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass
from tkinter import ttk
from typing import Any, Callable, Iterable, Sequence

from ..models import Rect
from .i18n import t


# ======================================================================== tokens
@dataclass(frozen=True)
class Colors:
    """The palette. ``*_soft`` colours are tinted backgrounds for the matching role."""
    bg: str = "#14161a"
    surface: str = "#1c1f26"
    surface_2: str = "#252a33"
    surface_3: str = "#30353f"
    border: str = "#2c313b"
    border_strong: str = "#464c58"
    text: str = "#eceef2"
    muted: str = "#a3a9b4"
    subtle: str = "#737a86"
    accent: str = "#ff66aa"
    accent_hover: str = "#ff85bc"
    accent_pressed: str = "#e5508f"
    accent_soft: str = "#3d2133"
    on_accent: str = "#14161a"
    good: str = "#5cd18d"
    good_soft: str = "#15291e"
    warn: str = "#f4c55a"
    warn_soft: str = "#2e2611"
    error: str = "#ff6b7d"
    error_soft: str = "#33181e"
    info: str = "#6cb6ff"
    info_soft: str = "#13243a"
    before: str = "#69707c"


COLORS = Colors()
SPACE = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24, "xxl": 32}
FONT_FAMILY = "Segoe UI"
# role -> (size in points, weight)
FONT_ROLES: dict[str, tuple[int, str]] = {
    "display": (20, "bold"),
    "title": (16, "bold"),
    "heading": (11, "bold"),
    "body": (10, "normal"),
    "strong": (10, "bold"),
    "small": (9, "normal"),
    "small_bold": (9, "bold"),
    "mono": (9, "normal"),
}
# Label roles -> (font role, colour attribute of COLORS)
LABEL_ROLES: dict[str, tuple[str, str]] = {
    "Body": ("body", "text"),
    "Strong": ("strong", "text"),
    "Muted": ("body", "muted"),
    "Small": ("small", "muted"),
    "Heading": ("heading", "text"),
    "Title": ("title", "text"),
    "Display": ("display", "text"),
    "Eyebrow": ("small_bold", "accent"),
    "Accent": ("strong", "accent"),
    "Error": ("body", "error"),
    "Good": ("body", "good"),
    "Warn": ("body", "warn"),
}
# Surfaces a widget can sit on: style prefix -> background colour attribute.
SURFACES = {"": "bg", "Card.": "surface"}
NOTICE_KINDS = {"info": ("info", "info_soft", "i"), "success": ("good", "good_soft", "✓"),
                "warn": ("warn", "warn_soft", "!"), "error": ("error", "error_soft", "✕")}
_FONTS: dict[str, str] = {}
_FONT_OBJECTS: list[tkfont.Font] = []


def px(widget: tk.Misc, value: float) -> int:
    """Scale a 96-dpi pixel size to the widget's actual screen DPI."""
    return round(value * widget.winfo_fpixels("1i") / 96.0)


def font(role: str) -> str:
    """Name of the Tk named font for a ``FONT_ROLES`` role (after :func:`apply_theme`)."""
    return _FONTS.get(role, "TkDefaultFont")


def _family(root: tk.Misc, mono: bool = False) -> str:
    families = set(tkfont.families(root))
    wanted = ("Cascadia Mono", "Consolas") if mono else (FONT_FAMILY,)
    for name in wanted:
        if name in families:
            return name
    return tkfont.nametofont("TkFixedFont" if mono else "TkDefaultFont").cget("family")


def apply_theme(root: tk.Misc) -> ttk.Style:
    """Create the named fonts and every ttk style the wizard uses (dark theme)."""
    c = COLORS
    _FONT_OBJECTS.clear()
    for role, (size, weight) in FONT_ROLES.items():
        name = f"App{role.title().replace('_', '')}"
        family = _family(root, mono=role == "mono")
        try:
            named = tkfont.nametofont(name, root=root)
            named.configure(family=family, size=size, weight=weight)
        except tk.TclError:
            named = tkfont.Font(root, name=name, family=family, size=size, weight=weight)
        _FONT_OBJECTS.append(named)   # a Font created here is deleted when garbage-collected
        _FONTS[role] = name
    for default in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
        tkfont.nametofont(default).configure(family=_family(root), size=FONT_ROLES["body"][0])

    style = ttk.Style(root)
    style.theme_use("clam")
    root.configure(background=c.bg)
    root.option_add("*TCombobox*Listbox.background", c.surface_2)
    root.option_add("*TCombobox*Listbox.foreground", c.text)
    root.option_add("*TCombobox*Listbox.selectBackground", c.accent)
    root.option_add("*TCombobox*Listbox.selectForeground", c.on_accent)
    root.option_add("*TCombobox*Listbox.font", font("body"))
    root.option_add("*TCombobox*Listbox.borderWidth", 0)

    style.configure(".", background=c.bg, foreground=c.text, font=font("body"),
                    bordercolor=c.border, lightcolor=c.bg, darkcolor=c.bg, troughcolor=c.surface_2,
                    focuscolor=c.accent, selectbackground=c.accent_soft, selectforeground=c.text,
                    insertcolor=c.text, fieldbackground=c.surface_2, arrowcolor=c.muted)
    style.map(".", foreground=[("disabled", c.subtle)])

    for prefix, bg_attr in SURFACES.items():
        bg = getattr(c, bg_attr)
        style.configure(f"{prefix}TFrame", background=bg)
        style.configure(f"{prefix}TLabel", background=bg, foreground=c.text)
        for role, (font_role, color) in LABEL_ROLES.items():
            style.configure(f"{prefix}{role}.TLabel", background=bg, foreground=getattr(c, color),
                            font=font(font_role))
        for kind in ("TCheckbutton", "TRadiobutton"):
            style.configure(f"{prefix}{kind}", background=bg, foreground=c.text, padding=(2, 4),
                            indicatorsize=px(root, 14), indicatordiameter=px(root, 14),
                            indicatorbackground=c.surface_2, indicatorforeground=c.on_accent,
                            indicatormargin=(0, 0, 8, 0), upperbordercolor=c.border_strong,
                            lowerbordercolor=c.border_strong, focuscolor=c.accent)
            style.map(f"{prefix}{kind}",
                      background=[("active", bg)],
                      foreground=[("disabled", c.subtle), ("active", c.text)],
                      indicatorbackground=[("disabled", c.surface), ("selected", c.accent),
                                           ("pressed", c.surface_3), ("active", c.surface_3)],
                      upperbordercolor=[("focus", c.accent), ("selected", c.accent)],
                      lowerbordercolor=[("focus", c.accent), ("selected", c.accent)])
        style.configure(f"{prefix}Ghost.TButton", background=bg, foreground=c.text,
                        bordercolor=bg, lightcolor=bg, darkcolor=bg, padding=(10, 6),
                        focusthickness=0, font=font("body"))
        style.map(f"{prefix}Ghost.TButton",
                  background=[("disabled", bg), ("pressed", c.surface_3), ("active", c.surface_2)],
                  lightcolor=[("disabled", bg), ("pressed", c.surface_3), ("active", c.surface_2)],
                  darkcolor=[("disabled", bg), ("pressed", c.surface_3), ("active", c.surface_2)],
                  bordercolor=[("focus", c.accent), ("active", c.surface_2)],
                  foreground=[("disabled", c.subtle)])

    def button(name: str, bg: str, fg: str, hover: str, pressed: str, border: str,
               focus_ring: str) -> None:
        style.configure(name, background=bg, foreground=fg, bordercolor=border, lightcolor=bg,
                        darkcolor=bg, padding=(16, 8), focusthickness=0, font=font("strong"),
                        anchor="center")
        state_bg = [("disabled", c.surface_2), ("pressed", pressed), ("active", hover)]
        style.map(name, background=state_bg, lightcolor=state_bg, darkcolor=state_bg,
                  foreground=[("disabled", c.subtle)],
                  bordercolor=[("disabled", c.surface_2), ("focus", focus_ring)])

    button("Primary.TButton", c.accent, c.on_accent, c.accent_hover, c.accent_pressed, c.accent, c.text)
    button("Secondary.TButton", c.surface_2, c.text, c.surface_3, c.border_strong, c.border_strong, c.accent)
    style.configure("TButton", background=c.surface_2, foreground=c.text, bordercolor=c.border_strong,
                    lightcolor=c.surface_2, darkcolor=c.surface_2, padding=(12, 6), focusthickness=0)
    style.map("TButton", background=[("active", c.surface_3)], bordercolor=[("focus", c.accent)])

    # Segmented toggles (language switch, 1-5 rating): radio buttons drawn as buttons.
    style.configure("Segment.Toolbutton", background=c.surface_2, foreground=c.muted,
                    bordercolor=c.border_strong, lightcolor=c.surface_2, darkcolor=c.surface_2,
                    padding=(10, 4), font=font("small_bold"), focusthickness=0, anchor="center")
    seg_bg = [("disabled", c.surface), ("selected", c.accent), ("pressed", c.surface_3),
              ("active", c.surface_3)]
    style.map("Segment.Toolbutton", background=seg_bg, lightcolor=seg_bg, darkcolor=seg_bg,
              foreground=[("disabled", c.subtle), ("selected", c.on_accent), ("active", c.text)],
              bordercolor=[("focus", c.accent), ("selected", c.accent)])

    style.configure("TEntry", fieldbackground=c.surface_2, foreground=c.text, bordercolor=c.border_strong,
                    lightcolor=c.surface_2, darkcolor=c.surface_2, padding=(8, 5), insertcolor=c.text)
    style.map("TEntry", bordercolor=[("focus", c.accent)], lightcolor=[("focus", c.accent)],
              fieldbackground=[("disabled", c.surface)])
    style.configure("TCombobox", fieldbackground=c.surface_2, background=c.surface_2, foreground=c.text,
                    bordercolor=c.border_strong, lightcolor=c.surface_2, darkcolor=c.surface_2,
                    arrowcolor=c.muted, padding=(8, 5), selectbackground=c.surface_2,
                    selectforeground=c.text)
    style.map("TCombobox", fieldbackground=[("readonly", c.surface_2)],
              background=[("active", c.surface_3), ("readonly", c.surface_2)],
              bordercolor=[("focus", c.accent)], lightcolor=[("focus", c.accent)],
              foreground=[("readonly", c.text)], arrowcolor=[("active", c.text)],
              selectbackground=[("focus", c.surface_2)], selectforeground=[("focus", c.text)])

    style.configure("Treeview", background=c.surface, fieldbackground=c.surface, foreground=c.text,
                    bordercolor=c.border, lightcolor=c.surface, darkcolor=c.surface,
                    rowheight=px(root, 30), font=font("body"))
    style.map("Treeview", background=[("selected", c.accent_soft)],
              foreground=[("selected", c.text)], bordercolor=[("focus", c.accent)])
    style.configure("Treeview.Heading", background=c.surface_2, foreground=c.muted,
                    bordercolor=c.border, lightcolor=c.surface_2, darkcolor=c.surface_2,
                    relief="flat", padding=(8, 6), font=font("small_bold"))
    style.map("Treeview.Heading", background=[("active", c.surface_3)])
    style.layout("Treeview", [("Treeview.field", {"sticky": "nswe", "border": "1", "children": [
        ("Treeview.padding", {"sticky": "nswe", "children": [
            ("Treeview.treearea", {"sticky": "nswe"})]})]})])

    for orient in ("Vertical", "Horizontal"):
        style.configure(f"{orient}.TScrollbar", background=c.surface_3, troughcolor=c.bg,
                        bordercolor=c.bg, lightcolor=c.surface_3, darkcolor=c.surface_3,
                        arrowcolor=c.muted, gripcount=0, arrowsize=px(root, 12))
        style.map(f"{orient}.TScrollbar", background=[("active", c.border_strong)],
                  lightcolor=[("active", c.border_strong)], darkcolor=[("active", c.border_strong)])
    style.configure("Accent.Horizontal.TProgressbar", background=c.accent, troughcolor=c.surface_2,
                    bordercolor=c.surface_2, lightcolor=c.accent, darkcolor=c.accent,
                    thickness=px(root, 4))
    style.configure("TSeparator", background=c.border)
    return style


def surface_prefix(widget: tk.Misc | None) -> str:
    """Style prefix of the surface a widget sits on ("" for the page, "Card." inside a card)."""
    while widget is not None:
        prefix = getattr(widget, "surface", None)
        if prefix is not None:
            return prefix
        widget = getattr(widget, "master", None)
    return ""


def surface_color(widget: tk.Misc | None) -> str:
    """Background colour of the surface ``widget`` sits on."""
    return getattr(COLORS, SURFACES[surface_prefix(widget)])


# ===================================================================== threading
def run_in_thread(widget: tk.Misc, work: Callable[[], Any], on_done: Callable[[Any], None],
                  on_error: Callable[[BaseException], None], poll_ms: int = 50) -> None:
    """Run ``work`` off the UI thread; call back on the UI thread with result or error."""
    outcome: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def target() -> None:
        try:
            outcome.put((True, work()))
        except BaseException as exc:  # reported to the UI thread, never lost
            outcome.put((False, exc))

    def poll() -> None:
        try:
            ok, value = outcome.get_nowait()
        except queue.Empty:
            try:
                widget.after(poll_ms, poll)
            except tk.TclError:
                pass  # window closed while working
            return
        try:
            alive = bool(widget.winfo_exists())
        except tk.TclError:
            alive = False
        if alive:
            (on_done if ok else on_error)(value)

    threading.Thread(target=target, daemon=True).start()
    widget.after(poll_ms, poll)


# ======================================================================= widgets
class WrapLabel(ttk.Label):
    """Label whose text re-wraps to the width it is given; ``role`` is a ``LABEL_ROLES`` name."""

    def __init__(self, parent: tk.Misc, text: str = "", role: str = "Body", **kwargs: Any) -> None:
        kwargs.setdefault("style", f"{surface_prefix(parent)}{role}.TLabel")
        super().__init__(parent, text=text, justify="left", **kwargs)
        self.bind("<Configure>", self._rewrap)

    def set_role(self, role: str) -> None:
        """Switch to another label role (keeps the surface)."""
        self.configure(style=f"{surface_prefix(self.master)}{role}.TLabel")

    def _rewrap(self, event: tk.Event) -> None:
        wrap = max(event.width - 4, 80)
        if str(wrap) != str(self.cget("wraplength")):
            self.configure(wraplength=wrap)


def label(parent: tk.Misc, text: str = "", role: str = "Body", **kwargs: Any) -> ttk.Label:
    """A one-line label with the right style for its surface."""
    return ttk.Label(parent, text=text, style=f"{surface_prefix(parent)}{role}.TLabel", **kwargs)


def button(parent: tk.Misc, text: str, command: Callable[[], Any], kind: str = "secondary",
           **kwargs: Any) -> ttk.Button:
    """A button of ``kind`` "primary" | "secondary" | "ghost"."""
    style = {"primary": "Primary.TButton", "secondary": "Secondary.TButton",
             "ghost": f"{surface_prefix(parent)}Ghost.TButton"}[kind]
    return ttk.Button(parent, text=text, command=command, style=style, takefocus=True, **kwargs)


def check(parent: tk.Misc, text: str, variable: tk.Variable, **kwargs: Any) -> ttk.Checkbutton:
    """A checkbox styled for its surface."""
    return ttk.Checkbutton(parent, text=text, variable=variable,
                           style=f"{surface_prefix(parent)}TCheckbutton", **kwargs)


def radio(parent: tk.Misc, text: str, value: Any, variable: tk.Variable, **kwargs: Any) -> ttk.Radiobutton:
    """A radio button styled for its surface."""
    return ttk.Radiobutton(parent, text=text, value=value, variable=variable,
                           style=f"{surface_prefix(parent)}TRadiobutton", **kwargs)


def frame(parent: tk.Misc, **kwargs: Any) -> ttk.Frame:
    """A plain frame with the background of its surface."""
    return ttk.Frame(parent, style=f"{surface_prefix(parent)}TFrame", **kwargs)


def bullets(parent: tk.Misc, lines: Iterable[str], role: str = "Body", marker: str = "•",
            marker_role: str = "Accent") -> ttk.Frame:
    """Bulleted list with a hanging indent."""
    box = frame(parent)
    box.columnconfigure(1, weight=1)
    for row, text in enumerate(lines):
        label(box, marker, marker_role).grid(row=row, column=0, sticky="nw", padx=(0, 8), pady=1)
        WrapLabel(box, text, role).grid(row=row, column=1, sticky="we", pady=1)
    return box


class Chip(tk.Label):
    """Small coloured pill ("Measured", "+2 ms"...); ``kind`` is a ``NOTICE_KINDS`` name or "accent"/"neutral"."""

    def __init__(self, parent: tk.Misc, text: str, kind: str = "neutral") -> None:
        fg, bg = self.colors(kind)
        super().__init__(parent, text=text, fg=fg, bg=bg, font=font("small_bold"), padx=8, pady=2,
                         borderwidth=0)

    @staticmethod
    def colors(kind: str) -> tuple[str, str]:
        """Foreground and background colours of a chip kind."""
        c = COLORS
        if kind == "accent":
            return c.accent, c.accent_soft
        if kind in NOTICE_KINDS:
            fg_attr, bg_attr, _glyph = NOTICE_KINDS[kind]
            return getattr(c, fg_attr), getattr(c, bg_attr)
        return c.muted, c.surface_3

    def set(self, text: str, kind: str = "neutral") -> None:
        """Change text and colour."""
        fg, bg = self.colors(kind)
        self.configure(text=text, fg=fg, bg=bg)


class Card(tk.Frame):
    """Raised section with a 1 px border, optional title/subtitle and an accent stripe.

    Put content into ``body``; ``header`` holds the title row (append extra
    widgets such as an :class:`InfoTip` or a :class:`Chip` to it).
    """

    surface = "Card."

    def __init__(self, parent: tk.Misc, title: str | None = None, subtitle: str | None = None,
                 accent: str | None = None, padding: int = 16, title_role: str = "Heading") -> None:
        c = COLORS
        super().__init__(parent, background=c.surface, highlightthickness=1,
                         highlightbackground=c.border, highlightcolor=c.border, borderwidth=0)
        if accent:
            tk.Frame(self, background=accent, width=px(self, 3)).pack(side="left", fill="y")
        pad = px(self, padding)
        inner = ttk.Frame(self, style="Card.TFrame", padding=pad)
        inner.pack(side="left", fill="both", expand=True)
        self.header = ttk.Frame(inner, style="Card.TFrame")
        self.title_label: ttk.Label | None = None
        if title:
            self.header.pack(fill="x", pady=(0, px(self, 4) if subtitle else px(self, 10)))
            self.title_label = ttk.Label(self.header, text=title, style=f"Card.{title_role}.TLabel")
            self.title_label.pack(side="left")
        if subtitle:
            WrapLabel(inner, subtitle, "Muted").pack(fill="x", pady=(0, px(self, 10)))
        self.body = ttk.Frame(inner, style="Card.TFrame")
        self.body.pack(fill="both", expand=True)


class ReadOnlyText(ttk.Frame):
    """Selectable, scrollable, read-only text box."""

    def __init__(self, parent: tk.Misc, height: int = 8) -> None:
        super().__init__(parent, style=f"{surface_prefix(parent)}TFrame")
        c = COLORS
        self.text = tk.Text(self, height=height, wrap="word", borderwidth=0, relief="flat",
                            state="disabled", padx=px(self, 10), pady=px(self, 8), background=c.bg,
                            foreground=c.text, insertbackground=c.text, selectbackground=c.accent_soft,
                            selectforeground=c.text, font=font("mono"), highlightthickness=1,
                            highlightbackground=c.border, highlightcolor=c.accent)
        bar = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=bar.set)
        self.text.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")

    def set(self, content: str) -> None:
        """Replace the whole content."""
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)
        self.text.configure(state="disabled")


class Expander(ttk.Frame):
    """A collapsible "details" section (technical text by default, or any ``content``)."""

    def __init__(self, parent: tk.Misc, title: str | None = None, height: int = 7,
                 opened: bool = False) -> None:
        super().__init__(parent, style=f"{surface_prefix(parent)}TFrame")
        self._title = title or t("gui.details.title")
        self._open = opened
        self._button = button(self, "", self.toggle, kind="ghost")
        self._button.pack(anchor="w")
        self._box = ReadOnlyText(self, height=height)
        self._refresh()

    def set(self, content: str) -> None:
        """Replace the technical text."""
        self._box.set(content)

    @property
    def is_open(self) -> bool:
        """Whether the section is expanded."""
        return self._open

    def toggle(self) -> None:
        """Open or close the section."""
        self._open = not self._open
        self._refresh()

    def _refresh(self) -> None:
        arrow = "▾" if self._open else "▸"
        self._button.configure(text=f"{arrow}  {self._title}")
        if self._open:
            self._box.pack(fill="both", expand=True, pady=(px(self, 4), 0))
        else:
            self._box.pack_forget()


class Notice(ttk.Frame):
    """Inline banner for status and errors, with expandable technical details.

    The frame itself stays packed (zero height while hidden) so its position
    in the page never changes; :meth:`show` / :meth:`hide` toggle the card.
    """

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, style=f"{surface_prefix(parent)}TFrame")
        self._card: tk.Frame | None = None

    def show(self, message: str, kind: str = "info", detail: str = "") -> None:
        """Display ``message`` as ``kind`` ("info" | "success" | "warn" | "error")."""
        self.hide()
        fg_attr, bg_attr, glyph = NOTICE_KINDS[kind]
        fg, bg = getattr(COLORS, fg_attr), getattr(COLORS, bg_attr)
        card = tk.Frame(self, background=bg, highlightthickness=1, highlightbackground=fg,
                        highlightcolor=fg)
        tk.Label(card, text=glyph, fg=bg, bg=fg, font=font("small_bold"), width=2).pack(
            side="left", anchor="n", padx=(px(self, 12), 0), pady=px(self, 12))
        inner = tk.Frame(card, background=bg)
        inner.pack(side="left", fill="both", expand=True, padx=px(self, 10), pady=px(self, 10))
        text = tk.Label(inner, text=message, fg=COLORS.text, bg=bg, font=font("body"),
                        justify="left", anchor="w")
        text.pack(fill="x")
        text.bind("<Configure>", lambda e: text.configure(wraplength=max(e.width - 4, 80)))
        if detail:
            holder = ttk.Frame(inner, style="Card.TFrame")
            holder.pack(fill="x", pady=(px(self, 6), 0))
            details = Expander(holder, height=5)
            details.pack(fill="x")
            details.set(detail)
        card.pack(fill="x")
        self._card = card

    def hide(self) -> None:
        """Remove the banner."""
        if self._card is not None:
            self._card.destroy()
            self._card = None

    @property
    def visible(self) -> bool:
        """Whether a banner is shown."""
        return self._card is not None


class Tooltip:
    """Hover/focus help bubble attached to ``widget``; ``text`` may be a callable."""

    DELAY_MS = 400

    def __init__(self, widget: tk.Misc, text: str | Callable[[], str]) -> None:
        self.widget = widget
        self._text = text
        self._tip: tk.Toplevel | None = None
        self._job: str | None = None
        for seq, handler in (("<Enter>", self._schedule), ("<Leave>", self.hide),
                             ("<FocusIn>", self._show_now), ("<FocusOut>", self.hide),
                             ("<ButtonPress>", self.hide), ("<Destroy>", self.hide)):
            widget.bind(seq, handler, add="+")

    def text(self) -> str:
        """Current tooltip text."""
        return self._text() if callable(self._text) else self._text

    def _schedule(self, _event: tk.Event | None = None) -> None:
        self._cancel()
        self._job = self.widget.after(self.DELAY_MS, self.show)

    def _show_now(self, _event: tk.Event | None = None) -> None:
        self._cancel()
        self.show()

    def _cancel(self) -> None:
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def show(self) -> None:
        """Open the bubble below the widget."""
        self._job = None
        if self._tip is not None or not self.text():
            return
        try:
            x = self.widget.winfo_rootx()
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + px(self.widget, 6)
        except tk.TclError:
            return
        c = COLORS
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        try:
            tip.wm_attributes("-topmost", True)
        except tk.TclError:
            pass
        tk.Label(tip, text=self.text(), justify="left", bg=c.surface_3, fg=c.text,
                 font=font("small"), wraplength=px(self.widget, 320), padx=px(self.widget, 10),
                 pady=px(self.widget, 7), highlightthickness=1, highlightbackground=c.border_strong
                 ).pack()
        tip.update_idletasks()
        screen_w = self.widget.winfo_screenwidth()
        x = min(x, screen_w - tip.winfo_reqwidth() - 4)
        tip.wm_geometry(f"+{max(x, 0)}+{y}")
        self._tip = tip

    def hide(self, _event: tk.Event | None = None) -> None:
        """Close the bubble."""
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


class InfoTip(tk.Canvas):
    """Small focusable "?" badge that explains a term on hover or keyboard focus."""

    def __init__(self, parent: tk.Misc, text: str) -> None:
        size = px(parent, 18)
        super().__init__(parent, width=size, height=size, highlightthickness=0, borderwidth=0,
                         background=surface_color(parent), takefocus=True, cursor="question_arrow")
        self._size = size
        self.tooltip = Tooltip(self, text)
        self.bind("<FocusIn>", lambda _e: self._draw(True), add="+")
        self.bind("<FocusOut>", lambda _e: self._draw(False), add="+")
        self.bind("<Enter>", lambda _e: self._draw(True), add="+")
        self.bind("<Leave>", lambda _e: self._draw(self.focus_get() is self), add="+")
        self._draw(False)

    def _draw(self, active: bool) -> None:
        c, s = COLORS, self._size
        self.delete("all")
        self.create_oval(1, 1, s - 2, s - 2, outline=c.accent if active else c.subtle, width=1.5,
                         fill=c.accent_soft if active else "")
        self.create_text(s / 2, s / 2, text="?", fill=c.accent if active else c.muted,
                         font=font("small_bold"))


class ScrollFrame(ttk.Frame):
    """Vertically scrollable container; children go into ``body``, centered up to ``max_width``."""

    def __init__(self, parent: tk.Misc, max_width: int | None = None) -> None:
        super().__init__(parent)
        self._max_width = max_width
        self._canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, background=COLORS.bg)
        self._bar = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._bar.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        self._bar.pack(side="right", fill="y")
        self.body = ttk.Frame(self._canvas)
        # A fixed width from the start: a shrink-wrapped body makes wrapping labels and
        # Columns feed back on each other until the canvas gets its first real size.
        self._window = self._canvas.create_window((0, 0), window=self.body, anchor="nw",
                                                  width=max_width or px(self, 900))
        self.body.bind("<Configure>", self._on_body)
        self._canvas.bind("<Configure>", self._on_canvas)
        self._canvas.bind("<Enter>", lambda _e: self._bind_wheel(True))
        self._canvas.bind("<Leave>", lambda _e: self._bind_wheel(False))

    def scroll_top(self) -> None:
        """Scroll back to the top (used when a new page is shown)."""
        self._canvas.yview_moveto(0)

    def _on_body(self, _event: tk.Event) -> None:
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas(self, event: tk.Event) -> None:
        width = min(event.width, self._max_width) if self._max_width else event.width
        self._canvas.coords(self._window, max((event.width - width) // 2, 0), 0)
        self._canvas.itemconfigure(self._window, width=width)

    def _bind_wheel(self, active: bool) -> None:
        if active:
            self._canvas.bind_all("<MouseWheel>", self._on_wheel)
        else:
            self._canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, event: tk.Event) -> None:
        first, last = self._canvas.yview()
        if first > 0.0 or last < 1.0:  # only scroll when content overflows
            self._canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")


class Columns(ttk.Frame):
    """Lays its children out in two columns, or one when narrower than ``breakpoint`` px."""

    def __init__(self, parent: tk.Misc, breakpoint: int, gap: int = 16) -> None:
        super().__init__(parent, style=f"{surface_prefix(parent)}TFrame")
        self._breakpoint, self._gap = breakpoint, gap
        self._items: list[tk.Widget] = []
        self._cols = 0
        self.bind("<Configure>", lambda e: self._layout(e.width))

    def add(self, widget: tk.Widget) -> None:
        """Add a child created with this frame as parent."""
        self._items.append(widget)
        self._cols = 0
        self._layout(self.winfo_width())

    def _layout(self, width: int) -> None:
        cols = 2 if width >= self._breakpoint else 1
        if cols == self._cols:
            return
        self._cols = cols
        gap = px(self, self._gap)
        for index in range(2):
            self.columnconfigure(index, weight=1 if index < cols else 0, uniform="col" if index < cols else "")
        for index, widget in enumerate(self._items):
            row, col = divmod(index, cols)
            pad_left = gap // 2 if col == 1 else 0
            pad_right = gap // 2 if cols == 2 and col == 0 else 0
            widget.grid(row=row, column=col, sticky="nsew", padx=(pad_left, pad_right),
                        pady=(0, gap))


class Stepper(tk.Canvas):
    """Horizontal progress indicator: numbered dots joined by a line, names below."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, height=px(parent, 62), highlightthickness=0, borderwidth=0,
                         background=surface_color(parent))
        self._names: list[str] = []
        self._current = 0
        self.bind("<Configure>", lambda _e: self._draw())

    def show(self, names: Sequence[str], current: int) -> None:
        """Show ``names`` with index ``current`` highlighted."""
        self._names, self._current = list(names), current
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        if not self._names:
            return
        c = COLORS
        width = max(self.winfo_width(), 1)
        radius = px(self, 12)
        cy = radius + px(self, 4)
        centers = stepper_centers(width, len(self._names))
        slot = width / len(self._names)
        for index in range(len(centers) - 1):
            done = index < self._current
            self.create_line(centers[index] + radius + 3, cy, centers[index + 1] - radius - 3, cy,
                             fill=c.accent if done else c.border_strong, width=2)
        for index, (x, name) in enumerate(zip(centers, self._names)):
            if index < self._current:
                self.create_oval(x - radius, cy - radius, x + radius, cy + radius, fill=c.accent,
                                 outline=c.accent)
                self.create_text(x, cy, text="✓", fill=c.on_accent, font=font("small_bold"))
                color, role = c.muted, "small"
            elif index == self._current:
                halo = radius + px(self, 4)
                self.create_oval(x - halo, cy - halo, x + halo, cy + halo, fill=c.accent_soft, outline="")
                self.create_oval(x - radius, cy - radius, x + radius, cy + radius, fill=c.accent,
                                 outline=c.accent)
                self.create_text(x, cy, text=str(index + 1), fill=c.on_accent, font=font("small_bold"))
                color, role = c.text, "small_bold"
            else:
                self.create_oval(x - radius, cy - radius, x + radius, cy + radius, fill=c.surface,
                                 outline=c.border_strong, width=1.5)
                self.create_text(x, cy, text=str(index + 1), fill=c.subtle, font=font("small_bold"))
                color, role = c.subtle, "small"
            self.create_text(x, cy + radius + px(self, 8), text=name, fill=color, font=font(role),
                             anchor="n", justify="center", width=max(slot - 6, 40))


class BarChart(tk.Canvas):
    """Before/after horizontal bars, one group per metric.

    ``rows`` are :class:`~osu_tablet_assistant.gui.session.ComparisonRow`-like
    objects with ``label``, ``before``, ``after``, ``unit``, ``digits`` and
    ``lower_is_better``.
    """

    def __init__(self, parent: tk.Misc, before_label: str, after_label: str, missing_label: str) -> None:
        super().__init__(parent, highlightthickness=0, borderwidth=0, background=surface_color(parent),
                         height=px(parent, 40))
        self._rows: list[Any] = []
        self._labels = (before_label, after_label, missing_label)
        self.bind("<Configure>", lambda _e: self._draw())

    def set_rows(self, rows: Sequence[Any]) -> None:
        """Replace the metrics shown."""
        self._rows = list(rows)
        group = px(self, 74)
        self.configure(height=max(group * len(self._rows), px(self, 20)))
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        c = COLORS
        width = max(self.winfo_width(), 1)
        tag_w = px(self, 64)          # "Antes" / "Después"
        value_w = px(self, 86)        # value text after the bar
        verdict_w = px(self, 120)
        bar_x = tag_w
        bar_max = max(width - tag_w - value_w - verdict_w, px(self, 40))
        group, bar_h = px(self, 74), px(self, 12)
        before_label, after_label, missing_label = self._labels
        for index, row in enumerate(self._rows):
            top = index * group
            self.create_text(0, top + px(self, 2), text=row.label, anchor="nw", fill=c.text,
                             font=font("strong"))
            lengths = bar_lengths((row.before, row.after), bar_max)
            for line, (name, value, length, color) in enumerate((
                    (before_label, row.before, lengths[0], c.before),
                    (after_label, row.after, lengths[1], c.accent))):
                y = top + px(self, 28) + line * px(self, 20)
                self.create_text(0, y, text=name, anchor="w", fill=c.muted, font=font("small"))
                if value is None:
                    self.create_text(bar_x, y, text=missing_label, anchor="w", fill=c.subtle,
                                     font=font("small"))
                    continue
                self.create_rectangle(bar_x, y - bar_h / 2, bar_x + bar_max, y + bar_h / 2,
                                      fill=c.surface_2, outline="")
                if length:
                    self.create_rectangle(bar_x, y - bar_h / 2, bar_x + length, y + bar_h / 2,
                                          fill=color, outline="")
                self.create_text(bar_x + bar_max + px(self, 8), y, anchor="w", fill=c.text,
                                 text=format_measure(value, row.digits, row.unit), font=font("small_bold"))
            verdict = compare_values(row.before, row.after, row.lower_is_better)
            if verdict is not None:
                change = relative_change(row.before, row.after)
                text = t(f"gui.compare.verdict.{verdict}",
                         pct=f"{abs(change) * 100:.0f}" if change is not None else "-")
                color = {"better": c.good, "worse": c.warn, "same": c.muted}[verdict]
                self.create_text(width, top + px(self, 38), text=text, anchor="e", fill=color,
                                 font=font("small_bold"))


class AreaPreview(tk.Canvas):
    """Tablet outline with the active area drawn to scale, plus the display it maps onto."""

    def __init__(self, parent: tk.Misc, height: int = 250) -> None:
        super().__init__(parent, highlightthickness=0, borderwidth=0, background=surface_color(parent),
                         height=px(parent, height))
        self._data: tuple[Any, ...] | None = None
        self.bind("<Configure>", lambda _e: self._draw())

    def set_area(self, tablet_w: float, tablet_h: float, area: Rect, current: Rect | None,
                 display: tuple[int, int] | None, labels: dict[str, str]) -> None:
        """Show ``area`` (mm, center-based) on a ``tablet_w`` x ``tablet_h`` mm tablet.

        ``current`` (dashed) is the area in use before the change; ``labels``
        holds the translated "tablet", "current" and "display" captions.
        """
        self._data = (tablet_w, tablet_h, area, current, display, labels)
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        if self._data is None:
            return
        tablet_w, tablet_h, area, current, display, labels = self._data
        c = COLORS
        width, height = max(self.winfo_width(), 1), max(self.winfo_height(), 1)
        show_display = display is not None and width > px(self, 300)
        tablet_box_w = min(width * (0.68 if show_display else 1.0), px(self, 520))
        geo = area_geometry(tablet_w, tablet_h, area, current, tablet_box_w, height - px(self, 22),
                            pad=px(self, 26))
        x0, y0, x1, y1 = geo.tablet
        bezel = px(self, 10)
        self.create_rectangle(x0 - bezel, y0 - bezel, x1 + bezel, y1 + bezel, fill=c.surface_2,
                              outline=c.border_strong, width=1)
        self.create_rectangle(x0, y0, x1, y1, fill=c.bg, outline=c.border, width=1)
        if geo.current is not None:
            self.create_polygon(*_flat(geo.current), fill="", outline=c.muted, dash=(4, 3), width=1)
        self.create_polygon(*_flat(geo.area), fill=c.accent_soft, outline=c.accent, width=2)
        ax0, ay0, ax1, ay1 = geo.area_bbox
        cx, cy = (ax0 + ax1) / 2, (ay0 + ay1) / 2
        top, left = ay0, ax0
        if geo.current is not None:   # keep the size labels clear of the dashed outline
            top = min(top, min(p[1] for p in geo.current))
            left = min(left, min(p[0] for p in geo.current))
        self.create_oval(cx - 3, cy - 3, cx + 3, cy + 3, fill=c.accent, outline="")
        self.create_text(cx, top - px(self, 4), text=f"{area.width:.1f} mm", anchor="s", fill=c.accent,
                         font=font("small_bold"))
        self.create_text(left - px(self, 6), cy, text=f"{area.height:.1f} mm", anchor="e",
                         fill=c.accent, font=font("small_bold"))
        self.create_text((x0 + x1) / 2, y1 + bezel + px(self, 6), anchor="n", fill=c.muted,
                         font=font("small"),
                         text=f"{labels['tablet']}: {tablet_w:g} x {tablet_h:g} mm")
        if geo.current is not None:
            self.create_line(x0, y0 - bezel - px(self, 12), x0 + px(self, 18), y0 - bezel - px(self, 12),
                             fill=c.muted, dash=(4, 3))
            self.create_text(x0 + px(self, 24), y0 - bezel - px(self, 12), anchor="w", fill=c.muted,
                             font=font("small"), text=labels["current"])
        if show_display and display is not None:
            start = x1 + bezel + px(self, 40)
            right = min(width - px(self, 8), start + (x1 - x0) * 0.8)
            box = display_box(display, start, right, height - px(self, 22))
            dx0, dy0, dx1, dy1 = box
            mid = (dy0 + dy1) / 2
            self.create_line(x1 + bezel + px(self, 8), mid, dx0 - px(self, 8), mid, fill=c.accent,
                             width=2, arrow="last", arrowshape=(8, 10, 4))
            self.create_rectangle(dx0, dy0, dx1, dy1, fill=c.accent_soft, outline=c.accent, width=2)
            self.create_rectangle((dx0 + dx1) / 2 - px(self, 10), dy1, (dx0 + dx1) / 2 + px(self, 10),
                                  dy1 + px(self, 6), fill=c.border_strong, outline="")
            self.create_text((dx0 + dx1) / 2, dy1 + px(self, 12), anchor="n", fill=c.muted,
                             font=font("small"), justify="center",
                             text=f"{labels['display']}\n{display[0]} x {display[1]} px")


def _flat(points: Sequence[tuple[float, float]]) -> list[float]:
    return [v for point in points for v in point]


def parse_float(text: str) -> float:
    """Parse a number typed by the user (accepts a decimal comma)."""
    return float(text.strip().replace(",", "."))


# ================================================================ pure helpers
def stepper_centers(width: float, count: int) -> list[float]:
    """X centers of ``count`` step dots: one equal slot each, dot in the slot's middle."""
    if count <= 0:
        return []
    slot = width / count
    return [slot / 2 + i * slot for i in range(count)]


def bar_lengths(values: Sequence[float | None], max_px: float, min_px: float = 3.0) -> list[int]:
    """Bar length per value, scaled so the largest fills ``max_px`` (None and 0 -> 0).

    Any positive value gets at least ``min_px`` so small values stay visible.
    """
    known = [v for v in values if v is not None and math.isfinite(v) and v > 0]
    top = max(known, default=0.0)
    out = []
    for value in values:
        if value is None or not math.isfinite(value) or value <= 0 or top <= 0:
            out.append(0)
        else:
            out.append(int(round(max(value / top * max_px, min_px))))
    return out


def relative_change(before: float | None, after: float | None) -> float | None:
    """(after - before) / before; None when either is missing or before is 0."""
    if before is None or after is None or before == 0:
        return None
    return (after - before) / before


def compare_values(before: float | None, after: float | None, lower_is_better: bool = True,
                   tolerance: float = 0.05) -> str | None:
    """"better" | "worse" | "same" for a metric, None when a value is missing."""
    if before is None or after is None:
        return None
    if before == 0:
        if after == 0:
            return "same"
        return "worse" if lower_is_better else "better"
    change = (after - before) / abs(before)
    if abs(change) < tolerance:
        return "same"
    improved = change < 0 if lower_is_better else change > 0
    return "better" if improved else "worse"


def format_measure(value: float, digits: int, unit: str) -> str:
    """``value`` with ``digits`` decimals and its unit, e.g. "0.042 mm"."""
    text = f"{value:.{digits}f}"
    return f"{text} {unit}".strip()


@dataclass(frozen=True)
class AreaGeometry:
    """Canvas coordinates computed by :func:`area_geometry`."""
    scale: float                                  # canvas px per tablet mm
    tablet: tuple[float, float, float, float]     # x0, y0, x1, y1 of the full active surface
    area: tuple[tuple[float, float], ...]         # 4 corners of the (rotated) area
    area_bbox: tuple[float, float, float, float]
    current: tuple[tuple[float, float], ...] | None


def rect_corners(rect: Rect) -> tuple[tuple[float, float], ...]:
    """Corners (mm) of an OTD center-based rect, rotated clockwise by ``rotation`` degrees."""
    angle = math.radians(rect.rotation or 0.0)
    cos, sin = math.cos(angle), math.sin(angle)
    hw, hh = rect.width / 2, rect.height / 2
    corners = []
    for dx, dy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)):
        corners.append((rect.x + dx * cos - dy * sin, rect.y + dx * sin + dy * cos))
    return tuple(corners)


def area_geometry(tablet_w: float, tablet_h: float, area: Rect, current: Rect | None,
                  box_w: float, box_h: float, pad: float = 24.0) -> AreaGeometry:
    """Fit a ``tablet_w`` x ``tablet_h`` mm tablet into a ``box_w`` x ``box_h`` px box.

    Returns the tablet rectangle (centered, aspect kept, ``pad`` px margin) and
    the area corners in canvas px. ``current`` is dropped when it equals ``area``.
    """
    tablet_w = tablet_w if tablet_w > 0 else max(area.width, 1.0)
    tablet_h = tablet_h if tablet_h > 0 else max(area.height, 1.0)
    avail_w, avail_h = max(box_w - 2 * pad, 1.0), max(box_h - 2 * pad, 1.0)
    scale = min(avail_w / tablet_w, avail_h / tablet_h)
    x0 = (box_w - tablet_w * scale) / 2
    y0 = (box_h - tablet_h * scale) / 2

    def to_canvas(rect: Rect) -> tuple[tuple[float, float], ...]:
        return tuple((x0 + mx * scale, y0 + my * scale) for mx, my in rect_corners(rect))

    corners = to_canvas(area)
    xs, ys = [p[0] for p in corners], [p[1] for p in corners]
    shown_current = None
    if current is not None and current != area and current.width > 0 and current.height > 0:
        shown_current = to_canvas(current)
    return AreaGeometry(scale, (x0, y0, x0 + tablet_w * scale, y0 + tablet_h * scale), corners,
                        (min(xs), min(ys), max(xs), max(ys)), shown_current)


def display_box(display: tuple[int, int], left: float, right: float, height: float,
                max_fraction: float = 0.45) -> tuple[float, float, float, float]:
    """Rectangle (x0, y0, x1, y1) of a monitor with the display's aspect, inside ``left``..``right``."""
    width_px, height_px = display
    avail_w = max(right - left, 1.0)
    avail_h = max(height * max_fraction, 1.0)
    aspect = width_px / height_px if width_px > 0 and height_px > 0 else 16 / 9
    w = min(avail_w, avail_h * aspect)
    h = w / aspect
    x0 = left + (avail_w - w) / 2
    y0 = (height - h) / 2
    return x0, y0, x0 + w, y0 + h
