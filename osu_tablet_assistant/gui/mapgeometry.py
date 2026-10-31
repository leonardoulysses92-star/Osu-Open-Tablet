"""Geometry and colours of the TabletMap (no Tk, so it can be tested anywhere).

Everything is in the PLAYER'S VIEW: the top of the picture is the top of the screen, whatever the
area rotation. ``(u, v)`` are normalized area coordinates (0 = left/top edge of the screen).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..engine import areafit
from ..engine.beatmap import playfield_to_desktop
from ..engine.playtypes import HIT, NO_CLICK, TOO_FAR, ObjectResult
from ..models import DisplaySpec, Rect, TabletSpec
from .palette import COLORS

HEAT_MAX_MISS = 0.5            # a cell with this miss rate gets the strongest tint


@dataclass(frozen=True)
class MapGeometry:
    """Normalized area coordinates ``(u, v)`` -> canvas pixels, for one tablet/area/size.

    The picture is built in the player's view: millimetres from the area centre, scaled to fit.
    """
    area: Rect
    tablet: TabletSpec | None
    width: float
    height: float
    pad: float = 12.0

    @property
    def tablet_polygon_mm(self) -> tuple[tuple[float, float], ...]:
        """Tablet corners in view millimetres relative to the area centre (the tablet if known, else the area)."""
        if self.tablet is None:
            vw, vh = areafit.view_size(self.area)
            return ((-vw / 2, -vh / 2), (vw / 2, -vh / 2), (vw / 2, vh / 2), (-vw / 2, vh / 2))
        corners = ((0.0, 0.0), (self.tablet.width_mm, 0.0), (self.tablet.width_mm, self.tablet.height_mm),
                   (0.0, self.tablet.height_mm))
        return tuple(areafit.tablet_to_view(x - self.area.x, y - self.area.y, self.area.rotation)
                     for x, y in corners)

    @property
    def _bounds(self) -> tuple[float, float, float, float]:
        xs = [p[0] for p in self.tablet_polygon_mm]
        ys = [p[1] for p in self.tablet_polygon_mm]
        return min(xs), min(ys), max(xs), max(ys)

    @property
    def scale(self) -> float:
        x0, y0, x1, y1 = self._bounds
        return min((self.width - 2 * self.pad) / max(x1 - x0, 1e-6), (self.height - 2 * self.pad) / max(y1 - y0, 1e-6))

    def mm_to_canvas(self, x_mm: float, y_mm: float) -> tuple[float, float]:
        """View millimetres from the area centre -> canvas pixels."""
        x0, y0, x1, y1 = self._bounds
        k = self.scale
        return (self.width / 2 + (x_mm - (x0 + x1) / 2) * k, self.height / 2 + (y_mm - (y0 + y1) / 2) * k)

    def to_canvas(self, u: float, v: float) -> tuple[float, float]:
        """Normalized area coordinates (player's view) -> canvas pixels."""
        vw, vh = areafit.view_size(self.area)
        return self.mm_to_canvas((u - 0.5) * vw, (v - 0.5) * vh)

    def tablet_polygon(self) -> list[tuple[float, float]]:
        return [self.mm_to_canvas(*p) for p in self.tablet_polygon_mm]

    def area_rect(self) -> tuple[float, float, float, float]:
        (x0, y0), (x1, y1) = self.to_canvas(0, 0), self.to_canvas(1, 1)
        return x0, y0, x1, y1


def playfield_uv(display: DisplaySpec) -> tuple[float, float, float, float]:
    """The playfield's extent in normalized area coordinates: ``(u_left, v_top, u_right, v_bottom)``."""
    left = display.offset_x - display.width_px / 2
    top = display.offset_y - display.height_px / 2
    x0, y0 = playfield_to_desktop(0, 0, display)
    x1, y1 = playfield_to_desktop(512, 384, display)
    return ((x0 - left) / display.width_px, (y0 - top) / display.height_px,
            (x1 - left) / display.width_px, (y1 - top) / display.height_px)


def cell_bounds(row: int, col: int, rows: int, cols: int, display: DisplaySpec) -> tuple[float, float, float, float]:
    """A grid cell of the playfield as ``(u0, v0, u1, v1)`` in normalized area coordinates."""
    ul, vt, ur, vb = playfield_uv(display)
    du, dv = (ur - ul) / cols, (vb - vt) / rows
    return ul + col * du, vt + row * dv, ul + (col + 1) * du, vt + (row + 1) * dv


def heat_color(miss_rate: float) -> str:
    """Tint from the surface colour (no problems) towards red (many misses), as ``#rrggbb``."""
    k = min(max(miss_rate / HEAT_MAX_MISS, 0.0), 1.0)
    base, hot = _rgb(COLORS.surface), _rgb(COLORS.error)
    return "#" + "".join(f"{round(a + (b - a) * k * 0.7):02x}" for a, b in zip(base, hot))


def dot_color(result: ObjectResult, radius_mm: float | None) -> str:
    """Green hit, amber close (late/early or within 1.5 radii), red miss."""
    if result.verdict == HIT:
        return COLORS.good
    if result.verdict in (NO_CLICK, TOO_FAR):
        close = result.err_mm is not None and radius_mm and result.err_mm <= 1.5 * radius_mm
        return COLORS.warn if close else COLORS.error
    return COLORS.warn


def _rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))   # type: ignore[return-value]


def mix(a: str, b: str, k: float) -> str:
    """``#rrggbb`` mix: ``k = 0`` is ``a``, ``k = 1`` is ``b``."""
    return "#" + "".join(f"{round(x + (y - x) * k):02x}" for x, y in zip(_rgb(a), _rgb(b)))
