"""The colour palette and the glass tokens (no Tk, so pure code such as the map geometry can use it).

Direction: "calm dark glass". A deep graphite-blue ground; panels read as frosted glass because
their fill is the ground blended a few percent toward a cool white, with a hairline border and a
brighter 1 px line along the top edge. Tk has no backdrop blur or real transparency, so the glass
is an emulation with opaque blended colours (see docs/DESIGN.md).

Tune the whole look here: change ``GROUND``, ``TINT`` or the ``GLASS`` amounts and every derived
colour follows. ``Colors`` keeps the field names the rest of the program already uses.
"""
from __future__ import annotations

from dataclasses import dataclass

GROUND = "#0e1219"        # window background: deep graphite blue
TINT = "#dde6f7"          # the cool white the glass is blended toward
ACCENT = "#ff66aa"        # the single accent hue (osu! pink), used sparingly
# How far each layer is blended from the ground toward TINT (0 = ground, 1 = tint).
GLASS = {
    "field": 0.02,        # inset input fields, segmented-control tracks
    "surface": 0.05,      # glass panels (cards, header)
    "surface_2": 0.09,    # raised controls (secondary buttons)
    "surface_3": 0.14,    # hover
    "border": 0.12,       # 1 px hairline around a panel
    "highlight": 0.21,    # 1 px top edge of a panel (light catching the glass)
    "border_strong": 0.24,
}


def hex_rgb(color: str) -> tuple[int, int, int]:
    """``#rrggbb`` -> (r, g, b)."""
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def mix(a: str, b: str, k: float) -> str:
    """``#rrggbb`` blend: ``k = 0`` is ``a``, ``k = 1`` is ``b``."""
    k = min(max(k, 0.0), 1.0)
    return "#" + "".join(f"{round(x + (y - x) * k):02x}" for x, y in zip(hex_rgb(a), hex_rgb(b)))


def relative_luminance(color: str) -> float:
    """WCAG 2.x relative luminance of an sRGB colour."""
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(v) for v in hex_rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: str, b: str) -> float:
    """WCAG contrast ratio between two colours (1..21)."""
    la, lb = sorted((relative_luminance(a), relative_luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _glass(level: str) -> str:
    return mix(GROUND, TINT, GLASS[level])


_SURFACE = _glass("surface")


@dataclass(frozen=True)
class Colors:
    """The palette. ``*_soft`` colours are tinted backgrounds for the matching role."""
    bg: str = GROUND
    field: str = _glass("field")
    surface: str = _SURFACE
    surface_2: str = _glass("surface_2")
    surface_3: str = _glass("surface_3")
    border: str = _glass("border")
    highlight: str = _glass("highlight")
    border_strong: str = _glass("border_strong")
    text: str = "#e9edf4"
    muted: str = "#a1a9b7"           # secondary text; >= 4.5:1 on every surface
    subtle: str = "#6c7482"          # disabled text and decoration only (not for reading)
    accent: str = ACCENT
    accent_hover: str = "#ff82bb"
    accent_pressed: str = "#e8579a"
    accent_soft: str = mix(_SURFACE, ACCENT, 0.18)
    on_accent: str = GROUND
    good: str = "#66c793"            # semantic colours, kept muted
    good_soft: str = mix(_SURFACE, "#66c793", 0.12)
    warn: str = "#e2b85e"
    warn_soft: str = mix(_SURFACE, "#e2b85e", 0.11)
    error: str = "#f07888"
    error_soft: str = mix(_SURFACE, "#f07888", 0.12)
    info: str = "#7fb0ee"
    info_soft: str = mix(_SURFACE, "#7fb0ee", 0.12)
    before: str = "#6f7786"


COLORS = Colors()
