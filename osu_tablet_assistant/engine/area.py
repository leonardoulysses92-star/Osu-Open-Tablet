"""Tablet area recommendation (size, aspect ratio and position).

The size ranges below are *community heuristics*, not measured optima. osu!
players broadly agree that small areas suit finger/wrist aim and fast jumps,
while larger areas suit arm aim and give steadier, more precise movement, and
that the right size depends on the hand (see e.g.
https://osu.ppy.sh/community/forums/topics/459861 and
https://osu.ppy.sh/community/forums/topics/586506). The millimetre ranges are
our rough reading of commonly shared player settings (confidence: low); they
are a starting point that the user is expected to adjust.
"""
from __future__ import annotations

from ..models import (DisplaySpec, MapPreference, PlayStyle, Reason, Rect, SkillLevel,
                      TabletSpec, UserPreferences)

# (min, typical, max) area width in mm per play style. Heuristic, low confidence.
STYLE_WIDTH_MM: dict[PlayStyle, tuple[float, float, float]] = {
    PlayStyle.FINGER: (40.0, 55.0, 70.0),
    PlayStyle.WRIST: (60.0, 80.0, 100.0),
    PlayStyle.MIXED: (75.0, 95.0, 120.0),
    PlayStyle.ARM: (100.0, 125.0, 160.0),
}
# Jumps: a smaller area shortens the physical distance between objects.
# Streams: a larger area gives finer, steadier control for long patterns.
MAP_FACTOR: dict[MapPreference, float] = {
    MapPreference.JUMPS: 0.92,
    MapPreference.BALANCED: 1.0,
    MapPreference.STREAMS: 1.08,
}
# Beginners: a slightly larger area damps hand shake; advanced players
# usually already tolerate a smaller one. Small nudges only (guess).
SKILL_FACTOR: dict[SkillLevel, float] = {
    SkillLevel.BEGINNER: 1.05,
    SkillLevel.INTERMEDIATE: 1.0,
    SkillLevel.ADVANCED: 0.97,
}
MIN_AREA_WIDTH_MM = 20.0   # floor for a user-provided width (smaller is unplayable)


def recommend_area(tablet: TabletSpec, display: DisplaySpec,
                   prefs: UserPreferences) -> tuple[Rect, tuple[Reason, ...]]:
    """Center-based tablet area in mm with the display's aspect ratio.

    Raises ValueError when the tablet or display dimensions are not positive.
    """
    if tablet.width_mm <= 0 or tablet.height_mm <= 0:
        raise ValueError("tablet dimensions must be positive")
    if display.width_px <= 0 or display.height_px <= 0:
        raise ValueError("display dimensions must be positive")
    aspect = display.width_px / display.height_px
    reasons: list[Reason] = []

    if prefs.preferred_area_width_mm is not None and prefs.preferred_area_width_mm > 0:
        width = float(prefs.preferred_area_width_mm)
        reasons.append(Reason("engine.reason.area_preferred", {"width": round(width, 1)}))
    else:
        low, typical, high = STYLE_WIDTH_MM[prefs.play_style]
        reasons.append(Reason(f"engine.reason.area_style_{prefs.play_style.value}",
                              {"min": low, "max": high}))
        width = typical * MAP_FACTOR[prefs.map_preference] * SKILL_FACTOR[prefs.skill_level]
        width = min(max(width, low), high)
        if prefs.map_preference is not MapPreference.BALANCED:
            reasons.append(Reason(f"engine.reason.area_map_{prefs.map_preference.value}"))
        if prefs.skill_level is not SkillLevel.INTERMEDIATE:
            reasons.append(Reason(f"engine.reason.area_skill_{prefs.skill_level.value}"))
        reasons.append(Reason("engine.reason.area_heuristic"))

    # Largest area with the display's aspect ratio that fits in the tablet.
    max_width = min(tablet.width_mm, tablet.height_mm * aspect)
    if width > max_width:
        reasons.append(Reason("engine.reason.area_clamped_tablet", {
            "wanted": round(width, 1), "width": round(max_width, 1),
            "tablet_w": round(tablet.width_mm, 1), "tablet_h": round(tablet.height_mm, 1)}))
        width = max_width
    elif width < MIN_AREA_WIDTH_MM:
        width = min(MIN_AREA_WIDTH_MM, max_width)
        reasons.append(Reason("engine.reason.area_clamped_min", {"width": round(width, 1)}))
    height = width / aspect

    reasons.append(Reason("engine.reason.area_aspect", {
        "width_px": display.width_px, "height_px": display.height_px,
        "width": round(width, 1), "height": round(height, 1)}))
    reasons.append(Reason("engine.reason.area_centered"))
    area = Rect(width=width, height=height, x=tablet.width_mm / 2, y=tablet.height_mm / 2)
    return area, tuple(reasons)
