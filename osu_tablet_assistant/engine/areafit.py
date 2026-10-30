"""Active-area geometry in the player's view, converted to native tablet millimetres (pure).

The coach reasons in the *player's view*: ``(0, 0)`` is the top-left of the
screen (and of the area) whatever the area rotation, so "the bottom-right
corner" always means the bottom-right of the screen. OpenTabletDriver stores
the area as a centre-based rectangle plus a rotation in native tablet
millimetres; :func:`rotate_to_tablet` and :func:`tablet_to_view` convert
vectors between the two.

Rotation 0 and 180 are the verified cases (a left-handed player usually has
180). 90/270 use the same rotation-matrix convention but were never checked
against OTD; :func:`rotation_verified` tells callers to say so.
"""
from __future__ import annotations

import math
from dataclasses import replace

from ..models import Reason, Rect, TabletSpec
from .adapt import AREA_DECIMALS, MIN_AREA_WIDTH_MM

VERIFIED_ROTATIONS = (0.0, 180.0)
_EPS = 1e-9


def _fmt(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def rotation_verified(area: Rect) -> bool:
    """True for the rotations checked against OTD (0 and 180)."""
    return any(abs((area.rotation - r + 180) % 360 - 180) < 1e-6 for r in VERIFIED_ROTATIONS)


def _cos_sin(rotation: float) -> tuple[float, float]:
    quarter = round(rotation / 90.0) % 4
    if abs(rotation - round(rotation / 90.0) * 90.0) < 1e-9:   # exact for multiples of 90
        return (1.0, 0.0, -1.0, 0.0)[quarter], (0.0, 1.0, 0.0, -1.0)[quarter]
    rad = math.radians(rotation)
    return math.cos(rad), math.sin(rad)


def rotate_to_tablet(dx: float, dy: float, rotation: float) -> tuple[float, float]:
    """A vector in the player's view (mm) -> the same vector in native tablet mm."""
    c, s = _cos_sin(rotation)
    return dx * c - dy * s, dx * s + dy * c


def tablet_to_view(tx: float, ty: float, rotation: float) -> tuple[float, float]:
    """Inverse of :func:`rotate_to_tablet`."""
    c, s = _cos_sin(rotation)
    return tx * c + ty * s, -tx * s + ty * c


def view_size(area: Rect) -> tuple[float, float]:
    """Width and height of the area as the player sees it (swapped for 90/270)."""
    swapped = abs(math.sin(math.radians(area.rotation))) > 0.5
    return (area.height, area.width) if swapped else (area.width, area.height)


def extent(area: Rect) -> tuple[float, float]:
    """Axis-aligned width/height the rotated area covers on the tablet."""
    rad = math.radians(area.rotation)
    c, s = abs(math.cos(rad)), abs(math.sin(rad))
    return area.width * c + area.height * s, area.width * s + area.height * c


def area_fits(area: Rect, tablet: TabletSpec | None) -> bool:
    """True when the rotated area lies entirely on the tablet (always True when its size is unknown)."""
    if tablet is None:
        return True
    ew, eh = extent(area)
    return (area.x - ew / 2 >= -_EPS and area.x + ew / 2 <= tablet.width_mm + _EPS
            and area.y - eh / 2 >= -_EPS and area.y + eh / 2 <= tablet.height_mm + _EPS)


def area_to_tablet_mm(area: Rect, u: float, v: float) -> tuple[float, float]:
    """Normalized area coordinates (player's view) -> native tablet mm."""
    vw, vh = view_size(area)
    tx, ty = rotate_to_tablet((u - 0.5) * vw, (v - 0.5) * vh, area.rotation)
    return area.x + tx, area.y + ty


def tablet_mm_to_area(area: Rect, x: float, y: float) -> tuple[float, float]:
    """Native tablet mm -> normalized area coordinates (player's view)."""
    vw, vh = view_size(area)
    dx, dy = tablet_to_view(x - area.x, y - area.y, area.rotation)
    return (dx / vw + 0.5 if vw else 0.5), (dy / vh + 0.5 if vh else 0.5)


def _round(value: float) -> float:
    return round(value, AREA_DECIMALS)


def _clamp_center(area: Rect, tablet: TabletSpec | None) -> tuple[Rect, bool]:
    """Move the centre (never the size) until the rotated area fits; True when it had to move."""
    if tablet is None:
        return area, False
    ew, eh = extent(area)
    x = min(max(area.x, ew / 2), tablet.width_mm - ew / 2) if ew <= tablet.width_mm else area.x
    y = min(max(area.y, eh / 2), tablet.height_mm - eh / 2) if eh <= tablet.height_mm else area.y
    out = replace(area, x=_round(x), y=_round(y))
    step = 10.0 ** -AREA_DECIMALS
    for _ in range(4):   # rounding must not push it off the tablet
        if area_fits(out, tablet) or (ew > tablet.width_mm or eh > tablet.height_mm):
            break
        out = replace(out, x=_round(out.x + math.copysign(step, tablet.width_mm / 2 - out.x)),
                      y=_round(out.y + math.copysign(step, tablet.height_mm / 2 - out.y)))
    return out, out.x != _round(area.x) or out.y != _round(area.y)


def shift_area(area: Rect, dx_mm: float, dy_mm: float, tablet: TabletSpec | None
               ) -> tuple[Rect, tuple[Reason, ...]]:
    """Move the area by ``(dx_mm, dy_mm)`` *in the player's view* (+x right, +y down), clamped to the tablet.

    Returns the new area (equal to ``area`` when there is no room) and plain-language reasons.
    """
    tx, ty = rotate_to_tablet(dx_mm, dy_mm, area.rotation)
    moved, clamped = _clamp_center(replace(area, x=area.x + tx, y=area.y + ty), tablet)
    # What really moved, in the player's view.
    rdx, rdy = tablet_to_view(moved.x - area.x, moved.y - area.y, area.rotation)
    reasons: list[Reason] = []
    if abs(rdx) < 10.0 ** -AREA_DECIMALS and abs(rdy) < 10.0 ** -AREA_DECIMALS:
        return area, (Reason("play.area.no_room"),)
    reasons.append(Reason("play.area.shift", {"dx": _fmt(rdx), "dy": _fmt(rdy)}))
    if clamped:
        reasons.append(Reason("play.area.clamped"))
    if not rotation_verified(area):
        reasons.append(Reason("play.area.rotation_unverified", {"rotation": _fmt(area.rotation)}))
    return moved, tuple(reasons)


def scale_area_axes(area: Rect, sx: float, sy: float, tablet: TabletSpec | None, *,
                    lock_aspect: bool = True, anchor: tuple[float, float] = (0.5, 0.5)
                    ) -> tuple[Rect, tuple[Reason, ...]]:
    """Scale the area by ``sx`` (view width) and ``sy`` (view height), keeping ``anchor`` fixed.

    ``anchor`` is a point of the area in normalized view coordinates (0.5, 0.5 = centre;
    (1, 1) = bottom-right corner stays put, so the area grows towards the top-left). With
    ``lock_aspect`` (default) one factor, the mean of ``sx`` and ``sy``, is used and a reason says so;
    with ``lock_aspect=False`` the aspect ratio changes, and a reason says that too. Growth is capped
    to what fits on the tablet and the width never goes below ``MIN_AREA_WIDTH_MM``.
    """
    reasons: list[Reason] = []
    if not (math.isfinite(sx) and math.isfinite(sy)) or sx <= 0 or sy <= 0 or area.width <= 0 or area.height <= 0:
        return area, (Reason("play.area.unknown"),)
    if lock_aspect:
        factor = (sx + sy) / 2.0
        if abs(sx - sy) > 0.01:
            reasons.append(Reason("play.area.scale_mean", {"fx": _fmt(sx), "fy": _fmt(sy), "f": _fmt(factor)}))
        sx = sy = factor
    vw, vh = view_size(area)
    swapped = vw != area.width or vh != area.height

    def dims(fx: float, fy: float) -> tuple[float, float]:
        """Native width/height after scaling the view width by ``fx`` and the view height by ``fy``."""
        return (vh * fy, vw * fx) if swapped else (vw * fx, vh * fy)

    if tablet is not None and (sx > 1.0 or sy > 1.0):   # cap growth to what fits on the tablet
        def fits(t: float) -> bool:
            w, h = dims(1.0 + (sx - 1.0) * t, 1.0 + (sy - 1.0) * t)
            ew, eh = extent(replace(area, width=w, height=h))
            return ew <= tablet.width_mm + _EPS and eh <= tablet.height_mm + _EPS

        if not fits(1.0):
            lo, hi = 0.0, 1.0
            for _ in range(30):
                mid = (lo + hi) / 2
                lo, hi = (mid, hi) if fits(mid) else (lo, mid)
            sx, sy = 1.0 + (sx - 1.0) * lo, 1.0 + (sy - 1.0) * lo
            reasons.append(Reason("play.area.capped"))
    if vw * sx < MIN_AREA_WIDTH_MM:
        sy *= MIN_AREA_WIDTH_MM / (vw * sx)
        sx = MIN_AREA_WIDTH_MM / vw
        reasons.append(Reason("play.area.at_min", {"min": _fmt(MIN_AREA_WIDTH_MM)}))
    nvw, nvh = vw * sx, vh * sy
    new_w, new_h = (_round(v) for v in dims(sx, sy))
    # Keep the anchor point where it was on the tablet.
    ax, ay = area_to_tablet_mm(area, *anchor)
    off = rotate_to_tablet((anchor[0] - 0.5) * nvw, (anchor[1] - 0.5) * nvh, area.rotation)
    candidate = replace(area, width=new_w, height=new_h, x=ax - off[0], y=ay - off[1])
    out, clamped = _clamp_center(candidate, tablet)
    if clamped:
        reasons.append(Reason("play.area.clamped"))
    if out.width == area.width and out.height == area.height and (out.x, out.y) == (_round(area.x), _round(area.y)):
        return area, (Reason("play.area.no_room"),)
    out = replace(out, x=_round(out.x), y=_round(out.y))
    if not lock_aspect and abs((out.width / out.height) / (area.width / area.height) - 1.0) > 0.005:
        reasons.append(Reason("play.area.aspect_changed"))
    reasons.insert(0, Reason("play.area.scale", {
        "old_w": _fmt(area.width), "old_h": _fmt(area.height), "new_w": _fmt(out.width),
        "new_h": _fmt(out.height), "pct": _fmt((out.width / area.width - 1.0) * 100.0)}))
    if not rotation_verified(area):
        reasons.append(Reason("play.area.rotation_unverified", {"rotation": _fmt(area.rotation)}))
    return out, tuple(reasons)
