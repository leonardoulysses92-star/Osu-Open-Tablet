"""engine.optimizer: cost, stop rule, one-change proposals and closed-loop convergence on a simulated player."""
from __future__ import annotations

import math
import statistics
import unittest
from dataclasses import replace

from osu_tablet_assistant.engine import areafit, optimizer as opt, recommend
from osu_tablet_assistant.engine import playobserve as po
from osu_tablet_assistant.engine.playtypes import (IMPROVING, INSUFFICIENT, PLATEAU, START, WORSE, FeelFit,
                                                    PlayReport, ReachFit, RoundRecord)
from osu_tablet_assistant.models import FilterConfig, Priority, ProfileInfo, Rect, UserPreferences
from playsim import AREA, DISPLAY, TABLET, Player, make_map, simulate

MAP = make_map(seed=11, n=90, gap_ms=320.0)
IDEAL = Rect(64.0, 37.0, 78.0, 49.0, 180.0)      # the area this simulated hand "wants"


def profile(area: Rect = AREA, filters: tuple = ()) -> ProfileInfo:
    return ProfileInfo("CTL-472", "Output", Rect(1920, 1080, 960, 540), area, True, filters)


def player_for(area: Rect, ideal: Rect = IDEAL, **kw) -> Player:
    """The pen travel is fixed by ``ideal``; what the cursor does depends on the active ``area``."""
    vw, vh = areafit.view_size(area)
    iw, ih = areafit.view_size(ideal)
    cu, cv = areafit.tablet_mm_to_area(area, ideal.x, ideal.y)
    return Player(gain_x=iw / vw, gain_y=ih / vh, bias_x_mm=(cu - 0.5) * vw, bias_y_mm=(cv - 0.5) * vh,
                  miss_rate=0.02, aim_noise_mm=0.25, **kw)


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


class ClosedLoopTests(unittest.TestCase):
    MAX_ROUNDS = 14

    def run_loop(self, seed: int) -> tuple[list[float], list[str], Rect]:
        state, area, kind, move, change, adapt_state = opt.new_state(), AREA, "start", (0.0, 0.0), (), None
        costs, kinds = [], []
        for n in range(self.MAX_ROUNDS):
            report = play(area, 1000 * seed + n)
            state, report = opt.record_round(state, report, area, (), change=change, kind=kind, move=move)
            if report.cost is not None:
                costs.append(report.cost)
            kinds.append(kind)
            proposal = opt.propose(state, profile(area), report, TABLET)
            if proposal.kind == "none":
                if proposal.verdict == PLATEAU:
                    break
                continue                                       # not enough data: play the same again
            area = proposal.area or area
            kind, move, change = proposal.kind, proposal.move, proposal.reasons
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


if __name__ == "__main__":
    unittest.main()
