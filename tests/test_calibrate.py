"""engine.calibrate: clock offset, desktop -> playfield fit, the newest replay, and the report lines (synthetic)."""
from __future__ import annotations

import math
import os
import random
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from osu_tablet_assistant.engine import calibrate as cal
from osu_tablet_assistant.engine import replay as rp
from osu_tablet_assistant.engine.beatmap import desktop_to_playfield
from osu_tablet_assistant.engine.osucfg import OsuConfig
from osu_tablet_assistant.engine.playtypes import PlayCapture
from osu_tablet_assistant.gui.pen_sampler import SamplerStats
from osubuild import make_osr
from playsim import Player, make_map, simulate

MAP = make_map(seed=41, n=110, gap_ms=300.0)
REPLAY_START_AFTER_S = 0.7          # the replay's time 0 is this long after the recording began


def live_capture(seed: int = 3, scale: float = 1.0, hr: bool = False) -> PlayCapture:
    return simulate(MAP, Player(miss_rate=0.05, scale=scale, hr=hr), seed=seed, t0=12.0)


def replay_bytes(capture: PlayCapture, scale: float = 1.0, mods: int = 0, transform=None, every: int = 4,
                 md5: str = "a" * 32, jitter_ms: float = 2.0, keys_from=None, lead: bool = False) -> tuple[bytes, float]:
    """An .osr of the same play: osu!'s cursor = the OS cursor through ``transform`` (playfield px)."""
    rng = random.Random(5)
    t0 = capture.samples[0].t + REPLAY_START_AFTER_S
    presses = []
    for c in (keys_from if keys_from is not None else capture.clicks):
        down = c.t + rng.gauss(0, jitter_ms / 1000.0)
        presses += [(down, 5), (down + 0.045, 0)]
    presses.sort()
    frames, last, keys, pi = [], 0, 0, 0
    for s in capture.samples[::every]:
        if s.t < t0:
            continue
        while pi < len(presses) and presses[pi][0] <= s.t:
            keys = presses[pi][1]
            pi += 1
        x, y = desktop_to_playfield(s.x, s.y, capture.display)
        if transform is not None:
            x, y = transform(x, y)
        t_ms = round((s.t - t0) * 1000.0 * scale)
        frames.append((t_ms - last, x, y, keys))
        last = t_ms
    return make_osr(frames, md5=md5, mods=mods, lead=lead), t0


class ClockTests(unittest.TestCase):
    def test_offset_from_key_edges(self) -> None:
        capture = live_capture()
        data, t0 = replay_bytes(capture)
        clock = cal.fit_clock([c.t for c in capture.clicks], cal.replay_key_downs(rp.parse_replay(data)))
        self.assertIsNotNone(clock)
        self.assertAlmostEqual(clock.offset_s, t0, delta=0.004)
        self.assertGreater(clock.matched, 80)
        self.assertLess(clock.spread_ms, 6.0)

    def test_double_time_with_and_without_the_mod_flag(self) -> None:
        capture = live_capture(scale=1.5)
        for mods in (rp.MOD_DOUBLE_TIME, 0):                       # a wrong mod field: the other speeds are tried
            data, t0 = replay_bytes(capture, scale=1.5, mods=mods)
            result = cal.calibrate(capture, rp.parse_replay(data))
            self.assertEqual(result.clock.scale, 1.5)
            self.assertAlmostEqual(result.clock.offset_s, t0, delta=0.005)
            self.assertEqual(result.verdict, "confirmed")

    def test_unrelated_clicks_give_no_clock(self) -> None:
        capture = live_capture()
        other = live_capture(seed=99)
        fake = [replace(c, t=c.t * 1.37 + 3.1) for c in other.clicks]
        data, _ = replay_bytes(capture, keys_from=fake)
        result = cal.calibrate(capture, rp.parse_replay(data))
        self.assertEqual((result.verdict, result.flags), ("unknown", ("no_clock",)))
        self.assertEqual([r.key for r in cal.calibration_notes(result)], ["play.calib.no_match"])

    def test_key_bits_count_k1_once(self) -> None:
        frames = [rp.ReplayFrame(t, 0, 0, k) for t, k in ((0, 0), (10, 5), (20, 5), (30, 1), (40, 0), (50, 1),
                                                           (60, 9), (70, 0))]
        replay = rp.Replay("", 0, 0, 0, 0, 0, tuple(frames))
        self.assertEqual(cal.replay_key_downs(replay), [10, 30, 50, 60])   # K1 (+M1) once; then M1 alone; K2


class FitTests(unittest.TestCase):
    def test_exact_affine_recovery_and_degenerate_input(self) -> None:
        rng = random.Random(1)
        a = (0.44, 0.01, -0.02, 0.45, -100.0, 12.0)
        pairs = []
        for _ in range(200):
            x, y = rng.uniform(0, 1920), rng.uniform(0, 1080)
            pairs.append((x, y, a[0] * x + a[1] * y + a[4], a[2] * x + a[3] * y + a[5]))
        fit = cal.fit_affine(pairs)
        for got, want in zip((fit.a11, fit.a12, fit.a21, fit.a22, fit.b1, fit.b2), a):
            self.assertAlmostEqual(got, want, places=6)
        self.assertLess(fit.rms_px, 1e-6)
        self.assertFalse(fit.scaled_identity)
        self.assertIsNone(cal.fit_affine([(1.0, 1.0, 2.0, 2.0)] * 50))      # every point the same: singular
        self.assertIsNone(cal.fit_affine(pairs[:3]))

    def test_outliers_are_dropped_on_the_second_fit(self) -> None:
        pairs = [(float(x), float(y), 0.5 * x + 3, 0.5 * y - 2) for x in range(0, 400, 20) for y in range(0, 300, 20)]
        pairs[5] = (pairs[5][0], pairs[5][1], 900.0, -700.0)               # a pen lift / pause frame
        fit = cal.fit_affine(pairs)
        self.assertAlmostEqual(fit.a11, 0.5, places=6)
        self.assertLess(fit.rms_px, 1e-6)

    def test_confirmed_transform(self) -> None:
        capture = live_capture()
        data, _ = replay_bytes(capture)
        result = cal.calibrate(capture, rp.parse_replay(data))
        self.assertEqual(result.verdict, "confirmed", result.flags)
        self.assertTrue(result.fit.scaled_identity)
        self.assertAlmostEqual(result.fit.scale, 480.0 / 1080.0, places=3)
        self.assertLess(result.fit.rms_px, 3.0)              # 4 ms frames and a ms-rounded clock: ~2 px here
        self.assertLess(result.assumed_rms_px, 3.0)
        self.assertFalse(result.fit.flipped)
        self.assertGreater(result.alternative_rms_px, 20.0)               # the mirrored replay fits badly
        self.assertEqual([r.key for r in cal.calibration_notes(result)], ["play.calib.confirmed"])

    def test_raw_input_like_transform_is_a_mismatch(self) -> None:
        capture = live_capture()
        data, _ = replay_bytes(capture, transform=lambda x, y: (256 + 1.25 * (x - 256) + 0.1 * (y - 192), y))
        result = cal.calibrate(capture, rp.parse_replay(data))
        self.assertIn("transformed", result.flags)
        self.assertEqual(result.verdict, "mismatch")
        note = cal.calibration_notes(result)[0]
        self.assertEqual((note.key, note.params["why"].key), ("play.calib.mismatch", "play.calib.why.transformed"))

    def test_a_cursor_that_does_not_follow_is_a_mismatch(self) -> None:
        capture = live_capture()
        rng = random.Random(3)
        data, _ = replay_bytes(capture, transform=lambda x, y: (x + rng.gauss(0, 9), y + rng.gauss(0, 9)))
        result = cal.calibrate(capture, rp.parse_replay(data))
        self.assertIn("residual", result.flags)
        self.assertEqual(cal.calibration_notes(result)[0].params["why"].key, "play.calib.why.residual")

    def test_a_shifted_playfield_is_measured(self) -> None:
        capture = live_capture()
        data, _ = replay_bytes(capture, transform=lambda x, y: (x, y + 8.0))   # e.g. a storyboard-style shift
        result = cal.calibrate(capture, rp.parse_replay(data))
        self.assertEqual(result.verdict, "shifted", result.flags)
        self.assertAlmostEqual(result.shift_px[0], 0.0, delta=0.3)
        self.assertAlmostEqual(result.shift_px[1], 8.0, delta=0.3)
        note = cal.calibration_notes(result)[0]
        self.assertEqual((note.key, note.params["dy"], note.params["scale"]), ("play.calib.shifted", 8.0, 100))

    def test_hard_rock_flipped_frames_are_recognised(self) -> None:
        capture = live_capture(hr=True)
        data, _ = replay_bytes(capture, mods=rp.MOD_HARD_ROCK, transform=lambda x, y: (x, 384.0 - y))
        result = cal.calibrate(capture, rp.parse_replay(data))
        self.assertTrue(result.fit.flipped)
        self.assertEqual(result.verdict, "confirmed")

    def test_the_measured_window_is_what_is_compared(self) -> None:
        from osu_tablet_assistant.engine.beatmap import box_in_rect
        box = box_in_rect(320, 180, 1280, 720)                              # windowed osu!
        capture = replace(live_capture(), playfield=box)
        data, _ = replay_bytes(capture, transform=None)
        # The replay was made with the full-screen formula, so the measured window disagrees: shifted / scaled.
        result = cal.calibrate(capture, rp.parse_replay(data))
        self.assertIn("scale", result.flags)
        self.assertEqual(result.verdict, "shifted")


class ReplayFileTests(unittest.TestCase):
    def test_lead_and_seed_frames_are_dropped(self) -> None:
        frames = [(16, 100.0 + i, 50.0, 0) for i in range(20)]
        replay = rp.parse_replay(make_osr(frames, lead=True, seed=True))
        self.assertEqual(len(replay.frames), 20)
        self.assertTrue(all(f.y == 50.0 for f in replay.frames))
        self.assertEqual(replay.frames[0].t_ms, 15)                        # 0 - 1 + 16: stable's "-1" lead counts

    def test_find_new_replay_in_stable_folders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Replays").mkdir()
            (root / "Data" / "r").mkdir(parents=True)
            now = time.time()
            old = root / "Replays" / "old.osr"
            old.write_bytes(b"x")
            os.utime(old, (now - 3600, now - 3600))
            self.assertIsNone(cal.find_new_replay(root, now - 60))
            local = root / "Data" / "r" / "abc-123.osr"
            local.write_bytes(b"x")
            os.utime(local, (now - 5, now - 5))
            exported = root / "Replays" / "me - song.osr"
            exported.write_bytes(b"x")
            os.utime(exported, (now - 2, now - 2))
            (root / "Replays" / "notes.txt").write_bytes(b"x")
            self.assertEqual(cal.find_new_replay(root, now - 60), exported)
            self.assertIsNone(cal.find_new_replay(None, now - 60))
            self.assertIsNone(cal.find_new_replay(root / "missing", now - 60))

    def test_find_new_replay_in_the_lazer_store(self) -> None:
        capture = live_capture()
        data, _ = replay_bytes(capture)
        with tempfile.TemporaryDirectory() as tmp:
            files = Path(tmp) / "files"
            now = time.time()
            for name, blob, age in (("aa11", b"\x89PNG....", 1), ("bb22", data, 3), ("cc33", data, 7200)):
                path = files / name[0] / name[:2] / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(blob)
                os.utime(path, (now - age, now - age))
            os.utime(files / "c" / "cc", (now - 7200, now - 7200))        # that folder did not change recently
            found = cal.find_new_replay(None, now - 60, lazer_files=files)
            self.assertEqual(found.name, "bb22")
            self.assertTrue(cal.looks_like_replay(data[:8]))
            self.assertFalse(cal.looks_like_replay(b"\x89PNG\r\n\x1a\n"))


class CheckPlayTests(unittest.TestCase):
    def install(self, data: bytes | None) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "Data" / "r").mkdir(parents=True)
        if data is not None:
            (root / "Data" / "r" / "play.osr").write_bytes(data)
        return root

    def test_lines_after_a_play_with_its_replay(self) -> None:
        capture = live_capture()
        with tempfile.TemporaryDirectory() as tmp:
            osu = Path(tmp) / "m.osu"
            osu.write_text("[Metadata]\nTitle:x\n", encoding="utf-8")
            import hashlib
            digest = hashlib.md5(osu.read_bytes()).hexdigest()
            data, _ = replay_bytes(capture, md5=digest)
            root = self.install(data)
            beatmap = replace(MAP, path=str(osu))
            elapsed = capture.samples[-1].t - capture.started_at
            check = cal.check_play(capture, beatmap, osu_dir=root, recorder_now=capture.samples[-1].t,
                                   wall_now=time.time() + 1.0)
            self.assertEqual([n.key for n in check.notes], ["play.calib.confirmed", "play.calib.map_ok"])
            self.assertTrue(check.map_confirmed)
            self.assertTrue(check.replay_path.endswith("play.osr"))
            self.assertGreater(elapsed, 10)

    def test_another_map_is_named_through_the_index(self) -> None:
        capture = live_capture()
        data, _ = replay_bytes(capture, md5="b" * 32)
        root = self.install(data)

        class Finder:
            def by_md5(self, md5):
                from osu_tablet_assistant.engine.beatmap import IndexEntry
                return IndexEntry("x.osu", "Other", "Song", "Other", "Song", "Easy", md5=md5) if md5 == "b" * 32 \
                    else None

        with tempfile.TemporaryDirectory() as tmp:
            osu = Path(tmp) / "m.osu"
            osu.write_text("[Metadata]\n", encoding="utf-8")
            check = cal.check_play(capture, replace(MAP, path=str(osu)), osu_dir=root, finder=Finder(),
                                   replay_path=root / "Data" / "r" / "play.osr")
            self.assertEqual(check.notes[-1].key, "play.calib.map_other")
            self.assertEqual(check.notes[-1].params["name"], "Other - Song [Easy]")
            self.assertFalse(check.map_confirmed)
            check = cal.check_play(capture, replace(MAP, path=str(osu)), osu_dir=root, finder=None,
                                   replay_path=root / "Data" / "r" / "play.osr")
            self.assertEqual(check.notes[-1].key, "play.calib.map_unknown")

    def test_without_a_replay_the_cfg_and_the_sampler_speak(self) -> None:
        capture = live_capture()
        root = self.install(None)
        cfg = OsuConfig(raw_input=True, mouse_speed=1.5, frame_sync="VSync")
        stats = SamplerStats(polls=1000, max_gap_ms=31.4, long_gaps=12, backfilled=40)
        check = cal.check_play(capture, MAP, osu_dir=root, cfg=cfg, stats=stats, refresh_hz=60.0)
        self.assertEqual([n.key for n in check.notes],
                         ["play.pointer.raw_speed", "play.pointer.low_fps", "play.calib.sampling"])
        self.assertEqual(check.notes[-1].params, {"gap": 31, "filled": 40})
        self.assertEqual(cal.check_play(capture, MAP, osu_dir=root, stats=SamplerStats(polls=5)).notes, ())

    def test_never_raises(self) -> None:
        bad = PlayCapture((), (), MAP and live_capture().area, live_capture().display)
        root = self.install(b"\x00garbage")
        self.assertEqual(cal.check_play(bad, None, osu_dir=root, recorder_now=math.nan).notes, ())
        self.assertIsInstance(cal.check_play(None, None), cal.PlayCheck)            # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
