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
STAGES = ("raw", "pre", "post")          # OTD pipeline position, in execution order
CONFIDENCES = ("high", "medium", "low")


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

    def clamp(self, value: Any) -> Any:
        """Coerce ``value`` to this param's type, bounds and step grid."""
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
        if self.step:   # grid of multiples of step; bounds win over the grid
            number = round(number / self.step) * self.step
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

    ``summary_key`` and ``added_latency_note`` are i18n keys. ``advanced`` and
    ``stage`` extend the architecture contract (both have defaults).
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
                advanced=bool(raw.get("advanced", False)), stage=str(raw.get("stage", "pre")))
            if spec.confidence not in CONFIDENCES or spec.stage not in STAGES:
                raise ValueError(f"{path}: invalid confidence or stage")
            if not spec.sources:
                raise ValueError(f"{path}: at least one source is required")
            if path in catalog:
                raise ValueError(f"{path}: duplicated path")
            catalog[path] = spec
    except (KeyError, TypeError) as exc:
        raise ValueError(f"malformed filter catalog: {exc!r}") from exc
    return catalog


@lru_cache(maxsize=1)
def _packaged_catalog() -> tuple[tuple[str, FilterSpec], ...]:
    text = resources.files(__package__).joinpath("filters.json").read_text(encoding="utf-8")
    return tuple(parse_catalog(json.loads(text)).items())


def load_catalog() -> dict[str, FilterSpec]:
    """The packaged filter catalog keyed by OTD Path (a fresh dict per call)."""
    return dict(_packaged_catalog())
