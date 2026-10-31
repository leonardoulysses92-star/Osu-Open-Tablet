"""The colour palette (no Tk, so pure code such as the map geometry can use it)."""
from __future__ import annotations

from dataclasses import dataclass


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
