"""The TabletMap: the whole tablet, the active area, the pen and how each hit went.

Orientation is the PLAYER'S VIEW: the top of the picture is the top of the screen, whatever the area
rotation (a left-handed 180 degree setup shows the tablet upside down relative to its own markings;
a caption says so). The geometry lives in :class:`MapGeometry` (no Tk) so it can be tested; the canvas
only draws it. Everything scales with the screen DPI.

Layers (bottom to top): tablet, playfield region, heat tint per cell with error arrows (pooled estimates;
cells with too few objects stay uncoloured, any grid from 3x4 to 9x12) and a one-line legend, active area
outline, result dots (green hit / amber close / red miss, where the pen WAS), pen trail and pen. A pen
update only redraws the trail layer.
"""
from __future__ import annotations

import tkinter as tk
from typing import Sequence

from ..engine.playtypes import CellStat, ObjectResult
from ..models import DisplaySpec, PenSample, Rect, TabletSpec
from .i18n import t
from .mapgeometry import MapGeometry, cell_bounds, dot_color, heat_color, mix, playfield_uv
from .widgets import COLORS, canvas_round_rect, font, icon_image, px, surface_color

MAX_DOTS = 300
TRAIL_POINTS = 40
ARROW_SCALE_MM = 4.0           # an error of this many mm draws a full-length arrow


class TabletMap(tk.Canvas):
    """Live and report picture of the tablet. Feed it with the ``set_*`` / ``update_*`` methods."""

    def __init__(self, parent: tk.Misc, height: int = 230, compact: bool = False) -> None:
        super().__init__(parent, highlightthickness=0, borderwidth=0, background=surface_color(parent),
                         height=px(parent, height))
        self.compact = compact
        self._tablet: TabletSpec | None = None
        self._area: Rect | None = None
        self._display: DisplaySpec | None = None
        self._cells: tuple[CellStat, ...] = ()
        self._grid = (3, 4)
        self._dots: list[tuple[float, float, str]] = []     # (u, v, colour): where the pen was
        self._trail: list[tuple[float, float]] = []
        self._pen: tuple[float, float] | None = None
        self._images: list = []        # antialiased bitmaps shown on the canvas (kept alive here)
        self._pen_images: list = []    # the pen marker, redrawn on its own while playing
        self._geo: MapGeometry | None = None
        self.bind("<Configure>", lambda _e: self.redraw())

    # --- data
    def set_setup(self, tablet: TabletSpec | None, area: Rect, display: DisplaySpec) -> None:
        self._tablet, self._area, self._display = tablet, area, display
        self.redraw()

    def set_cells(self, cells: Sequence[CellStat], rows: int | None = None, cols: int | None = None) -> None:
        """The analysis grid (3x4 up to 9x12); its size is read from the cells when not given."""
        cells = tuple(cells)
        rows = rows or (max((c.row for c in cells), default=2) + 1)
        cols = cols or (max((c.col for c in cells), default=3) + 1)
        self._cells, self._grid = cells, (rows, cols)
        self.redraw()

    def clear_play(self) -> None:
        self._dots, self._trail, self._pen, self._cells = [], [], None, ()
        self.redraw()

    def cursor_to_uv(self, x: float, y: float) -> tuple[float, float] | None:
        d = self._display
        if d is None or d.width_px <= 0 or d.height_px <= 0:
            return None
        return (x - (d.offset_x - d.width_px / 2)) / d.width_px, (y - (d.offset_y - d.height_px / 2)) / d.height_px

    def update_pen(self, samples: Sequence[PenSample]) -> None:
        """Replace the trail with the latest samples (oldest first)."""
        pts = [uv for uv in (self.cursor_to_uv(s.x, s.y) for s in samples[-TRAIL_POINTS:]) if uv is not None]
        self._trail, self._pen = pts, (pts[-1] if pts else None)
        if self._geo is None:
            self.redraw()
        else:                     # only the trail layer changes (cheap at any grid size)
            self.delete("trail")
            self._pen_images = []
            self._draw_trail(self._geo, self._pen_images)

    def add_results(self, results: Sequence[ObjectResult], radius_mm: float | None) -> None:
        """Dots where the pen was at each judged click (replaces the previous dots)."""
        self._dots = [(r.cursor_u, r.cursor_v, dot_color(r, radius_mm)) for r in results[-MAX_DOTS:]
                      if r.cursor_u is not None and r.cursor_v is not None]
        self.redraw()

    # --- drawing
    def redraw(self) -> None:
        self.delete("all")
        self._geo = None
        if self._area is None or self._display is None:
            return
        c = COLORS
        width, height = max(self.winfo_width(), 2), max(self.winfo_height(), 2)
        caption_h = 0 if self.compact else px(self, 16)
        geo = MapGeometry(self._area, self._tablet, width, height - caption_h, px(self, 10))
        self._geo = geo
        bg = str(self.cget("background"))
        keep: list = []
        polygon = geo.tablet_polygon()
        xs, ys = sorted({round(p[0]) for p in polygon}), sorted({round(p[1]) for p in polygon})
        if len(xs) == 2 and len(ys) == 2:          # axis-aligned tablet: a rounded body, like the real thing
            canvas_round_rect(self, xs[0], ys[0], xs[1], ys[1], px(self, 8), c.field, bg, c.border_strong, keep=keep)
        else:
            self.create_polygon(*[v for p in polygon for v in p], fill=c.field, outline=c.border_strong)
        rows, cols = self._grid
        for cell in self._cells:
            u0, v0, u1, v1 = cell_bounds(cell.row, cell.col, rows, cols, self._display)
            (x0, y0), (x1, y1) = geo.to_canvas(u0, v0), geo.to_canvas(u1, v1)
            # pooled estimates; a cell with too few objects stays uncoloured and has no arrow
            self.create_rectangle(x0, y0, x1, y1, fill=heat_color(cell.est_miss) if cell.enough else "",
                                  outline=c.border)
            if cell.enough and (abs(cell.est_x_mm) + abs(cell.est_y_mm)) > 0.3:
                self._arrow((x0 + x1) / 2, (y0 + y1) / 2, cell.est_x_mm, cell.est_y_mm, geo,
                            min(px(self, 22), 0.45 * min(abs(x1 - x0), abs(y1 - y0))))
        if self._cells and not self.compact:
            self.create_text(px(self, 4), px(self, 2), anchor="nw", fill=c.subtle, font=font("small"),
                             text=t("play.map.legend"))
        if not self._cells:
            ul, vt, ur, vb = playfield_uv(self._display)
            (x0, y0), (x1, y1) = geo.to_canvas(ul, vt), geo.to_canvas(ur, vb)
            self.create_rectangle(x0, y0, x1, y1, outline=c.border_strong, dash=(3, 3))
        ax0, ay0, ax1, ay1 = geo.area_rect()
        self.create_rectangle(ax0, ay0, ax1, ay1, outline=c.accent, width=2)
        for u, v, color in self._dots:
            x, y = geo.to_canvas(u, v)
            r = px(self, 2.5)
            self.create_oval(x - r, y - r, x + r, y + r, fill=color, outline="")
        self._draw_trail(geo, keep)
        self._images = keep
        if not self.compact:
            self.create_text(width / 2, height - px(self, 2), anchor="s", fill=c.muted, font=font("small"),
                             text=t("play.map.orientation"))

    def _draw_trail(self, geo: MapGeometry, keep: list) -> None:
        n = len(self._trail)
        for i in range(1, n):
            (x0, y0), (x1, y1) = geo.to_canvas(*self._trail[i - 1]), geo.to_canvas(*self._trail[i])
            k = i / n
            self.create_line(x0, y0, x1, y1, fill=mix(COLORS.surface_3, COLORS.accent, k), width=max(1, round(px(self, 2) * k)),
                             capstyle="round", tags="trail")
        if self._pen is not None:
            x, y = geo.to_canvas(*self._pen)
            pen = icon_image(self, "dot", 14, COLORS.accent, COLORS.field)   # antialiased pen marker
            keep.append(pen)
            self.create_image(round(x), round(y), image=pen, tags="trail")

    def _arrow(self, cx: float, cy: float, ex_mm: float, ey_mm: float, geo: MapGeometry,
               max_px: float | None = None) -> None:
        """Error arrow of a cell: it points from where the object was towards where the cursor went."""
        max_px = px(self, 22) if max_px is None else max_px
        length = min(max(abs(ex_mm) + abs(ey_mm), 0.0) / ARROW_SCALE_MM, 1.0) * max_px
        norm = max((ex_mm ** 2 + ey_mm ** 2) ** 0.5, 1e-6)
        self.create_line(cx, cy, cx + ex_mm / norm * length, cy + ey_mm / norm * length, fill=COLORS.text,
                         arrow="last", width=max(1, px(self, 1.5)))


