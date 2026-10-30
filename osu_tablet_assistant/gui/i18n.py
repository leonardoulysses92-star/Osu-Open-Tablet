"""Tiny i18n layer: flat ``{"key": "text"}`` JSON files per language.

Every ``locales/<lang>/*.json`` is loaded and merged. A key missing in the
current language falls back to English, then to the key itself.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SUPPORTED = ("es", "en")
_FALLBACK = "en"
_LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"

_language = "es"
_catalogs: dict[str, dict[str, str]] = {}


def load_catalog(lang: str, root: Path | None = None) -> dict[str, str]:
    """Merge every JSON file of one language (unreadable files are skipped)."""
    merged: dict[str, str] = {}
    for path in sorted(((root or _LOCALES_DIR) / lang).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            merged.update({str(k): str(v) for k, v in data.items()})
    return merged


def _catalog(lang: str) -> dict[str, str]:
    if lang not in _catalogs:
        _catalogs[lang] = load_catalog(lang)
    return _catalogs[lang]


def reload() -> None:
    """Drop the cache so locale files are read again."""
    _catalogs.clear()


def set_language(lang: str) -> None:
    """Select the UI language; raises ValueError for unsupported codes."""
    global _language
    if lang not in SUPPORTED:
        raise ValueError(f"unsupported language: {lang!r}")
    _language = lang


def current_language() -> str:
    """Code of the language selected with :func:`set_language`."""
    return _language


def t(key: str, **params: Any) -> str:
    """Translate ``key``; ``str.format`` placeholders are filled from ``params``."""
    text = _catalog(_language).get(key)
    if text is None:
        text = _catalog(_FALLBACK).get(key)
    if text is None:
        return key
    if not params:
        return text
    try:
        return text.format(**params)
    except (KeyError, IndexError, ValueError):
        return text


def tr(reason: Any) -> str:
    """Translate a ``models.Reason`` (anything with ``key`` and ``params``)."""
    return t(reason.key, **reason.params)


def missing_keys(root: Path | None = None) -> dict[str, set[str]]:
    """Keys each language lacks compared with the union of all languages."""
    catalogs = {lang: set(load_catalog(lang, root)) for lang in SUPPORTED}
    union = set().union(*catalogs.values())
    return {lang: union - keys for lang, keys in catalogs.items()}
