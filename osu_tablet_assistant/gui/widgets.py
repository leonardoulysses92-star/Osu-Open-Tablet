"""Design tokens, the "calm dark glass" ttk theme and the reusable widgets of the wizard.

Everything visual is defined here (colours in :mod:`.palette`) so the pages stay consistent:

* ``COLORS`` (palette.py), ``SPACE``, ``RADIUS`` and ``FONT_ROLES`` are the design tokens;
* :func:`apply_theme` turns them into named ttk styles. Rounded, antialiased shapes are ttk
  *image elements*: small bitmaps computed by :mod:`.raster` and stretched by Tk as nine-slice
  images, so resizing a card or a button costs no Python work at all;
* the widgets (``Card``, ``Notice``, ``Chip``, ``Stepper``, ``BarChart``, ``AreaPreview``,
  ``Tooltip``...) only use those styles and tokens.

Tk widgets are opaque rectangles (no real transparency, no backdrop blur), so every rounded shape is
blended against the known colour of the surface it sits on: each widget looks up its surface by
walking up its parents (``surface_prefix``) and the styles exist once per surface.

The geometry helpers at the end (``stepper_centers``, ``bar_lengths``, ``area_geometry``...) are pure
functions with no Tk dependency, unit-tested headlessly.
"""
from __future__ import annotations

import math
import queue
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from collections import OrderedDict
from dataclasses import dataclass, field
from tkinter import ttk
from typing import Any, Callable, Hashable, Iterable, Sequence

from ..models import Rect
from . import raster
from .i18n import t
from .palette import COLORS, mix

# ======================================================================== tokens
SPACE = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24, "xxl": 32}
# Corner radii (96-dpi px, scaled with ``px``).
RADIUS = {"card": 14, "tile": 12, "notice": 12, "control": 8, "field": 7, "segment": 9, "toggle": 7,
          "check": 4, "tooltip": 8, "bezel": 10}
TILE = 64                      # uniform centre of a control's nine-slice image (px)
FRAME_TILE = 128               # ... and of a panel's (cards are large: fewer, bigger blits per redraw)
FONT_FAMILY = "Segoe UI"
# role -> (size in points, weight)
FONT_ROLES: dict[str, tuple[int, str]] = {
    "display": (21, "bold"),
    "title": (16, "bold"),
    "heading": (12, "bold"),
    "body": (10, "normal"),
    "strong": (10, "bold"),
    "small": (9, "normal"),
    "small_bold": (9, "bold"),
    "mono": (9, "normal"),
}
# Preferred faces per role, first installed one wins (Windows 11 ships the variable Segoe UI;
# GDI exposes its named instances as families with names cut at 31 characters). A "Semib..."
# face replaces synthetic bold.
FONT_FACES: dict[str, tuple[str, ...]] = {
    "display": ("Segoe UI Variable Display Semib", "Segoe UI Semibold"),
    "title": ("Segoe UI Variable Display Semib", "Segoe UI Semibold"),
    "heading": ("Segoe UI Variable Text Semibold", "Segoe UI Semibold"),
    "strong": ("Segoe UI Variable Text Semibold", "Segoe UI Semibold"),
    "small_bold": ("Segoe UI Variable Text Semibold", "Segoe UI Semibold"),
    "body": ("Segoe UI Variable Text", FONT_FAMILY),
    "small": ("Segoe UI Variable Text", FONT_FAMILY),
    "mono": ("Cascadia Mono", "Consolas"),
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
# Surfaces a widget can sit on: style prefix -> background colour attribute of COLORS.
SURFACES = {"": "bg", "Card.": "surface", "Field.": "field",
            "Notice.info.": "info_soft", "Notice.success.": "good_soft",
            "Notice.warn.": "warn_soft", "Notice.error.": "error_soft"}
# Notice kind -> (foreground colour, background colour, icon name in raster.ICONS)
NOTICE_KINDS = {"info": ("info", "info_soft", "info"), "success": ("good", "good_soft", "success"),
                "warn": ("warn", "warn_soft", "warn"), "error": ("error", "error_soft", "error")}
# "tile" is a tall secondary button with centred, wrapped text (the companion's big intent buttons).
_BUTTON_STYLES = {"primary": "Primary.TButton", "secondary": "Secondary.TButton", "ghost": "Ghost.TButton",
                  "tile": "Tile.TButton"}
_FONTS: dict[str, str] = {}
_FONT_OBJECTS: list[tkfont.Font] = []


def px(widget: tk.Misc, value: float) -> int:
    """Scale a 96-dpi pixel size to the widget's actual screen DPI."""
    return round(value * widget.winfo_fpixels("1i") / 96.0)


def font(role: str) -> str:
    """Name of the Tk named font for a ``FONT_ROLES`` role (after :func:`apply_theme`)."""
    return _FONTS.get(role, "TkDefaultFont")


def _face(root: tk.Misc, role: str) -> tuple[str, str]:
    """(family, weight) for a role: the first installed face of ``FONT_FACES``, else the Tk default."""
    families = set(tkfont.families(root))
    weight = FONT_ROLES[role][1]
    for name in FONT_FACES.get(role, ()):
        if name in families:
            return name, "normal" if "Semib" in name else weight
    fallback = "TkFixedFont" if role == "mono" else "TkDefaultFont"
    return tkfont.nametofont(fallback, root=root).cget("family"), weight


# ================================================================= image cache
class ImageCache:
    """Bounded LRU of ``tk.PhotoImage`` objects by key (one per Tcl interpreter).

    The same key returns the same image object. Images that ttk elements use are also
    pinned by the theme, so an eviction here can never blank a style.
    """

    def __init__(self, limit: int = 512) -> None:
        self.limit = limit
        self._items: OrderedDict[Hashable, Any] = OrderedDict()

    def get(self, key: Hashable, factory: Callable[[], Any]) -> Any:
        """The cached image for ``key``, created with ``factory()`` on a miss."""
        if key in self._items:
            self._items.move_to_end(key)
            return self._items[key]
        item = self._items[key] = factory()
        while len(self._items) > self.limit:
            self._items.popitem(last=False)
        return item

    def __len__(self) -> int:
        return len(self._items)


@dataclass
class _Theme:
    """Per-interpreter theme state: ttk style object, images and what was already created."""
    root: tk.Misc
    style: ttk.Style
    images: ImageCache = field(default_factory=ImageCache)
    pinned: list[Any] = field(default_factory=list)
    done: set[Hashable] = field(default_factory=set)
    names: dict[Hashable, str] = field(default_factory=dict)

    def px(self, value: float) -> int:
        return px(self.root, value)

    def photo(self, key: Hashable, rows: Callable[[], raster.Rows]) -> tk.PhotoImage:
        """A PhotoImage of ``rows()`` (computed only on a cache miss)."""
        def make() -> tk.PhotoImage:
            data = rows()
            image = tk.PhotoImage(master=self.root, width=len(data[0]), height=len(data))
            image.put(raster.tcl_data(data))
            return image
        return self.images.get(key, make)

    def rr(self, width: int, height: int, radius: float, fill: str, bg: str, **kw: Any) -> tk.PhotoImage:
        """A cached rounded-rectangle image (see :func:`raster.rounded_rect`)."""
        key = ("rr", width, height, radius, fill, bg, tuple(sorted(kw.items())))
        return self.photo(key, lambda: raster.rounded_rect(width, height, radius, fill, bg, **kw))

    def nine(self, key: Hashable, rows: Callable[[], raster.Rows], edge_x: int, edge_y: int,
             tile: tuple[int, int] = (TILE, 8)) -> tk.PhotoImage:
        """A nine-slice source image with a ``tile`` (x, y) px uniform centre, built from compact ``rows``.

        ``rows()`` is the shape with a 1 px uniform centre column at ``edge_x`` and row at ``edge_y``
        (``0`` = that axis is not sliced). Only those compact pixels are computed in Python; Tk
        replicates the centre with ``copy -to`` (C speed). A wide centre keeps Tk's tiling cheap
        (it redraws a stretched element by blitting the centre once per tile), but the image size
        is also a control's minimum size, so controls use a small tile and panels a large one.
        """
        tile_x, tile_y = tile

        def make() -> tk.PhotoImage:
            data = rows()
            cw, ch = len(data[0]), len(data)
            small = tk.PhotoImage(master=self.root, width=cw, height=ch)
            small.put(raster.tcl_data(data))
            width = cw + (tile_x - 1 if edge_x else 0)
            height = ch + (tile_y - 1 if edge_y else 0)
            big = tk.PhotoImage(master=self.root, width=width, height=height)

            def spans(edge: int, size: int, total: int) -> list[tuple[int, int, int, int]]:
                if not edge:
                    return [(0, size, 0, total)]
                far = size - edge - 1
                return [(0, edge, 0, edge), (edge, edge + 1, edge, total - far), (edge + 1, size, total - far, total)]
            for sx0, sx1, dx0, dx1 in spans(edge_x, cw, width):
                for sy0, sy1, dy0, dy1 in spans(edge_y, ch, height):
                    if sx1 > sx0 and sy1 > sy0:
                        self.root.tk.call(big, "copy", small, "-from", sx0, sy0, sx1, sy1,
                                          "-to", dx0, dy0, dx1, dy1)
            return big
        return self.images.get(("nine", key, edge_x, edge_y, tile), make)

    def rounded(self, radius: float, fill: str, bg: str, tile: tuple[int, int] = (TILE, 8),
                height: int | None = None, **kw: Any) -> tuple[tk.PhotoImage, Any]:
        """A nine-slice rounded rectangle and its element ``border``; ``height`` fixes it (a pill row)."""
        edge = _slice(radius)
        compact_h = height if height is not None else 2 * edge + 1
        image = self.nine(("rr", radius, fill, bg, height, tuple(sorted(kw.items()))),
                          lambda: raster.rounded_rect(2 * edge + 1, compact_h, radius, fill, bg, **kw),
                          edge, 0 if height is not None else edge, tile)
        return image, ((edge, 0, edge, 0) if height is not None else edge)

    def element(self, name: str, images: Sequence[tuple[str, tk.PhotoImage]], border: Any,
                sticky: str = "nsew", padding: Any = 0) -> str:
        """Create image element ``name`` once; ``images`` are (statespec, image), the default last."""
        if name not in self.done:
            self.done.add(name)
            self.pinned.extend(image for _spec, image in images)
            pairs = [(spec, image) for spec, image in images[:-1]]
            self.style.element_create(name, "image", images[-1][1], *pairs, border=border, sticky=sticky,
                                      padding=padding)
        return name


def _theme(widget: tk.Misc) -> _Theme:
    root = widget._root()
    theme = root.__dict__.get("_glass_theme")
    if theme is None:
        apply_theme(root)
        theme = root.__dict__["_glass_theme"]
    return theme


def _key(prefix: str) -> str:
    return prefix.replace(".", "") or "Page"


def _bg(prefix: str) -> str:
    return getattr(COLORS, SURFACES[prefix])


def _slice(radius: float) -> int:
    return math.ceil(radius) + 1


# ===================================================================== theme
def apply_theme(root: tk.Misc) -> ttk.Style:
    """Create the named fonts and every ttk style the wizard uses (dark glass theme)."""
    c = COLORS
    _FONT_OBJECTS.clear()
    for role, (size, _weight) in FONT_ROLES.items():
        name = f"App{role.title().replace('_', '')}"
        family, weight = _face(root, role)
        try:
            named = tkfont.nametofont(name, root=root)
            named.configure(family=family, size=size, weight=weight)
        except tk.TclError:
            named = tkfont.Font(root, name=name, family=family, size=size, weight=weight)
        _FONT_OBJECTS.append(named)   # a Font created here is deleted when garbage-collected
        _FONTS[role] = name
    body_family = _face(root, "body")[0]
    for default in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
        tkfont.nametofont(default, root=root).configure(family=body_family, size=FONT_ROLES["body"][0])

    style = ttk.Style(root)
    style.theme_use("clam")
    theme = root.__dict__.get("_glass_theme")
    if theme is None:
        theme = _Theme(root, style)
        root.__dict__["_glass_theme"] = theme
    root.configure(background=c.bg)
    for option, value in (("background", c.surface_2), ("foreground", c.text), ("selectBackground", c.accent),
                          ("selectForeground", c.on_accent), ("font", font("body")), ("borderWidth", 0),
                          ("relief", "flat")):
        root.option_add(f"*TCombobox*Listbox.{option}", value)
    # ttk widgets created without a style inside a card pick the card variant automatically.
    for cls, name in (("TFrame", "Card.TFrame"), ("TLabel", "Card.TLabel"), ("TEntry", "Card.TEntry"),
                      ("TCombobox", "Card.TCombobox"), ("TScrollbar", "Card.Vertical.TScrollbar"),
                      ("TCheckbutton", "Card.TCheckbutton"), ("TRadiobutton", "Card.TRadiobutton")):
        root.option_add(f"*GlassCard*{cls}.style", name)

    style.configure(".", background=c.bg, foreground=c.text, font=font("body"),
                    bordercolor=c.border, lightcolor=c.bg, darkcolor=c.bg, troughcolor=c.bg,
                    focuscolor=c.accent, selectbackground=c.accent_soft, selectforeground=c.text,
                    insertcolor=c.text, fieldbackground=c.field, arrowcolor=c.muted)
    style.map(".", foreground=[("disabled", c.subtle)], background=[])

    for prefix in ("", "Card."):
        _ensure_base(theme, prefix)
        for kind in _BUTTON_STYLES:
            _button_style(theme, prefix, kind)
        _check_style(theme, prefix, radio=False)
        _check_style(theme, prefix, radio=True)
        _entry_style(theme, prefix)
        _combobox_style(theme, prefix)
        _scrollbar_style(theme, prefix)
        _track_style(theme, prefix)
    _ensure_base(theme, "Field.")
    _scrollbar_style(theme, "Field.")
    style.layout("TButton", style.layout("Secondary.TButton"))
    style.configure("TButton", **style.configure("Secondary.TButton"))
    _toolbutton_style(theme)
    _tree_style(theme)
    _progress_style(theme)
    style.configure("TSeparator", background=c.border)
    style.configure("Header.TFrame", background=c.surface)
    return style


def _ensure_base(th: _Theme, prefix: str) -> None:
    """Frame and label styles of a surface."""
    if ("base", prefix) in th.done:
        return
    th.done.add(("base", prefix))
    c, s, bg = COLORS, th.style, _bg(prefix)
    s.configure(f"{prefix}TFrame", background=bg)
    s.configure(f"{prefix}TLabel", background=bg, foreground=c.text)
    for role, (font_role, color) in LABEL_ROLES.items():
        s.configure(f"{prefix}{role}.TLabel", background=bg, foreground=getattr(c, color), font=font(font_role))


def _button_fills(kind: str, bg: str) -> dict[str, tuple[str, str | None]]:
    """Fill and border per state of a button kind on surface ``bg``."""
    c = COLORS
    if kind == "primary":
        return {"normal": (c.accent, None), "active": (c.accent_hover, None), "pressed": (c.accent_pressed, None),
                "disabled": (c.surface_2, c.border)}
    if kind in ("secondary", "tile"):
        return {"normal": (c.surface_2, c.border_strong), "active": (c.surface_3, c.border_strong),
                "pressed": (c.surface_2, c.border), "disabled": (mix(bg, c.surface_2, 0.5), c.border)}
    return {"normal": (bg, None), "active": (c.surface_2, None), "pressed": (c.surface_3, None),
            "disabled": (bg, None)}


def _button_style(th: _Theme, prefix: str, kind: str) -> str:
    name = prefix + _BUTTON_STYLES[kind]
    if name in th.done:
        return name
    th.done.add(name)
    c, bg = COLORS, _bg(prefix)
    radius, ring_w = th.px(RADIUS["control"]), max(th.px(2), 2)
    fills = _button_fills(kind, bg)
    ring = c.text if kind == "primary" else c.accent

    def image(state: str, focus: bool = False) -> tk.PhotoImage:
        fill, border = fills[state]
        return th.rounded(radius, fill, bg, border=border, ring=ring if focus else None, ring_width=ring_w)[0]
    elem = th.element(f"glass{_key(prefix)}.{kind}.Button.border", [
        ("disabled", image("disabled")), ("pressed", image("pressed")),
        ("focus active", image("active", True)), ("focus", image("normal", True)),
        ("active", image("active")), ("", image("normal"))], border=_slice(radius))
    th.style.layout(name, [(elem, {"sticky": "nsew", "children": [
        ("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})])
    ghost = kind == "ghost"
    padding = {"ghost": (12, 6), "tile": (10, 11)}.get(kind, (16, 7))
    th.style.configure(name, background=bg, foreground=c.on_accent if kind == "primary" else c.text,
                       padding=(th.px(padding[0]), th.px(padding[1])), anchor="center", justify="center",
                       font=font("body" if ghost else "strong"), relief="flat", focusthickness=0)
    if kind == "tile":
        th.style.configure(name, wraplength=th.px(230))
    th.style.map(name, foreground=[("disabled", c.subtle)], background=[])
    return name


def _check_style(th: _Theme, prefix: str, radio: bool) -> str:
    name = f"{prefix}{'TRadiobutton' if radio else 'TCheckbutton'}"
    if name in th.done:
        return name
    th.done.add(name)
    c, bg = COLORS, _bg(prefix)
    size, gap = th.px(16), th.px(9)
    radius = size / 2 if radio else th.px(RADIUS["check"])
    thin, thick = 1.0, max(float(th.px(2)), 2.0)
    mark_name = "pip" if radio else "check"

    def image(fill: str, border: str, width: float = thin, mark: str | None = None) -> tk.PhotoImage:
        def rows() -> raster.Rows:
            base = raster.rounded_rect(size, size, radius, fill, bg, border, width)
            if mark is not None:
                base = raster.overlay(base, raster.icon(mark_name, size, mark, fill), fill)
            return raster.pad_sides(base, 0, gap, bg)
        return th.photo(("check", radio, size, gap, bg, fill, border, width, mark), rows)
    elem = th.element(f"glass{_key(prefix)}.{'Radio' if radio else 'Check'}.indicator", [
        ("disabled selected", image(c.border_strong, c.border_strong, mark=c.muted)),
        ("disabled", image(mix(bg, c.surface_2, 0.5), c.border)),
        ("selected focus", image(c.accent, mix(c.accent, c.text, 0.65), thick, mark=c.on_accent)),
        ("selected", image(c.accent, c.accent, mark=c.on_accent)),
        ("focus", image(c.field, c.accent, thick)),
        ("active", image(c.field, c.muted)),
        ("", image(c.field, c.border_strong))], border=0, sticky="")
    cls = "Radiobutton" if radio else "Checkbutton"
    th.style.layout(name, [(f"{cls}.padding", {"sticky": "nswe", "children": [
        (elem, {"side": "left", "sticky": ""}), (f"{cls}.label", {"side": "left", "sticky": "nswe"})]})])
    th.style.configure(name, background=bg, foreground=c.text, padding=(0, th.px(3)), font=font("body"))
    th.style.map(name, foreground=[("disabled", c.subtle)], background=[])
    return name


def _field_images(th: _Theme, prefix: str) -> tuple[int, dict[str, tk.PhotoImage]]:
    c, bg = COLORS, _bg(prefix)
    radius = th.px(RADIUS["field"])
    focus_w = max(th.px(1.5), 1.5)
    return _slice(radius), {
        "disabled": th.rounded(radius, mix(bg, c.surface_2, 0.5), bg, border=c.border)[0],
        "focus": th.rounded(radius, c.field, bg, border=c.accent, border_width=focus_w)[0],
        "hover": th.rounded(radius, c.field, bg, border=mix(c.border_strong, c.muted, 0.35))[0],
        "normal": th.rounded(radius, c.field, bg, border=c.border_strong)[0]}


def _entry_style(th: _Theme, prefix: str) -> str:
    name = f"{prefix}TEntry"
    if name in th.done:
        return name
    th.done.add(name)
    c, bg = COLORS, _bg(prefix)
    border, images = _field_images(th, prefix)
    elem = th.element(f"glass{_key(prefix)}.Entry.field", [
        ("disabled", images["disabled"]), ("readonly", images["disabled"]), ("focus", images["focus"]),
        ("hover", images["hover"]), ("", images["normal"])], border=border)
    th.style.layout(name, [(elem, {"sticky": "nswe", "children": [
        ("Entry.padding", {"sticky": "nswe", "children": [("Entry.textarea", {"sticky": "nswe"})]})]})])
    th.style.configure(name, background=bg, fieldbackground=c.field, foreground=c.text, insertcolor=c.text,
                       padding=(th.px(10), th.px(6)), selectbackground=c.accent_soft, selectforeground=c.text)
    th.style.map(name, foreground=[("disabled", c.subtle)], fieldbackground=[], background=[])
    return name


def _combobox_style(th: _Theme, prefix: str) -> str:
    name = f"{prefix}TCombobox"
    if name in th.done:
        return name
    th.done.add(name)
    c, bg = COLORS, _bg(prefix)
    border, images = _field_images(th, prefix)
    k = _key(prefix)
    field_elem = th.element(f"glass{k}.Combobox.field", [
        ("disabled", images["disabled"]), ("focus", images["focus"]), ("hover", images["hover"]),
        ("", images["normal"])], border=border)
    size, left, right = th.px(14), th.px(2), th.px(10)

    def arrow(color: str, fill: str) -> tk.PhotoImage:
        return th.photo(("arrow", size, left, right, color, fill), lambda: raster.pad_sides(
            raster.icon("chevron_down", size, color, fill), left, right, fill))
    arrow_elem = th.element(f"glass{k}.Combobox.downarrow", [
        ("disabled", arrow(c.subtle, mix(bg, c.surface_2, 0.5))), ("", arrow(c.muted, c.field))],
        border=0, sticky="")
    th.style.layout(name, [(field_elem, {"sticky": "nswe", "children": [
        (arrow_elem, {"side": "right", "sticky": "ns"}),
        ("Combobox.padding", {"expand": "1", "sticky": "nswe", "children": [
            ("Combobox.textarea", {"sticky": "nswe"})]})]})])
    th.style.configure(name, background=bg, fieldbackground=c.field, foreground=c.text,
                       padding=(th.px(10), th.px(5)), selectbackground=c.field, selectforeground=c.text,
                       insertcolor=c.text)
    th.style.map(name, foreground=[("disabled", c.subtle)], fieldbackground=[], background=[],
                 selectbackground=[("readonly", c.field)], selectforeground=[("readonly", c.text)])
    return name


def _scrollbar_style(th: _Theme, prefix: str) -> str:
    name = f"{prefix}Vertical.TScrollbar"
    if name in th.done:
        return name
    th.done.add(name)
    c, bg = COLORS, _bg(prefix)
    width = th.px(10)
    thumb = max(th.px(6), 4)
    left = (width - thumb) // 2
    radius = thumb / 2
    edge = _slice(radius)

    def image(color: str) -> tk.PhotoImage:
        return th.nine(("thumb", width, thumb, color, bg), lambda: raster.pad_sides(
            raster.rounded_rect(thumb, 2 * edge + 1, radius, color, bg), left, width - thumb - left, bg), 0, edge,
                        tile=(1, 16))
    elem = th.element(f"glass{_key(prefix)}.Vertical.Scrollbar.thumb", [
        ("pressed", image(c.muted)), ("active", image(c.subtle)), ("", image(c.border_strong))],
        border=(0, edge, 0, edge))
    th.style.layout(name, [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
        (elem, {"expand": "1", "sticky": "nswe"})]})])
    th.style.configure(name, troughcolor=bg, background=bg, bordercolor=bg, lightcolor=bg, darkcolor=bg,
                       arrowsize=width, width=width, gripcount=0)
    return name


def _track_style(th: _Theme, prefix: str) -> str:
    """The rounded track of a segmented control."""
    name = f"{prefix}Segment.TFrame"
    if name in th.done:
        return name
    th.done.add(name)
    c, bg = COLORS, _bg(prefix)
    image, border = th.rounded(th.px(RADIUS["segment"]), c.field, bg, border=c.border)
    elem = th.element(f"glass{_key(prefix)}.Segment.border", [("", image)], border=border)
    th.style.layout(name, [(elem, {"sticky": "nsew"})])
    th.style.configure(name, background=c.field)
    return name


def _toolbutton_style(th: _Theme) -> None:
    """Segments of a segmented control (language switch, 1-5 rating): they sit on the track colour."""
    c, bg = COLORS, COLORS.field
    radius, ring_w = th.px(RADIUS["toggle"]), max(th.px(1.5), 1.5)

    def image(fill: str, ring: str | None = None) -> tk.PhotoImage:
        return th.rounded(radius, fill, bg, tile=(16, 4), ring=ring, ring_width=ring_w)[0]
    elem = th.element("glass.Segment.Toolbutton.border", [
        ("disabled selected", image(c.surface_3)), ("disabled", image(bg)),
        ("selected focus", image(c.accent, c.text)), ("selected active", image(c.accent_hover)),
        ("selected", image(c.accent)), ("focus", image(bg, c.accent)), ("pressed", image(c.surface_3)),
        ("active", image(c.surface_2)), ("", image(bg))], border=_slice(radius))
    s = th.style
    s.layout("Segment.Toolbutton", [(elem, {"sticky": "nsew", "children": [
        ("Toolbutton.padding", {"sticky": "nsew", "children": [("Toolbutton.label", {"sticky": "nsew"})]})]})])
    s.configure("Segment.Toolbutton", background=bg, foreground=c.muted, padding=(th.px(11), th.px(4)),
                font=font("small_bold"), anchor="center", focusthickness=0)
    s.map("Segment.Toolbutton", background=[],
          foreground=[("disabled", c.subtle), ("selected", c.on_accent), ("active", c.text)])


def _tree_style(th: _Theme) -> None:
    c, s = COLORS, th.style
    for name in ("Treeview", "Marked.Treeview"):
        s.configure(name, background=c.surface, fieldbackground=c.surface, foreground=c.text,
                    bordercolor=c.surface, lightcolor=c.surface, darkcolor=c.surface,
                    rowheight=th.px(32), font=font("body"), indent=th.px(16))
        s.map(name, background=[("selected", c.accent_soft)], foreground=[("selected", c.text)])
        s.layout(name, [("Treeview.field", {"sticky": "nswe", "border": "0", "children": [
            ("Treeview.padding", {"sticky": "nswe", "children": [("Treeview.treearea", {"sticky": "nswe"})]})]})])
    item = [("Treeitem.indicator", {"side": "left", "sticky": ""}), ("Treeitem.image", {"side": "left", "sticky": ""}),
            ("Treeitem.text", {"side": "left", "sticky": ""})]
    s.layout("Treeview.Item", [("Treeitem.padding", {"sticky": "nswe", "children": item})])   # no dotted focus box
    s.layout("Marked.Treeview.Item", [("Treeitem.padding", {"sticky": "nswe", "children": item[1:]})])
    rows = tuple((c.surface,) * 5 for _ in range(7)) + ((c.border,) * 5,)   # flat cell, hairline at the bottom
    cell = th.element("glass.Treeheading.cell", [("", th.nine(("heading", c.surface, c.border), lambda: rows, 2, 2))],
                      border=(2, 2, 2, 5))
    s.layout("Treeview.Heading", [(cell, {"sticky": "nswe", "children": [
        ("Treeheading.padding", {"sticky": "nswe", "children": [
            ("Treeheading.image", {"side": "right", "sticky": ""}), ("Treeheading.text", {"sticky": "we"})]})]})])
    s.configure("Treeview.Heading", background=c.surface, foreground=c.muted, font=font("small_bold"),
                padding=(th.px(8), th.px(6)), relief="flat")
    s.map("Treeview.Heading", foreground=[("active", c.text)], background=[])


def _progress_style(th: _Theme) -> None:
    c = COLORS
    height = max(th.px(6) // 2 * 2, 4)
    radius = height / 2
    trough_color = c.surface_3
    trough_image, border = th.rounded(radius, trough_color, c.bg, height=height)
    trough = th.element("glassPage.Horizontal.Progressbar.trough", [("", trough_image)], border=border)
    bar = th.element("glassPage.Horizontal.Progressbar.pbar", [
        ("", th.rounded(radius, c.accent, trough_color, height=height)[0])], border=border)
    th.style.layout("Accent.Horizontal.TProgressbar", [(trough, {"sticky": "nswe", "children": [
        (bar, {"side": "left", "sticky": "ns"})]})])
    th.style.configure("Accent.Horizontal.TProgressbar", background=c.bg, thickness=height)


def _chip_style(th: _Theme, prefix: str, kind: str) -> str:
    name = f"{prefix}Chip.{kind}.TLabel"
    if name in th.done:
        return name
    th.done.add(name)
    fg, fill = Chip.colors(kind)
    bg = _bg(prefix)
    line = tkfont.nametofont(font("small_bold"), root=th.root).metrics("linespace")
    pad_y = th.px(2)
    height = line + 2 * pad_y
    image, border = th.rounded(height / 2, fill, bg, tile=(16, 4), height=height)   # short words stay short
    elem = th.element(f"glass{_key(prefix)}.Chip{kind}.Label.border", [("", image)], border=border)
    th.style.layout(name, [(elem, {"sticky": "nsew", "children": [
        ("Label.padding", {"sticky": "nsew", "children": [("Label.label", {"sticky": "nsew"})]})]})])
    th.style.configure(name, background=bg, foreground=fg, font=font("small_bold"),
                       padding=(th.px(9), pad_y), anchor="center")
    return name


def rounded_style(widget: tk.Misc, fill: str, bg: str, border: str | None = None, radius: float | None = None,
                  highlight: str | None = None, border_width: float = 1.0) -> str:
    """Name of a ttk.Frame style drawn as an antialiased rounded rectangle (created on first use).

    ``bg`` is the colour of the surface the frame sits on (its corners are blended into it).
    ``radius`` is in 96-dpi px.
    """
    th = _theme(widget)
    radius = th.px(RADIUS["card"] if radius is None else radius)
    key = ("frame", fill, bg, border, radius, highlight, border_width)
    name = th.names.get(key)
    if name is not None:
        return name
    number = len(th.names)
    name = th.names[key] = f"Glass{number}.TFrame"
    image, edge = th.rounded(radius, fill, bg, tile=(FRAME_TILE, FRAME_TILE // 2), border=border,
                             border_width=border_width,
                             highlight=highlight)
    elem = th.element(f"glass{number}.Frame.border", [("", image)], border=edge)
    th.style.layout(name, [(elem, {"sticky": "nsew"})])
    th.style.configure(name, background=fill)
    return name


def icon_image(widget: tk.Misc, name: str, size: float = 16, color: str | None = None, bg: str | None = None,
               knock: str | None = None) -> tk.PhotoImage:
    """A cached antialiased icon (``raster.ICONS``) of ``size`` 96-dpi px drawn on ``bg``."""
    th = _theme(widget)
    side = max(th.px(size), 6)
    color = color or COLORS.muted
    bg = bg or surface_color(widget)
    return th.photo(("icon", name, side, color, bg, knock), lambda: raster.icon(name, side, color, bg, knock))


def dark_title_bar(window: tk.Misc) -> None:
    """Ask Windows 10/11 for a dark title bar on ``window`` (best effort; no-op elsewhere)."""
    if sys.platform != "win32":
        return

    def apply(_event: tk.Event | None = None) -> None:
        try:
            import ctypes
            hwnd = int(window.wm_frame(), 16) or window.winfo_id()
            value = ctypes.c_int(1)
            for attribute in (20, 19):           # DWMWA_USE_IMMERSIVE_DARK_MODE (19 before Windows 10 20H1)
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value),
                                                              ctypes.sizeof(value)) == 0:
                    break
        except (AttributeError, OSError, ValueError, tk.TclError):
            pass
    try:
        window.bind("<Map>", lambda e: apply() if e.widget is window else None, add="+")
        apply()
    except tk.TclError:
        pass


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
    return _bg(surface_prefix(widget))


def _surface_style(parent: tk.Misc, base: str) -> str:
    prefix = surface_prefix(parent)
    _ensure_base(_theme(parent), prefix)
    return f"{prefix}{base}"


# ======================================================== canvas drawing helpers
def canvas_round_rect(canvas: tk.Canvas, x0: float, y0: float, x1: float, y1: float, radius: float, fill: str,
                      bg: str, border: str | None = None, keep: list[Any] | None = None,
                      tags: str | tuple[str, ...] = ()) -> None:
    """Antialiased rounded rectangle on a canvas: four corner bitmaps plus straight canvas rectangles.

    ``bg`` is the canvas colour under the corners; ``keep`` collects the images (the caller must
    hold them for as long as the drawing is shown).
    """
    x0, y0, x1, y1 = round(x0), round(y0), round(x1), round(y1)
    r = int(max(min(radius, (x1 - x0) / 2, (y1 - y0) / 2), 1))
    th = _theme(canvas)
    images = [th.photo(("quad", i, r, fill, bg, border),
                       lambda i=i: raster.quadrants(raster.rounded_rect(2 * r, 2 * r, r, fill, bg, border))[i])
              for i in range(4)]
    if keep is not None:
        keep.extend(images)
    for image, (x, y, anchor) in zip(images, ((x0, y0, "nw"), (x1, y0, "ne"), (x0, y1, "sw"), (x1, y1, "se"))):
        canvas.create_image(x, y, image=image, anchor=anchor, tags=tags)
    canvas.create_rectangle(x0 + r, y0, x1 - r, y1, fill=fill, width=0, tags=tags)
    canvas.create_rectangle(x0, y0 + r, x1, y1 - r, fill=fill, width=0, tags=tags)
    if border is not None:
        for bx0, by0, bx1, by1 in ((x0 + r, y0, x1 - r, y0 + 1), (x0 + r, y1 - 1, x1 - r, y1),
                                   (x0, y0 + r, x0 + 1, y1 - r), (x1 - 1, y0 + r, x1, y1 - r)):
            canvas.create_rectangle(bx0, by0, bx1, by1, fill=border, width=0, tags=tags)


def canvas_pill(canvas: tk.Canvas, x0: float, cy: float, length: float, height: float, fill: str, bg: str,
                right_bg: str | None = None, keep: list[Any] | None = None) -> None:
    """Horizontal pill (a bar with round ends) from ``x0``; its right cap blends into ``right_bg``."""
    th = _theme(canvas)
    h = max(int(round(height / 2)) * 2, 2)
    r = h // 2
    length = max(int(round(length)), h)
    x0, y0 = int(round(x0)), int(round(cy - r))
    caps = []
    for side, under in (("left", bg), ("right", right_bg or bg)):
        def rows(under: str = under, side: str = side) -> raster.Rows:
            disc = raster.rounded_rect(h, h, r, fill, under)
            return tuple(row[:r] if side == "left" else row[r:] for row in disc)
        caps.append(th.photo(("cap", side, h, fill, under), rows))
    if keep is not None:
        keep.extend(caps)
    canvas.create_image(x0, y0, image=caps[0], anchor="nw")
    canvas.create_image(x0 + length, y0, image=caps[1], anchor="ne")
    if length > h:
        canvas.create_rectangle(x0 + r, y0, x0 + length - r, y0 + h, fill=fill, width=0)


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
        kwargs.setdefault("style", _surface_style(parent, f"{role}.TLabel"))
        super().__init__(parent, text=text, justify="left", **kwargs)
        self.bind("<Configure>", self._rewrap)

    def set_role(self, role: str) -> None:
        """Switch to another label role (keeps the surface)."""
        self.configure(style=_surface_style(self.master, f"{role}.TLabel"))

    def _rewrap(self, event: tk.Event) -> None:
        wrap = max(event.width - 4, 80)
        if str(wrap) != str(self.cget("wraplength")):
            self.configure(wraplength=wrap)


def label(parent: tk.Misc, text: str = "", role: str = "Body", **kwargs: Any) -> ttk.Label:
    """A one-line label with the right style for its surface."""
    return ttk.Label(parent, text=text, style=_surface_style(parent, f"{role}.TLabel"), **kwargs)


class Button(ttk.Button):
    """A themed ttk.Button that keeps its surface when re-styled.

    ``configure(style="Primary.TButton")`` becomes the variant drawn for the surface the button
    sits on (its rounded corners are blended into that colour).
    """

    def __init__(self, parent: tk.Misc, prefix: str, **kwargs: Any) -> None:
        self._prefix = prefix
        self._icon: str | None = None
        kwargs["style"] = self._surface_style(parent, kwargs.get("style", _BUTTON_STYLES["secondary"]))
        super().__init__(parent, **kwargs)

    def _surface_style(self, widget: tk.Misc, style: str) -> str:
        for kind, base in _BUTTON_STYLES.items():
            if style in (base, self._prefix + base):
                _ensure_base(_theme(widget), self._prefix)
                return _button_style(_theme(widget), self._prefix, kind)
        return style

    def kind(self) -> str:
        """"primary" | "secondary" | "ghost" (from the current style)."""
        style = str(self.cget("style"))
        return next((k for k, base in _BUTTON_STYLES.items() if style.endswith(base)), "secondary")

    def configure(self, cnf: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        restyled = "style" in kwargs or (isinstance(cnf, dict) and "style" in cnf)
        if isinstance(cnf, dict) and "style" in cnf:
            cnf = dict(cnf, style=self._surface_style(self, cnf["style"]))
        if "style" in kwargs:
            kwargs["style"] = self._surface_style(self, kwargs["style"])
        result = super().configure(cnf, **kwargs)
        if restyled and self._icon is not None:
            self.set_icon(self._icon)       # the icon is drawn on the fill of the new kind
        return result

    config = configure

    def set_icon(self, name: str | None) -> None:
        """Show icon ``name`` (``raster.ICONS``) left of the text, drawn on each state's fill colour."""
        self._icon = name
        if name is None:
            super().configure(image="", compound="none")
            return
        kind = self.kind()
        fills = _button_fills(kind, _bg(self._prefix))
        c = COLORS
        color = c.on_accent if kind == "primary" else c.muted
        spec = (icon_image(self, name, 14, color, fills["normal"][0]),
                "disabled", icon_image(self, name, 14, c.subtle, fills["disabled"][0]),
                "pressed", icon_image(self, name, 14, color, fills["pressed"][0]),
                "active", icon_image(self, name, 14, color, fills["active"][0]))
        super().configure(image=spec, compound="left")


def button(parent: tk.Misc, text: str, command: Callable[[], Any], kind: str = "secondary",
           icon: str | None = None, **kwargs: Any) -> Button:
    """A button of ``kind`` "primary" | "secondary" | "ghost", optionally with an ``icon``."""
    btn = Button(parent, surface_prefix(parent), text=text, command=command, style=_BUTTON_STYLES[kind],
                 takefocus=True, **kwargs)
    if icon:
        btn.set_icon(icon)
    return btn


def check(parent: tk.Misc, text: str, variable: tk.Variable, **kwargs: Any) -> ttk.Checkbutton:
    """A checkbox styled for its surface."""
    style = _check_style(_theme(parent), surface_prefix(parent), radio=False)
    return ttk.Checkbutton(parent, text=text, variable=variable, style=style, **kwargs)


def radio(parent: tk.Misc, text: str, value: Any, variable: tk.Variable, **kwargs: Any) -> ttk.Radiobutton:
    """A radio button styled for its surface."""
    style = _check_style(_theme(parent), surface_prefix(parent), radio=True)
    return ttk.Radiobutton(parent, text=text, value=value, variable=variable, style=style, **kwargs)


def frame(parent: tk.Misc, **kwargs: Any) -> ttk.Frame:
    """A plain frame with the background of its surface."""
    return ttk.Frame(parent, style=_surface_style(parent, "TFrame"), **kwargs)


def segmented(parent: tk.Misc) -> ttk.Frame:
    """The rounded track of a segmented control: pack ``Segment.Toolbutton`` radio buttons into it."""
    style = _track_style(_theme(parent), surface_prefix(parent))
    return ttk.Frame(parent, style=style, padding=px(parent, 3))


def hairline(parent: tk.Misc, **pack: Any) -> tk.Frame:
    """A packed 1 px divider (``pack`` options; full width by default)."""
    line = tk.Frame(parent, height=1, background=COLORS.border)
    line.pack(**({"fill": "x"} | pack))
    return line


def bullets(parent: tk.Misc, lines: Iterable[str], role: str = "Body", marker: str = "•",
            marker_role: str = "Accent") -> ttk.Frame:
    """Bulleted list with a hanging indent."""
    box = frame(parent)
    box.columnconfigure(1, weight=1)
    for row, text in enumerate(lines):
        label(box, marker, marker_role).grid(row=row, column=0, sticky="nw", padx=(0, px(parent, 8)), pady=1)
        WrapLabel(box, text, role).grid(row=row, column=1, sticky="we", pady=1)
    return box


class Chip(ttk.Label):
    """Small rounded pill ("Measured", "+2 ms"...); ``kind`` is a ``NOTICE_KINDS`` name or "accent"/"neutral"."""

    def __init__(self, parent: tk.Misc, text: str, kind: str = "neutral") -> None:
        self._prefix = surface_prefix(parent)
        _ensure_base(_theme(parent), self._prefix)
        super().__init__(parent, text=text, style=_chip_style(_theme(parent), self._prefix, kind))

    @staticmethod
    def colors(kind: str) -> tuple[str, str]:
        """Foreground and background colours of a chip kind."""
        c = COLORS
        if kind == "accent":
            return c.accent, c.accent_soft
        if kind in NOTICE_KINDS:
            fg_attr, bg_attr, _icon = NOTICE_KINDS[kind]
            return getattr(c, fg_attr), getattr(c, bg_attr)
        return c.muted, c.surface_3

    def set(self, text: str, kind: str = "neutral") -> None:
        """Change text and colour."""
        self.configure(text=text, style=_chip_style(_theme(self), self._prefix, kind))


class RoundedFrame(ttk.Frame):
    """Container drawn as a frosted-glass panel: rounded, antialiased, hairline border, top highlight.

    It is an ordinary ttk.Frame (children are packed or gridded into it); the background is a
    nine-slice ttk image element, so resizing is handled by Tk with no Python redraw and no
    flicker. Children sit on the ``Card.`` surface. ``accent`` tints the border;
    :meth:`set_selected` / :meth:`set_hover` switch to the selected (accent, 2 px) or hover border.
    """

    surface = "Card."

    def __init__(self, parent: tk.Misc, padding: Any = 0, radius: float | None = None,
                 accent: str | None = None, selected: bool = False) -> None:
        self._bg = surface_color(parent)
        self._radius = RADIUS["card"] if radius is None else radius
        self._accent = accent
        self.selected, self.hovered = selected, False
        _ensure_base(_theme(parent), "Card.")
        super().__init__(parent, class_="GlassCard", style=self._style_for(parent), padding=padding)

    def _style_for(self, widget: tk.Misc) -> str:
        c = COLORS
        if self.selected:
            return rounded_style(widget, c.surface, self._bg, c.accent, self._radius, None, max(px(widget, 2), 2))
        if self.hovered:
            return rounded_style(widget, c.surface, self._bg, c.border_strong, self._radius, c.highlight)
        if self._accent:
            return rounded_style(widget, c.surface, self._bg, mix(c.border, self._accent, 0.5), self._radius,
                                 mix(c.highlight, self._accent, 0.45))
        return rounded_style(widget, c.surface, self._bg, c.border, self._radius, c.highlight)

    def set_selected(self, selected: bool) -> None:
        """Show (or clear) the selected border."""
        if selected != self.selected:
            self.selected = selected
            self.configure(style=self._style_for(self))

    def set_hover(self, hovered: bool) -> None:
        """Show (or clear) the hover border."""
        if hovered != self.hovered:
            self.hovered = hovered
            self.configure(style=self._style_for(self))


GlassCard = RoundedFrame


class Card(RoundedFrame):
    """Glass section with optional title/subtitle; ``accent`` tints its border.

    Put content into ``body``; ``header`` holds the title row (append extra
    widgets such as an :class:`InfoTip` or a :class:`Chip` to it).
    """

    def __init__(self, parent: tk.Misc, title: str | None = None, subtitle: str | None = None,
                 accent: str | None = None, padding: int = 18, title_role: str = "Heading") -> None:
        super().__init__(parent, padding=px(parent, padding), accent=accent)
        self.header = ttk.Frame(self, style="Card.TFrame")
        self.title_label: ttk.Label | None = None
        if title:
            self.header.pack(fill="x", pady=(0, px(self, 4) if subtitle else px(self, 12)))
            self.title_label = ttk.Label(self.header, text=title, style=f"Card.{title_role}.TLabel")
            self.title_label.pack(side="left")
        if subtitle:
            WrapLabel(self, subtitle, "Muted").pack(fill="x", pady=(0, px(self, 12)))
        self.body = ttk.Frame(self, style="Card.TFrame")
        self.body.pack(fill="both", expand=True)


class ReadOnlyText(ttk.Frame):
    """Selectable, scrollable, read-only text box (a rounded inset field)."""

    def __init__(self, parent: tk.Misc, height: int = 8) -> None:
        super().__init__(parent, style=_surface_style(parent, "TFrame"))
        c = COLORS
        box = ttk.Frame(self, style=rounded_style(self, c.field, surface_color(parent), c.border, RADIUS["field"]),
                        padding=px(self, 3))
        box.surface = "Field."           # type: ignore[attr-defined]
        box.pack(fill="both", expand=True)
        self.text = tk.Text(box, height=height, wrap="word", borderwidth=0, relief="flat",
                            state="disabled", padx=px(self, 10), pady=px(self, 7), background=c.field,
                            foreground=c.text, insertbackground=c.text, selectbackground=c.accent_soft,
                            selectforeground=c.text, font=font("mono"), highlightthickness=0)
        bar = ttk.Scrollbar(box, orient="vertical", command=self.text.yview, style="Field.Vertical.TScrollbar")
        self.text.configure(yscrollcommand=bar.set)
        self.text.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y", pady=px(self, 4))

    def set(self, content: str) -> None:
        """Replace the whole content."""
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)
        self.text.configure(state="disabled")


class Expander(ttk.Frame):
    """A collapsible "details" section (technical text by default, or any ``content``)."""

    def __init__(self, parent: tk.Misc, title: str | None = None, height: int = 7,
                 opened: bool = False, widgets: bool = False,
                 on_toggle: Callable[[bool], None] | None = None) -> None:
        """``widgets=True``: the section is an empty frame (``content``) to pack widgets into, not a text box.
        ``on_toggle`` is told the new open state (e.g. to remember it across screen refreshes)."""
        super().__init__(parent, style=_surface_style(parent, "TFrame"))
        self._title = title or t("gui.details.title")
        self._open = opened
        self._on_toggle = on_toggle
        self._button = button(self, self._title, self.toggle, kind="ghost", icon="chevron_right")
        self._button.pack(anchor="w")
        self._box: ttk.Frame = frame(self) if widgets else ReadOnlyText(self, height=height)
        self.content: ttk.Frame = self._box
        self._refresh()

    def set(self, content: str) -> None:
        """Replace the technical text."""
        if isinstance(self._box, ReadOnlyText):
            self._box.set(content)

    @property
    def is_open(self) -> bool:
        """Whether the section is expanded."""
        return self._open

    def toggle(self) -> None:
        """Open or close the section."""
        self._open = not self._open
        self._refresh()
        if self._on_toggle is not None:
            self._on_toggle(self._open)

    def _refresh(self) -> None:
        self._button.set_icon("chevron_down" if self._open else "chevron_right")
        self._button.configure(text=self._title)
        if self._open:
            self._box.pack(fill="both", expand=True, pady=(px(self, 4), 0))
        else:
            self._box.pack_forget()


class Notice(ttk.Frame):
    """Inline rounded banner for status and errors, with expandable technical details.

    The frame itself stays packed (zero height while hidden) so its position
    in the page never changes; :meth:`show` / :meth:`hide` toggle the banner.
    """

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, style=_surface_style(parent, "TFrame"))
        self._card: ttk.Frame | None = None

    def show(self, message: str, kind: str = "info", detail: str = "") -> None:
        """Display ``message`` as ``kind`` ("info" | "success" | "warn" | "error")."""
        self.hide()
        fg_attr, bg_attr, icon_name = NOTICE_KINDS[kind]
        fg, bg = getattr(COLORS, fg_attr), getattr(COLORS, bg_attr)
        style = rounded_style(self, bg, surface_color(self), mix(bg, fg, 0.32), RADIUS["notice"])
        card = ttk.Frame(self, style=style, padding=(px(self, 14), px(self, 11)))
        tk.Label(card, image=icon_image(self, icon_name, 18, fg, bg), background=bg,
                 borderwidth=0).pack(side="left", anchor="n", pady=(px(self, 1), 0))
        inner = tk.Frame(card, background=bg)
        inner.surface = f"Notice.{kind}."     # type: ignore[attr-defined]
        inner.pack(side="left", fill="both", expand=True, padx=(px(self, 10), 0))
        text = tk.Label(inner, text=message, fg=COLORS.text, bg=bg, font=font("body"),
                        justify="left", anchor="w")
        text.pack(fill="x")
        text.bind("<Configure>", lambda e: text.configure(wraplength=max(e.width - 4, 80)))
        if detail:
            details = Expander(inner, height=5)
            details.pack(fill="x", pady=(px(self, 4), 0))
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


_TRANSPARENT_KEY = "#010203"       # colour key made transparent on Windows (rounded tooltip corners)


def _tooltip_style(widget: tk.Misc, outside: str) -> str:
    th = _theme(widget)
    key = ("tooltip", outside)
    name = th.names.get(key)
    if name is not None:
        return name
    number = len(th.names)
    name = th.names[key] = f"Tip{number}.TLabel"
    c = COLORS
    fill = c.surface_3
    image, edge = th.rounded(th.px(RADIUS["tooltip"]), fill, c.bg, border=c.border_strong,
                             highlight=mix(c.border_strong, c.text, 0.2), outside=outside)
    elem = th.element(f"glasstip{number}.Label.border", [("", image)], border=edge)
    th.style.layout(name, [(elem, {"sticky": "nsew", "children": [
        ("Label.padding", {"sticky": "nsew", "children": [("Label.label", {"sticky": "nsew"})]})]})])
    th.style.configure(name, background=fill, foreground=c.text, font=font("small"),
                       padding=(th.px(11), th.px(7)))
    return name


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
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        outside = COLORS.bg
        if sys.platform == "win32":
            try:   # the colour key makes the corners outside the rounded bubble transparent
                tip.wm_attributes("-transparentcolor", _TRANSPARENT_KEY)
                outside = _TRANSPARENT_KEY
            except tk.TclError:
                pass
        tip.configure(background=outside)
        try:
            tip.wm_attributes("-topmost", True)
        except tk.TclError:
            pass
        ttk.Label(tip, text=self.text(), justify="left", wraplength=px(self.widget, 320),
                  style=_tooltip_style(self.widget, outside)).pack()
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
        self._images: list[Any] = []
        self.tooltip = Tooltip(self, text)
        self.bind("<FocusIn>", lambda _e: self._draw(True), add="+")
        self.bind("<FocusOut>", lambda _e: self._draw(False), add="+")
        self.bind("<Enter>", lambda _e: self._draw(True), add="+")
        self.bind("<Leave>", lambda _e: self._draw(self.focus_get() is self), add="+")
        self._draw(False)

    def _draw(self, active: bool) -> None:
        c, s = COLORS, self._size
        bg = str(self.cget("background"))
        fill, ring = (c.accent_soft, c.accent) if active else (bg, c.subtle)
        image = _theme(self).rr(s, s, s / 2, fill, bg, border=ring, border_width=1.2)
        self._images = [image]
        self.delete("all")
        self.create_image(0, 0, image=image, anchor="nw")
        self.create_text(s / 2, s / 2, text="?", fill=c.accent if active else c.muted, font=font("small_bold"))


class ScrollFrame(ttk.Frame):
    """Vertically scrollable container; children go into ``body``, centered up to ``max_width``."""

    def __init__(self, parent: tk.Misc, max_width: int | None = None) -> None:
        super().__init__(parent)
        self._max_width = max_width
        self._canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, background=COLORS.bg)
        self._bar = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._bar.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        self._bar.pack(side="right", fill="y", padx=(0, px(self, 2)), pady=px(self, 4))
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
        super().__init__(parent, style=_surface_style(parent, "TFrame"))
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
    """Horizontal progress indicator: numbered discs joined by a line, names below."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, height=px(parent, 60), highlightthickness=0, borderwidth=0,
                         background=surface_color(parent))
        self._names: list[str] = []
        self._current = 0
        self._images: list[Any] = []
        self._size = (0, 0)
        self.bind("<Configure>", self._on_configure)

    def _on_configure(self, event: tk.Event) -> None:
        if (event.width, event.height) != self._size:   # redraw only when the size really changed
            self._size = (event.width, event.height)
            self._draw()

    def show(self, names: Sequence[str], current: int) -> None:
        """Show ``names`` with index ``current`` highlighted."""
        self._names, self._current = list(names), current
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        if not self._names:
            return
        c, th = COLORS, _theme(self)
        bg = str(self.cget("background"))
        width = max(self.winfo_width(), 1)
        size = px(self, 22) // 2 * 2
        radius = size // 2
        cy = radius + px(self, 4)
        centers = stepper_centers(width, len(self._names))
        slot = width / len(self._names)
        done_fill = mix(bg, c.accent, 0.2)
        images = {
            "done": th.photo(("step-done", size, bg), lambda: raster.overlay(
                raster.rounded_rect(size, size, radius, done_fill, bg),
                raster.icon("check", size, c.accent, done_fill), done_fill)),
            "current": th.rr(size, size, radius, c.accent, bg),
            "todo": th.rr(size, size, radius, c.field, bg, border=c.border_strong)}
        self._images = list(images.values())
        gap = px(self, 6)
        for index in range(len(centers) - 1):
            done = index < self._current
            self.create_rectangle(round(centers[index] + radius + gap), cy, round(centers[index + 1] - radius - gap),
                                  cy + 1, fill=mix(bg, c.accent, 0.55) if done else c.border_strong, width=0)
        for index, (x, name) in enumerate(zip(centers, self._names)):
            state = "done" if index < self._current else "current" if index == self._current else "todo"
            self.create_image(round(x), cy, image=images[state])
            if state != "done":
                self.create_text(x, cy, text=str(index + 1), font=font("small_bold"),
                                 fill=c.on_accent if state == "current" else c.muted)
            self.create_text(x, cy + radius + px(self, 8), text=name, anchor="n", justify="center",
                             fill=c.text if state == "current" else c.muted,
                             font=font("small_bold" if state == "current" else "small"), width=max(slot - 6, 40))


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
        self._images: list[Any] = []
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
        bg = str(self.cget("background"))
        keep: list[Any] = []
        width = max(self.winfo_width(), 1)
        tag_w = px(self, 64)          # "Antes" / "Después"
        value_w = px(self, 86)        # value text after the bar
        verdict_w = px(self, 120)
        bar_x = tag_w
        bar_max = max(width - tag_w - value_w - verdict_w, px(self, 40))
        group, bar_h = px(self, 74), px(self, 10)
        track = c.surface_2
        before_label, after_label, missing_label = self._labels
        for index, row in enumerate(self._rows):
            top = index * group
            self.create_text(0, top + px(self, 2), text=row.label, anchor="nw", fill=c.text,
                             font=font("strong"))
            lengths = bar_lengths((row.before, row.after), bar_max)
            for line, (name, value, length, color) in enumerate((
                    (before_label, row.before, lengths[0], c.before),
                    (after_label, row.after, lengths[1], c.accent))):
                y = top + px(self, 30) + line * px(self, 20)
                self.create_text(0, y, text=name, anchor="w", fill=c.muted, font=font("small"))
                if value is None:
                    self.create_text(bar_x, y, text=missing_label, anchor="w", fill=c.muted,
                                     font=font("small"))
                    continue
                canvas_pill(self, bar_x, y, bar_max, bar_h, track, bg, keep=keep)
                if length:
                    canvas_pill(self, bar_x, y, length, bar_h, color, bg, right_bg=track, keep=keep)
                self.create_text(bar_x + bar_max + px(self, 10), y, anchor="w", fill=c.text,
                                 text=format_measure(value, row.digits, row.unit), font=font("small_bold"))
            verdict = compare_values(row.before, row.after, row.lower_is_better)
            if verdict is not None:
                change = relative_change(row.before, row.after)
                text = t(f"gui.compare.verdict.{verdict}",
                         pct=f"{abs(change) * 100:.0f}" if change is not None else "-")
                color = {"better": c.good, "worse": c.warn, "same": c.muted}[verdict]
                self.create_text(width, top + px(self, 40), text=text, anchor="e", fill=color,
                                 font=font("small_bold"))
        self._images = keep


class AreaPreview(tk.Canvas):
    """Tablet outline with the active area drawn to scale, plus the display it maps onto."""

    def __init__(self, parent: tk.Misc, height: int = 250) -> None:
        super().__init__(parent, highlightthickness=0, borderwidth=0, background=surface_color(parent),
                         height=px(parent, height))
        self._data: tuple[Any, ...] | None = None
        self._images: list[Any] = []
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
        bg = str(self.cget("background"))
        keep: list[Any] = []
        width, height = max(self.winfo_width(), 1), max(self.winfo_height(), 1)
        show_display = display is not None and width > px(self, 300)
        tablet_box_w = min(width * (0.68 if show_display else 1.0), px(self, 520))
        geo = area_geometry(tablet_w, tablet_h, area, current, tablet_box_w, height - px(self, 22),
                            pad=px(self, 26))
        x0, y0, x1, y1 = geo.tablet
        bezel = px(self, 10)
        canvas_round_rect(self, x0 - bezel, y0 - bezel, x1 + bezel, y1 + bezel, px(self, RADIUS["bezel"]),
                          c.surface_2, bg, c.border_strong, keep=keep)
        self.create_rectangle(x0, y0, x1, y1, fill=c.field, outline=c.border, width=1)
        if geo.current is not None:
            self.create_polygon(*_flat(geo.current), fill="", outline=c.muted, dash=(4, 3), width=1)
        self.create_polygon(*_flat(geo.area), fill=c.accent_soft, outline=c.accent, width=2)
        ax0, ay0, ax1, ay1 = geo.area_bbox
        cx, cy = (ax0 + ax1) / 2, (ay0 + ay1) / 2
        top, left = ay0, ax0
        if geo.current is not None:   # keep the size labels clear of the dashed outline
            top = min(top, min(p[1] for p in geo.current))
            left = min(left, min(p[0] for p in geo.current))
        dot = icon_image(self, "dot", 8, c.accent, c.accent_soft)
        keep.append(dot)
        self.create_image(round(cx), round(cy), image=dot)
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
            self.create_line(x1 + bezel + px(self, 8), mid, dx0 - px(self, 10), mid, fill=c.accent,
                             width=2, arrow="last", arrowshape=(8, 10, 4))
            canvas_round_rect(self, dx0 - 4, dy0 - 4, dx1 + 4, dy1 + 4, px(self, 6), c.surface_2, bg,
                              c.border_strong, keep=keep)
            self.create_rectangle(dx0, dy0, dx1, dy1, fill=c.accent_soft, outline=c.accent, width=1)
            self.create_rectangle((dx0 + dx1) / 2 - px(self, 10), dy1 + 4, (dx0 + dx1) / 2 + px(self, 10),
                                  dy1 + 4 + px(self, 5), fill=c.border_strong, outline="")
            self.create_text((dx0 + dx1) / 2, dy1 + px(self, 14), anchor="n", fill=c.muted,
                             font=font("small"), justify="center",
                             text=f"{labels['display']}\n{display[0]} x {display[1]} px")
        self._images = keep


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
