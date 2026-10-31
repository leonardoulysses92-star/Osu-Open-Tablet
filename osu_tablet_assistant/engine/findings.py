"""Plain-language findings of a play report: at most four short lines (pure).

Each finding is a ``Reason`` the GUI translates (``play.find.*``); ``zone`` parameters are zone ids
(``engine.playtypes.ZONES``), ``place`` parameters are ``hotspots.place_id`` strings ("bottom.center_right.corner"),
``size`` is small / medium / large and ``towards`` a ``hotspots.direction_id``; the GUI turns them into words.
A hotspot (small region of the fine grid) replaces the coarser edge finding it falls in. Technical numbers stay
in the report.
"""
from __future__ import annotations

from ..models import Reason
from .hotspots import direction_id, place_id
from .playobserve import zone_contains
from .playtypes import LATE, NOISY, OK, OVER, SHORT, Hotspot, PlayReport

MAX_FINDINGS = 4
MIN_SEVERITY = 0.15            # weaker zone findings are not worth the player's attention
JITTER_MM = 0.06
LAG_MM = 0.8
TIMING_SPREAD_MS = 45.0
MISS_RATE = 0.25
REACH_MARGIN = 0.02            # |gain - 1| below this is not mentioned
_CORNERS = {"top_left": ("top", "left"), "top_right": ("top", "right"),
            "bottom_left": ("bottom", "left"), "bottom_right": ("bottom", "right")}


def _size(mm: float) -> str:
    return f"{mm:.0f}" if mm >= 2 else f"{mm:.1f}"


def hotspot_reason(spot: Hotspot) -> Reason:
    """One plain sentence about a hotspot: where (in words), which way, about how many mm, how big the zone is."""
    params = {"place": place_id(spot.fx, spot.fy), "mm": _size(spot.err_mm), "size": spot.size}
    if spot.kind == "off":
        params["towards"] = direction_id(spot.dx_mm, spot.dy_mm)
    return Reason(f"play.find.spot_{spot.kind}", params)


def findings(report: PlayReport, limit: int = MAX_FINDINGS) -> list[Reason]:
    """The most useful things to tell the player, strongest first. Never empty for a trusted report."""
    scored: list[tuple[float, Reason]] = []
    chosen_zones: dict[str, str] = {}
    radius = report.radius_mm or 3.0
    for spot in report.hotspots:
        scored.append((1.05 + min(spot.err_mm / radius, 1.0), hotspot_reason(spot)))
    for edge in sorted(report.edges, key=lambda e: -e.severity):
        if edge.kind == OK or edge.severity < MIN_SEVERITY:
            continue
        # the finer hotspot already says it, with a better place
        if any(spot.kind == edge.kind and zone_contains(edge.zone, spot.fx, spot.fy) for spot in report.hotspots):
            continue
        # a corner adds nothing when one of its sides already says the same
        parts = _CORNERS.get(edge.zone, ())
        if any(chosen_zones.get(side) == edge.kind for side in parts):
            continue
        chosen_zones[edge.zone] = edge.kind
        key = {SHORT: "play.find.short", OVER: "play.find.over", LATE: "play.find.late",
               NOISY: "play.find.noisy"}[edge.kind]
        scored.append((1.0 + edge.severity, Reason(key, {"zone": edge.zone, "mm": _size(edge.err_mm)})))
    reach = report.reach
    if reach is not None:
        for axis, gain, ci in (("x", reach.gain_x, reach.ci_x), ("y", reach.gain_y, reach.ci_y)):
            if abs(gain - 1.0) >= REACH_MARGIN and (ci[1] < 1.0 or ci[0] > 1.0):
                key = "play.find.reach_short" if gain < 1 else "play.find.reach_over"
                scored.append((0.8 + abs(gain - 1.0), Reason(key, {"dir": axis, "pct": f"{abs(gain - 1.0) * 100:.0f}"})))
    feel = report.feel
    if feel is not None:
        lag_limit = max(LAG_MM, 0.25 * (report.radius_mm or 0.0))
        if feel.lag_mm is not None and feel.lag_mm < -lag_limit:
            scored.append((0.7, Reason("play.find.lag", {"mm": _size(-feel.lag_mm)})))
        if feel.jitter_mm is not None and feel.n_slow >= 5 and feel.jitter_mm > JITTER_MM:
            scored.append((0.6, Reason("play.find.jitter")))
    if report.time_spread_ms is not None and report.time_spread_ms > TIMING_SPREAD_MS:
        scored.append((0.3, Reason("play.find.timing")))
    if report.miss_rate > MISS_RATE:
        scored.append((0.2, Reason("play.find.misses", {"pct": f"{report.miss_rate * 100:.0f}"})))
    scored.sort(key=lambda item: -item[0])
    out = [reason for _score, reason in scored[:limit]]
    return out or [Reason("play.find.good")]
