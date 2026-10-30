"""Keep the community data fresh: a manifest-based, data-only bundle.

Remote layout (default: the project's GitHub ``data/`` folder)::

    manifest.json            {"schema": 1, "version": N, "date": "YYYY-MM-DD",
                              "files": {"community_presets.json": {"sha256": ..., "size": ...},
                                        "filters.json": {...}}}
    community_presets.json
    filters.json

Rules: https only, a timeout and a size cap per file, the manifest's
SHA-256 and size must match, both documents must pass the same strict
validators as the packaged data, and a downloaded catalog may not introduce a
filter path the packaged catalog does not know (the bundle is *data*: it can
never make the program use a plugin it was not written for). Nothing is ever
executed. A valid bundle is cached in the app data dir and activated; on any
problem the cache or the packaged data stays in use. No telemetry: the only
requests are GETs of these three files, without query strings or identifiers.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from . import filters as kfilters
from . import presets as kpresets

SCHEMA = 1
MANIFEST = "manifest.json"
PRESETS_FILE = "community_presets.json"
FILTERS_FILE = "filters.json"
FILES = (PRESETS_FILE, FILTERS_FILE)
MAX_MANIFEST_BYTES = 64 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
TIMEOUT_S = 10.0
STALE_DAYS = 90
CACHE_DIR_NAME = "data"
_SHA = re.compile(r"^[0-9a-f]{64}$")


class FetchError(Exception):
    """A download failed (``reason``: offline | http | too_large | refused)."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


class BundleError(ValueError):
    """A downloaded or cached bundle is invalid (``reason`` is a short code)."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


Fetcher = Callable[[str, int, float], bytes]


def _https(url: str) -> bool:
    parts = urlparse(url)
    return parts.scheme == "https" and bool(parts.netloc) and not parts.query and not parts.fragment


def urllib_fetch(url: str, max_bytes: int, timeout: float) -> bytes:
    """GET ``url`` (https only) and return at most ``max_bytes`` bytes."""
    if not _https(url):
        raise FetchError("refused", f"not an https URL: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "osu-open-tablet"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:   # noqa: S310 (https checked)
            if not _https(response.geturl()):
                raise FetchError("refused", f"redirected to {response.geturl()}")
            data = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise FetchError("http", f"HTTP {exc.code} for {url}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise FetchError("offline", f"{type(exc).__name__}: {exc}") from exc
    if len(data) > max_bytes:
        raise FetchError("too_large", f"{url} is larger than {max_bytes} bytes")
    return data


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Manifest:
    version: int
    date: str
    files: dict[str, tuple[str, int]]   # name -> (sha256, size)


def parse_manifest(raw: bytes) -> Manifest:
    """Validate ``manifest.json``; raises BundleError."""
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BundleError("manifest", f"manifest is not JSON: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise BundleError("schema", f"unsupported manifest schema: {doc.get('schema') if isinstance(doc, dict) else doc!r}")
    version, when, files = doc.get("version"), doc.get("date"), doc.get("files")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise BundleError("manifest", "manifest version must be a positive integer")
    try:
        date.fromisoformat(when)
    except (TypeError, ValueError) as exc:
        raise BundleError("manifest", "manifest date must be YYYY-MM-DD") from exc
    if not isinstance(files, dict) or set(files) != set(FILES):
        raise BundleError("manifest", f"manifest must list exactly {FILES}")
    out = {}
    for name in FILES:
        entry = files[name]
        if not isinstance(entry, dict) or not isinstance(entry.get("sha256"), str) \
                or not _SHA.match(entry["sha256"]) or isinstance(entry.get("size"), bool) \
                or not isinstance(entry.get("size"), int) or not 0 < entry["size"] <= MAX_FILE_BYTES:
            raise BundleError("manifest", f"bad manifest entry for {name}")
        out[name] = (entry["sha256"], entry["size"])
    return Manifest(version, when, out)


@dataclass(frozen=True)
class Bundle:
    """A validated bundle, ready to activate."""
    manifest: Manifest
    filters_doc: dict[str, Any]
    presets: kpresets.PresetBundle
    raw: dict[str, bytes] = field(repr=False, default_factory=dict)


def validate_bundle(manifest_raw: bytes, files: dict[str, bytes]) -> Bundle:
    """Checksums, sizes, schemas and the no-new-plugin-path rule; raises BundleError."""
    manifest = parse_manifest(manifest_raw)
    for name, (sha, size) in manifest.files.items():
        data = files.get(name)
        if data is None:
            raise BundleError("missing", f"{name} missing")
        if len(data) != size:
            raise BundleError("truncated", f"{name}: {len(data)} bytes, manifest says {size}")
        if hashlib.sha256(data).hexdigest() != sha:
            raise BundleError("checksum", f"{name}: SHA-256 mismatch")
    try:
        filters_doc = json.loads(files[FILTERS_FILE].decode("utf-8"))
        catalog = kfilters.parse_catalog(filters_doc)
    except (UnicodeDecodeError, ValueError) as exc:
        raise BundleError("schema", f"filters.json invalid: {exc}") from exc
    known = set(kfilters.parse_catalog(kfilters.packaged_document()))
    unknown = set(catalog) - known
    if unknown:
        raise BundleError("unknown_path", f"filters.json adds unknown plugin paths: {sorted(unknown)}")
    try:
        presets = kpresets.parse_presets(json.loads(files[PRESETS_FILE].decode("utf-8")), catalog)
    except (UnicodeDecodeError, ValueError) as exc:
        raise BundleError("schema", f"community_presets.json invalid: {exc}") from exc
    skipped = [w for w in presets.warnings if w.startswith("skipped")]
    if skipped:   # strict: a published bundle must not contain entries we would drop
        raise BundleError("schema", "; ".join(skipped[:5]))
    if presets.version != manifest.version or presets.generated != manifest.date:
        raise BundleError("manifest", "manifest version/date do not match community_presets.json")
    return Bundle(manifest, filters_doc, presets, dict(files))


# --------------------------------------------------------------------------
# Cache and activation
# --------------------------------------------------------------------------
def activate(bundle: Bundle | None) -> None:
    """Make ``bundle`` the data the program uses (None = back to the packaged data)."""
    if bundle is None:
        kfilters.set_catalog_override(None)
        kpresets.set_presets_override(None)
        return
    kfilters.set_catalog_override(bundle.filters_doc)
    kpresets.set_presets_override(bundle.presets)


def _write_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def save_cache(cache_dir: Path, bundle: Bundle, manifest_raw: bytes) -> None:
    """Write the bundle files, then the manifest last (a half-written cache fails its checksums)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        _write_atomic(cache_dir / name, bundle.raw[name])
    _write_atomic(cache_dir / MANIFEST, manifest_raw)


def load_cache(cache_dir: Path) -> Bundle | None:
    """The cached bundle if it is complete and still valid, else None (never raises)."""
    try:
        manifest_raw = (cache_dir / MANIFEST).read_bytes()
        files = {name: (cache_dir / name).read_bytes() for name in FILES}
        return validate_bundle(manifest_raw, files)
    except (OSError, BundleError):
        return None


def activate_cache(cache_dir: Path) -> Bundle | None:
    """At startup: use a valid cache if there is one (packaged data otherwise)."""
    bundle = load_cache(cache_dir)
    activate(bundle)
    return bundle


# --------------------------------------------------------------------------
# Update
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class UpdateResult:
    """Outcome for the GUI: ``key``/``params`` form a translated message."""
    ok: bool
    key: str
    params: dict[str, Any] = field(default_factory=dict)
    detail: str = ""
    bundle: Bundle | None = None


def _join(base: str, name: str) -> str:
    return base.rstrip("/") + "/" + name


def update(base_url: str, cache_dir: Path, fetcher: Fetcher = urllib_fetch) -> UpdateResult:
    """Download, validate, cache and activate the bundle at ``base_url``. Never raises."""
    if not _https(base_url):
        return UpdateResult(False, "core.update.error.url", {"url": base_url})
    current = load_cache(cache_dir)
    packaged_version = kpresets.parse_presets(kpresets.packaged_document()).version
    current_version = current.manifest.version if current else packaged_version
    try:
        manifest_raw = fetcher(_join(base_url, MANIFEST), MAX_MANIFEST_BYTES, TIMEOUT_S)
        manifest = parse_manifest(manifest_raw)
        if manifest.version <= current_version:   # nothing newer than what we already have
            activate(current)
            when = current.manifest.date if current else kpresets.load_presets().generated
            return UpdateResult(True, "core.update.up_to_date", {"date": when}, bundle=current)
        files = {name: fetcher(_join(base_url, name), MAX_FILE_BYTES, TIMEOUT_S) for name in FILES}
        bundle = validate_bundle(manifest_raw, files)
    except FetchError as exc:
        return UpdateResult(False, f"core.update.error.{exc.reason}", detail=exc.detail)
    except BundleError as exc:
        return UpdateResult(False, f"core.update.error.{exc.reason}", detail=exc.detail)
    try:
        save_cache(cache_dir, bundle, manifest_raw)
    except OSError as exc:
        activate(bundle)   # usable for this run even if it could not be cached
        return UpdateResult(True, "core.update.not_cached", {"date": manifest.date}, f"{exc}", bundle)
    activate(bundle)
    return UpdateResult(True, "core.update.updated", {
        "date": manifest.date, "presets": len(bundle.presets.presets),
        "sources": bundle.presets.source_count}, bundle=bundle)


def status(bundle: Bundle | None = None, today: date | None = None) -> dict[str, Any]:
    """What the GUI shows: date, preset and source counts, origin, staleness."""
    if bundle is not None:
        when, presets, origin = bundle.manifest.date, bundle.presets, "downloaded"
    else:
        presets = kpresets.load_presets()
        when, origin = presets.generated, kpresets.data_status(presets).origin
    try:
        age = ((today or date.today()) - date.fromisoformat(when)).days
    except (TypeError, ValueError):
        age = None
    return {"date": when or "-", "presets": len(presets.presets), "sources": presets.source_count,
            "origin": origin, "age_days": age, "stale": age is not None and age > STALE_DAYS}
