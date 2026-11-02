"""engine.osudb: parse synthetic osu!.db files of every format generation; bad files never raise."""
from __future__ import annotations

import dataclasses
import tempfile
import time
import unittest
from pathlib import Path

from osu_tablet_assistant.engine import osudb
from osubuild import DbMap, make_osudb

MAPS = [
    DbMap(),
    DbMap(artist="xi", title="Blue Zenith", title_unicode="ブルーゼニス", version="FOUR DIMENSIONS",
          folder="2 xi - Blue Zenith", osu_file="xi - Blue Zenith (Asphyxia) [FOUR DIMENSIONS].osu",
          md5="ABCDEF" + "1" * 26, cs=4.2, ar=9.6, od=9.0, hp=6.5, stars=(30, 2, 0, 5), timing_points=40,
          last_played=None),
    DbMap(artist="t+pazolite", title="Oshama Scramble!", version="Insane", folder="3 t+pazolite",
          osu_file="o.osu", md5="2" * 32, mode=3, stars=(0, 0, 0, 0), timing_points=0),
]


class FormatGenerationTests(unittest.TestCase):
    def check(self, version: int, exact_floats: bool) -> None:
        db = osudb.parse_osudb(make_osudb(MAPS, version=version))
        self.assertTrue(db.complete, db.error)
        self.assertEqual((db.version, db.expected, len(db.entries)), (version, 3, 3))
        first, second, third = db.entries
        self.assertEqual(first.key, "Artist - Song [Hard]")
        self.assertEqual((second.artist, second.title, second.title_unicode), ("xi", "Blue Zenith", "ブルーゼニス"))
        self.assertEqual(second.folder, "2 xi - Blue Zenith")
        self.assertEqual(second.osu_file, "xi - Blue Zenith (Asphyxia) [FOUR DIMENSIONS].osu")
        self.assertEqual(second.md5, ("abcdef" + "1" * 26))                     # lower case for lookups
        self.assertIsNone(second.last_played)
        self.assertAlmostEqual(first.last_played, 1_700_000_000.0, places=3)
        self.assertEqual((third.mode, third.version, third.folder), (3, "Insane", "3 t+pazolite"))
        if exact_floats:
            self.assertAlmostEqual(second.cs, 4.2, places=5)
            self.assertAlmostEqual(second.ar, 9.6, places=5)
        else:                                                                  # one byte each before 20140609
            self.assertEqual((second.cs, second.ar, second.od, second.hp), (4.0, 9.0, 9.0, 6.0))

    def test_current_format_int_float_stars(self) -> None:
        self.check(20250108, True)

    def test_int_double_stars_without_entry_size(self) -> None:
        self.check(20210101, True)

    def test_entry_size_before_20191106(self) -> None:
        self.check(20190101, True)

    def test_byte_difficulty_before_20140609(self) -> None:
        self.check(20130101, False)

    def test_boundaries_of_the_versions(self) -> None:
        for version in (20140609, 20191106, 20250107):
            self.check(version, True)

    def test_player_name_is_not_kept(self) -> None:
        db = osudb.parse_osudb(make_osudb(MAPS, player="SecretPlayerName"))
        self.assertNotIn("secret", repr(db).lower())
        self.assertNotIn("player", {f.name for f in dataclasses.fields(osudb.OsuDbEntry)})


class RobustnessTests(unittest.TestCase):
    def test_truncated_file_keeps_what_was_read(self) -> None:
        data = make_osudb(MAPS)
        for cut in (len(data) - 40, len(data) // 2, 60, 10, 0):
            db = osudb.parse_osudb(data[:cut])
            self.assertFalse(db.complete)
            self.assertLessEqual(len(db.entries), 2)
        self.assertEqual(len(osudb.parse_osudb(data[:len(data) - 40]).entries), 2)

    def test_garbage_and_lying_counts(self) -> None:
        self.assertFalse(osudb.parse_osudb(b"\xff" * 300).complete)
        self.assertFalse(osudb.parse_osudb(b"").complete)
        db = osudb.parse_osudb(make_osudb(MAPS, count=1_000_000))   # header promises more than there is
        self.assertFalse(db.complete)
        self.assertEqual(len(db.entries), 3)

    def test_a_bad_entry_is_skipped_by_its_size_in_old_files(self) -> None:
        data = bytearray(make_osudb(MAPS, version=20190101))
        data[data.index(b"\x0b\x02xi")] = 0x07                         # break the first string marker of entry 2
        db = osudb.parse_osudb(bytes(data))
        self.assertEqual([e.version for e in db.entries if e.version], ["Hard", "Insane"])

    def test_load_missing_and_real_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "osu!.db"
            self.assertIsNone(osudb.load_osudb(path))
            path.write_bytes(make_osudb(MAPS))
            self.assertEqual(len(osudb.load_osudb(path).entries), 3)

    def test_ticks(self) -> None:
        self.assertIsNone(osudb.ticks_to_unix(0))
        self.assertAlmostEqual(osudb.ticks_to_unix(osudb.TICKS_AT_EPOCH + 10_000_000), 1.0)

    def test_parse_speed_for_a_large_db(self) -> None:
        many = [dataclasses.replace(MAPS[1], version=f"D{i}", folder=f"{i // 4} xi", md5=f"{i:032x}")
                for i in range(4000)]
        data = make_osudb(many)
        started = time.perf_counter()
        db = osudb.parse_osudb(data)
        elapsed = time.perf_counter() - started
        self.assertTrue(db.complete)
        self.assertEqual(len(db.entries), 4000)
        self.assertLess(elapsed, 3.0)                                  # ~0.1 s here; generous for slow CI


if __name__ == "__main__":
    unittest.main()
