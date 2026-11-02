"""engine.mapindex: osu!.db lookups, the Songs delta, the lazer store and the finder (synthetic installs)."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from osu_tablet_assistant.engine import beatmap as bm
from osu_tablet_assistant.engine import mapindex as mi
from osu_tablet_assistant.engine import osudb
from osubuild import DbMap, make_osudb, osu_text


class Install:
    """A fake osu!stable folder: Songs with .osu files, and an osu!.db listing some of them."""

    def __init__(self, test: unittest.TestCase, with_db: bool = True) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="osu-index-")
        test.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "osu!"
        self.songs = self.root / "Songs"
        self.songs.mkdir(parents=True)
        (self.root / "osu!.exe").write_bytes(b"")
        self.cache = Path(tmp.name) / "cache" / "idx.json"
        self.maps: list[DbMap] = []
        self.add("1 Artist - Song", "Artist - Song (me) [Hard].osu", osu_text(version="Hard"))
        self.add("2 xi - Blue Zenith", "xi - Blue Zenith [FOUR DIMENSIONS].osu",
                 osu_text("xi", "Blue Zenith", "FOUR DIMENSIONS", title_unicode="ブルーゼニス"),
                 title_unicode="ブルーゼニス")
        self.add("3 Artist - Song (copy)", "copy.osu", osu_text(version="Hard"), modified=1_695_000_000.0)
        if with_db:
            self.write_db()

    def add(self, folder: str, name: str, text: str, in_db: bool = True, **kw) -> Path:
        path = self.songs / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if in_db:
            header = bm.read_header(path)
            self.maps.append(DbMap(artist=header["Artist"], title=header["Title"], version=header["Version"],
                                   folder=folder, osu_file=name, md5=hashlib.md5(path.read_bytes()).hexdigest(),
                                   **kw))
        return path

    def write_db(self, version: int = 20250108) -> None:
        db = self.root / "osu!.db"
        db.write_bytes(make_osudb(self.maps, version=version))
        past = time.time() - 3600                      # written an hour ago: the folders above are "older"
        os.utime(db, (past, past))
        for folder in self.songs.iterdir():
            os.utime(folder, (past - 60, past - 60))


class MapIndexTests(unittest.TestCase):
    def test_exact_unicode_loose_and_md5(self) -> None:
        inst = Install(self)
        index = mi.MapIndex(inst.root, cache_path=inst.cache)
        self.assertTrue(index.refresh())
        self.assertTrue(index.ready and index.complete)
        hits = index.lookup("osu!  - xi - Blue Zenith [FOUR DIMENSIONS]")
        self.assertEqual([Path(h.path).name for h in hits], ["xi - Blue Zenith [FOUR DIMENSIONS].osu"])
        self.assertEqual(index.lookup("osu!  - xi - ブルーゼニス [FOUR DIMENSIONS]"), hits)       # unicode title
        self.assertEqual(index.lookup("osu! - XI  -  blue zenith [four dimensions]"), hits)  # spacing / case
        self.assertEqual(index.lookup("osu!  - xi - Blue-Zenith! [FOUR_DIMENSIONS]"), hits)  # loose second try
        self.assertEqual(index.lookup("xi - Blue Zenith [FOUR DIMENSIONS]"), hits)          # bare key works
        self.assertEqual(index.lookup("osu!"), [])
        self.assertEqual(index.by_md5(hits[0].md5.upper()), hits[0])
        self.assertEqual(hits[0].source, "osudb")
        self.assertEqual(bm.find_beatmap("osu!  - xi - Blue Zenith [FOUR DIMENSIONS]", index).version,
                         "FOUR DIMENSIONS")                                                # old API takes it

    def test_ambiguous_titles_give_every_candidate(self) -> None:
        index = mi.MapIndex(Install(self).root)
        index.refresh()
        hits = index.lookup("osu!  - Artist - Song [Hard]")
        self.assertEqual(len(hits), 2)
        self.assertEqual(Path(index.find(bm.parse_window_title("osu!  - Artist - Song [Hard]")).path).name,
                         "copy.osu")                                           # the most recently modified first

    def test_reloads_only_when_the_db_changes(self) -> None:
        inst = Install(self)
        calls: list[Path] = []

        def loader(path: Path):
            calls.append(path)
            return osudb.load_osudb(path)

        index = mi.MapIndex(inst.root, loader=loader)
        self.assertTrue(index.refresh())
        self.assertFalse(index.refresh())
        self.assertEqual(len(calls), 1)
        inst.add("4 New - One", "n.osu", osu_text("New", "One", "Easy"))
        inst.write_db()
        self.assertTrue(index.refresh())
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(index.lookup("osu!  - New - One [Easy]")), 1)

    def test_delta_reads_only_folders_the_db_does_not_cover(self) -> None:
        inst = Install(self)
        index = mi.MapIndex(inst.root, cache_path=inst.cache)
        index.refresh()
        gen = index.generation
        self.assertEqual(index.delta(), 0)                                 # everything is in osu!.db
        new = inst.add("9 Imported - Today", "today.osu", osu_text("Imported", "Today", "Normal"), in_db=False)
        self.assertEqual(index.lookup("osu!  - Imported - Today [Normal]"), [])
        self.assertEqual(index.delta(), 1)
        hits = index.lookup("osu!  - Imported - Today [Normal]")
        self.assertEqual([h.path for h in hits], [str(new)])
        self.assertGreater(index.generation, gen)
        self.assertEqual(index.delta(), 0)                                 # nothing re-read the second time
        self.assertTrue(inst.cache.exists())                               # the header cache is shared
        self.assertEqual(index.by_md5(hashlib.md5(new.read_bytes()).hexdigest()).path, str(new))
        self.assertIsNone(index.by_md5("f" * 32))
        changed = inst.add("1 Artist - Song", "Artist - Song (me) [Insane].osu", osu_text(version="Insane"),
                           in_db=False)                                    # a known folder that changed after
        self.assertEqual(index.delta(bm.parse_window_title("osu!  - Artist - Song [Insane]")), 2)
        self.assertEqual([h.path for h in index.lookup("osu!  - Artist - Song [Insane]")], [str(changed)])

    def test_background_load_and_wait(self) -> None:
        inst = Install(self)
        index = mi.MapIndex(inst.root, background=True)
        self.assertTrue(index.refresh())
        self.assertTrue(index.wait(5.0))
        self.assertFalse(index.loading)
        self.assertEqual(index.size, 3)

    def test_unreadable_db(self) -> None:
        inst = Install(self)
        index = mi.MapIndex(inst.root, loader=lambda p: None)
        index.refresh()
        self.assertTrue(index.failed and not index.ready)
        self.assertEqual(index.lookup("osu!  - Artist - Song [Hard]"), [])


class LazerTests(unittest.TestCase):
    def store(self) -> tuple[Path, dict[str, bytes]]:
        tmp = tempfile.TemporaryDirectory(prefix="osu-lazer-")
        self.addCleanup(tmp.cleanup)
        files = Path(tmp.name) / "files"
        blobs = {
            "a": ("\ufeff" + osu_text("Lazer", "Map", "Hard")).encode("utf-8"),     # with a BOM
            "b": osu_text("Lazer", "Map", "Easy").encode("utf-8"),
            "c": b"ID3\x03\x00 not a beatmap" + bytes(range(200)),
            "d": b"\x89PNG\r\n\x1a\n" + b"\x00" * 50,
            "e": b"[Events]\nosu file format v14 but not at the start",
        }
        names = {}
        for key, data in blobs.items():
            name = hashlib.sha256(data).hexdigest()
            path = files / name[:1] / name[:2] / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            names[key] = name
        return files, {k: blobs[k] for k in blobs} | {"_names": names}   # type: ignore[dict-item]

    def test_sniff(self) -> None:
        self.assertTrue(mi.sniff_osu(b"osu file format v14\r\n"))
        self.assertTrue(mi.sniff_osu(b"\xef\xbb\xbfosu file format v7"))
        self.assertFalse(mi.sniff_osu(b"ID3osu file format v"))
        self.assertFalse(mi.sniff_osu(b""))

    def test_index_cache_and_progress(self) -> None:
        files, blobs = self.store()
        cache = files.parent / "lazer.json"
        index = mi.LazerIndex(files, cache)
        self.assertEqual(index.build(), 2)
        self.assertTrue(index.complete)
        self.assertEqual(index.progress, (5, 5))
        hits = index.lookup("osu! - Lazer - Map [Hard]")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].md5, hashlib.md5(blobs["a"]).hexdigest())     # MD5 of the file as stored
        self.assertEqual(index.by_md5(hits[0].md5), hits[0])
        data = json.loads(cache.read_text(encoding="utf-8"))
        self.assertEqual(len(data["osu"]), 2)
        self.assertEqual(len(data["other"]), 3)
        again = mi.LazerIndex(files, cache)                                    # from the cache: nothing re-read
        self.assertEqual(len(again.lookup("osu! - Lazer - Map [Easy]")), 1)
        reads: list = []
        again._read_one = lambda path: reads.append(path)                       # type: ignore[method-assign]
        again.build()
        self.assertEqual(reads, [])
        self.assertEqual(again.progress, (5, 5))

    def test_background_build_and_stop(self) -> None:
        files, _ = self.store()
        index = mi.LazerIndex(files, None, save_every=1)
        index.start()
        index.stop(timeout=5.0)
        self.assertFalse(index.running)
        index.start()                                                          # resumes where it stopped
        index._thread.join(5.0)
        self.assertTrue(index.complete)
        self.assertEqual(len(index.lookup("osu! - Lazer - Map [Hard]")), 1)

    def test_missing_store(self) -> None:
        index = mi.LazerIndex("/nonexistent/lazer/files")
        self.assertEqual(index.build(), 0)
        self.assertTrue(index.complete)

    def test_files_dir_from_appdata_and_storage_ini(self) -> None:
        self.assertIsNone(mi.lazer_files_dir({}))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(mi.lazer_files_dir({"APPDATA": tmp}), Path(tmp) / "osu" / "files")
            (Path(tmp) / "osu").mkdir()
            (Path(tmp) / "osu" / "storage.ini").write_text("FullPath = D:\\lazer-data\n", encoding="utf-8")
            self.assertEqual(mi.lazer_files_dir({"APPDATA": tmp}), Path("D:\\lazer-data") / "files")


class FinderTests(unittest.TestCase):
    def test_db_path_then_delta_for_a_new_map(self) -> None:
        inst = Install(self)
        finder = mi.MapFinder(inst.cache, background=False)
        exe = str(inst.root / "osu!.exe")
        self.assertIsNone(finder.find("osu!", exe))                    # menu: nothing, but the index is loaded
        self.assertIsNotNone(finder._db and finder._db.ready)
        found = finder.find("osu!  - xi - Blue Zenith [FOUR DIMENSIONS]", exe)
        self.assertEqual((found.version, finder.source), ("FOUR DIMENSIONS", "osudb"))
        inst.add("9 Imported - Today", "today.osu", osu_text("Imported", "Today", "Normal"), in_db=False)
        self.assertEqual(finder.find("osu!  - Imported - Today [Normal]", exe).title, "Today")
        self.assertEqual(finder.source, "songs")
        self.assertEqual(len(finder.find("osu!  - Artist - Song [Hard]", exe) and finder.candidates), 2)
        self.assertIsNone(finder.find("osu!  - Nobody - Nothing [X]", exe))
        self.assertEqual(finder.osu_dir, inst.root)

    def test_a_stale_db_entry_gives_way_to_the_next_candidate(self) -> None:
        inst = Install(self)
        (inst.songs / "3 Artist - Song (copy)" / "copy.osu").unlink()            # deleted after osu!.db was written
        finder = mi.MapFinder(None, background=False)
        found = finder.find("osu!  - Artist - Song [Hard]", str(inst.root / "osu!.exe"))
        self.assertEqual(Path(found.path).name, "Artist - Song (me) [Hard].osu")

    def test_without_db_the_old_scan_is_used(self) -> None:
        inst = Install(self, with_db=False)
        finder = mi.MapFinder(inst.cache, background=False)
        exe = str(inst.root / "osu!.exe")
        self.assertEqual(finder.find("osu!  - xi - Blue Zenith [FOUR DIMENSIONS]", exe).title, "Blue Zenith")
        self.assertEqual(finder.source, "songs")
        self.assertIsNone(finder.find("osu!  - Artist - Song [Hard]", None))

    def test_corrupt_db_falls_back_to_the_scan(self) -> None:
        inst = Install(self)
        (inst.root / "osu!.db").write_bytes(b"\x00")
        finder = mi.MapFinder(None, background=False)
        finder._db_index(inst.root)._loader = lambda p: None
        self.assertEqual(finder.find("osu!  - Artist - Song [Hard]", str(inst.root / "osu!.exe")).version, "Hard")

    def test_background_load_is_pending_then_found(self) -> None:
        inst = Install(self)
        finder = mi.MapFinder(None, background=True, wait_s=0.0)
        exe = str(inst.root / "osu!.exe")
        gate = __import__("threading").Event()
        index = finder._db_index(inst.root)
        real = index._loader
        index._loader = lambda p: (gate.wait(5.0), real(p))[1]
        gen = finder.generation
        self.assertIsNone(finder.find("osu!  - Artist - Song [Hard]", exe))
        self.assertTrue(finder.pending)
        gate.set()
        index.wait(5.0)
        self.assertGreater(finder.generation, gen)                    # the coach retries the same title then
        self.assertEqual(finder.find("osu!  - Artist - Song [Hard]", exe).version, "Hard")
        self.assertFalse(finder.pending)

    def test_lazer_store(self) -> None:
        files, _ = LazerTests.store(self)                              # type: ignore[arg-type]
        finder = mi.MapFinder(None, background=False, lazer_files=files)
        found = finder.find("osu! - Lazer - Map [Easy]", "C:/Users/x/AppData/Local/osulazer/current/osu!.exe")
        self.assertEqual((found.version, finder.source, finder.pending), ("Easy", "lazer", False))
        self.assertIsNotNone(finder.by_md5(finder.entry.md5))
        self.assertIsNone(finder.progress)
        finder.close()


if __name__ == "__main__":
    unittest.main()
