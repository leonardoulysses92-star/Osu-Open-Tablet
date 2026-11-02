"""Faster tracking: the title poll, the recorder's pre-roll ring buffer, the sampler back-fill, osu!'s window
rectangle, and how CoachCore wires them (fakes only; nothing touches osu!)."""
from __future__ import annotations

import os
import unittest
from dataclasses import replace

from osu_tablet_assistant.engine import beatmap as bm
from osu_tablet_assistant.engine.playtypes import PlayfieldBox
from osu_tablet_assistant.gui import coachcore as coach, pen_sampler as ps, playwatch
from osu_tablet_assistant.gui.pen_sampler import PenSampler
from osu_tablet_assistant.otd.livetune import ProcessEntry
from test_coach import DISPLAY, TITLE, USER_AREA, Env


class Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class TitleTests(unittest.TestCase):
    def test_kinds(self) -> None:
        self.assertEqual(playwatch.title_kind("osu!"), "menu")
        self.assertEqual(playwatch.title_kind(None), "menu")
        self.assertEqual(playwatch.title_kind("osu!  - Artist - Song [Hard]"), "play")
        self.assertEqual(playwatch.title_kind("osu! - AC/DC - Thunderstruck [Insane]"), "play")
        self.assertEqual(playwatch.title_kind("osu!  - Artist - Song (me) [Hard].osu"), "edit")
        self.assertEqual(playwatch.title_kind("osu! - C:\\Users\\x\\maps\\a.osu"), "edit")
        self.assertEqual(playwatch.title_kind("osu! - Spectating Someone - Artist - Song [Hard]"), "watch")
        self.assertEqual(playwatch.title_kind("osu!  - Artist - Song [Hard] (watching replay)"), "watch")

    def test_title_key_is_the_exact_text(self) -> None:
        self.assertEqual(bm.title_key("osu!  - xi - Blue Zenith [FOUR DIMENSIONS]"), "xi - Blue Zenith [FOUR DIMENSIONS]")
        self.assertIsNone(bm.title_key("osu!"))

    def test_the_process_list_is_cached_and_refreshed_when_the_window_is_gone(self) -> None:
        calls: list[int] = []
        clock = Clock()
        pid = [5]

        def lister():
            calls.append(1)
            return [ProcessEntry(pid[0], "osu!.exe", "C:/osu!/osu!.exe")]

        cache = playwatch._OsuPid(lister, clock, ttl_s=3.0)
        self.assertEqual(cache.get(), 5)
        clock.now = 1.0
        self.assertEqual(cache.get(), 5)
        self.assertEqual(len(calls), 1)                              # within the TTL: no new process listing
        clock.now = 3.5
        pid[0] = 9
        self.assertEqual(cache.get(), 9)
        self.assertEqual(len(calls), 2)
        self.assertEqual(cache.get(refresh=True), 9)
        self.assertEqual(len(calls), 3)

    def test_cached_title_poll_asks_again_when_osu_restarted(self) -> None:
        old = playwatch._OSU_PID
        self.addCleanup(setattr, playwatch, "_OSU_PID", old)
        pid = [5]
        playwatch._OSU_PID = playwatch._OsuPid(lambda: [ProcessEntry(pid[0], "osu!.exe", "x")], Clock(), 99.0)
        windows = lambda pids: [(p, "osu!  - A - B [C]") for p in pids if p == 7]     # noqa: E731
        self.assertIsNone(playwatch.osu_window_title(windows=windows))
        pid[0] = 7                                                   # a new osu! process
        self.assertEqual(playwatch.osu_window_title(windows=windows), "osu!  - A - B [C]")


class RecorderRingTests(unittest.TestCase):
    def make(self, ring_s: float = 10.0):
        clock = Clock(100.0)
        keys = {"z": False, "x": False}
        sampler = PenSampler(position_source=lambda: None, clock=clock)
        rec = playwatch.PlayRecorder(sampler, DISPLAY, USER_AREA, ("z", "x"), key_source=lambda: dict(keys),
                                     clock=clock, ring_s=ring_s, playfield_source=lambda: PlayfieldBox(1, 2, 3))
        return rec, sampler, clock, keys

    def feed(self, rec, sampler, clock, keys, start: float, end: float, press_every: float = 0.5) -> None:
        t, last_press = start, start
        while t < end:
            clock.now = t
            sampler.record(t * 10.0, 5.0, t=t)
            keys["z"] = t - last_press < 0.05
            if t - last_press >= press_every:
                last_press = t
            rec.poll_keys()
            t = round(t + 0.01, 6)

    def test_armed_keeps_only_the_ring_and_starts_retroactively(self) -> None:
        rec, sampler, clock, keys = self.make(ring_s=10.0)
        rec._start_threads = lambda: None                            # drive by hand
        self.assertTrue(rec.arm())
        self.assertTrue(rec.armed)
        self.feed(rec, sampler, clock, keys, 100.0, 130.0)
        rec._pull_samples()
        rec._trim(clock.now - rec.ring_s)                            # what the key thread does while armed
        snap = rec.snapshot()
        self.assertGreaterEqual(snap.samples[0].t, 119.99)           # at most the last 10 s are kept
        rec.start(since=125.0)                                       # the title named the map at 125 s
        self.assertFalse(rec.armed)
        self.assertTrue(rec.running)
        snap = rec.snapshot()
        self.assertAlmostEqual(snap.started_at, 125.0)
        self.assertGreaterEqual(snap.samples[0].t, 125.0)
        self.assertLess(snap.samples[0].t, 125.02)
        self.assertTrue(all(c.t >= 125.0 for c in snap.clicks) and snap.clicks)
        self.assertEqual(snap.playfield, PlayfieldBox(1, 2, 3))      # measured at the start
        self.assertAlmostEqual(rec.live().travel_px, (snap.samples[-1].x - snap.samples[0].x), places=3)

    def test_a_start_older_than_the_ring_is_capped(self) -> None:
        rec, sampler, clock, keys = self.make(ring_s=2.0)
        rec._start_threads = lambda: None
        rec.arm()
        self.feed(rec, sampler, clock, keys, 100.0, 110.0)
        rec.start(since=50.0)
        self.assertAlmostEqual(rec.snapshot().started_at, clock.now - 2.0)

    def test_begin_at_and_presses_since(self) -> None:
        rec, sampler, clock, keys = self.make()
        rec._start_threads = lambda: None
        rec.running = True
        self.feed(rec, sampler, clock, keys, 100.0, 104.0)
        self.assertGreater(rec.presses_since(102.0), 2)
        self.assertEqual(rec.presses_since(200.0), 0)
        self.assertTrue(rec.begin_at(102.0))
        snap = rec.snapshot()
        self.assertGreaterEqual(snap.samples[0].t, 102.0)
        self.assertGreaterEqual(snap.started_at, 102.0)
        self.assertFalse(rec.begin_at(50.0))                         # nothing that old is kept any more
        rec.running = False
        self.assertFalse(rec.begin_at(103.0))

    def test_arm_without_keys_or_twice(self) -> None:
        sampler = PenSampler(position_source=lambda: None, clock=Clock())
        rec = playwatch.PlayRecorder(sampler, DISPLAY, USER_AREA, key_source=None if os.name != "nt" else None)
        rec._source = None
        self.assertFalse(rec.arm())
        rec2, *_ = self.make()
        rec2._start_threads = lambda: None
        self.assertTrue(rec2.arm())
        self.assertTrue(rec2.arm())
        rec2.disarm()
        self.assertFalse(rec2.armed)
        rec2.disarm()

    def test_real_threads_arm_start_and_stop(self) -> None:
        pos = iter(range(10 ** 9))
        sampler = PenSampler(position_source=lambda: (float(next(pos)), 0.0), poll_hz=2000.0)
        rec = playwatch.PlayRecorder(sampler, DISPLAY, USER_AREA, key_source=lambda: {}, poll_hz=2000.0)
        self.assertTrue(rec.arm())
        import time
        time.sleep(0.05)
        rec.start(since=rec.now() - 0.02)
        time.sleep(0.03)
        rec.stop()
        snap = rec.snapshot()
        self.assertFalse(sampler.running)
        self.assertGreater(len(snap.samples), 5)
        self.assertGreaterEqual(snap.samples[0].t, snap.started_at)
        self.assertIsNotNone(rec.sampler_stats)


class BackfillTests(unittest.TestCase):
    def make(self, history, ticks):
        clock = Clock(10.0)
        pos = [(100.0, 100.0)]
        sampler = PenSampler(position_source=lambda: pos[0], clock=clock, history=history, ticks=ticks,
                             heartbeat_s=1.0)
        return sampler, clock, pos

    def test_points_lost_in_a_stall_are_filled_in(self) -> None:
        tick = [50_000]
        hist = [[(100, 100, 50_000)]]
        sampler, clock, pos = self.make(lambda p: hist[0], lambda: tick[0])
        sampler.poll()                                               # t = 10.000 at (100, 100)
        clock.now, tick[0] = 10.030, 50_030                          # a 30 ms stall
        pos[0] = (130.0, 100.0)
        hist[0] = [(130, 100, 50_030), (120, 100, 50_020), (110, 100, 50_010), (100, 100, 50_000)]
        sampler.poll()
        got = [(round(s.t, 3), s.x) for s in sampler.drain()]
        self.assertEqual(got, [(10.0, 100.0), (10.01, 110.0), (10.02, 120.0), (10.03, 130.0)])
        stats = sampler.stats
        self.assertEqual((stats.polls, stats.long_gaps, stats.backfilled), (2, 1, 2))
        self.assertAlmostEqual(stats.max_gap_ms, 30.0, places=3)
        clock.now, tick[0] = 10.050, 50_050                          # the same history again: nothing twice
        pos[0] = (140.0, 100.0)
        hist[0] = [(140, 100, 50_050)] + hist[0]
        sampler.poll()
        self.assertEqual([s.x for s in sampler.drain()], [140.0])
        self.assertEqual(sampler.stats.backfilled, 2)

    def test_coarse_stamps_are_spread_over_the_gap(self) -> None:
        tick = [1000]
        hist = [[]]
        sampler, clock, pos = self.make(lambda p: hist[0], lambda: tick[0])
        sampler.poll()
        clock.now, tick[0] = 10.040, 1016                            # one 16 ms tick for a 40 ms stall
        pos[0] = (140.0, 100.0)
        hist[0] = [(140, 100, 1016), (130, 100, 1016), (120, 100, 1016), (110, 100, 1000), (100, 100, 1000)]
        sampler.poll()
        got = sampler.drain()
        self.assertEqual([s.x for s in got], [100.0, 110.0, 120.0, 130.0, 140.0])
        self.assertTrue(all(b.t > a.t for a, b in zip(got, got[1:])))
        self.assertTrue(all(10.0 < s.t < 10.04 for s in got[1:-1]))

    def test_failing_history_is_ignored(self) -> None:
        def boom(_p):
            raise OSError("no")

        sampler, clock, pos = self.make(boom, lambda: 0)
        sampler.poll()
        clock.now = 10.5
        pos[0] = (1.0, 2.0)
        self.assertIsNotNone(sampler.poll())
        self.assertEqual(sampler.stats.history_failed, 1)
        none_sampler, clock2, pos2 = self.make(lambda p: None, lambda: 0)
        none_sampler.poll()
        clock2.now = 11.0
        none_sampler.poll()
        self.assertEqual(none_sampler.stats.history_failed, 1)

    def test_small_gaps_do_not_ask_the_history(self) -> None:
        asked = []
        sampler, clock, pos = self.make(lambda p: asked.append(p) or [], lambda: 0)
        for i in range(5):
            clock.now = 10.0 + i * 0.001
            sampler.poll()
        self.assertEqual(asked, [])

    def test_negative_coordinate_fix_up(self) -> None:
        self.assertEqual(ps.fix_display_point(65535 - 99, 20), (-100, 20))
        self.assertEqual(ps.fix_display_point(1919, 65536 - 1080), (1919, -1080))

    def test_windows_history_fails_soft(self) -> None:
        pair = ps.windows_move_history()
        if os.name != "nt":
            self.assertIsNone(pair)
            return
        self.assertIsNotNone(pair)
        read, tick = pair
        self.assertIsInstance(tick(), int)
        cursor = ps.windows_cursor_source()()
        result = read((int(cursor[0]), int(cursor[1]))) if cursor else None
        self.assertTrue(result is None or isinstance(result, list))


class PlayfieldBoxTests(unittest.TestCase):
    def test_full_screen_client_matches_the_old_assumption(self) -> None:
        box = ps.osu_playfield_box(5, lambda pid: (0, 0, 1920, 1080))
        for p in ((0, 0), (512, 384), (256, 192)):
            self.assertEqual(tuple(round(v, 6) for v in bm.playfield_to_desktop(*p, DISPLAY, box)),
                             tuple(round(v, 6) for v in bm.playfield_to_desktop(*p, DISPLAY)))

    def test_windowed_and_odd_shapes(self) -> None:
        box = ps.osu_playfield_box(5, lambda pid: (100, 50, 1280, 720))
        self.assertAlmostEqual(box.scale, 1.5)
        self.assertEqual(bm.playfield_to_desktop(0, 0, DISPLAY, box), (100 + 160 + 64 * 1.5, 50 + 48 * 1.5))
        back = bm.desktop_to_playfield(*bm.playfield_to_desktop(300, 100, DISPLAY, box), DISPLAY, box)
        self.assertAlmostEqual(back[0], 300)
        tall = bm.box_in_rect(0, 0, 640, 960)                        # portrait: width limits the scale
        self.assertAlmostEqual(tall.scale, 1.0)
        self.assertIsNone(bm.box_in_rect(0, 0, 0, 100))
        self.assertIsNone(ps.osu_playfield_box(None))
        self.assertIsNone(ps.osu_playfield_box(5, lambda pid: None))
        self.assertIsNone(ps.osu_playfield_box(5, lambda pid: 1 / 0))

    def test_windows_client_rect_fails_soft(self) -> None:
        self.assertIsNone(ps.windows_client_rect(0))
        self.assertIsNone(ps.windows_client_rect(os.getpid()))       # this process has no "osu!" window

    def test_analysis_uses_the_measured_box(self) -> None:
        from osu_tablet_assistant.engine import playobserve as po
        from playsim import Player, make_map, simulate
        beatmap = make_map(seed=4, n=80)
        capture = simulate(beatmap, Player(), seed=2, t0=5.0)
        plain = po.analyze_play(beatmap, capture)
        same = po.analyze_play(beatmap, replace(capture, playfield=bm.box_in_rect(0, 0, 1920, 1080)))
        self.assertAlmostEqual(plain.miss_rate, same.miss_rate)
        off = po.analyze_play(beatmap, replace(capture, playfield=bm.box_in_rect(300, 200, 1280, 720)))
        self.assertGreater(off.miss_rate, plain.miss_rate)           # the wrong window: the clicks miss


class CoachWiringTests(unittest.TestCase):
    def test_pre_roll_arms_on_a_play_title_and_becomes_the_capture(self) -> None:
        env = Env(self)
        made: list = []
        clock = Clock(50.0)

        def factory(area):
            sampler = PenSampler(position_source=lambda: None, clock=clock)
            rec = playwatch.PlayRecorder(sampler, DISPLAY, area, ("z", "x"), key_source=lambda: {}, clock=clock)
            rec._start_threads = lambda: None
            made.append(rec)
            return rec

        core = coach.CoachCore(env.tuner, None, DISPLAY, recorder_factory=factory, watcher=env.core.watcher,
                               title_source=lambda: env.title, cache_path=env.dir / "idx3.json", preroll=True)
        env.title = "osu!"
        core.readiness()
        self.assertFalse(core.track_map())
        self.assertEqual(made, [])                                   # menu: nothing armed
        env.title = TITLE
        clock.now = 60.0
        self.assertTrue(core.track_map())                            # map recognised ...
        self.assertEqual(len(made), 1)
        self.assertTrue(made[0].armed)                               # ... and the pre-roll armed
        for i in range(300):
            made[0].sampler.record(float(i), 0.0, t=55.0 + i * 0.03)
        clock.now = 64.0
        self.assertTrue(core.start())
        self.assertIs(core.recorder, made[0])
        snap = core.recorder.snapshot()
        self.assertAlmostEqual(snap.started_at, 60.0 - playwatch.TITLE_LEAD_S)
        self.assertGreaterEqual(snap.samples[0].t, 59.75)
        core.abort()
        env.title = "osu!"
        core.track_map()
        env.title = TITLE                                            # armed again, then the menu disarms it
        core.track_map()
        self.assertTrue(made[-1].armed)
        env.title = "osu!"
        core.track_map()
        self.assertFalse(made[-1].armed)
        core.stop_recording()

    def test_observing_play_title_trims_and_watch_title_is_flagged(self) -> None:
        env = Env(self)
        clock = Clock(10.0)
        recs: list = []

        def factory(area):
            sampler = PenSampler(position_source=lambda: None, clock=clock)
            rec = playwatch.PlayRecorder(sampler, DISPLAY, area, ("z", "x"), key_source=lambda: {}, clock=clock)
            rec._start_threads = lambda: None
            recs.append(rec)
            return rec

        core = coach.CoachCore(env.tuner, None, DISPLAY, recorder_factory=factory, watcher=env.core.watcher,
                               title_source=lambda: env.title, cache_path=env.dir / "idx4.json")
        core.readiness()
        self.assertTrue(core.start())
        for i in range(100):
            recs[0].sampler.record(float(i), 0.0, t=10.0 + i * 0.1)
        env.title = "osu!"
        core.track_map()
        clock.now = 16.0
        env.title = "osu!  - Artist - Song [Hard]  "                 # the play title again (another attempt)
        core.track_map()
        self.assertGreaterEqual(recs[0].snapshot().samples[0].t, 16.0 - playwatch.TITLE_LEAD_S)
        clock.now = 18.0
        env.title = "osu! - Spectating Someone - Artist - Song [Hard]"
        core.track_map()
        result = core.finish()
        self.assertEqual(result.pointer[0].key, "play.map.watching")

    def test_a_pending_index_is_retried_without_a_new_title(self) -> None:
        env = Env(self)

        class Finder:
            generation, pending, lazer_files = 0, True, None
            found = None

            def find(self, title, exe, flavor=None):
                return self.found

            def by_md5(self, md5):
                return None

            def close(self):
                pass

        finder = Finder()
        core = coach.CoachCore(env.tuner, None, DISPLAY, recorder_factory=lambda a: None, watcher=env.core.watcher,
                               title_source=lambda: env.title, map_finder=finder)    # type: ignore[arg-type]
        ready = core.readiness()
        self.assertIn("play.prep.map_indexing", [i.key for i in ready.issues])
        self.assertFalse(core.track_map())                           # same title, nothing new in the index
        finder.found = bm.load_beatmap(next((env.dir / "osu!" / "Songs").glob("*/*.osu")))
        self.assertFalse(core.track_map())                           # the generation did not move yet
        finder.generation = 1
        self.assertTrue(core.track_map())                            # the index learned something: looked up again
        self.assertEqual(core.beatmap.version, "Hard")
        self.assertFalse(core.track_map())

    def test_finish_adds_the_pointer_lines(self) -> None:
        env = Env(self)
        (env.dir / "osu!" / "osu!.me.cfg").write_text("RawInput = 1\nAbsoluteToOsuWindow = 1\n", encoding="utf-8")
        result = env.play_round()
        self.assertIn("play.pointer.raw_window", [r.key for r in result.pointer])
        self.assertTrue(coach.say(result.pointer[0]))


if __name__ == "__main__":
    unittest.main()
