"""Discover installed OTD plugins and the filter paths they provide (read-only).

OTD 0.6.x ships **no** built-in filters: on 0.6.7 with HawkuFilters, Temporal
Resampler, Radial Follow and The Saturn Collection installed,
``OpenTabletDriver.Console listfilters`` lists only plugin paths. So
``BUILTIN_FILTER_PATHS`` is empty and everything comes from ``KNOWN_PLUGINS``
plus a namespace heuristic (filter paths already present in ``settings.json``
whose first segment matches an installed plugin DLL name).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from ..models import PluginInfo
from .settings_io import OtdSettingsError, load_settings

PLUGINS_DIR = "Plugins"
METADATA_FILE = "metadata.json"

BUILTIN_FILTER_PATHS: frozenset[str] = frozenset()


@dataclass(frozen=True)
class KnownPlugin:
    """Paths a well-known plugin provides. ``aliases`` match metadata name, folder or DLL stem."""
    aliases: tuple[str, ...]
    filters: tuple[str, ...]
    tools: tuple[str, ...] = ()


KNOWN_PLUGINS: tuple[KnownPlugin, ...] = (
    KnownPlugin(("HawkuFilters",),
                ("TabletDriverFilters.Hawku.Smoothing", "OpenTabletDriver.Plugin.NoiseReduction")),
    KnownPlugin(("DevocubFilters", "Devocub Antichatter"), ("TabletDriverFilters.Devocub.Antichatter",)),
    KnownPlugin(("Temporal Resampler", "TemporalResampler"), ("TemporalResampler",)),
    KnownPlugin(("AbstractQbit's Radial Follow Smoothing", "RadialFollow"),
                ("RadialFollow.RadialFollowSmoothingScreenSpace", "RadialFollow.RadialFollowSmoothingTabletSpace")),
    # Saturn.HRRNC only exists in Saturn 1.1.x (not in 1.0.0), so it is not assumed here;
    # it is still found through the settings.json namespace heuristic once used.
    KnownPlugin(("The Saturn Collection", "Saturn", "TheSaturnCollection"),
                ("Saturn.Multifilter", "Saturn.AreaRoundingDFC"),
                ("Saturn.CustomResetAbsoluteModeSettings", "Saturn.MultifilterExtraSettings")),
    KnownPlugin(("Kuuube's Chatter Exterminator", "Kuuube_s_Chatter_Exterminator"),
                ("Kuuube_s_Chatter_Exterminator.Kuuube_s_CHATTER_EXTERMINATOR_RAW",
                 "Kuuube_s_Chatter_Exterminator.Kuuube_s_CHATTER_EXTERMINATOR_SMOOTH")),
    # Added with the 2026-10-30 catalog audit (paths read in each plugin's source).
    KnownPlugin(("Reconstructor",), ("VoiDPlugins.Filter.Reconstructor",)),
    KnownPlugin(("Spectral Reconstructor", "SpectralReconstructor", "TrajectoryEigenFilter.OTD"),
                ("TrajectoryEigenFilter.OTD.TrajectoryEigenFilterPlugin",)),
    KnownPlugin(("BezierInterpolator", "Bezier Interpolator"), ("BezierInterpolator.BezierInterp",)),
    KnownPlugin(("jaaakb's ScuffedInter-Extrapolator", "ScuffedInterp", "ScuffedInterpolator"),
                ("ScuffedInterpolator.ScuffedInterp",)),
    KnownPlugin(("OnTake's Frenet-Serret Frame Predictor", "FrenetSerretFramePredictor"),
                ("FrenetSerretFramePredictor.FrenetSerretFramePredictor",)),
    KnownPlugin(("Hover Distance Limiter", "Hover_Distance_Limiter"),
                ("Hover_Distance_Limiter.Hover_Distance_Limiter",)),
)


def list_installed_plugins(config_dir: Path) -> list[PluginInfo]:
    """Plugins under ``<config>/Plugins``, sorted by name. Missing dir -> []."""
    return [info for info, _ in _scan(config_dir)]


def available_filter_paths(config_dir: Path) -> set[str]:
    """Filter paths usable with this installation: built-in + plugin-provided."""
    paths = set(BUILTIN_FILTER_PATHS)
    for _, filters in _scan(config_dir):
        paths |= filters
    return paths


def _scan(config_dir: Path) -> list[tuple[PluginInfo, set[str]]]:
    """(PluginInfo, filter paths) for each plugin folder."""
    root = Path(config_dir) / PLUGINS_DIR
    try:
        folders = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []
    used_filters, used_tools = _paths_in_settings(config_dir)
    found: list[tuple[PluginInfo, set[str]]] = []
    for folder in folders:
        meta = _read_metadata(folder / METADATA_FILE)
        name = meta.get("Name") if isinstance(meta.get("Name"), str) and meta["Name"] else folder.name
        version = meta.get("PluginVersion")
        dll_stems = [p.stem for p in folder.glob("*.dll")]
        keys = {_norm(k) for k in (name, folder.name, *dll_stems)}
        filters: set[str] = set()
        tools: set[str] = set()
        for known in KNOWN_PLUGINS:
            if keys & {_norm(a) for a in known.aliases}:
                filters.update(known.filters)
                tools.update(known.tools)
        filters |= {p for p in used_filters if _norm(p.split(".")[0]) in keys}
        tools |= {p for p in used_tools if _norm(p.split(".")[0]) in keys}
        info = PluginInfo(name=name, version=str(version) if version is not None else None,
                          directory=str(folder), provides=tuple(sorted(filters | tools)))
        found.append((info, filters))
    found.sort(key=lambda item: item[0].name.lower())
    return found


def _read_metadata(path: Path) -> dict:
    """Parsed ``metadata.json`` or {} when missing / malformed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _paths_in_settings(config_dir: Path) -> tuple[set[str], set[str]]:
    """Filter and tool paths referenced anywhere in ``settings.json`` (empty on error)."""
    try:
        settings = load_settings(config_dir)
    except OtdSettingsError:
        return set(), set()
    filters: set[str] = set()
    profiles = settings.get("Profiles")
    for prof in profiles if isinstance(profiles, list) else []:
        stores = prof.get("Filters") if isinstance(prof, dict) else None
        filters |= _store_paths(stores)
    return filters, _store_paths(settings.get("Tools"))


def _store_paths(stores: object) -> set[str]:
    if not isinstance(stores, list):
        return set()
    return {s["Path"] for s in stores if isinstance(s, dict) and isinstance(s.get("Path"), str)}


def _norm(text: str) -> str:
    """Lower-case alphanumerics only: "Kuuube's Chatter" == "Kuuube_s_Chatter"."""
    return re.sub(r"[^a-z0-9]", "", text.lower())
