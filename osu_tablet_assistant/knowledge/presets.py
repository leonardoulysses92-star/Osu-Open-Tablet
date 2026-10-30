"""Community tablet presets: strict loader and validator (data only, never code).

``community_presets.json`` (packaged next to this module, or a validated
download installed by ``knowledge.updater``) has the shape
``{version, generated, schema, about?, excluded?, presets: [...]}``.

* A malformed *document* (wrong schema number, missing keys) raises
  :class:`PresetsError` from :func:`parse_presets`; :func:`load_presets`
  turns that into an empty bundle with a warning, so the program never crashes.
* A malformed *entry* is skipped with a warning. That includes a filter path
  the catalog does not know and a property name the filter does not have
  (OTD would silently ignore it, so the preset would not be what it claims).
* Numbers are clamped to the catalog bounds (no step rounding: published
  values keep their precision); every clamp is recorded on the preset.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from importlib import resources
from typing import Any
from urllib.parse import urlparse

from .filters import FilterSpec, load_catalog

SCHEMA = 1
CONFIDENCES = ("high", "medium", "low")
ATTRIBUTIONS = ("player", "community", "unattributed", "user")
FLAGS = ("enabled_unclear", "area_mismatch", "possible_typo", "missing_plugin_data", "tablet_specific",
         "frequency_conflict", "stale", "untouched_defaults", "prediction", "conflicting_sources",
         "partial_values", "unverified")
_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_ENTRY_KEYS = {"id", "player", "attribution", "tablet", "area_mm", "values_area_mm", "rotation_deg",
               "filters", "style_tags", "source_url", "source_date", "retrieved", "confidence", "flags",
               "notes"}
_REQUIRED_KEYS = {"id", "filters", "source_url", "retrieved", "confidence"}
_DOC_KEYS = {"version", "generated", "schema", "about", "excluded", "presets"}
MAX_AREA_MM = 400.0
MAX_PRESETS = 2000


class PresetsError(ValueError):
    """The presets document as a whole is unusable."""


@dataclass(frozen=True)
class PresetFilter:
    """One filter of a preset; ``enabled`` None = the source does not show it."""
    path: str
    enabled: bool | None
    settings: dict[str, Any]


@dataclass(frozen=True)
class CommunityPreset:
    """A published (or user-provided) setup, already validated against the catalog."""
    id: str
    player: str | None
    attribution: str
    tablet: str | None
    area_mm: tuple[float, float] | None          # (width, height) as published
    values_area_mm: tuple[float, float] | None   # area the filter values were made for
    rotation_deg: float | None
    filters: tuple[PresetFilter, ...]
    style_tags: tuple[str, ...]
    source_url: str | None
    source_date: str | None
    retrieved: str
    confidence: str
    flags: tuple[str, ...] = ()
    notes: str = ""
    clamped: tuple[tuple[str, str, Any, Any], ...] = ()   # (path, property, published, used)

    @property
    def candidate_id(self) -> str:
        """Id of the engine candidate built from this preset."""
        return f"community:{self.id}"

    @property
    def name(self) -> str:
        """Who published it (player name without the bracketed rank details), or the id."""
        if self.player:
            return self.player.split(" (")[0].strip() or self.id
        return self.id

    @property
    def enabled_filters(self) -> tuple[PresetFilter, ...]:
        """Filters that are on (or whose state the source does not show)."""
        return tuple(f for f in self.filters if f.enabled is not False)

    @property
    def required_paths(self) -> frozenset[str]:
        """OTD paths a user needs installed to use this preset."""
        return frozenset(f.path for f in self.enabled_filters)

    @property
    def source_area(self) -> tuple[float, float] | None:
        """The area the filter values belong to (``values_area_mm`` wins)."""
        return self.values_area_mm or self.area_mm


@dataclass(frozen=True)
class PresetBundle:
    """A loaded presets document."""
    version: int
    generated: str
    presets: tuple[CommunityPreset, ...]
    warnings: tuple[str, ...] = ()
    excluded: tuple[tuple[str, str], ...] = ()

    @property
    def source_count(self) -> int:
        """Distinct sources (URLs) behind the presets."""
        return len({p.source_url for p in self.presets if p.source_url})

    def by_id(self, preset_id: str) -> CommunityPreset | None:
        """Preset with that id (``community:`` prefix accepted)."""
        preset_id = preset_id.removeprefix("community:")
        return next((p for p in self.presets if p.id == preset_id), None)


EMPTY = PresetBundle(0, "", ())


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def _is_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return len(value) == 10


def _is_https(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 500:
        return False
    parts = urlparse(value)
    return parts.scheme == "https" and bool(parts.netloc)


def _area(raw: Any, where: str) -> tuple[float, float] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"width", "height"}:
        raise ValueError(f"{where}: area must be {{width, height}}")
    w, h = raw["width"], raw["height"]
    for v in (w, h):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) \
                or not 0 < v <= MAX_AREA_MM:
            raise ValueError(f"{where}: area values must be in (0, {MAX_AREA_MM:g}] mm")
    return float(w), float(h)


def _bounded(spec: FilterSpec, name: str, value: Any, where: str) -> tuple[Any, bool]:
    """``value`` checked against the catalog param: (value to use, was clamped)."""
    param = spec.param(name)
    if param is None:
        raise ValueError(f"{where}: unknown property {name!r} for {spec.path}")
    if param.kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{where}: {name} must be true/false")
        return value, False
    if param.kind == "enum":
        if value not in param.choices:
            raise ValueError(f"{where}: {name} must be one of {param.choices}")
        return value, False
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{where}: {name} must be a number")
    used = min(max(float(value), float(param.min)), float(param.max))
    used = int(round(used)) if param.kind == "int" else round(used, 6)
    return used, abs(used - float(value)) > 1e-9


def parse_entry(raw: Any, catalog: dict[str, FilterSpec]) -> CommunityPreset:
    """Validate one preset entry; raises ValueError with a readable reason."""
    if not isinstance(raw, dict):
        raise ValueError("entry is not an object")
    pid = raw.get("id")
    where = f"preset {pid!r}"
    if not isinstance(pid, str) or not _ID.match(pid):
        raise ValueError(f"{where}: invalid id")
    unknown = set(raw) - _ENTRY_KEYS
    if unknown:
        raise ValueError(f"{where}: unknown keys {sorted(unknown)}")
    missing = _REQUIRED_KEYS - set(raw)
    if missing:
        raise ValueError(f"{where}: missing keys {sorted(missing)}")
    attribution = raw.get("attribution", "player" if raw.get("player") else "unattributed")
    if attribution not in ATTRIBUTIONS:
        raise ValueError(f"{where}: invalid attribution")
    if raw["confidence"] not in CONFIDENCES:
        raise ValueError(f"{where}: invalid confidence")
    source = raw["source_url"]
    if source is None:
        if attribution != "user":   # only the program's own user needs no public source
            raise ValueError(f"{where}: a source_url is required")
    elif not _is_https(source):
        raise ValueError(f"{where}: source_url must be an https URL")
    if raw.get("source_date") is not None and not _is_date(raw["source_date"]):
        raise ValueError(f"{where}: source_date must be YYYY-MM-DD or null")
    if not _is_date(raw["retrieved"]):
        raise ValueError(f"{where}: retrieved must be YYYY-MM-DD")
    for key in ("player", "tablet", "notes"):
        if raw.get(key) is not None and not isinstance(raw[key], str):
            raise ValueError(f"{where}: {key} must be text")
    rotation = raw.get("rotation_deg")
    if rotation is not None and (isinstance(rotation, bool) or not isinstance(rotation, (int, float))):
        raise ValueError(f"{where}: rotation_deg must be a number")
    tags, flags = raw.get("style_tags", []), raw.get("flags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise ValueError(f"{where}: style_tags must be a list of text")
    if not isinstance(flags, list) or not all(f in FLAGS for f in flags):
        raise ValueError(f"{where}: unknown flag")
    if not isinstance(raw["filters"], list):
        raise ValueError(f"{where}: filters must be a list")
    filters, clamped = [], []
    for index, item in enumerate(raw["filters"]):
        fwhere = f"{where} filter #{index + 1}"
        if not isinstance(item, dict) or set(item) - {"path", "enabled", "settings"} \
                or not isinstance(item.get("path"), str) or not isinstance(item.get("settings"), dict):
            raise ValueError(f"{fwhere}: needs path, enabled, settings")
        spec = catalog.get(item["path"])
        if spec is None:
            raise ValueError(f"{fwhere}: unknown filter path {item['path']!r}")
        enabled = item.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise ValueError(f"{fwhere}: enabled must be true/false/null")
        settings = {}
        for name, value in item["settings"].items():
            used, was_clamped = _bounded(spec, str(name), value, fwhere)
            settings[str(name)] = used
            if was_clamped:
                clamped.append((spec.path, str(name), value, used))
        filters.append(PresetFilter(spec.path, enabled, settings))
    return CommunityPreset(
        id=pid, player=raw.get("player"), attribution=attribution, tablet=raw.get("tablet"),
        area_mm=_area(raw.get("area_mm"), where), values_area_mm=_area(raw.get("values_area_mm"), where),
        rotation_deg=float(rotation) if rotation is not None else None, filters=tuple(filters),
        style_tags=tuple(tags), source_url=source, source_date=raw.get("source_date"),
        retrieved=raw["retrieved"], confidence=raw["confidence"], flags=tuple(flags),
        notes=raw.get("notes") or "", clamped=tuple(clamped))


def parse_presets(doc: Any, catalog: dict[str, FilterSpec] | None = None) -> PresetBundle:
    """Validate a decoded presets document.

    Raises :class:`PresetsError` for a document-level problem; bad entries are
    skipped and reported in ``warnings``.
    """
    if not isinstance(doc, dict):
        raise PresetsError("presets document is not an object")
    if set(doc) - _DOC_KEYS:
        raise PresetsError(f"unknown top-level keys {sorted(set(doc) - _DOC_KEYS)}")
    if doc.get("schema") != SCHEMA:
        raise PresetsError(f"unsupported schema {doc.get('schema')!r} (expected {SCHEMA})")
    version = doc.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise PresetsError("version must be a positive integer")
    if not _is_date(doc.get("generated")):
        raise PresetsError("generated must be YYYY-MM-DD")
    entries = doc.get("presets")
    if not isinstance(entries, list) or len(entries) > MAX_PRESETS:
        raise PresetsError("presets must be a list")
    catalog = load_catalog() if catalog is None else catalog
    presets: list[CommunityPreset] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for raw in entries:
        try:
            preset = parse_entry(raw, catalog)
        except ValueError as exc:
            warnings.append(f"skipped: {exc}")
            continue
        if preset.id in seen:
            warnings.append(f"skipped: duplicated id {preset.id!r}")
            continue
        seen.add(preset.id)
        presets.append(preset)
        warnings += [f"{preset.id}: {path}.{name} {value!r} clamped to {used!r}"
                     for path, name, value, used in preset.clamped]
    excluded = []
    for item in doc.get("excluded") or ():
        if isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("reason"), str):
            excluded.append((item["id"], item["reason"]))
    return PresetBundle(version, doc["generated"], tuple(presets), tuple(warnings), tuple(excluded))


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
def packaged_document() -> Any:
    """The decoded ``community_presets.json`` shipped with the program."""
    text = resources.files(__package__).joinpath("community_presets.json").read_text(encoding="utf-8")
    return json.loads(text)


@lru_cache(maxsize=1)
def _packaged_bundle() -> PresetBundle:
    try:
        return parse_presets(packaged_document())
    except (OSError, ValueError) as exc:   # JSONDecodeError and PresetsError are ValueErrors
        return replace_warnings(EMPTY, (f"packaged presets unusable: {exc}",))


def replace_warnings(bundle: PresetBundle, warnings: tuple[str, ...]) -> PresetBundle:
    """``bundle`` with extra warnings appended."""
    return PresetBundle(bundle.version, bundle.generated, bundle.presets, bundle.warnings + warnings,
                        bundle.excluded)


_override: PresetBundle | None = None


def set_presets_override(bundle: PresetBundle | None) -> None:
    """Use a validated downloaded bundle instead of the packaged one (None = packaged)."""
    global _override
    _override = bundle


def load_presets() -> PresetBundle:
    """The active presets (downloaded and validated if installed, else packaged). Never raises."""
    return _override if _override is not None else _packaged_bundle()


@dataclass(frozen=True)
class DataStatus:
    """What the Welcome / Settings screens show about the community data."""
    generated: str
    preset_count: int
    source_count: int
    origin: str = "bundled"          # "bundled" | "downloaded"
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def age_days(self, today: date | None = None) -> int | None:
        """Days since ``generated`` (None if unknown)."""
        if not _is_date(self.generated):
            return None
        return ((today or date.today()) - date.fromisoformat(self.generated)).days


def data_status(bundle: PresetBundle | None = None, origin: str | None = None) -> DataStatus:
    """Status of ``bundle`` (default: the active one)."""
    bundle = bundle or load_presets()
    return DataStatus(bundle.generated, len(bundle.presets), bundle.source_count,
                      origin or ("downloaded" if _override is not None else "bundled"), bundle.warnings)
