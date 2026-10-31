"""The player's saved profile: what to remember so the whole procedure is not repeated (no Tk).

Stored as JSON next to the assistant's own config (``<app data dir>/profile.json``), one entry per
tablet. Only things the player already chose are kept: the tablet and display that were picked, the
answers to the questions, and the last few settings that were saved or kept (area and filters) with
the date and where they came from. **No play data** (cursor, clicks, maps) is ever stored here.

Like ``appconfig``: writes are atomic, a missing or corrupt file gives an empty profile (a corrupt one
is kept aside as ``profile.corrupt.json``) and every field is validated on load.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .appconfig import app_data_dir
from .models import FilterConfig, Rect

PROFILE_FILE = "profile.json"
MAX_HISTORY = 12                 # saved settings kept per tablet (newest first)
MAX_COACH = 8                    # coach session summaries kept per tablet
SOURCES = ("wizard", "coach", "live")
_PREF_KEYS = {"play_style": ("finger", "wrist", "arm", "mixed"),
              "map_preference": ("jumps", "streams", "balanced"),
              "priority": ("latency", "balanced", "smoothness"),
              "skill_level": ("beginner", "intermediate", "advanced"),
              "plugin_policy": ("tune_only", "installed", "allow_new")}
_BOOL_PREFS = ("notices_jitter", "notices_input_lag")


def default_profile_path() -> Path:
    return app_data_dir() / PROFILE_FILE


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _num(value: Any, lo: float, hi: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    return float(value) if lo <= value <= hi else None


@dataclass
class SavedSetting:
    """Area and filters that were kept, with when and how."""
    when: str                                   # ISO timestamp (local time, seconds)
    source: str                                 # wizard | coach | live
    area: dict[str, float]                      # width, height, x, y, rotation (native tablet mm)
    filters: list[dict[str, Any]] = field(default_factory=list)   # {"path", "enabled", "settings"}
    note: str = ""                              # short free text, e.g. "5 rounds, 38 % better"

    def rect(self) -> Rect:
        a = self.area
        return Rect(a["width"], a["height"], a["x"], a["y"], a.get("rotation", 0.0))

    def filter_configs(self) -> tuple[FilterConfig, ...]:
        return tuple(FilterConfig(f["path"], bool(f.get("enabled", True)), dict(f.get("settings", {})))
                     for f in self.filters)

    def same_setup(self, other: "SavedSetting") -> bool:
        return self.area == other.area and self.filters == other.filters


@dataclass
class SavedProfile:
    tablet_name: str
    tablet: dict[str, float] | None = None      # width_mm, height_mm, report_rate_hz, max_pressure (custom tablets)
    display: dict[str, float] | None = None     # width_px, height_px, left, top, refresh_hz
    prefs: dict[str, Any] = field(default_factory=dict)
    history: list[SavedSetting] = field(default_factory=list)
    coach: list[dict[str, Any]] = field(default_factory=list)
    updated: str = ""

    @property
    def last(self) -> SavedSetting | None:
        return self.history[0] if self.history else None


# ----------------------------------------------------------------- validation
def _setting_from(data: Any) -> SavedSetting | None:
    if not isinstance(data, dict) or data.get("source") not in SOURCES or not isinstance(data.get("when"), str):
        return None
    area = data.get("area")
    if not isinstance(area, dict):
        return None
    clean = {k: _num(area.get(k), -1e4, 1e4) for k in ("width", "height", "x", "y")}
    rotation = _num(area.get("rotation", 0.0), -360, 360)
    if any(v is None for v in clean.values()) or rotation is None or clean["width"] <= 0 or clean["height"] <= 0:
        return None
    filters = []
    for item in data.get("filters", []) if isinstance(data.get("filters"), list) else []:
        if isinstance(item, dict) and isinstance(item.get("path"), str) and isinstance(item.get("settings", {}), dict):
            filters.append({"path": item["path"], "enabled": bool(item.get("enabled", True)),
                            "settings": {str(k): v for k, v in item.get("settings", {}).items()
                                         if isinstance(v, (int, float, str, bool))}})
    note = data.get("note")
    return SavedSetting(data["when"][:32], data["source"], {**clean, "rotation": rotation}, filters,
                        note[:120] if isinstance(note, str) else "")


def _profile_from(data: Any) -> SavedProfile | None:
    if not isinstance(data, dict) or not isinstance(data.get("tablet_name"), str) or not data["tablet_name"].strip():
        return None
    prof = SavedProfile(data["tablet_name"].strip()[:120])
    tablet = data.get("tablet")
    if isinstance(tablet, dict):
        vals = {"width_mm": _num(tablet.get("width_mm"), 1, 2000), "height_mm": _num(tablet.get("height_mm"), 1, 2000),
                "report_rate_hz": _num(tablet.get("report_rate_hz"), 1, 20000),
                "max_pressure": _num(tablet.get("max_pressure", 0), 0, 1e6)}
        if all(v is not None for v in vals.values()):
            prof.tablet = vals       # type: ignore[assignment]
    display = data.get("display")
    if isinstance(display, dict):
        vals = {"width_px": _num(display.get("width_px"), 1, 20000), "height_px": _num(display.get("height_px"), 1, 20000),
                "left": _num(display.get("left", 0), -50000, 50000), "top": _num(display.get("top", 0), -50000, 50000),
                "refresh_hz": _num(display.get("refresh_hz", 60), 1, 1000)}
        if all(v is not None for v in vals.values()):
            prof.display = vals      # type: ignore[assignment]
    prefs = data.get("prefs")
    if isinstance(prefs, dict):
        for key, allowed in _PREF_KEYS.items():
            if prefs.get(key) in allowed:
                prof.prefs[key] = prefs[key]
        for key in _BOOL_PREFS:
            if isinstance(prefs.get(key), bool):
                prof.prefs[key] = prefs[key]
        width = _num(prefs.get("preferred_area_width_mm"), 1, 2000)
        if width is not None:
            prof.prefs["preferred_area_width_mm"] = width
    for item in data.get("history", []) if isinstance(data.get("history"), list) else []:
        setting = _setting_from(item)
        if setting is not None:
            prof.history.append(setting)
    prof.history = prof.history[:MAX_HISTORY]
    for item in data.get("coach", []) if isinstance(data.get("coach"), list) else []:
        if isinstance(item, dict) and isinstance(item.get("when"), str):
            prof.coach.append({"when": item["when"][:32], "rounds": int(_num(item.get("rounds"), 0, 999) or 0),
                               "gain_pct": _num(item.get("gain_pct"), -100, 100)})
    prof.coach = prof.coach[:MAX_COACH]
    prof.updated = data["updated"][:32] if isinstance(data.get("updated"), str) else ""
    return prof


def to_dict(profile: SavedProfile) -> dict[str, Any]:
    return {"tablet_name": profile.tablet_name, "tablet": profile.tablet, "display": profile.display,
            "prefs": profile.prefs, "updated": profile.updated, "coach": profile.coach,
            "history": [{"when": h.when, "source": h.source, "area": h.area, "filters": h.filters, "note": h.note}
                        for h in profile.history]}


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------- store
class ProfileStore:
    """All saved profiles, one per tablet. Never raises on bad data; write errors are returned as False."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else default_profile_path()
        self.profiles: dict[str, SavedProfile] = {}

    def load(self) -> "ProfileStore":
        self.profiles = {}
        try:
            text = self.path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            return self
        try:
            data = json.loads(text)
        except ValueError:
            try:   # keep the broken file for inspection; the next save writes a clean one
                shutil.copy2(self.path, self.path.with_name("profile.corrupt.json"))
            except OSError:
                pass
            return self
        items = data.get("profiles") if isinstance(data, dict) else None
        for item in items if isinstance(items, list) else []:
            prof = _profile_from(item)
            if prof is not None:
                self.profiles[_norm(prof.tablet_name)] = prof
        return self

    def save(self) -> bool:
        """Atomically write everything. False when the folder is not writable (the profile is a convenience)."""
        text = json.dumps({"version": 1, "profiles": [to_dict(p) for p in self.profiles.values()]},
                          indent=2, ensure_ascii=False)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".profile-", suffix=".tmp", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(text)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
        except (OSError, ValueError):
            return False
        return True

    # --- queries and updates
    def get(self, tablet_name: str) -> SavedProfile | None:
        return self.profiles.get(_norm(tablet_name))

    def ensure(self, tablet_name: str) -> SavedProfile:
        key = _norm(tablet_name)
        if key not in self.profiles:
            self.profiles[key] = SavedProfile(tablet_name)
        return self.profiles[key]

    def latest(self) -> SavedProfile | None:
        """The most recently updated profile (for the welcome screen)."""
        return max(self.profiles.values(), key=lambda p: p.updated, default=None)

    def remember_setting(self, tablet_name: str, setting: SavedSetting) -> SavedProfile:
        """Add ``setting`` as the newest entry; the same setup again only refreshes the date and source."""
        prof = self.ensure(tablet_name)
        prof.history = [h for h in prof.history if not h.same_setup(setting)]
        prof.history.insert(0, setting)
        del prof.history[MAX_HISTORY:]
        prof.updated = now_stamp()
        return prof

    def remember_coach(self, tablet_name: str, rounds: int, gain_pct: float | None) -> None:
        prof = self.ensure(tablet_name)
        prof.coach.insert(0, {"when": now_stamp(), "rounds": int(rounds), "gain_pct": gain_pct})
        del prof.coach[MAX_COACH:]
        prof.updated = now_stamp()

    def forget(self, tablet_name: str | None = None) -> None:
        """Delete one tablet's profile, or all of them."""
        if tablet_name is None:
            self.profiles.clear()
        else:
            self.profiles.pop(_norm(tablet_name), None)
