"""engine.optimizer: cost, stop rule, one-change proposals and closed-loop convergence on a simulated player."""
from __future__ import annotations

import math
import statistics
import unittest
from dataclasses import replace

from osu_tablet_assistant.engine import areafit, optimizer as opt, recommend
from osu_tablet_assistant.engine import playobserve as po
from osu_tablet_assistant.engine.playtypes import (IMPROVING, INSUFFICIENT, PLATEAU, START, WORSE, FeelFit, Hotspot,
                                                    PlayReport, ReachFit, RoundRecord)
from osu_tablet_assistant.models import FilterConfig, Priority, ProfileInfo, Rect, UserPreferences
from playsim import AREA, TABLET, Player, make_map, player_for, simulate

MAP = make_map(seed=11, n=90, gap_ms=320.0)
IDEAL = Rect(64.0, 37.0, 78.0, 49.0, 180.0)      # the area this simulated hand "wants"


def profile(area: Rect = AREA, filters: tuple = ()) -> ProfileInfo:
    return ProfileInfo("CTL-472", "Output", Rect(1920, 1080, 960, 540), area, True, filters)


def play(area: Rect, seed: int, ideal: Rect = IDEAL, **kw) -> PlayReport:
    capture = simulate(MAP, player_for(area, ideal, **kw), seed=seed, area=area, t0=20.0 + seed, )
    return po.analyze_play(MAP, replace(capture, samples=capture.samples[::2]), seed=seed)


def fake_report(trusted: bool = True, n: int = 60, miss: float = 0.05, err: float = 0.3, **kw) -> PlayReport:
    from osu_tablet_assistant.engine.playtypes import Alignment, ObjectResult, HIT, TOO_FAR
    results = tuple(ObjectResult(i, i * 300.0, HIT if i % 100 >= miss * 100 else TOO_FAR, i * 0.3,
                                 (i % 7 - 3) * 2.0, 0, 0, err * 3.0, 0, 0, 0.5, 0.5, 0.5, 0.5)
                    for i in range(n))
    base = dict(alignment=Alignment(0, 1, False, 0.9 if trusted else 0.2, n), n_objects=n, n_judged=n,
                results=results, radius_mm=3.0, reach=ReachFit(1.0, 1.0, 0.0, 0.0, (0.98, 1.02), (0.98, 1.02), n),
                feel=FeelFit(0.0, 10, 0.02, 10, 600.0))
    base.update(kw)
    return PlayReport(**base)


class CostTests(unittest.TestCase):
    def test_cost_is_absolute_and_lower_for_better_play(self) -> None:
        good = fake_report(miss=0.02, err=0.2)
        bad = fake_report(miss=0.30, err=0.8)
        c_good, c_bad = opt.round_cost(good)[0], opt.round_cost(bad)[0]
        self.assertLess(c_good, c_bad)
        self.assertEqual(opt.round_cost(good)[0], c_good)          # nothing else influences it
        self.assertTrue(0.0 <= c_good < 1.0)

    def test_interval_contains_the_cost_and_is_deterministic(self) -> None:
        cost, ci = opt.round_cost(fake_report())
        self.assertLessEqual(ci[0], cost)
        self.assertGreaterEqual(ci[1], cost)
        self.assertEqual(opt.round_cost(fake_report()), (cost, ci))

    def test_untrusted_or_short_reports_have_no_cost(self) -> None:
        self.assertEqual(opt.round_cost(fake_report(trusted=False)), (None, None))
        self.assertEqual(opt.round_cost(fake_report(n=10)), (None, None))
        self.assertEqual(opt.round_cost(PlayReport()), (None, None))

    def test_reach_and_feel_matter(self) -> None:
        base = opt.round_cost(fake_report())[0]
        off = fake_report(reach=ReachFit(0.85, 0.85, 0, 0, (0.8, 0.9), (0.8, 0.9), 60))
        self.assertGreater(opt.round_cost(off)[0], base)
        laggy = fake_report(feel=FeelFit(-3.0, 10, 0.02, 10, 600.0))
        self.assertGreater(opt.round_cost(laggy)[0], base)

    def test_priority_shifts_weight_between_lag_and_jitter(self) -> None:
        laggy = fake_report(feel=FeelFit(-3.0, 10, 0.02, 10, 600.0))
        latency = UserPreferences(priority=Priority.LATENCY)
        smooth = UserPreferences(priority=Priority.SMOOTHNESS)
        self.assertGreater(opt.round_cost(laggy, latency)[0], opt.round_cost(laggy, smooth)[0])


def rec(i: int, cost: float | None, noise: float = 0.0, accepted: bool = True, kind: str = "shift") -> RoundRecord:
    ci = None if cost is None else (cost - noise, cost + noise)
    return RoundRecord(i, cost, ci, 50, AREA, (), (), accepted, 1.0, kind)


class ConvergenceTests(unittest.TestCase):
    def verdict(self, *recs: RoundRecord) -> str:
        return opt.convergence(replace(opt.new_state(), rounds=tuple(recs))).verdict

    def test_verdicts(self) -> None:
        self.assertEqual(self.verdict(), START)
        self.assertEqual(self.verdict(rec(0, 0.5)), START)
        self.assertEqual(self.verdict(rec(0, 0.5), rec(1, 0.4)), IMPROVING)
        self.assertEqual(self.verdict(rec(0, 0.5), rec(1, 0.6)), WORSE)
        self.assertEqual(self.verdict(rec(0, 0.5), rec(1, 0.4), rec(2, None, accepted=False)), INSUFFICIENT)

    def test_plateau_needs_two_small_gains(self) -> None:
        self.assertEqual(self.verdict(rec(0, 0.50), rec(1, 0.40), rec(2, 0.395)), IMPROVING)
        self.assertEqual(self.verdict(rec(0, 0.50), rec(1, 0.40), rec(2, 0.395), rec(3, 0.394)), PLATEAU)

    def test_noise_widens_the_plateau(self) -> None:
        noisy = (rec(0, 0.50, 0.05), rec(1, 0.45, 0.05), rec(2, 0.42, 0.05))
        self.assertEqual(self.verdict(*noisy), PLATEAU)    # 7 % gains are inside a 10 % noise
        quiet = (rec(0, 0.50, 0.0), rec(1, 0.45, 0.0), rec(2, 0.42, 0.0))
        self.assertEqual(self.verdict(*quiet), IMPROVING)


class ProposalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = opt.new_state()

    def first(self, area: Rect, ideal: Rect = IDEAL, **kw):
        report = play(area, 1, ideal, **kw)
        state, report = opt.record_round(self.state, report, area, ())
        return state, report

    def test_cursor_landing_right_moves_the_area_right_in_the_players_view(self) -> None:
        ideal = replace(AREA, x=AREA.x - 2.0)        # rotation 180: tablet x - 2  =  view +2 mm
        state, report = self.first(AREA, ideal)
        self.assertGreater(report.reach.bias_x_mm, 1.0)
        proposal = opt.propose(state, profile(), report, TABLET)
        self.assertEqual(proposal.kind, "shift")
        self.assertGreater(proposal.move[0], 0.5)
        self.assertLess(proposal.area.x, AREA.x)     # the area moved to where the hand really is
        self.assertTrue(areafit.area_fits(proposal.area, TABLET))
        self.assertEqual((proposal.area.width, proposal.area.rotation), (AREA.width, 180.0))
        self.assertEqual(proposal.reasons[0].key, "play.prop.shift")

    def test_gain_below_one_proposes_a_smaller_area(self) -> None:
        ideal = replace(AREA, width=AREA.width * 0.94, height=AREA.height * 0.94)
        state, report = self.first(AREA, ideal)
        proposal = opt.propose(state, profile(), report, TABLET)
        self.assertEqual(proposal.kind, "scale")
        self.assertLess(proposal.area.width, AREA.width)
        self.assertAlmostEqual(proposal.area.width / proposal.area.height, AREA.width / AREA.height, places=2)
        self.assertGreaterEqual(proposal.area.width, AREA.width * (1 - opt.MAX_SCALE_STEP) - 0.01)

    def test_one_physical_change_per_proposal(self) -> None:
        ideal = Rect(AREA.width * 0.94, AREA.height * 0.94, AREA.x - 2, AREA.y, 180.0)
        state, report = self.first(AREA, ideal)
        proposal = opt.propose(state, profile(), report, TABLET)
        self.assertEqual(proposal.kind, "shift")                  # shift first ...
        self.assertEqual((proposal.area.width, proposal.area.height), (AREA.width, AREA.height))   # ... size untouched

    def test_insufficient_data_changes_nothing(self) -> None:
        short = PlayReport()
        state, short = opt.record_round(self.state, short, AREA, ())
        proposal = opt.propose(state, profile(), short, TABLET)
        self.assertEqual((proposal.kind, proposal.verdict), ("none", INSUFFICIENT))
        self.assertIsNone(proposal.changes)
        self.assertEqual(proposal.message.key, "play.msg.play_more")
        self.assertFalse(state.rounds[-1].accepted)

    def test_never_leaves_the_tablet(self) -> None:
        edge = Rect(71.66, 41.44, 152.0 - 35.9, 47.5, 180.0)
        ideal = replace(edge, x=edge.x - 5.0)
        state, report = self.first(edge, ideal)
        proposal = opt.propose(state, profile(edge), report, TABLET)
        if proposal.area is not None:
            self.assertTrue(areafit.area_fits(proposal.area, TABLET))

    def test_a_worse_round_halves_trust_and_the_move_is_not_repeated(self) -> None:
        good = fake_report(miss=0.02, err=0.2)
        worse = fake_report(miss=0.35, err=0.9)
        state, _ = opt.record_round(self.state, good, AREA, ())
        state, _ = opt.record_round(state, worse, AREA, (), kind="shift", move=(2.0, 0.0))
        self.assertEqual(state.trust, opt.START_TRUST / 2)
        self.assertIn(("shift", 1, 0), state.undone)
        report = fake_report(reach=ReachFit(1.0, 1.0, 3.0, 0.0, (0.99, 1.01), (0.99, 1.01), 60))
        self.assertNotEqual(opt.propose(state, profile(), report, TABLET).kind, "shift")

    def test_returns_to_the_best_round_when_worse_by_more_than_noise(self) -> None:
        a2 = replace(AREA, x=AREA.x - 3)
        state, _ = opt.record_round(self.state, fake_report(miss=0.02, err=0.2), AREA, ())
        state, report = opt.record_round(state, fake_report(miss=0.35, err=0.9), a2, (), kind="shift",
                                         move=(3.0, 0.0))
        proposal = opt.propose(state, profile(a2), report, TABLET)
        self.assertEqual((proposal.kind, proposal.revert_to), ("revert", 0))
        self.assertEqual(proposal.area, AREA)

    def test_filters_only_through_adapt_and_only_one_side_at_a_time(self) -> None:
        radial = FilterConfig(recommend.RADIAL_TABLET, True,
                              {"OuterRadius": 0.5, "InnerRadius": 0.1, "SmoothingCoefficient": 0.5})
        flat = fake_report(reach=ReachFit(1.0, 1.0, 0.0, 0.0, (0.99, 1.01), (0.99, 1.01), 60))
        state, _ = opt.record_round(self.state, flat, AREA, ())
        laggy = replace(flat, feel=FeelFit(-3.0, 12, 0.02, 12, 600.0))
        both = replace(flat, feel=FeelFit(-3.0, 12, 0.2, 12, 600.0))
        prof = profile(filters=(radial,))
        self.assertEqual(opt.propose(state, prof, both, TABLET).kind, "none")
        self.assertEqual(opt.propose(state, prof, flat, TABLET, allow_filters=True).kind, "none")
        proposal = opt.propose(state, prof, laggy, TABLET)
        self.assertEqual(proposal.kind, "filter")
        self.assertIsNotNone(proposal.new_adapt)
        self.assertIsNone(opt.propose(state, prof, laggy, TABLET, allow_filters=False).changes)

    def test_plateau_when_nothing_is_left(self) -> None:
        flat = fake_report(reach=ReachFit(1.0, 1.0, 0.1, 0.0, (0.99, 1.01), (0.99, 1.01), 60))
        state = self.state
        for _ in range(3):
            state, flat2 = opt.record_round(state, flat, AREA, ())
        proposal = opt.propose(state, profile(), flat2, TABLET)
        self.assertEqual((proposal.kind, proposal.verdict), ("none", PLATEAU))


def spot(dx: float, dy: float, share: float, n: int = 40, fx: float = 0.9, fy: float = 0.9) -> Hotspot:
    return Hotspot("short", 4, 6, 5, 7, 0.8, 0.8, fx, fy, 6.0, 6.0, n, dx, dy, math.hypot(dx, dy), 9.0, share,
                   0.25, 0.33)


class LocalProblemTests(unittest.TestCase):
    """A problem in one small zone must not move the whole area much (fine-grid hotspots)."""

    def propose(self, *spots: Hotspot):
        report = fake_report(hotspots=spots)
        state, report = opt.record_round(opt.new_state(), report, AREA, ())
        return opt.propose(state, profile(), report, TABLET)

    def test_one_small_patch_suggests_practice_not_a_change(self) -> None:
        proposal = self.propose(spot(-1.5, -1.5, share=0.05))
        self.assertEqual((proposal.kind, proposal.verdict), ("none", PLATEAU))
        self.assertEqual(proposal.message.key, "play.msg.practice_zone")
        self.assertEqual(proposal.message.params["place"], "bottom.right.corner")
        self.assertIsNone(proposal.changes)

    def test_a_large_region_moves_the_area_in_proportion_to_its_share(self) -> None:
        proposal = self.propose(spot(-2.0, 0.0, share=0.4, n=60))
        self.assertEqual(proposal.kind, "shift")
        self.assertEqual(proposal.message.key, "play.msg.shift_local")
        self.assertAlmostEqual(proposal.move[0], -0.8, delta=0.15)       # 2 mm x 40 % of the play
        self.assertLessEqual(math.hypot(*proposal.move), opt.LOCAL_MAX_SHIFT_MM + 1e-6)
        self.assertIn("play.prop.local", [r.key for r in proposal.reasons])
        self.assertTrue(areafit.area_fits(proposal.area, TABLET))

    def test_opposite_regions_cancel_and_few_objects_never_move_anything(self) -> None:
        self.assertEqual(self.propose(spot(-2.0, 0.0, 0.4, fx=0.9), spot(2.0, 0.0, 0.4, fx=0.1)).kind, "none")
        self.assertEqual(self.propose(spot(-3.0, 0.0, 0.5, n=10)).kind, "none")

    def test_global_problems_still_come_first(self) -> None:
        shifted = fake_report(hotspots=(spot(-2.0, 0.0, 0.4, n=60),),
                              reach=ReachFit(1.0, 1.0, 2.0, 0.0, (0.99, 1.01), (0.99, 1.01), 60))
        state, shifted = opt.record_round(opt.new_state(), shifted, AREA, ())
        proposal = opt.propose(state, profile(), shifted, TABLET)
        self.assertEqual((proposal.kind, proposal.message.key), ("shift", "play.msg.shift"))

    def test_a_simulated_corner_patch_does_not_move_the_area(self) -> None:
        beatmap = make_map(seed=201, n=300, gap_ms=300)
        player = Player(miss_rate=0.02, aim_noise_mm=0.4, hz=250, patch=(0.85, 0.85, 1.0, 1.0), patch_short_mm=2.0)
        report = po.analyze_play(beatmap, simulate(beatmap, player, seed=1, area=AREA))
        self.assertTrue(report.hotspots)
        state, report = opt.record_round(opt.new_state(), report, AREA, ())
        proposal = opt.propose(state, profile(), report, TABLET, allow_filters=False)
        self.assertNotEqual(proposal.kind, "scale")
        self.assertLess(proposal.move_mm, opt.MIN_SHIFT_MM + 1e-9)
        self.assertEqual(proposal.message.key, "play.msg.practice_zone")


class ClosedLoopTests(unittest.TestCase):
    MAX_ROUNDS = 14

    def run_loop(self, seed: int) -> tuple[list[float], list[str], Rect]:
        state, area, kind, move, change, new_probe = opt.new_state(), AREA, "start", (0.0, 0.0), (), None
        costs, kinds = [], []
        for n in range(self.MAX_ROUNDS):
            report = play(area, 1000 * seed + n)
            state, report = opt.record_round(state, report, area, (), change=change, kind=kind, move=move,
                                             new_probe=new_probe)
            if report.cost is not None:
                costs.append(report.cost)
            kinds.append(kind)
            proposal = opt.propose(state, profile(area), report, TABLET)
            if proposal.kind == "none":
                if proposal.verdict == PLATEAU:
                    break
                continue                                       # not enough data: play the same again
            area = proposal.area or area
            kind, move, change, new_probe = proposal.kind, proposal.move, proposal.reasons, proposal.new_probe
        return costs, kinds, area

    def test_converges_in_under_twelve_rounds_without_oscillating(self) -> None:
        rounds, reverts, final_gaps, first_last = [], 0, [], []
        for seed in range(20):
            costs, kinds, area = self.run_loop(seed)
            rounds.append(len(kinds))
            reverts += kinds.count("revert")
            final_gaps.append(abs(area.width / IDEAL.width - 1.0))
            first_last.append((costs[0], min(costs)))
        self.assertLess(statistics.fmean(rounds), 12, rounds)
        self.assertLessEqual(reverts / 20, 1.0)
        self.assertLess(statistics.fmean(final_gaps), 0.08, final_gaps)
        self.assertTrue(all(best <= first for first, best in first_last))
        self.assertLess(statistics.fmean(b / f for f, b in first_last), 0.9)


def weak(bias_x: float = 0.0, gain: float = 1.0, gain_y: float | None = None, **kw) -> PlayReport:
    """A report whose evidence is too weak for any normal stage (gain intervals include 1, bias below 0.5 mm)."""
    gy = gain if gain_y is None else gain_y
    return fake_report(reach=ReachFit(gain, gy, bias_x, 0.0, (0.94, 1.04), (0.94, 1.04), 60), **kw)


class ProbeTests(unittest.TestCase):
    """Area trials when the evidence is weak: labelled, small, bounded, judged by the next round."""

    def first(self, report: PlayReport, aspect_lock: bool = True):
        state, report = opt.record_round(opt.new_state(), report, AREA, ())
        return state, opt.propose(state, profile(), report, TABLET, aspect_lock=aspect_lock)

    def test_weak_bias_gives_a_small_labelled_trial_shift_in_the_players_view(self) -> None:
        state, proposal = self.first(weak(bias_x=0.3))
        self.assertEqual(proposal.kind, "probe")
        self.assertEqual(proposal.message.key, "play.msg.probe")
        self.assertEqual(proposal.reasons[0].key, "play.probe.try")
        what = proposal.reasons[0].params["what"]
        self.assertEqual((what.key, what.params["way"]), ("play.probe.what.shift", "right"))
        self.assertAlmostEqual(proposal.move[0], opt.PROBE_MIN_SHIFT_MM, places=3)   # a 0.3 mm hint: the minimum
        self.assertAlmostEqual(proposal.area.x, AREA.x - opt.PROBE_MIN_SHIFT_MM, places=3)   # rotation 180: view +x = tablet -x
        self.assertEqual((proposal.area.width, proposal.area.rotation), (AREA.width, 180.0))
        self.assertTrue(areafit.area_fits(proposal.area, TABLET))
        self.assertIsNotNone(proposal.new_probe.live)
        self.assertIsNone(proposal.probe_outcome)

    def test_weak_gain_gives_a_capped_size_trial(self) -> None:
        _, proposal = self.first(weak(gain=0.95))                 # a 5 % hint, capped at 3 %
        self.assertEqual(proposal.kind, "probe")
        self.assertEqual(proposal.reasons[0].params["what"].params["way"], "smaller")
        self.assertAlmostEqual(proposal.area.width / AREA.width, 1.0 - opt.PROBE_MAX_SCALE, places=3)
        self.assertAlmostEqual(proposal.area.width / proposal.area.height, AREA.width / AREA.height, places=3)
        _, small = self.first(weak(gain=0.998))                   # below the hint floor: nothing to try
        self.assertEqual(small.kind, "none")

    def test_free_proportion_tries_one_axis(self) -> None:
        _, proposal = self.first(weak(gain=0.97, gain_y=1.0), aspect_lock=False)
        self.assertEqual(proposal.kind, "probe")
        self.assertEqual(proposal.reasons[0].params["what"].params["way"], "narrower")
        self.assertLess(proposal.area.width, AREA.width)
        self.assertEqual(proposal.area.height, AREA.height)

    def test_edges_that_fall_short_suggest_a_smaller_area(self) -> None:
        from osu_tablet_assistant.engine.playtypes import EdgeFinding
        edges = tuple(EdgeFinding(z, "short", 0.6, 10) for z in ("left", "right", "top", "bottom"))
        hints = opt.probe_hints(weak(edges=edges))
        self.assertEqual(hints[0][:2], ("size", "both"))
        self.assertLess(hints[0][2], 0.0)

    def next_round(self, state, proposal, report):
        return opt.record_round(state, report, proposal.area, (), change=proposal.reasons, kind=proposal.kind,
                                move=proposal.move, new_probe=proposal.new_probe)

    def test_a_trial_that_helps_is_kept_and_said_so(self) -> None:
        state, proposal = self.first(weak(bias_x=0.3, miss=0.05, err=0.35))
        state, report = self.next_round(state, proposal, weak(bias_x=0.0, miss=0.0, err=0.15))
        self.assertEqual(state.probe.outcome, "kept")
        nxt = opt.propose(state, profile(proposal.area), report, TABLET)
        self.assertEqual(nxt.probe_outcome.key, "play.probe.kept")
        self.assertEqual(nxt.probe_outcome.params["what"].params["way"], "right")
        self.assertNotEqual(nxt.kind, "probe_back")

    def test_a_trial_that_does_not_help_is_put_back_and_the_opposite_is_tried(self) -> None:
        base = weak(bias_x=0.3)
        state, proposal = self.first(base)
        state, report = self.next_round(state, proposal, base)          # same cost: not better beyond the noise
        self.assertEqual(state.probe.outcome, "reverted")
        nxt = opt.propose(state, profile(proposal.area), report, TABLET)
        self.assertEqual(nxt.probe_outcome.key, "play.probe.reverted")
        self.assertEqual(nxt.kind, "probe")
        self.assertEqual(nxt.undo_area, AREA)                             # counted from the area before the trial
        self.assertIn("play.probe.from_base", [r.key for r in nxt.reasons])
        self.assertEqual(nxt.reasons[0].params["what"].params["way"], "left")
        self.assertAlmostEqual(nxt.area.x, AREA.x + opt.PROBE_MIN_SHIFT_MM, places=3)

    def test_trials_are_bounded_never_repeated_and_end_back_where_they_started(self) -> None:
        state, proposal = self.first(weak(bias_x=0.4, gain=0.985, err=0.3))
        seen, areas, kinds = set(), [], []
        for n in range(30):
            if proposal.kind not in ("probe", "probe_back"):
                break
            kinds.append(proposal.kind)
            areas.append(proposal.area)
            if proposal.kind == "probe":
                key = proposal.new_probe.live.key
                self.assertNotIn(key, seen)                                # a put-back trial is never repeated
                seen.add(key)
            # every trial is worse than the start, alternately a bit less and a bit more (so the plateau rule,
            # which would normally stop this after two trials, never applies and the budgets are what end it)
            worse = dict(err=1.5, miss=0.3) if n % 2 else dict(err=0.45)
            state, report = self.next_round(state, proposal, weak(bias_x=0.4, gain=0.985, **worse))
            proposal = opt.propose(state, profile(areas[-1]), report, TABLET)
        self.assertGreater(len(seen), 4)
        used = dict(state.probe.used)
        self.assertTrue(all(n <= opt.PROBE_ROUNDS_PER_FAMILY for n in used.values()), used)
        self.assertEqual(kinds[-1], "probe_back")
        self.assertEqual(areas[-1], AREA)                                  # nothing helped: back to the start
        for area in areas:
            dx, dy = areafit.tablet_to_view(area.x - AREA.x, area.y - AREA.y, 180.0)
            self.assertLessEqual(math.hypot(dx, dy), opt.PROBE_MAX_SHIFT_MM + 1e-6)
            self.assertLessEqual(abs(area.width / AREA.width - 1.0), opt.PROBE_MAX_SCALE + 1e-4)
            self.assertTrue(areafit.area_fits(area, TABLET))
        self.assertEqual(opt.propose(state, profile(), report, TABLET).kind, "none")   # used up: no new trials

    def test_strong_evidence_still_comes_first(self) -> None:
        _, proposal = self.first(fake_report(reach=ReachFit(1.0, 1.0, 2.0, 0.0, (0.99, 1.01), (0.99, 1.01), 60)))
        self.assertEqual(proposal.kind, "shift")

    def test_skipping_a_trial_never_offers_it_again(self) -> None:
        state, proposal = self.first(weak(bias_x=0.3))
        state = opt.skip_probe(state, proposal.new_probe)
        state, report = opt.record_round(state, weak(bias_x=0.3), AREA, ())
        again = opt.propose(state, profile(), report, TABLET)
        self.assertEqual(again.kind, "probe")
        self.assertNotEqual(again.new_probe.live.key, proposal.new_probe.live.key)

    def test_rotation_is_kept_and_the_direction_follows_the_view(self) -> None:
        upright = replace(AREA, rotation=0.0)
        state, report = opt.record_round(opt.new_state(), weak(bias_x=0.3), upright, ())
        proposal = opt.propose(state, profile(upright), report, TABLET)
        self.assertAlmostEqual(proposal.area.x, upright.x + opt.PROBE_MIN_SHIFT_MM, places=3)   # rotation 0: same way
        self.assertEqual(proposal.area.rotation, 0.0)


class ProbeSimulationTests(unittest.TestCase):
    """Closed loops on a noisy simulated player (aim scatter 1.5 mm), feeding every proposal back."""
    NOISE = 1.5

    def loop(self, ideal: Rect, seed: int, rounds: int = 8):
        state, area, kind, move, change, new_probe = opt.new_state(), AREA, "start", (0.0, 0.0), (), None
        trace = []
        for n in range(rounds):
            player = player_for(area, ideal)
            player.aim_noise_mm = self.NOISE
            capture = simulate(MAP, player, seed=1000 * seed + n, area=area, t0=20.0 + n)
            report = po.analyze_play(MAP, replace(capture, samples=capture.samples[::2]), seed=seed)
            state, report = opt.record_round(state, report, area, (), change=change, kind=kind, move=move,
                                             new_probe=new_probe)
            proposal = opt.propose(state, profile(area), report, TABLET)
            trace.append((kind, area, report.cost, proposal.kind,
                          proposal.probe_outcome.key if proposal.probe_outcome else None))
            if proposal.kind == "none":
                if proposal.verdict == PLATEAU:
                    break
                kind, move, change, new_probe = "start", (0.0, 0.0), (), None
                continue
            area = proposal.area or area
            kind, move, change, new_probe = proposal.kind, proposal.move, proposal.reasons, proposal.new_probe
        return trace, area

    def test_an_area_too_big_on_x_and_shifted_changes_within_four_rounds_and_ends_better(self) -> None:
        ideal = Rect(AREA.width / 1.05, AREA.height, AREA.x - 2.0, AREA.y, 180.0)   # 5 % too big on x, 2 mm off
        for seed in range(4):
            trace, area = self.loop(ideal, seed)
            changed = [i for i, step in enumerate(trace) if step[1] != AREA]
            self.assertTrue(changed and changed[0] <= 3, (seed, trace))        # the area moved by round 4
            costs = [step[2] for step in trace if step[2] is not None]
            self.assertLess(costs[-1], costs[0], (seed, costs))
            self.assertNotEqual(area, AREA)

    def test_a_well_tuned_player_does_not_drift(self) -> None:
        kept = reverted = probes = 0
        for seed in range(6):
            trace, area = self.loop(AREA, seed)
            for _, played, _, _, outcome in trace:
                dx, dy = areafit.tablet_to_view(played.x - AREA.x, played.y - AREA.y, 180.0)
                self.assertLessEqual(max(abs(dx), abs(dy)), opt.PROBE_MAX_SHIFT_MM + 1e-6, (seed, trace))
                self.assertLessEqual(abs(played.width / AREA.width - 1.0), opt.PROBE_MAX_SCALE + 1e-4, (seed, trace))
                kept += outcome == "play.probe.kept"
                reverted += outcome == "play.probe.reverted"
            probes += sum(step[0] == "probe" for step in trace)
            self.assertLess(len(trace), 8, (seed, trace))                       # the plateau rule still stops it
        self.assertGreater(probes, 0)
        self.assertGreater(reverted, kept)

    def test_the_trials_are_deterministic(self) -> None:
        self.assertEqual(self.loop(AREA, 3), self.loop(AREA, 3))


if __name__ == "__main__":
    unittest.main()
