"""Catalog of the OTD filters this project can recommend.

The data lives in ``filters.json`` (packaged next to this module) and is keyed
by the OTD ``Path``. Every entry records where its facts come from
(``sources``) and how sure we are about them (``confidence``).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

KINDS = ("float", "int", "bool", "enum")
# OTD pipeline position, in execution order. "raw" = OTD 0.6.8 ``Internal``; in
# 0.6.x ``PipelinePosition.Raw`` is an alias of PreTransform, so those are "pre".
# Inside a stage OTD keeps the order of the profile's Filters list.
STAGES = ("raw", "pre", "post")
CONFIDENCES = ("high", "medium", "low")
# Finer grids for live fine-tuning (``engine.adapt`` step levels): catalog step / divisor, never below
# GRID_FLOOR. FINEST_GRID_DIVISOR is the finest grid any automatic move may use (``otd.livetune`` checks it).
GRID_FLOOR = 0.0001
FINEST_GRID_DIVISOR = 10


def grid_decimals(grid: float) -> int:
    """Decimals that show a value on ``grid`` exactly (0.5 -> 1, 0.05 -> 2, 0.002 -> 3)."""
    return max(0, -math.floor(math.log10(grid) + 1e-9)) if grid > 0 else 6


@dataclass(frozen=True)
class FilterParam:
    """One OTD property of a filter, with sensible osu! tuning bounds."""
    name: str
    kind: str                            # "float" | "int" | "bool" | "enum"
    default: Any
    min: float | None = None
    max: float | None = None
    step: float | None = None
    unit: str = ""
    choices: tuple[str, ...] = ()        # only for kind == "enum"

    def grid(self, divisor: float = 1) -> float | None:
        """Grid for automatic moves: the catalog step divided by ``divisor`` (1 = the catalog step).

        Ints keep whole steps (at least 1). Floats never go below :data:`GRID_FLOOR` (or the catalog
        step when that is already finer). None when the param has no grid.
        """
        if self.kind == "int":
            return float(max(1, round(self.step or 1)))
        if self.kind != "float" or not self.step:
            return None
        fine = self.step / max(float(divisor), 1.0)
        return float(f"{max(fine, min(self.step, GRID_FLOOR)):.6g}")

    def clamp(self, value: Any, grid: float | None = None) -> Any:
        """Coerce ``value`` to this param's type, bounds and step grid (``grid`` overrides the catalog step)."""
        if self.kind == "bool":
            return bool(value)
        if self.kind == "enum":
            return value if value in self.choices else self.default
        try:
            number = float(value)
        except (TypeError, ValueError):
            return self.default
        if not math.isfinite(number):
            return self.default
        step = self.step if grid is None else grid
        if step:   # grid of multiples of step; bounds win over the grid
            number = round(number / step) * step
            if grid is not None:
                number = round(number, grid_decimals(step))
        if self.min is not None:
            number = max(number, self.min)
        if self.max is not None:
            number = min(number, self.max)
        if self.kind == "int":
            return int(round(number))
        return round(number, 6)


@dataclass(frozen=True)
class FilterSpec:
    """Everything the engine knows about one filter.

    ``summary_key``, ``added_latency_note`` and ``policy_note`` are i18n keys.
    ``advanced``, ``stage``, ``auto_recommend``, ``policy_note`` and
    ``homepage`` extend the architecture contract (all have defaults).
    ``auto_recommend`` False means the engine never picks the filter on its own
    (prediction / input-assist risk, or not a smoothing filter): user decision only.
    """
    path: str
    display_name: str
    plugin_name: str
    params: tuple[FilterParam, ...]
    added_latency_note: str
    best_for: tuple[str, ...]
    summary_key: str
    sources: tuple[str, ...]
    confidence: str
    advanced: bool = False
    stage: str = "pre"
    auto_recommend: bool = True
    policy_note: str = ""
    homepage: str = ""

    def param(self, name: str) -> FilterParam | None:
        """The param called ``name``, or None."""
        return next((p for p in self.params if p.name == name), None)

    def defaults(self) -> dict[str, Any]:
        """Property -> plugin default, in catalog order."""
        return {p.name: p.default for p in self.params}


def _parse_param(raw: dict[str, Any], path: str) -> FilterParam:
    kind = raw["kind"]
    if kind not in KINDS:
        raise ValueError(f"{path}.{raw.get('name')}: unknown kind {kind!r}")
    param = FilterParam(
        name=str(raw["name"]), kind=kind, default=raw["default"],
        min=raw.get("min"), max=raw.get("max"), step=raw.get("step"),
        unit=str(raw.get("unit", "")), choices=tuple(raw.get("choices", ())))
    if kind in ("float", "int"):
        if param.min is None or param.max is None or param.min > param.max:
            raise ValueError(f"{path}.{param.name}: invalid bounds")
        if isinstance(param.default, bool) or not isinstance(param.default, (int, float)) \
                or not param.min <= param.default <= param.max:
            raise ValueError(f"{path}.{param.name}: default outside bounds")
    elif kind == "bool" and not isinstance(param.default, bool):
        raise ValueError(f"{path}.{param.name}: bool default expected")
    elif kind == "enum" and param.default not in param.choices:
        raise ValueError(f"{path}.{param.name}: default not among choices")
    return param


def parse_catalog(data: dict[str, Any]) -> dict[str, FilterSpec]:
    """Validate the decoded ``filters.json`` document; raises ValueError."""
    try:
        entries = data["filters"]
        catalog: dict[str, FilterSpec] = {}
        for raw in entries:
            path = str(raw["path"])
            params = tuple(_parse_param(p, path) for p in raw["params"])
            if len({p.name for p in params}) != len(params):
                raise ValueError(f"{path}: duplicated param name")
            spec = FilterSpec(
                path=path, display_name=str(raw["display_name"]),
                plugin_name=str(raw["plugin_name"]), params=params,
                added_latency_note=str(raw["added_latency_note"]),
                best_for=tuple(raw["best_for"]), summary_key=str(raw["summary_key"]),
                sources=tuple(raw["sources"]), confidence=str(raw["confidence"]),
                advanced=bool(raw.get("advanced", False)), stage=str(raw.get("stage", "pre")),
                auto_recommend=raw.get("auto_recommend", True),
                policy_note=str(raw.get("policy_note") or ""), homepage=str(raw.get("homepage") or ""))
            if spec.confidence not in CONFIDENCES or spec.stage not in STAGES:
                raise ValueError(f"{path}: invalid confidence or stage")
            if not isinstance(spec.auto_recommend, bool):
                raise ValueError(f"{path}: auto_recommend must be a bool")
            if not spec.sources:
                raise ValueError(f"{path}: at least one source is required")
            if not all(isinstance(s, str) and s.startswith("https://") for s in spec.sources) \
                    or (spec.homepage and not spec.homepage.startswith("https://")):
                raise ValueError(f"{path}: sources and homepage must be https URLs")
            if path in catalog:
                raise ValueError(f"{path}: duplicated path")
            catalog[path] = spec
    except (KeyError, TypeError) as exc:
        raise ValueError(f"malformed filter catalog: {exc!r}") from exc
    return catalog


@lru_cache(maxsize=1)
def _packaged_catalog() -> tuple[tuple[str, FilterSpec], ...]:
    return tuple(parse_catalog(packaged_document()).items())


def packaged_document() -> dict[str, Any]:
    """The decoded ``filters.json`` shipped with the program."""
    text = resources.files(__package__).joinpath("filters.json").read_text(encoding="utf-8")
    return json.loads(text)


_override: tuple[tuple[str, FilterSpec], ...] | None = None


def set_catalog_override(data: dict[str, Any] | None) -> None:
    """Use a validated downloaded ``filters.json`` instead of the packaged one (None = packaged).

    Only ``knowledge.updater`` calls this, after checksum and schema checks;
    paths unknown to the packaged catalog are rejected there (data only).
    Raises ValueError when ``data`` is not a valid catalog.
    """
    global _override
    _override = None if data is None else tuple(parse_catalog(data).items())


def load_catalog() -> dict[str, FilterSpec]:
    """The active filter catalog keyed by OTD Path (a fresh dict per call)."""
    return dict(_override if _override is not None else _packaged_catalog())
