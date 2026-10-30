"""Tablet database: built-in list, optional OTD ``Configurations`` folder, custom entries.

``tablets_builtin.json`` fields: ``name`` (the exact OTD profile name),
``width_mm``/``height_mm`` (active area), ``max_report_rate_hz``,
``max_pressure``, ``resolution_lpmm``. ``verified`` means area, pressure and
resolution were checked against OTD's own configuration files;
``rate_estimated`` means the report rate comes from vendor specs and is only
approximate (OTD configurations do not carry it).
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..models import TabletSource, TabletSpec

_BUILTIN_FILE = Path(__file__).with_name("tablets_builtin.json")
_DEFAULT_RATE_HZ = 133.0
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_name(name: str) -> str:
    """Case, whitespace and punctuation insensitive key for matching names."""
    return _NON_ALNUM.sub("", name.casefold())


@lru_cache(maxsize=1)
def _builtin_rows() -> tuple[dict[str, Any], ...]:
    """Raw rows of the built-in JSON file (empty if the file is unreadable)."""
    try:
        data = json.loads(_BUILTIN_FILE.read_text(encoding="utf-8"))
        rows = data["tablets"]
    except (OSError, ValueError, KeyError, TypeError):
        return ()
    return tuple(r for r in rows if isinstance(r, dict))


def _spec_from_row(row: dict[str, Any], source: TabletSource) -> TabletSpec | None:
    """Build a TabletSpec from a dict, or None if the row is malformed."""
    try:
        name = str(row["name"]).strip()
        width = float(row["width_mm"])
        height = float(row["height_mm"])
        rate = float(row.get("max_report_rate_hz") or _DEFAULT_RATE_HZ)
        pressure = int(row.get("max_pressure") or 0)
        lpmm = row.get("resolution_lpmm")
        lpmm = float(lpmm) if lpmm else None
    except (KeyError, TypeError, ValueError):
        return None
    if not name or width <= 0 or height <= 0 or rate <= 0:
        return None
    return TabletSpec(name, width, height, rate, pressure, lpmm, source)


def builtin_tablets() -> list[TabletSpec]:
    """The tablets shipped with this project."""
    specs = (_spec_from_row(r, TabletSource.BUILTIN) for r in _builtin_rows())
    return [s for s in specs if s is not None]


def is_verified(name: str) -> bool:
    """True if a built-in tablet's area and pressure were checked against OTD."""
    key = normalize_name(name)
    return any(
        normalize_name(str(r.get("name", ""))) == key and r.get("verified") is True
        for r in _builtin_rows()
    )


def _otd_config_tablets(install_dir: Path | None) -> list[TabletSpec]:
    """Parse ``<install_dir>/Configurations/**/*.json`` if that folder exists."""
    if install_dir is None:
        return []
    root = Path(install_dir) / "Configurations"
    if not root.is_dir():
        return []
    known_rates = {normalize_name(s.name): s.max_report_rate_hz for s in builtin_tablets()}
    found: list[TabletSpec] = []
    for path in sorted(root.rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            digitizer = data["Specifications"]["Digitizer"]
            pen = data["Specifications"].get("Pen") or {}
            name = str(data["Name"]).strip()
            width = float(digitizer["Width"])
            max_x = float(digitizer.get("MaxX") or 0)
            row = {
                "name": name,
                "width_mm": width,
                "height_mm": float(digitizer["Height"]),
                "max_report_rate_hz": known_rates.get(normalize_name(name)),
                "max_pressure": pen.get("MaxPressure", 0),
                "resolution_lpmm": max_x / width if max_x and width else None,
            }
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
        spec = _spec_from_row(row, TabletSource.OTD_CONFIG)
        if spec is not None:
            found.append(spec)
    return found


def list_known_tablets(install_dir: Path | None) -> list[TabletSpec]:
    """OTD configuration tablets (if available) plus built-in ones, sorted by name.

    A tablet present in both places is listed once, with the OTD data.
    """
    merged: dict[str, TabletSpec] = {normalize_name(s.name): s for s in builtin_tablets()}
    for spec in _otd_config_tablets(install_dir):
        merged[normalize_name(spec.name)] = spec
    return sorted(merged.values(), key=lambda s: s.name.casefold())


def match_tablet(profile_name: str, tablets: list[TabletSpec]) -> TabletSpec | None:
    """Find the tablet an OTD profile name refers to (case/space/punctuation insensitive)."""
    key = normalize_name(profile_name)
    if not key:
        return None
    for spec in tablets:
        if normalize_name(spec.name) == key:
            return spec
    return None


def custom_tablet(
    name: str,
    width_mm: float,
    height_mm: float,
    report_rate_hz: float = _DEFAULT_RATE_HZ,
    max_pressure: int = 0,
    resolution_lpmm: float | None = None,
) -> TabletSpec:
    """Build a user-typed TabletSpec; raises ValueError on invalid values."""
    name = name.strip()
    if not name:
        raise ValueError("name")
    if width_mm <= 0 or height_mm <= 0:
        raise ValueError("size")
    if report_rate_hz <= 0:
        raise ValueError("rate")
    if max_pressure < 0:
        raise ValueError("pressure")
    return TabletSpec(
        name,
        float(width_mm),
        float(height_mm),
        float(report_rate_hz),
        int(max_pressure),
        resolution_lpmm,
        TabletSource.CUSTOM,
    )
