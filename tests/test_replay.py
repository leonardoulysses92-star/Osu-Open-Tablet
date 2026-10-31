"""engine.replay: parse .osr files (synthetic), keep the minimum, judge a play from it, check the inference."""
from __future__ import annotations

import dataclasses
import hashlib
import lzma
import struct
import tempfile
import unittest
from pathlib import Path

from osu_tablet_assistant.engine import playobserve as po
from osu_tablet_assistant.engine import replay as rp
from osu_tablet_assistant.engine.beatmap import desktop_to_playfield
from osu_tablet_assistant.engine.playtypes import PlayCapture
from playsim import AREA, DISPLAY, Player, make_map, simulate

MAP = make_map(seed=31, n=120, gap_ms=300.0)


def _s(text: str) -> bytes:
    if not text:
        return b"\x00"
    raw = text.encode("utf-8")
    n, out = len(raw), bytearray()
    while True:                                # ULEB128
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            break
    return b"\x0b" + bytes(out) + raw


def make_osr(capture: PlayCapture, md5: str = "a" * 32, mods: int = 0, counts=(100, 10, 3, 7),
             player: str = "SecretPlayerName", scale: float = 1.0, mode: int = 0) -> bytes:
    """A replay file for a simulated capture (cursor in osu! pixels, keys from the click times)."""
    t0 = capture.samples[0].t
    frames, last, press = [], 0, []
    for c in capture.clicks:
        press.append((c.t, 1))
        press.append((c.t + 0.04, 0))
    press.sort()
    pi, keys = 0, 0
    for smp in capture.samples[::4]:               # ~250 Hz is plenty
        while pi < len(press) and press[pi][0] <= smp.t:
            keys = press[pi][1]
            pi += 1
        t_ms = round((smp.t - t0) * 1000.0 * scale)
        x, y = desktop_to_playfield(smp.x, smp.y, capture.display)
        frames.append(f"{t_ms - last}|{x:.2f}|{y:.2f}|{keys}")
        last = t_ms
    frames.insert(0, "0|256|-500|0")
    frames.append("-12345|0|0|32767")
    data = lzma.compress(",".join(frames).encode("ascii"), format=lzma.FORMAT_ALONE)
    head = (bytes([mode]) + struct.pack("<i", 20260101) + _s(md5) + _s(player) + _s("deadbeef" * 4)
            + struct.pack("<6H", counts[0], counts[1], counts[2], 5, 2, counts[3]) + struct.pack("<i", 123456)
            + struct.pack("<H", 99) + b"\x00" + struct.pack("<i", mods) + _s("1000|0.9,2000|1") + struct.pack("<q", 6380000000000000000)
            + struct.pack("<i", len(data)))
    return head + data + struct.pack("<q", 987654321)


def capture() -> PlayCapture:
    return simulate(MAP, Player(miss_rate=0.1, gain_x=0.95), seed=5, t0=2.0)


class ParseTests(unittest.TestCase):
    def test_round_trip_header_and_frames(self) -> None:
        r = rp.parse_replay(make_osr(capture(), md5="c" * 32, mods=rp.MOD_HARD_ROCK | rp.MOD_DOUBLE_TIME))
        self.assertEqual((r.n300, r.n100, r.n50, r.n_miss), (100, 10, 3, 7))
        self.assertEqual(r.beatmap_md5, "c" * 32)
        self.assertEqual((r.time_scale, r.hard_rock), (1.5, True))
        self.assertAlmostEqual(r.accuracy, (300 * 100 + 100 * 10 + 50 * 3) / (300 * 120))
        self.assertGreater(len(r.frames), 100)
        self.assertTrue(all(b.t_ms >= a.t_ms for a, b in zip(r.frames, r.frames[1:])))

    def test_only_the_minimum_is_kept(self) -> None:
        r = rp.parse_replay(make_osr(capture(), player="SecretPlayerName"))
        names = {f.name for f in dataclasses.fields(r)}
        self.assertEqual(names, {"beatmap_md5", "mods", "n300", "n100", "n50", "n_miss", "frames"})
        self.assertNotIn("SecretPlayerName", repr(r)[:100000])
        self.assertNotIn("secret", repr(r).lower())

    def test_bad_files_give_none(self) -> None:
        good = make_osr(capture())
        for bad in (b"", b"junk", good[:60], good[:-300], make_osr(capture(), mode=1),
                    good[:40] + b"\xff" * 40 + good[80:]):
            self.assertIsNone(rp.parse_replay(bad))
        self.assertIsNone(rp.parse_replay(b"\x00" * (rp.MAX_FILE_BYTES + 1)))

    def test_load_from_disk_and_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.osr"
            path.write_bytes(make_osr(capture()))
            self.assertIsNotNone(rp.load_replay(path))
            self.assertIsNone(rp.load_replay(Path(tmp) / "none.osr"))

    def test_md5_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            osu = Path(tmp) / "m.osu"
            osu.write_text("[Metadata]\nTitle:x\n", encoding="utf-8")
            digest = hashlib.md5(osu.read_bytes()).hexdigest()
            beatmap = dataclasses.replace(MAP, path=str(osu))
            same = rp.parse_replay(make_osr(capture(), md5=digest))
            other = rp.parse_replay(make_osr(capture(), md5="0" * 32))
            self.assertTrue(rp.matches_beatmap(same, beatmap))
            self.assertFalse(rp.matches_beatmap(other, beatmap))
            self.assertIsNone(rp.matches_beatmap(same, dataclasses.replace(MAP, path="")))


class JudgeFromReplayTests(unittest.TestCase):
    def test_a_replay_is_judged_like_the_live_capture(self) -> None:
        live_capture = capture()
        replay = rp.parse_replay(make_osr(live_capture))
        from_replay = rp.replay_to_capture(replay, AREA, DISPLAY)
        self.assertIsNotNone(from_replay.ended_at)
        self.assertGreater(len(from_replay.clicks), 80)
        live = po.analyze_play(MAP, live_capture)
        real = po.analyze_play(MAP, from_replay)
        self.assertTrue(real.trusted)
        self.assertAlmostEqual(real.miss_rate, live.miss_rate, delta=0.06)
        check = rp.compare_inference(live, real, replay)
        self.assertGreater(check.agreement, 0.9)
        self.assertEqual(check.real_accuracy, replay.accuracy)

    def test_double_time_replay_is_read_at_real_speed(self) -> None:
        fast = simulate(MAP, Player(scale=1.5, miss_rate=0.05), seed=6, t0=3.0)
        replay = rp.parse_replay(make_osr(fast, mods=rp.MOD_DOUBLE_TIME, scale=1.5))
        report = po.analyze_play(MAP, rp.replay_to_capture(replay, AREA, DISPLAY))
        self.assertEqual(report.alignment.time_scale, 1.5)
        self.assertTrue(report.trusted)

    def test_hard_rock_replay_is_recognised(self) -> None:
        hr = simulate(MAP, Player(hr=True, miss_rate=0.05), seed=7, t0=3.0)
        replay = rp.parse_replay(make_osr(hr, mods=rp.MOD_HARD_ROCK))
        report = po.analyze_play(MAP, rp.replay_to_capture(replay, AREA, DISPLAY))
        self.assertTrue(report.alignment.flip_y)

    def test_compare_without_overlap_is_none(self) -> None:
        empty = po.analyze_play(MAP, capture().__class__((), (), AREA, DISPLAY))
        full = po.analyze_play(MAP, capture())
        self.assertIsNone(rp.compare_inference(empty, full))


if __name__ == "__main__":
    unittest.main()
