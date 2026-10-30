"""engine.beatmap: .osu parsing, window-title lookup, index cache, playfield transform."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from osu_tablet_assistant.engine import beatmap as bm
from osu_tablet_assistant.models import DisplaySpec

OSU = """osu file format v14

[General]
AudioFilename: a.mp3

[Metadata]
Title:Blue Zenith
TitleUnicode:ブルーゼニス
Artist:xi
ArtistUnicode:xi
Creator:Asphyxia
Version:FOUR DIMENSIONS
BeatmapID:292301

[Difficulty]
HPDrainRate:6
CircleSize:4
OverallDifficulty:9
ApproachRate:9.3

[Events]
//Background and Video events

[HitObjects]
256,192,1000,5,0,0:0:0:0:
100,100,500,1,0
300,200,1500,2,0,L|400:200,1,100
256,192,2000,12,0,3000,0:0:0:0:
bad,line
"""


class ParseTests(unittest.TestCase):
    def test_parse_metadata_difficulty_and_sorted_objects(self) -> None:
        m = bm.parse_osu(OSU, "x.osu")
        self.assertEqual((m.title, m.artist, m.version), ("Blue Zenith", "xi", "FOUR DIMENSIONS"))
        self.assertEqual(m.title_unicode, "ブルーゼニス")
        self.assertEqual((m.cs, m.od, m.ar, m.hp), (4.0, 9.0, 9.3, 6.0))
        self.assertEqual([o.time_ms for o in m.objects], [500, 1000, 1500, 2000])
        self.assertEqual([o.kind for o in m.objects], ["circle", "circle", "slider", "spinner"])
        self.assertTrue(m.objects[1].new_combo)
        self.assertEqual(len(m.judged_objects), 3)       # spinners are not judged

    def test_garbage_does_not_raise(self) -> None:
        self.assertEqual(bm.parse_osu("").objects, ())
        self.assertEqual(bm.parse_osu("[HitObjects]\n1,2\nx,y,z,w\n").objects, ())

    def test_load_missing_or_empty_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(bm.load_beatmap(Path(tmp) / "none.osu"))
            empty = Path(tmp) / "e.osu"
            empty.write_text("[Metadata]\nTitle:x\n", encoding="utf-8")
            self.assertIsNone(bm.load_beatmap(empty))

    def test_difficulty_formulas(self) -> None:
        self.assertAlmostEqual(bm.circle_radius_osu_px(4), 54.4 - 4.48 * 4)
        self.assertAlmostEqual(bm.hit_window_ms(9, "300"), 26.0)
        self.assertAlmostEqual(bm.hit_window_ms(9, "100"), 68.0)
        self.assertAlmostEqual(bm.hit_window_ms(9), 110.0)
        self.assertGreater(bm.hit_window_ms(20, "300"), 0)


class TitleTests(unittest.TestCase):
    def test_menu_and_garbage_give_none(self) -> None:
        for title in (None, "", "osu!", "osu!cuttingedge", "Notepad", "osu! - not a map"):
            self.assertIsNone(bm.parse_window_title(title), title)

    def test_playing_title(self) -> None:
        info = bm.parse_window_title("osu!  - xi - Blue Zenith [FOUR DIMENSIONS]")
        self.assertEqual((info.artist, info.title, info.version), ("xi", "Blue Zenith", "FOUR DIMENSIONS"))

    def test_brackets_in_title_keep_the_last_as_version(self) -> None:
        info = bm.parse_window_title("osu!  - A - B [remix] [Hard]")
        self.assertEqual(info.version, "Hard")
        self.assertEqual(info.title, "B [remix]")


class IndexTests(unittest.TestCase):
    def make_songs(self, tmp: str) -> Path:
        songs = Path(tmp) / "Songs"
        (songs / "1 xi - Blue Zenith").mkdir(parents=True)
        (songs / "1 xi - Blue Zenith" / "a.osu").write_text(OSU, encoding="utf-8")
        other = OSU.replace("FOUR DIMENSIONS", "Easy")
        (songs / "1 xi - Blue Zenith" / "b.osu").write_text(other, encoding="utf-8")
        (songs / "empty").mkdir()
        return songs

    def test_find_by_title_case_insensitive_and_unicode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = bm.BeatmapIndex(self.make_songs(tmp))
            self.assertEqual(index.scan(), 2)
            found = bm.find_beatmap("osu!  - XI - blue zenith [four dimensions]", index)
            self.assertEqual(found.version, "FOUR DIMENSIONS")
            self.assertEqual(len(found.objects), 4)
            unicode_title = bm.parse_window_title("osu!  - xi - ブルーゼニス [Easy]")
            self.assertEqual(bm.find_beatmap(unicode_title, index).version, "Easy")
            self.assertIsNone(bm.find_beatmap("osu!  - xi - Other [Easy]", index))
            self.assertIsNone(bm.find_beatmap("osu!", index))

    def test_missing_songs_dir_is_empty_not_error(self) -> None:
        self.assertEqual(bm.BeatmapIndex("/nonexistent/path/Songs").scan(), 0)

    def test_cache_is_used_until_the_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            songs, cache = self.make_songs(tmp), Path(tmp) / "cache" / "idx.json"
            first = bm.BeatmapIndex(songs, cache)
            first.scan()
            self.assertTrue(cache.exists())
            data = json.loads(cache.read_text(encoding="utf-8"))
            key = next(iter(data))
            data[key][2]["Title"] = "From Cache"      # proves the cached header is reused
            cache.write_text(json.dumps(data), encoding="utf-8")
            second = bm.BeatmapIndex(songs, cache)
            second.scan()
            self.assertIn("From Cache", {e.title for e in second.entries})
            (songs / "1 xi - Blue Zenith" / Path(key).name).write_text(OSU.replace("Blue Zenith", "Changed!"),
                                                                       encoding="utf-8")
            third = bm.BeatmapIndex(songs, cache)
            third.scan()
            self.assertNotIn("From Cache", {e.title for e in third.entries})

    def test_corrupt_cache_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "idx.json"
            cache.write_text("{not json", encoding="utf-8")
            self.assertEqual(bm.BeatmapIndex(self.make_songs(tmp), cache).scan(), 2)


class TransformTests(unittest.TestCase):
    D = DisplaySpec(1920, 1080, 960, 540)

    def test_centre_and_corners_1080p(self) -> None:
        self.assertEqual(bm.playfield_to_desktop(256, 192, self.D), (960.0, 540.0))
        scale = 1080 / 480
        x, y = bm.playfield_to_desktop(0, 0, self.D)
        self.assertAlmostEqual(x, 960 - 256 * scale)
        self.assertAlmostEqual(y, 48 * scale)

    def test_round_trip_with_offset_monitor(self) -> None:
        d = DisplaySpec(2560, 1440, 1920 + 1280, 720)
        for p in ((0, 0), (512, 384), (123.4, 56.7)):
            back = bm.desktop_to_playfield(*bm.playfield_to_desktop(*p, d), d)
            self.assertAlmostEqual(back[0], p[0], places=6)
            self.assertAlmostEqual(back[1], p[1], places=6)


if __name__ == "__main__":
    unittest.main()
