"""knowledge.updater with a fake fetcher (no network) + tools/refresh_data."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from osu_tablet_assistant.knowledge import filters as kfilters
from osu_tablet_assistant.knowledge import presets as kpresets
from osu_tablet_assistant.knowledge import updater as up

ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE = ROOT / "osu_tablet_assistant" / "knowledge"
BASE = "https://raw.githubusercontent.com/leonardoulysses92-star/Osu-Open-Tablet/main/data/"


def _bundle(version: int = 2, presets_doc: dict | None = None, filters_doc: dict | None = None
            ) -> dict[str, bytes]:
    presets_doc = presets_doc or json.loads((KNOWLEDGE / up.PRESETS_FILE).read_text(encoding="utf-8"))
    presets_doc = dict(presets_doc, version=version, generated="2026-12-01")
    presets_doc["presets"] = presets_doc["presets"][:5]
    files = {up.PRESETS_FILE: json.dumps(presets_doc).encode(),
             up.FILTERS_FILE: json.dumps(filters_doc or kfilters.packaged_document()).encode()}
    manifest = {"schema": 1, "version": version, "date": "2026-12-01",
                "files": {n: {"sha256": hashlib.sha256(d).hexdigest(), "size": len(d)} for n, d in files.items()}}
    files[up.MANIFEST] = json.dumps(manifest).encode()
    return files


class FakeFetcher:
    def __init__(self, files: dict[str, bytes] | None = None, error: up.FetchError | None = None) -> None:
        self.files, self.error, self.urls = files or {}, error, []

    def __call__(self, url: str, max_bytes: int, timeout: float) -> bytes:
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        name = url.rsplit("/", 1)[-1]
        if name not in self.files:
            raise up.FetchError("http", f"HTTP 404 for {url}")
        data = self.files[name]
        if len(data) > max_bytes:
            raise up.FetchError("too_large", url)
        return data


class UpdaterTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name) / "data"
        self.addCleanup(up.activate, None)    # never leak an override into other tests

    def test_good_bundle_is_cached_and_activated(self) -> None:
        fetch = FakeFetcher(_bundle())
        result = up.update(BASE, self.cache, fetch)
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.key, "core.update.updated")
        self.assertEqual(result.params["presets"], 5)
        self.assertEqual(len(kpresets.load_presets().presets), 5)
        self.assertEqual(up.status(result.bundle)["origin"], "downloaded")
        self.assertTrue((self.cache / up.MANIFEST).is_file())
        # no telemetry: only the three files, plain GETs without query strings
        self.assertEqual(fetch.urls, [BASE + n for n in (up.MANIFEST, *up.FILES)])
        up.activate(None)
        self.assertGreater(len(kpresets.load_presets().presets), 5)
        self.assertIsNotNone(up.activate_cache(self.cache))       # next start uses the cache
        self.assertEqual(len(kpresets.load_presets().presets), 5)
        again = up.update(BASE, self.cache, FakeFetcher(_bundle()))
        self.assertEqual(again.key, "core.update.up_to_date")

    def test_same_version_as_bundled_is_up_to_date(self) -> None:
        fetch = FakeFetcher(_bundle(version=1))
        result = up.update(BASE, self.cache, fetch)
        self.assertEqual(result.key, "core.update.up_to_date")
        self.assertEqual(fetch.urls, [BASE + up.MANIFEST])

    def _fails(self, files: dict[str, bytes] | None, key: str, error: up.FetchError | None = None) -> None:
        before = kpresets.load_presets()
        result = up.update(BASE, self.cache, FakeFetcher(files, error))
        self.assertFalse(result.ok)
        self.assertEqual(result.key, key)
        self.assertIs(kpresets.load_presets(), before)            # bundled data stays in use
        self.assertFalse((self.cache / up.MANIFEST).exists())

    def test_corrupt_checksum(self) -> None:
        files = _bundle()
        files[up.PRESETS_FILE] = files[up.PRESETS_FILE].replace(b'"high"', b'"HIGH"', 1)[:-1] + b" "
        self._fails(files, "core.update.error.checksum")

    def test_truncated(self) -> None:
        files = _bundle()
        files[up.FILTERS_FILE] = files[up.FILTERS_FILE][:100]
        self._fails(files, "core.update.error.truncated")

    def test_404_offline_too_large(self) -> None:
        files = _bundle()
        del files[up.FILTERS_FILE]
        self._fails(files, "core.update.error.http")
        self._fails(None, "core.update.error.offline", up.FetchError("offline", "no route"))
        big = _bundle()
        big[up.MANIFEST] = b" " * (up.MAX_MANIFEST_BYTES + 1)
        self._fails(big, "core.update.error.too_large")

    def test_schema_mismatch(self) -> None:
        files = _bundle()
        manifest = json.loads(files[up.MANIFEST])
        manifest["schema"] = 99
        files[up.MANIFEST] = json.dumps(manifest).encode()
        self._fails(files, "core.update.error.schema")
        presets_doc = json.loads((KNOWLEDGE / up.PRESETS_FILE).read_text(encoding="utf-8"))
        presets_doc["schema"] = 2
        self._fails(_bundle(presets_doc=presets_doc), "core.update.error.schema")

    def test_unknown_plugin_path_rejected(self) -> None:
        doc = kfilters.packaged_document()
        evil = dict(doc["filters"][0], path="Evil.Plugin")
        doc["filters"].append(evil)
        self._fails(_bundle(filters_doc=doc), "core.update.error.unknown_path")

    def test_only_https(self) -> None:
        result = up.update("http://example.org/data/", self.cache, FakeFetcher(_bundle()))
        self.assertEqual(result.key, "core.update.error.url")
        with self.assertRaises(up.FetchError):
            up.urllib_fetch("file:///etc/passwd", 10, 1.0)

    def test_corrupt_cache_falls_back_to_bundled(self) -> None:
        self.assertTrue(up.update(BASE, self.cache, FakeFetcher(_bundle())).ok)
        (self.cache / up.PRESETS_FILE).write_bytes(b"{}")
        up.activate(None)
        self.assertIsNone(up.activate_cache(self.cache))
        self.assertGreater(len(kpresets.load_presets().presets), 5)

    def test_status_staleness(self) -> None:
        s = up.status(today=date(2026, 11, 15))
        self.assertEqual((s["origin"], s["stale"], s["age_days"]), ("bundled", False, 16))
        self.assertTrue(up.status(today=date(2027, 3, 1))["stale"])


class RefreshDataTests(unittest.TestCase):
    def setUp(self) -> None:
        spec = importlib.util.spec_from_file_location("refresh_data", ROOT / "tools" / "refresh_data.py")
        self.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tool)

    def test_repo_data_folder_matches_knowledge_files(self) -> None:
        self.assertTrue(self.tool.is_current(), "run: python tools/refresh_data.py")

    def test_write_produces_a_valid_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = self.tool.write(Path(tmp))
            files = {n: (Path(tmp) / n).read_bytes() for n in up.FILES}
            bundle = up.validate_bundle(manifest.read_bytes(), files)
            self.assertEqual(bundle.manifest.version, kpresets.load_presets().version)


if __name__ == "__main__":
    unittest.main()
