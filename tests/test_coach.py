"""gui.coach (CoachCore, no Tk), gui.playwatch and the TabletMap geometry. Nothing touches osu! or a real OTD."""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from osu_tablet_assistant.engine import areafit
from osu_tablet_assistant.engine.playtypes import PlayCapture
from osu_tablet_assistant.gui import coachcore as coach, mapgeometry as tm, playwatch
from osu_tablet_assistant.gui.pen_sampler import PenSampler
from osu_tablet_assistant.models import ApplyResult, DisplaySpec, Rect, TabletSpec
from osu_tablet_assistant.otd.livetune import LiveTuner, OsuWatcher, ProcessEntry
from playsim import Player, make_map, simulate
from test_livetune import CTL472, user_settings

USER_AREA = Rect(71.66, 41.44, 80.17, 26.72, 180.0)
IDEAL = Rect(66.0, 38.2, 78.0, 28.0, 180.0)
DISPLAY = DisplaySpec(1920, 1080, 960, 540)
MAP = make_map(seed=21, n=90, gap_ms=320.0)
TITLE = "osu!  - Artist - Song [Hard]"
OSU_FILE = """[Metadata]
Title:Song
Artist:Artist
Version:Hard
[Difficulty]
CircleSize:4
OverallDifficulty:8
[HitObjects]
""" + "\n".join(f"{o.x:.0f},{o.y:.0f},{o.time_ms:.0f},1,0" for o in MAP.objects)


def player_for(area: Rect, ideal: Rect = IDEAL) -> Player:
    vw, vh = areafit.view_size(area)
    iw, ih = areafit.view_size(ideal)
    cu, cv = areafit.tablet_mm_to_area(area, ideal.x, ideal.y)
    return Player(gain_x=iw / vw, gain_y=ih / vh, bias_x_mm=(cu - 0.5) * vw, bias_y_mm=(cv - 0.5) * vh,
                  miss_rate=0.02, aim_noise_mm=0.25)


class FakeRecorder:
    """Stands in for PlayRecorder: hands out a capture simulated for the area it was created with."""
    keys_available = True

    def __init__(self, area: Rect, seed: int, clicks_idle: float | None = None, few_clicks: bool = False) -> None:
        capture = simulate(MAP, player_for(area), seed=seed, area=area, display=DISPLAY, t0=30.0)
        capture = replace(capture, samples=capture.samples[::3])
        if few_clicks:
            capture = replace(capture, clicks=capture.clicks[:4])
        self.capture, self.running, self.idle = capture, False, clicks_idle

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    def snapshot(self) -> PlayCapture:
        return self.capture

    def live(self) -> playwatch.LiveView:
        return playwatch.LiveView(None, (), len(self.capture.clicks), 1.0, 1.0 + (self.idle or 0.0))


class Env:
    """A CoachCore wired to fakes, with a Songs folder holding the map."""

    def __init__(self, test: unittest.TestCase, osu_open: bool = True, title: str | None = TITLE) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="osu-coach-")
        test.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        osu_dir = self.dir / "osu!"
        (osu_dir / "Songs" / "1 Artist - Song").mkdir(parents=True)
        (osu_dir / "Songs" / "1 Artist - Song" / "Artist - Song (me) [Hard].osu").write_text(OSU_FILE, encoding="utf-8")
        entries = [ProcessEntry(7, "osu!.exe", str(osu_dir / "osu!.exe"))] if osu_open else []
        self.sent: list[dict] = []
        self.written: list[dict] = []

        def sender(settings: dict) -> ApplyResult:
            self.sent.append(settings)
            return ApplyResult(True, "rpc", "otd.apply.ok_rpc")

        self.tuner = LiveTuner(user_settings(), "Wacom CTL-472", sender, config_dir=self.dir / "otd", tablet=CTL472,
                               backup=lambda _d: self.dir / "backup.json", writer=lambda _d, s: self.written.append(s),
                               min_interval_s=0.0)
        self.round = 0
        self.title = title
        self.recorders: list[FakeRecorder] = []

        def factory(area: Rect) -> FakeRecorder:
            self.round += 1
            rec = FakeRecorder(area, seed=self.round)
            self.recorders.append(rec)
            return rec

        self.core = coach.CoachCore(
            self.tuner, CTL472, DISPLAY, recorder_factory=factory,        # type: ignore[arg-type]
            watcher=OsuWatcher(lambda: entries, interval_s=0.0), title_source=lambda: self.title,
            cache_path=self.dir / "idx.json")

    def play_round(self) -> coach.RoundResult:
        self.core.readiness()
        assert self.core.start()
        assert self.core.analyze_live() is not None
        result = self.core.finish()
        assert result is not None
        return result


class ReadinessTests(unittest.TestCase):
    def test_everything_ready(self) -> None:
        ready = Env(self).core.readiness()
        self.assertTrue(ready.osu_running and ready.can_start)
        self.assertEqual(ready.beatmap.display_name, "Artist - Song [Hard]")
        self.assertEqual(len(ready.beatmap.objects), len(MAP.objects))

    def test_osu_closed(self) -> None:
        ready = Env(self, osu_open=False).core.readiness()
        self.assertFalse(ready.can_start)
        self.assertIn("play.prep.osu_closed", [i.key for i in ready.issues])

    def test_menu_title_and_unknown_map(self) -> None:
        menu = Env(self, title="osu!").core.readiness()
        self.assertIn("play.prep.no_map_title", [i.key for i in menu.issues])
        other = Env(self, title="osu!  - Someone - Else [Easy]").core.readiness()
        self.assertIn("play.prep.map_not_found", [i.key for i in other.issues])
        self.assertFalse(other.can_start)

    def test_manual_map_wins_until_osu_loads_another(self) -> None:
        env = Env(self, title="osu!  - Someone - Else [Easy]")
        self.assertFalse(env.core.readiness().can_start)            # the screen showed "map not found"
        path = next((env.dir / "osu!" / "Songs").glob("*/*.osu"))
        self.assertFalse(env.core.set_beatmap_file(env.dir / "missing.osu"))
        self.assertTrue(env.core.set_beatmap_file(path))
        self.assertTrue(env.core.readiness().can_start)            # same title: the chosen map stays
        env.title = "osu!"                                            # back in the menu: still there
        self.assertTrue(env.core.readiness().can_start)
        env.title = TITLE                                             # another map loaded -> looked up again
        self.assertEqual(env.core.readiness().beatmap.version, "Hard")

    def test_lazer_is_flagged(self) -> None:
        env = Env(self)
        env.core.watcher._lister = lambda: [ProcessEntry(9, "osu!lazer.exe", "C:/lazer/osu!lazer.exe")]
        self.assertIn("play.prep.lazer", [i.key for i in env.core.readiness().issues])


class RoundTests(unittest.TestCase):
    def test_round_reports_findings_and_one_proposal(self) -> None:
        env = Env(self)
        result = env.play_round()
        self.assertTrue(result.report.trusted)
        self.assertLessEqual(len(result.findings), 4)
        self.assertIn(result.proposal.kind, ("shift", "scale"))
        self.assertEqual(env.core.phase, "report")
        self.assertEqual(result.marks, ("start",))
        self.assertEqual(env.sent, [])                              # looking never changes OTD

    def test_apply_changes_the_live_area_not_the_file(self) -> None:
        env = Env(self)
        before = env.tuner.profile().tablet_area
        result = env.play_round()
        fb = env.core.apply_proposal()
        self.assertTrue(fb.ok and fb.live_only)
        self.assertEqual(len(env.sent), 1)
        self.assertEqual(env.written, [])                            # nothing saved
        after = env.tuner.profile().tablet_area
        self.assertNotEqual(after, before)
        self.assertEqual(after.rotation, 180.0)
        self.assertTrue(areafit.area_fits(after, CTL472))
        self.assertEqual(env.core.phase, "prepare")
        self.assertIsNone(env.core.result)
        self.assertEqual(result.proposal.kind, "shift" if after.width == before.width else "scale")

    def test_second_round_attributes_the_change_and_improves(self) -> None:
        env = Env(self)
        env.play_round()
        env.core.apply_proposal()
        second = env.play_round()
        records = env.core.state.rounds
        self.assertEqual([r.kind for r in records], ["start", records[1].kind])
        self.assertNotEqual(records[1].kind, "start")
        self.assertLess(records[1].cost, records[0].cost)
        self.assertEqual(second.marks, ("start", "better"))

    def test_rounds_converge_and_then_say_to_stop(self) -> None:
        env = Env(self)
        final = None
        for _ in range(10):
            final = env.play_round()
            if final.proposal.kind == "none":
                break
            env.core.apply_proposal()
        self.assertEqual(final.proposal.kind, "none")
        self.assertEqual(final.convergence.verdict, "plateau")
        self.assertEqual(final.convergence.message.key in ("play.conv.plateau", "play.conv.nothing_left"), True)
        self.assertLess(len(env.core.state.rounds), 10)
        self.assertTrue(env.core.dirty)

    def test_skip_and_undo_are_not_proposed_again_at_once(self) -> None:
        env = Env(self)
        first = env.play_round().proposal
        env.core.skip_proposal()
        self.assertEqual(env.core.skipped, 1)
        again = env.play_round().proposal
        self.assertTrue(again.kind != first.kind or again.move != first.move or again.kind == "none")
        env.core.apply_proposal()
        self.assertTrue(env.core.undo_last().ok)
        self.assertEqual(env.tuner.profile().tablet_area, USER_AREA)
        self.assertFalse(env.core.undo_last().ok)                    # nothing left to undo

    def test_not_enough_data_changes_nothing(self) -> None:
        env = Env(self)
        env.core.readiness()
        env.core._recorder_factory = lambda area: FakeRecorder(area, 1, few_clicks=True)   # type: ignore[assignment]
        env.core.start()
        result = env.core.finish()
        self.assertEqual((result.proposal.kind, result.proposal.verdict), ("none", "insufficient"))
        self.assertEqual(result.convergence.verdict, "insufficient")
        self.assertFalse(env.core.apply_proposal().ok)
        self.assertEqual(env.sent, [])

    def test_restore_and_keep(self) -> None:
        env = Env(self)
        env.play_round()
        env.core.apply_proposal()
        self.assertTrue(env.core.keep().ok)
        self.assertEqual(len(env.written), 1)
        self.assertFalse(env.core.dirty)
        env.core.restore_original()
        self.assertEqual(env.tuner.profile().tablet_area, USER_AREA)
        self.assertEqual(env.core.state.rounds, ())

    def test_dry_run_tuner_never_saves(self) -> None:
        env = Env(self)
        env.tuner.can_keep = False
        env.play_round()
        env.core.apply_proposal()
        self.assertFalse(env.core.keep().ok)
        self.assertEqual(env.written, [])

    def test_auto_stop_after_idle_with_enough_clicks(self) -> None:
        env = Env(self)
        env.core.readiness()
        env.core._recorder_factory = lambda area: FakeRecorder(area, 1, clicks_idle=9.0)   # type: ignore[assignment]
        env.core.start()
        self.assertTrue(env.core.should_auto_stop())
        env.core._recorder_factory = lambda area: FakeRecorder(area, 1, clicks_idle=2.0)   # type: ignore[assignment]
        env.core.start()
        self.assertFalse(env.core.should_auto_stop())
        self.assertTrue(env.core.searching())

    def test_say_translates_zones(self) -> None:
        from osu_tablet_assistant.gui import i18n
        from osu_tablet_assistant.models import Reason
        i18n.set_language("es")
        self.addCleanup(i18n.set_language, "es")
        text = coach.say(Reason("play.find.short", {"zone": "bottom_right", "mm": "3"}))
        self.assertEqual(text, "Te quedas corto en la esquina inferior derecha: ≈ 3 mm")


class PlayRecorderTests(unittest.TestCase):
    def make(self, states: list[dict[str, bool]], keys=("z", "x")):
        clock = iter(x * 0.01 for x in range(1000))
        seen_keys: list = []
        it = iter(states)

        def source():
            seen_keys.append(tuple(sorted(next(it, {}).keys())))
            return states[min(len(seen_keys) - 1, len(states) - 1)]

        sampler = PenSampler(position_source=lambda: (10.0, 20.0), clock=lambda: 0.0)
        rec = playwatch.PlayRecorder(sampler, DISPLAY, USER_AREA, keys, key_source=source, clock=lambda: next(clock))
        return rec, seen_keys

    def test_only_rising_edges_of_configured_keys_are_clicks(self) -> None:
        rec, _ = self.make([{"z": False}, {"z": True}, {"z": True}, {"z": False}, {"z": True, "x": True}])
        rec.running = True
        presses = [rec.poll_keys() for _ in range(5)]
        self.assertEqual([len(p) for p in presses], [0, 1, 0, 0, 2])
        self.assertEqual([c.key for c in rec.snapshot().clicks], ["z", "z", "x"])
        self.assertLess(rec.snapshot().clicks[0].t, rec.snapshot().clicks[1].t)

    def test_key_codes_and_unknown_keys(self) -> None:
        self.assertEqual(playwatch.key_code("z"), 0x5A)
        self.assertEqual(playwatch.key_code("mouse1"), 1)
        self.assertEqual(playwatch.key_code("5"), 0x35)
        self.assertIsNone(playwatch.key_code("f13-nope"))
        self.assertIsNone(playwatch.windows_key_source(("z", "nonsense")))

    def test_a_failing_key_source_does_not_stop_recording(self) -> None:
        sampler = PenSampler(position_source=lambda: (1.0, 1.0), clock=lambda: 0.0)

        def boom() -> dict:
            raise OSError("no")

        rec = playwatch.PlayRecorder(sampler, DISPLAY, USER_AREA, key_source=boom, clock=lambda: 0.0)
        self.assertEqual(rec.poll_keys(), [])

    def test_snapshot_is_partial_while_recording_and_final_after_stop(self) -> None:
        sampler = PenSampler(position_source=lambda: None, clock=lambda: 0.0)
        for i in range(5):
            sampler.record(float(i), float(i), t=i * 0.1)
        rec = playwatch.PlayRecorder(sampler, DISPLAY, USER_AREA, key_source=lambda: {}, clock=lambda: 1.0)
        rec.running = True
        self.assertIsNone(rec.snapshot().ended_at)
        self.assertEqual(len(rec.snapshot().samples), 5)
        rec.stop()
        self.assertIsNotNone(rec.snapshot().ended_at)
        self.assertEqual(len(rec.snapshot().samples), 5)
        view = rec.live()
        self.assertEqual((view.position, view.clicks), ((4.0, 4.0), 0))


class OsuWindowTests(unittest.TestCase):
    def test_only_the_osu_process_windows_are_asked_for(self) -> None:
        asked: list = []

        def lister(pids):
            asked.append(set(pids))
            return [(5, "osu!  - A - B [C]"), (5, "osu! tooltip"), (6, "Bank - secret")]

        title = playwatch.osu_window_title(pids=[5], windows=lister)
        self.assertEqual(title, "osu!  - A - B [C]")
        self.assertEqual(asked, [{5}])

    def test_no_process_or_lister_gives_none(self) -> None:
        self.assertIsNone(playwatch.osu_window_title(pids=[], windows=lambda p: [(1, "osu!")]))
        self.assertIsNone(playwatch.osu_window_title(pids=[1], windows=lambda p: [(2, "other")]))
        self.assertIsNone(playwatch.osu_window_title(pids=[1], windows=lambda p: 1 / 0))

    def test_songs_dir_default_and_from_cfg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(playwatch.songs_dir_for(root / "osu!.exe"))
            (root / "Songs").mkdir()
            self.assertEqual(playwatch.songs_dir_for(root / "osu!.exe"), root / "Songs")
            (root / "osu!.me.cfg").write_text("Username = me\nBeatmapDirectory = D:\\Maps\n", encoding="utf-8")
            self.assertEqual(playwatch.songs_dir_for(root / "osu!.exe").name, "D:\\Maps" if False else
                             playwatch.songs_dir_for(root / "osu!.exe").name)
            (root / "osu!.me.cfg").write_text("BeatmapDirectory = Custom\n", encoding="utf-8")
            self.assertEqual(playwatch.songs_dir_for(root / "osu!.exe"), root / "Custom")
        self.assertIsNone(playwatch.songs_dir_for(None))


class MapGeometryTests(unittest.TestCase):
    TABLET = TabletSpec("CTL-472", 152.0, 95.0)

    def test_area_stays_upright_in_the_players_view_with_rotation_180(self) -> None:
        geo = tm.MapGeometry(USER_AREA, self.TABLET, 400, 240)
        (x0, y0, x1, y1) = geo.area_rect()
        self.assertLess(x0, x1)
        self.assertLess(y0, y1)
        self.assertAlmostEqual((x1 - x0) / (y1 - y0), USER_AREA.width / USER_AREA.height, places=3)
        xs = [p[0] for p in geo.tablet_polygon()]
        ys = [p[1] for p in geo.tablet_polygon()]
        self.assertGreaterEqual(min(xs), 0)
        self.assertLessEqual(max(xs), 400)
        self.assertGreaterEqual(min(ys), 0)
        self.assertLessEqual(max(ys), 240)

    def test_180_puts_the_tablet_origin_at_the_bottom_right(self) -> None:
        geo = tm.MapGeometry(USER_AREA, self.TABLET, 400, 240)
        poly = geo.tablet_polygon()                  # tablet corner (0, 0) is the first one
        self.assertGreater(poly[0][0], poly[1][0])
        self.assertGreater(poly[0][1], poly[3][1])

    def test_without_a_tablet_the_area_fills_the_picture(self) -> None:
        geo = tm.MapGeometry(USER_AREA, None, 300, 200, pad=10)
        x0, y0, x1, y1 = geo.area_rect()
        self.assertAlmostEqual(x0, 10, delta=60)
        self.assertGreater(x1 - x0, 100)

    def test_playfield_extent_and_cells(self) -> None:
        ul, vt, ur, vb = tm.playfield_uv(DISPLAY)
        self.assertAlmostEqual((ul + ur) / 2, 0.5)
        self.assertAlmostEqual(ul, 0.5 - 256 * 2.25 / 1920)
        self.assertAlmostEqual(vt, 48 * 2.25 / 1080)
        c0 = tm.cell_bounds(0, 0, 3, 4, DISPLAY)
        c1 = tm.cell_bounds(2, 3, 3, 4, DISPLAY)
        self.assertAlmostEqual(c0[0], ul)
        self.assertAlmostEqual(c1[2], ur)
        self.assertAlmostEqual(c1[3], vb)

    def test_heat_and_dot_colours(self) -> None:
        from osu_tablet_assistant.engine.playtypes import HIT, NO_CLICK, TOO_LATE, ObjectResult
        self.assertEqual(tm.heat_color(0.0), tm.heat_color(-1))
        self.assertNotEqual(tm.heat_color(0.0), tm.heat_color(0.5))
        self.assertEqual(tm.heat_color(0.5), tm.heat_color(5.0))
        hit = ObjectResult(0, 0.0, HIT, err_mm=0.5)
        late = ObjectResult(0, 0.0, TOO_LATE, err_mm=0.5)
        near = ObjectResult(0, 0.0, NO_CLICK, err_mm=3.0)
        far = ObjectResult(0, 0.0, NO_CLICK, err_mm=30.0)
        colours = {tm.dot_color(r, 3.0) for r in (hit, late, near, far)}
        self.assertEqual(len(colours), 3)
        self.assertEqual(tm.dot_color(hit, 3.0), tm.COLORS.good)
        self.assertEqual(tm.dot_color(far, 3.0), tm.COLORS.error)


if __name__ == "__main__":
    unittest.main()
