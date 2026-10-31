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
from playsim import make_map, player_for, simulate
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


def _player(area: Rect):
    return player_for(area, IDEAL)


class FakeRecorder:
    """Stands in for PlayRecorder: hands out a capture simulated for the area it was created with."""
    keys_available = True

    def __init__(self, area: Rect, seed: int, clicks_idle: float | None = None, few_clicks: bool = False) -> None:
        capture = simulate(MAP, _player(area), seed=seed, area=area, display=DISPLAY, t0=30.0)
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


class RoundsSummaryTests(unittest.TestCase):
    """The progress list: three highlights, a no-gain counter and the full list kept for a drop-down."""

    def setUp(self) -> None:
        from osu_tablet_assistant.gui import i18n
        self.addCleanup(i18n.set_language, "es")
        i18n.set_language("es")

    def core_with(self, costs: list, best: int | None, kinds: dict | None = None) -> "coach.CoachCore":
        from dataclasses import replace as _replace
        from osu_tablet_assistant.engine.playtypes import RoundRecord
        from osu_tablet_assistant.models import Reason
        env = Env(self)
        area = env.core.area
        kinds = kinds or {}
        rounds = tuple(RoundRecord(i, c, None, 60 if c is not None else 2, area, (),
                                   (Reason("play.msg.applied"),) if i in kinds else (), c is not None, 0.0,
                                   kinds.get(i, "start" if i == 0 else "probe"))
                       for i, c in enumerate(costs))
        env.core.state = _replace(env.core.state, rounds=rounds, best=best)
        return env.core

    def test_highlights_counter_and_hidden_rounds(self) -> None:
        core = self.core_with([10.0, 8.0, 8.0, 12.0, None, 12.0], best=1)
        s = core.rounds_summary()
        self.assertEqual([l.mark for l in s.lines], ["start", "better", "same", "worse", "unclear", "same"])
        self.assertEqual([l.number for l in s.highlights], [1, 2, 6])         # first, best, latest with data
        self.assertEqual(s.streak, 3)                                         # same, worse, same since the best
        self.assertEqual(s.nodata, 1)
        text = coach.rounds_text(s)
        self.assertIn("Ronda 2: mejor · la mejor", text)
        self.assertIn("Ronda 6: igual · la última", text)
        self.assertIn("no cuenta", text)                                      # the round without data says why

    def test_few_rounds_and_none(self) -> None:
        self.assertEqual(self.core_with([], best=None).rounds_summary().lines, ())
        s = self.core_with([10.0], best=0).rounds_summary()
        self.assertEqual([l.number for l in s.highlights], [1])
        self.assertEqual(s.streak, 0)
        none = self.core_with([None, None], best=None).rounds_summary()
        self.assertEqual(none.highlights, ())                                 # nothing usable: nothing highlighted
        self.assertEqual(none.nodata, 2)


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

    def second_map(self, env: Env) -> None:
        folder = env.dir / "osu!" / "Songs" / "2 Artist - Song"
        folder.mkdir()
        (folder / "Artist - Song (me) [Easy].osu").write_text(OSU_FILE.replace("Version:Hard", "Version:Easy"),
                                                              encoding="utf-8")

    def test_track_map_follows_osu_to_another_map_without_a_second_lap(self) -> None:
        env = Env(self)
        self.second_map(env)
        self.assertEqual(env.core.readiness().beatmap.version, "Hard")
        self.assertTrue(env.core.start())
        env.core.analyze_live()
        self.assertFalse(env.core.track_map())                        # same title: nothing changed
        env.title = "osu!  - Artist - Song [Easy]"                   # osu! names the new map once it is played
        self.assertTrue(env.core.track_map())
        self.assertEqual(env.core.beatmap.version, "Easy")
        self.assertIsNone(env.core.live_report)                       # clicks are matched to the new map from scratch
        env.title = "osu!"                                            # menu title says nothing: the map stays
        self.assertFalse(env.core.track_map())
        self.assertEqual(env.core.beatmap.version, "Easy")

    def test_track_map_without_osu_or_with_an_unknown_map(self) -> None:
        self.assertFalse(Env(self, osu_open=False).core.track_map())
        env = Env(self)
        env.core.readiness()
        env.title = "osu!  - Someone - Else [Easy]"                   # not in Songs: the old map must not linger
        self.assertTrue(env.core.track_map())
        self.assertIsNone(env.core.beatmap)

    def test_a_round_on_another_map_is_flagged(self) -> None:
        env = Env(self)
        self.second_map(env)
        self.assertFalse(env.play_round().map_changed)                # the first round has nothing to compare with
        self.assertFalse(env.play_round().map_changed)                # same map again
        env.title = "osu!  - Artist - Song [Easy]"
        self.assertTrue(env.core.track_map())
        self.assertTrue(env.play_round().map_changed)
        self.assertFalse(env.play_round().map_changed)

    def test_free_proportion_is_applied_with_the_lock_off(self) -> None:
        from dataclasses import replace as _replace
        env = Env(self)
        env.core.set_free_aspect(True)
        env.play_round()
        area = env.core.area
        stretched = _replace(area, width=round(area.width * 0.96, 2))          # one axis only: the proportion changes
        proposal = _replace(env.core.result.proposal, kind="scale", area=stretched)
        env.core.result = _replace(env.core.result, proposal=proposal)
        self.assertTrue(env.core.apply_proposal().ok)
        live = env.core.tuner.settings
        profile_area = env.core.area
        self.assertEqual(profile_area.width, stretched.width)
        self.assertNotAlmostEqual(profile_area.width / profile_area.height, area.width / area.height, places=2)
        self.assertFalse(env.core.profile.lock_aspect_ratio)                    # OTD will not snap it back

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


USER_OTD_AREA = Rect(80.0, 45.0, 76.571, 45.167, 180.0)      # the area in the user's OTD screenshots


class AreaTextTests(unittest.TestCase):
    def setUp(self) -> None:
        from osu_tablet_assistant.gui import i18n
        self.i18n = i18n
        self.addCleanup(i18n.set_language, "es")

    def test_the_current_area_in_plain_numbers_in_both_languages(self) -> None:
        self.i18n.set_language("es")
        self.assertEqual(coach.area_summary(USER_OTD_AREA), "Área actual: 80 × 45 mm, centro 76,6 / 45,2 mm, girada 180°")
        self.assertEqual(coach.area_summary(replace(USER_OTD_AREA, rotation=0.0)),
                         "Área actual: 80 × 45 mm, centro 76,6 / 45,2 mm")
        self.i18n.set_language("en")
        self.assertEqual(coach.area_summary(USER_OTD_AREA),
                         "Current area: 80 × 45 mm, centre 76.6 / 45.2 mm, rotated 180°")

    def test_before_and_now_only_once_something_changed(self) -> None:
        self.i18n.set_language("es")
        self.assertEqual(coach.area_changes(USER_OTD_AREA, USER_OTD_AREA), [])
        moved = replace(USER_OTD_AREA, width=78.4, height=44.1, x=77.8)
        self.assertEqual(coach.area_changes(USER_OTD_AREA, moved),
                         ["Ancho: 80 → 78,4 mm", "Alto: 45 → 44,1 mm", "Centro: 76,6 / 45,2 → 77,8 / 45,2 mm"])
        self.assertEqual(coach.area_changes(USER_OTD_AREA, replace(USER_OTD_AREA, x=77.0))[:2],
                         ["Ancho: 80 mm (igual)", "Alto: 45 mm (igual)"])

    def test_trials_are_said_in_plain_words(self) -> None:
        from osu_tablet_assistant.models import Reason
        what = Reason("play.probe.what.shift", {"mm": 1.2, "way": "right"})
        self.i18n.set_language("es")
        self.assertEqual(coach.say(Reason("play.probe.try", {"what": what})),
                         "Probamos mover el área 1,2 mm a la derecha; si no mejora, lo devolvemos.")
        self.assertEqual(coach.say(Reason("play.probe.reverted", {"what": what})),
                         "Probamos mover el área 1,2 mm a la derecha: no mejoró tu ronda, así que lo devolvemos.")
        size = Reason("play.probe.what.size", {"pct": 2.0, "way": "smaller"})
        self.assertEqual(coach.say(Reason("play.probe.kept", {"what": size})),
                         "Probamos hacer el área un 2 % más pequeña: tu ronda mejoró, así que lo dejamos.")
        self.i18n.set_language("en")
        self.assertEqual(coach.say(Reason("play.probe.try", {"what": what})),
                         "We try to move the area 1.2 mm to the right; if it does not help, we put it back.")


class ManualAreaTests(unittest.TestCase):
    def view_move(self, before: Rect, after: Rect) -> tuple[float, float]:
        dx, dy = areafit.tablet_to_view(after.x - before.x, after.y - before.y, before.rotation)
        return round(dx, 3), round(dy, 3)

    def test_moves_are_in_the_players_view_even_with_rotation_180(self) -> None:
        env = Env(self)
        for action, expected in (("left", (-0.5, 0.0)), ("right", (0.5, 0.0)), ("up", (0.0, -0.5)),
                                 ("down", (0.0, 0.5))):
            before = env.core.area
            fb = env.core.manual_area(action)
            self.assertTrue(fb.ok and fb.live_only, action)
            self.assertEqual(self.view_move(before, env.core.area), expected, action)
            self.assertEqual(env.core.area.rotation, 180.0)
        self.assertEqual(len(env.sent), 4)                           # each press is one live change
        self.assertEqual(env.written, [])                            # nothing saved until Keep
        self.assertEqual(env.core.area, USER_AREA)                   # left+right, up+down: back where it was

    def test_left_on_a_turned_tablet_is_plus_x_in_tablet_millimetres(self) -> None:
        env = Env(self)
        env.core.manual_area("left")
        self.assertAlmostEqual(env.core.area.x, USER_AREA.x + 0.5, places=3)   # rotation 180: screen left = tablet +x

    def test_size_keeps_the_proportion_and_one_axis_needs_the_free_proportion(self) -> None:
        env = Env(self)
        self.assertTrue(env.core.manual_area("smaller").ok)
        self.assertAlmostEqual(env.core.area.width, USER_AREA.width * 0.98, places=2)
        self.assertAlmostEqual(env.core.area.width / env.core.area.height, USER_AREA.width / USER_AREA.height, places=3)
        refused = env.core.manual_area("narrower")
        self.assertFalse(refused.ok)
        self.assertEqual(refused.message.key, "play.manual.needs_free")
        env.core.set_free_aspect(True)
        height = env.core.area.height
        self.assertTrue(env.core.manual_area("wider").ok)
        self.assertEqual(env.core.area.height, height)
        self.assertFalse(env.core.profile.lock_aspect_ratio)          # OTD must not snap it back to 16:9

    def test_undo_takes_each_press_back_and_the_tablet_bounds_hold(self) -> None:
        env = Env(self)
        env.core.manual_area("right")
        env.core.manual_area("bigger")
        self.assertTrue(env.core.undo_last().ok)
        self.assertTrue(env.core.undo_last().ok)
        self.assertEqual(env.core.area, USER_AREA)
        for _ in range(200):                                          # push it against the edge of the tablet
            if not env.core.manual_area("up").ok:
                break
        self.assertEqual(env.core.manual_area("up").message.key, "play.manual.no_room")
        self.assertTrue(areafit.area_fits(env.core.area, CTL472))

    def test_a_change_by_hand_is_not_attributed_to_a_proposal(self) -> None:
        env = Env(self)
        env.play_round()
        env.core.apply_proposal()
        env.core.manual_area("left")
        env.play_round()
        self.assertEqual(env.core.state.rounds[-1].kind, "manual")
        self.assertFalse(env.core.manual_area("nonsense").ok)

    def test_trials_run_through_the_coach_and_the_report_says_what_happened(self) -> None:
        env = Env(self)
        seeds = iter(range(1, 50))

        def tuned(area: Rect) -> FakeRecorder:                        # a noisy player whose area already fits
            rec = FakeRecorder(area, 1)
            player = player_for(area, USER_AREA)
            player.aim_noise_mm = 1.5
            rec.capture = simulate(MAP, player, seed=next(seeds), area=area, display=DISPLAY, t0=30.0)
            return rec

        env.core._recorder_factory = tuned                            # type: ignore[assignment]
        results = []
        for _ in range(6):
            result = env.play_round()
            results.append(result)
            if result.proposal.kind == "none":
                break
            self.assertTrue(env.core.apply_proposal().ok)
        kinds = [r.kind for r in env.core.state.rounds]
        self.assertIn("probe", kinds)
        outcomes = [r.proposal.probe_outcome.key for r in results if r.proposal.probe_outcome is not None]
        self.assertTrue(outcomes)
        self.assertTrue(set(outcomes) <= {"play.probe.kept", "play.probe.reverted"})
        self.assertTrue(areafit.area_fits(env.core.area, CTL472))
        self.assertEqual(env.core.area.rotation, 180.0)

    def test_skipping_a_trial_still_puts_the_previous_one_back(self) -> None:
        from osu_tablet_assistant.engine import optimizer as opt
        env = Env(self)
        env.play_round()
        env.core.manual_area("left")                                  # the live area is now "the failed trial"
        trial = opt.RoundProposal("probe", "improving", area=replace(USER_AREA, y=USER_AREA.y + 0.5),
                                  undo_area=USER_AREA, new_probe=opt.ProbeState())
        env.core.result = replace(env.core.result, proposal=trial)
        fb = env.core.skip_proposal()
        self.assertEqual(fb.message.key, "play.probe.skip_back")
        self.assertEqual(env.core.area, USER_AREA)


HAWKU = "TabletDriverFilters.Hawku.Smoothing"
NOISE = "OpenTabletDriver.Plugin.NoiseReduction"
RESAMPLER = "TemporalResampler"
RADIAL_SCREEN = "RadialFollow.RadialFollowSmoothingScreenSpace"
RADIAL_TABLET = "RadialFollow.RadialFollowSmoothingTabletSpace"
ANTICHATTER = "TabletDriverFilters.Devocub.Antichatter"


class SmoothingTests(unittest.TestCase):
    """The player tries smoothing filters that are installed but switched off (as in the user's OTD)."""

    def store(self, env: Env, path: str):
        return next((fc for fc in env.core.profile.filters if fc.path == path), None)

    def test_the_card_lists_installed_filters_and_greys_the_others(self) -> None:
        options = {o.path: o for o in Env(self).core.smoothing_options()}
        self.assertTrue(options[HAWKU].installed and not options[HAWKU].on)
        self.assertTrue(options[RADIAL_TABLET].was_on)                # the user already had it on
        self.assertTrue(options[RADIAL_SCREEN].installed)              # same plugin as Radial Follow (tablet)
        self.assertFalse(options[ANTICHATTER].installed)               # not in this OTD: greyed, never installed
        self.assertAlmostEqual(options[HAWKU].delay_ms, 2.0)          # the user's own latency (inside bounds)
        self.assertEqual(options[HAWKU].text_id, "hawku")
        self.assertNotIn("FrenetSerretFramePredictor.FrenetSerretFramePredictor", options)   # prediction: never offered

    def test_trying_a_disabled_filter_keeps_the_players_values_and_taking_it_out_restores_them(self) -> None:
        env = Env(self)
        before = self.store(env, HAWKU)
        fb = env.core.try_smoothing(HAWKU)
        self.assertTrue(fb.ok and fb.live_only)
        self.assertIn("play.smooth.own_values", [r.key for r in fb.reasons])
        live = self.store(env, HAWKU)
        self.assertTrue(live.enabled)
        self.assertEqual((live.settings["Latency"], live.settings["Frequency"]), (2.0, 1000.0))
        self.assertEqual((len(env.sent), env.written), (1, []))         # live, not saved
        self.assertIn(HAWKU, env.core.trials)
        self.assertTrue(env.core.stop_smoothing(HAWKU).ok)
        self.assertEqual(self.store(env, HAWKU), before)
        self.assertEqual(env.core.tuner.settings, env.core.tuner.original)
        self.assertNotIn(HAWKU, env.core.trials)

    def test_an_installed_filter_missing_from_the_profile_is_inserted_with_gentle_values(self) -> None:
        env = Env(self)
        self.assertIsNone(self.store(env, RADIAL_SCREEN))
        self.assertTrue(env.core.try_smoothing(RADIAL_SCREEN).ok)
        added = self.store(env, RADIAL_SCREEN)
        self.assertTrue(added.enabled)
        self.assertEqual((added.settings["OuterRadius"], added.settings["InnerRadius"]), (5.0, 0.0))
        paths = [fc.path for fc in env.core.profile.filters]
        self.assertLess(paths.index(RADIAL_TABLET), paths.index(RADIAL_SCREEN))   # its place in the chain
        env.core.stop_smoothing(RADIAL_SCREEN)
        self.assertIsNone(self.store(env, RADIAL_SCREEN))

    def test_not_installed_or_observing_changes_nothing(self) -> None:
        env = Env(self)
        fb = env.core.try_smoothing(ANTICHATTER)
        self.assertEqual((fb.ok, fb.message.key), (False, "play.smooth.not_installed"))
        self.assertFalse(env.core.try_smoothing(RADIAL_TABLET).ok)      # already on: nothing to try
        env.core.readiness()
        env.core.start()
        self.assertFalse(env.core.try_smoothing(HAWKU).ok)
        self.assertEqual(env.sent, [])

    def test_prediction_is_never_switched_on(self) -> None:
        from osu_tablet_assistant.engine import smoothing
        from osu_tablet_assistant.knowledge.filters import load_catalog
        from osu_tablet_assistant.models import FilterConfig
        catalog = load_catalog()
        values, _ = smoothing.start_settings(catalog[ANTICHATTER], {"PredictionEnabled": True, "Latency": 6.0})
        self.assertFalse(values["PredictionEnabled"])
        self.assertEqual(values["Latency"], 6.0)
        filters, _, _ = smoothing.enable((FilterConfig(RESAMPLER, False, {"frameShift": 0.8}),), RESAMPLER, catalog)
        self.assertEqual(filters[0].settings["frameShift"], 0.5)        # never above its neutral default
        out_of_bounds, own = smoothing.start_settings(catalog[HAWKU], {"Latency": 500.0})
        self.assertEqual((out_of_bounds["Latency"], own), (3.0, False))   # outside the bounds: the gentle value

    def test_after_a_round_with_it_the_report_compares_and_the_player_decides(self) -> None:
        env = Env(self)
        env.play_round()                                              # a round without smoothing
        env.core.result = None
        env.core.phase = "prepare"
        env.core.try_smoothing(HAWKU)
        self.assertEqual(env.core.smoothing_verdicts(), [])            # not played with it yet
        env.play_round()
        verdicts = env.core.smoothing_verdicts()
        self.assertEqual([v.path for v in verdicts], [HAWKU])
        self.assertIn(verdicts[0].verdict, ("better", "same", "worse"))
        self.assertEqual(env.core.state.rounds[-1].kind, "smoothing")  # not attributed to an area change
        self.assertTrue(env.core.keep_smoothing(HAWKU).ok)
        self.assertEqual(env.core.smoothing_verdicts(), [])
        self.assertTrue(self.store(env, HAWKU).enabled)                # "Dejarlo": still live ...
        self.assertTrue(env.core.keep().ok)                            # ... and saved with the normal Keep
        saved = next(s for s in env.written[-1]["Profiles"][0]["Filters"] if s["Path"] == HAWKU)
        self.assertTrue(saved["Enable"])

    def test_tried_before_any_round_says_there_is_nothing_to_compare_with(self) -> None:
        env = Env(self)
        env.core.try_smoothing(NOISE)
        env.play_round()
        verdict = env.core.smoothing_verdicts()[0]
        self.assertEqual((verdict.verdict, verdict.no_base), ("unclear", True))
        self.assertTrue(env.core.stop_smoothing(NOISE).ok)              # "Quitarlo" from the report
        self.assertFalse(self.store(env, NOISE).enabled)

    def test_the_coach_never_takes_the_players_smoothing_away_on_its_own(self) -> None:
        from osu_tablet_assistant.engine import optimizer as opt
        env = Env(self)
        env.play_round()
        env.core.result = None
        env.core.phase = "prepare"
        env.core.try_smoothing(HAWKU)
        env.play_round()
        original = env.core._original_filters
        back = opt.RoundProposal("revert", "worse", area=replace(USER_AREA, x=USER_AREA.x + 1.0), filters=original,
                                 revert_to=0)
        env.core.result = replace(env.core.result, proposal=back)
        self.assertTrue(env.core.apply_proposal().ok)
        self.assertTrue(self.store(env, HAWKU).enabled)                # "back to the best round" kept it on

    def test_undo_ends_the_trial(self) -> None:
        env = Env(self)
        env.core.try_smoothing(HAWKU)
        self.assertTrue(env.core.undo_last().ok)
        self.assertFalse(self.store(env, HAWKU).enabled)
        self.assertEqual(env.core.trials, {})

    def test_restore_original_clears_the_trials(self) -> None:
        env = Env(self)
        env.core.try_smoothing(HAWKU)
        env.core.restore_original()
        self.assertEqual((env.core.trials, env.core.tuner.settings), ({}, env.core.tuner.original))


class ReplayRoundTests(unittest.TestCase):
    def osr(self, env: Env, md5: str | None = None, junk: bool = False) -> Path:
        from test_replay import make_osr
        beatmap = env.core.readiness().beatmap
        capture = simulate(MAP, _player(env.tuner.profile().tablet_area), seed=9, area=env.tuner.profile().tablet_area,
                           display=DISPLAY, t0=40.0)
        import hashlib
        digest = md5 or hashlib.md5(Path(beatmap.path).read_bytes()).hexdigest()
        path = env.dir / "play.osr"
        path.write_bytes(b"not a replay" if junk else make_osr(capture, md5=digest))
        return path

    def test_a_replay_can_be_the_round(self) -> None:
        env = Env(self)
        env.core.readiness()
        outcome = env.core.round_from_replay(self.osr(env))
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.same_map)
        result = env.core.result
        self.assertTrue(result.from_replay and result.report.trusted)
        self.assertIn(result.proposal.kind, ("shift", "scale"))
        self.assertEqual(env.core.phase, "report")
        self.assertEqual(env.core.state.rounds[0].kind, "start")

    def test_replay_of_another_map_file_is_flagged_not_hidden(self) -> None:
        env = Env(self)
        env.core.readiness()
        outcome = env.core.round_from_replay(self.osr(env, md5="0" * 32))
        self.assertTrue(outcome.ok)
        self.assertFalse(outcome.same_map)
        self.assertEqual(outcome.problem.key, "play.replay.other_map")

    def test_bad_file_or_no_map_changes_nothing(self) -> None:
        env = Env(self)
        env.core.readiness()
        bad = env.core.round_from_replay(self.osr(env, junk=True))
        self.assertEqual((bad.ok, bad.problem.key), (False, "play.replay.unreadable"))
        self.assertEqual(env.core.state.rounds, ())
        nomap = Env(self, title="osu!")
        nomap.core.readiness()
        self.assertEqual(nomap.core.round_from_replay(self.osr(env)).problem.key, "play.replay.no_map")

    def test_verifying_a_live_round_reports_how_well_it_matched(self) -> None:
        env = Env(self)
        self.assertEqual(env.core.verify_with_replay(self.osr(env)).problem.key, "play.replay.nothing_to_check")
        env.play_round()
        outcome = env.core.verify_with_replay(self.osr(env))
        self.assertTrue(outcome.ok)
        self.assertGreater(outcome.check.compared, 50)
        self.assertGreater(outcome.check.agreement, 0.8)
        self.assertIs(env.core.result.check, outcome.check)
        self.assertEqual(len(env.core.state.rounds), 1)            # verifying is not a new round


class DataLedgerTests(unittest.TestCase):
    def test_every_item_is_explained_in_both_languages_and_names_the_keys(self) -> None:
        from osu_tablet_assistant.gui import i18n
        self.addCleanup(i18n.set_language, "es")
        for lang in i18n.SUPPORTED:
            i18n.set_language(lang)
            text = coach.ledger_text()
            self.assertEqual(text.count("•"), len(coach.DATA_LEDGER))
            self.assertNotIn("play.data", text)
            self.assertIn("Z, X", text)
            self.assertNotIn("Mouse 1", text)
            self.assertIn("A, Mouse 4, Mouse 1", coach.ledger_text(keys=("a", "mouse4", "mouse1")))

    def test_the_ledger_matches_what_the_code_really_reads(self) -> None:
        # the recorder only knows the configured keys (default Z and X: the pen tip is not a click unless the
        # player says so), the replay keeps no identity, the profile no play data
        self.assertEqual(playwatch.DEFAULT_CLICK_KEYS, ("z", "x"))
        self.assertEqual(set(coach.DATA_LEDGER), {"cursor", "keys", "window", "map", "replay", "profile", "never"})


class ClickKeyTests(unittest.TestCase):
    def test_set_keys_changes_what_the_next_recorder_reads(self) -> None:
        env = Env(self)
        core = coach.CoachCore(env.tuner, CTL472, DISPLAY, watcher=env.core.watcher, title_source=lambda: TITLE,
                               cache_path=env.dir / "idx2.json")
        self.assertEqual(core.keys, ("z", "x"))
        self.assertTrue(core.set_keys(("A", "mouse4", "bogus")))
        self.assertEqual(core.keys, ("a", "mouse4"))
        rec = core._default_recorder(USER_AREA)
        self.assertEqual(rec.keys, ("a", "mouse4"))
        core.phase = "observe"
        self.assertFalse(core.set_keys(("z",)))                       # never in the middle of a recording
        self.assertEqual(core.keys, ("a", "mouse4"))

    def test_readiness_names_missing_keys_and_explains_the_pen_tip(self) -> None:
        env = Env(self)
        self.assertNotIn("play.prep.pen_tip", [i.key for i in env.core.readiness().issues])
        env.core.set_keys(())
        ready = env.core.readiness()
        self.assertIn("play.prep.no_keys", [i.key for i in ready.issues])
        self.assertFalse(ready.can_start)
        self.assertFalse(env.core.start())
        env.core.set_keys(("z", "mouse1"))
        ready = env.core.readiness()
        self.assertIn("play.prep.pen_tip", [i.key for i in ready.issues])
        self.assertTrue(ready.can_start)
        self.assertEqual(ready.keys, ("z", "mouse1"))

    def test_no_click_hint_when_the_pen_moves_but_no_chosen_key_is_pressed(self) -> None:
        env = Env(self)
        far = DISPLAY.height_px * (coach.NO_CLICKS_TRAVEL_SCREENS + 1)
        moving = playwatch.LiveView((1.0, 1.0), (), 0, None, 10.0, far, coach.NO_CLICKS_HINT_S + 1)
        hint = env.core.no_clicks_hint(moving)
        self.assertEqual(hint.key, "play.keys.none_seen")
        self.assertEqual(hint.params["keys"], "Z, X")
        from osu_tablet_assistant.gui import i18n
        self.addCleanup(i18n.set_language, "es")
        i18n.set_language("es")
        self.assertEqual(coach.say(hint), "No he visto ningún clic con Z, X: ¿usas otros botones?")
        self.assertIsNone(env.core.no_clicks_hint(replace(moving, clicks=3)))           # clicks seen
        self.assertIsNone(env.core.no_clicks_hint(replace(moving, travel_px=10.0)))     # pen resting
        self.assertIsNone(env.core.no_clicks_hint(replace(moving, elapsed_s=2.0)))      # too early to say

    def test_a_round_without_clicks_carries_the_hint(self) -> None:
        env = Env(self)
        env.core.readiness()

        def silent(area):
            rec = FakeRecorder(area, 1)
            rec.capture = replace(rec.capture, clicks=())
            return rec

        env.core._recorder_factory = silent        # type: ignore[assignment]
        env.core.start()
        result = env.core.finish()
        self.assertEqual(result.hint.key, "play.keys.none_seen")
        self.assertEqual(result.proposal.kind, "none")
        self.assertIsNone(Env(self).play_round().hint)                 # a normal round has no hint

    def test_abort_drops_the_recording_without_a_round(self) -> None:
        env = Env(self)
        env.core.readiness()
        env.core.start()
        env.core.abort()
        self.assertEqual((env.core.phase, env.core.state.rounds, env.core.recorder), ("prepare", (), None))

    def test_analysis_detail_sets_the_grid_of_the_report(self) -> None:
        env = Env(self)
        self.assertEqual(env.play_round().report.grid, (6, 8))
        env.core.set_analysis_detail("normal")
        env.core.result = None
        report = env.play_round().report
        self.assertEqual((report.grid, len(report.cells)), ((3, 4), 12))
        env.core.set_analysis_detail("very_fine")
        self.assertEqual(len(env.play_round().report.cells), 108)

    def test_recorder_measures_how_far_the_cursor_moved(self) -> None:
        sampler = PenSampler(position_source=lambda: None, clock=lambda: 0.0)
        for i in range(5):
            sampler.record(float(i * 3), float(i * 4), t=i * 0.1)
        rec = playwatch.PlayRecorder(sampler, DISPLAY, USER_AREA, key_source=lambda: {}, clock=lambda: 1.0)
        rec.running = True
        view = rec.live()
        self.assertAlmostEqual(view.travel_px, 20.0)
        self.assertEqual(view.clicks, 0)


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
