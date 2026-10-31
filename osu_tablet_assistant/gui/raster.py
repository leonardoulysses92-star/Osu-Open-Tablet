"""Antialiased bitmaps for the glass look, computed in pure Python (no Tk).

Tk on Windows draws canvas shapes and frame borders without antialiasing, so rounded corners drawn
with ``create_arc`` or ovals look jagged. Instead, the shapes here are described by signed distance
functions and every pixel gets the coverage ``clamp(0.5 - distance, 0, 1)`` (a box-filter
approximation that is accurate for curves a few pixels wide and larger). The coverage blends the
shape's colours against a KNOWN opaque background, because Tk widgets are opaque rectangles: there is
no real transparency to blend against.

Results are rows of ``#rrggbb`` strings (``Rows``), memoised, so the widgets module only turns them
into ``tk.PhotoImage`` objects. Nothing here imports tkinter, so it is unit-tested headlessly.
"""
from __future__ import annotations

import math
from functools import lru_cache
from typing import Callable, Sequence

from .palette import hex_rgb

Rows = tuple[tuple[str, ...], ...]
Sdf = Callable[[float, float], float]

_CACHE = 1024


# ---------------------------------------------------------------- distances
def box_sdf(x0: float, y0: float, x1: float, y1: float, radius: float) -> Sdf:
    """Signed distance (px, negative inside) to the rounded box ``x0..x1`` x ``y0..y1``."""
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    hw, hh = (x1 - x0) / 2, (y1 - y0) / 2
    r = max(min(radius, hw, hh), 0.0)

    def sdf(x: float, y: float) -> float:
        qx, qy = abs(x - cx) - (hw - r), abs(y - cy) - (hh - r)
        outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
        return outside + min(max(qx, qy), 0.0) - r
    return sdf


def disc_sdf(cx: float, cy: float, r: float) -> Sdf:
    """Signed distance to a disc."""
    return lambda x, y: math.hypot(x - cx, y - cy) - r


def segment_sdf(ax: float, ay: float, bx: float, by: float, half_width: float) -> Sdf:
    """Signed distance to a capsule (a stroke with round caps) from a to b."""
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy or 1e-9

    def sdf(x: float, y: float) -> float:
        h = min(max(((x - ax) * dx + (y - ay) * dy) / length2, 0.0), 1.0)
        return math.hypot(x - ax - dx * h, y - ay - dy * h) - half_width
    return sdf


def union(*shapes: Sdf) -> Sdf:
    """The union of several shapes."""
    return lambda x, y: min(shape(x, y) for shape in shapes)


def coverage(distance: float) -> float:
    """Pixel coverage from the signed distance at the pixel centre."""
    return min(max(0.5 - distance, 0.0), 1.0)


# ------------------------------------------------------------------- colours
def _blend(a: tuple[float, float, float], b: tuple[int, int, int], k: float) -> tuple[float, float, float]:
    if k <= 0.0:
        return a
    if k >= 1.0:
        return (float(b[0]), float(b[1]), float(b[2]))
    return (a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k)


def _hex(c: tuple[float, float, float]) -> str:
    return "#%02x%02x%02x" % (round(c[0]), round(c[1]), round(c[2]))


# -------------------------------------------------------------------- shapes
@lru_cache(maxsize=_CACHE)
def rounded_rect(width: int, height: int, radius: float, fill: str, bg: str, border: str | None = None,
                 border_width: float = 1.0, highlight: str | None = None, ring: str | None = None,
                 ring_width: float = 0.0, outside: str | None = None) -> Rows:
    """A ``width`` x ``height`` rounded rectangle on ``bg``.

    Layers from the outside in: an optional focus ``ring`` band of ``ring_width`` px (the band is
    always reserved, so a ring appearing never moves anything), the ``border`` hairline (its top edge
    shades into ``highlight``), then ``fill``. ``outside`` replaces pixels the shape does not touch
    at all (a transparent-colour key for rounded toplevels).
    """
    bg_rgb = hex_rgb(bg)
    fill_rgb = hex_rgb(fill)
    outer = box_sdf(0, 0, width, height, radius)
    body_r = max(radius - ring_width, 0.0)
    body = box_sdf(ring_width, ring_width, width - ring_width, height - ring_width, body_r)
    inner = (box_sdf(ring_width + border_width, ring_width + border_width, width - ring_width - border_width,
                     height - ring_width - border_width, max(body_r - border_width, 0.0))
             if border is not None else body)
    ring_rgb = hex_rgb(ring) if ring else None
    border_rgb = hex_rgb(border) if border else fill_rgb
    light_rgb = hex_rgb(highlight) if highlight else None
    shade_span = max(body_r, border_width + 1.0)
    rows = []
    for y in range(height):
        cy = y + 0.5
        top_k = 0.0
        if light_rgb is not None:   # 1 at the top border row, fading out along the corner arcs
            top_k = min(max(1.0 - (cy - ring_width - border_width) / shade_span, 0.0), 1.0)
            edge_rgb = tuple(round(b + (l - b) * top_k) for b, l in zip(border_rgb, light_rgb))
        else:
            edge_rgb = border_rgb
        row = []
        for x in range(width):
            cx = x + 0.5
            a_out = coverage(outer(cx, cy))
            if a_out <= 0.0 and outside is not None:
                row.append(outside)
                continue
            color: tuple[float, float, float] = (float(bg_rgb[0]), float(bg_rgb[1]), float(bg_rgb[2]))
            if ring_rgb is not None:
                color = _blend(color, ring_rgb, a_out)
            a_body = coverage(body(cx, cy)) if ring_width else a_out
            color = _blend(color, edge_rgb, a_body)   # type: ignore[arg-type]
            if border is not None:
                color = _blend(color, fill_rgb, coverage(inner(cx, cy)))
            row.append(_hex(color))
        rows.append(tuple(row))
    return tuple(rows)


def quadrants(rows: Rows) -> tuple[Rows, Rows, Rows, Rows]:
    """Split an even-sized square into its (top-left, top-right, bottom-left, bottom-right) quarters."""
    h, w = len(rows) // 2, len(rows[0]) // 2
    return (tuple(r[:w] for r in rows[:h]), tuple(r[w:] for r in rows[:h]),
            tuple(r[:w] for r in rows[h:]), tuple(r[w:] for r in rows[h:]))


# --------------------------------------------------------------------- icons
ICONS = ("check", "cross", "chevron_right", "chevron_down", "dot", "pip", "info", "success", "warn", "error",
         "logo")


def _icon_layers(name: str, s: float) -> list[tuple[Sdf, str]]:
    """Shapes of icon ``name`` at size ``s`` px, each tagged "fg" (drawn) or "knock" (cut out)."""
    w = max(s * 0.11, 1.0)                    # stroke half width
    if name == "check":
        return [(union(segment_sdf(s * .22, s * .53, s * .42, s * .72, w),
                       segment_sdf(s * .42, s * .72, s * .78, s * .31, w)), "fg")]
    if name == "cross":
        return [(union(segment_sdf(s * .28, s * .28, s * .72, s * .72, w),
                       segment_sdf(s * .72, s * .28, s * .28, s * .72, w)), "fg")]
    if name == "chevron_right":
        return [(union(segment_sdf(s * .40, s * .25, s * .63, s * .50, w * .85),
                       segment_sdf(s * .63, s * .50, s * .40, s * .75, w * .85)), "fg")]
    if name == "chevron_down":
        return [(union(segment_sdf(s * .25, s * .40, s * .50, s * .63, w * .85),
                       segment_sdf(s * .50, s * .63, s * .75, s * .40, w * .85)), "fg")]
    if name == "dot":
        return [(disc_sdf(s / 2, s / 2, s * .30), "fg")]
    if name == "pip":                         # the centre of a selected radio button
        return [(disc_sdf(s / 2, s / 2, s * .19), "fg")]
    disc = (disc_sdf(s / 2, s / 2, s * .48), "fg")
    k = w * 0.95
    if name == "info":
        return [disc, (disc_sdf(s * .5, s * .30, k * 1.15), "knock"),
                (segment_sdf(s * .5, s * .46, s * .5, s * .72, k), "knock")]
    if name == "success":
        return [disc, (union(segment_sdf(s * .29, s * .52, s * .44, s * .67, k),
                             segment_sdf(s * .44, s * .67, s * .72, s * .37, k)), "knock")]
    if name == "warn":
        return [disc, (segment_sdf(s * .5, s * .27, s * .5, s * .55, k), "knock"),
                (disc_sdf(s * .5, s * .71, k * 1.15), "knock")]
    if name == "error":
        return [disc, (union(segment_sdf(s * .34, s * .34, s * .66, s * .66, k),
                             segment_sdf(s * .66, s * .34, s * .34, s * .66, k)), "knock")]
    if name == "logo":                       # an osu!-style circle: a disc with a ring cut out
        ring = s * 0.25
        return [disc, (lambda x, y: abs(math.hypot(x - s / 2, y - s / 2) - ring) - s * 0.055, "knock")]
    raise KeyError(name)


@lru_cache(maxsize=_CACHE)
def icon(name: str, size: int, color: str, bg: str, knock: str | None = None) -> Rows:
    """Icon ``name`` (see ``ICONS``) of ``size`` px in ``color`` on ``bg``; cut-outs show ``knock`` (default bg)."""
    layers = [(sdf, hex_rgb(color) if role == "fg" else hex_rgb(knock or bg)) for sdf, role in _icon_layers(name, size)]
    bg_rgb = hex_rgb(bg)
    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            c: tuple[float, float, float] = (float(bg_rgb[0]), float(bg_rgb[1]), float(bg_rgb[2]))
            for sdf, rgb in layers:
                c = _blend(c, rgb, coverage(sdf(x + 0.5, y + 0.5)))
            row.append(_hex(c))
        rows.append(tuple(row))
    return tuple(rows)


def overlay(base: Rows, top: Rows, transparent: str) -> Rows:
    """``top`` drawn over ``base`` (same size); pixels of ``top`` equal to ``transparent`` let ``base`` show."""
    return tuple(tuple(t if t != transparent else b for b, t in zip(brow, trow)) for brow, trow in zip(base, top))


def pad_sides(rows: Rows, left: int, right: int, color: str) -> Rows:
    """``rows`` with plain ``color`` columns added on both sides (spacing baked into an image)."""
    return tuple((color,) * left + tuple(row) + (color,) * right for row in rows)


def tcl_data(rows: Sequence[Sequence[str]]) -> str:
    """Rows as the data string ``PhotoImage.put`` expects."""
    return " ".join("{" + " ".join(row) + "}" for row in rows)
