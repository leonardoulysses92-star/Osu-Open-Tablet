"""engine.hotspots: the finer analysis grid, pooled cells and small problem regions, on simulated players."""
from __future__ import annotations

import math
import time
import unittest
from dataclasses import replace

from osu_tablet_assistant.engine import findings, hotspots, playobserve as po
from osu_tablet_assistant.engine.playtypes import ANALYSIS_DETAILS, Hotspot, grid_for
from osu_tablet_assistant.gui import i18n
from osu_tablet_assistant.gui.coachcore import say
from playsim import Player, make_map, simulate

PATCH = (0.85, 0.85, 1.0, 1.0)          # bottom-right 15 % x 15 % of the playfield
SEEDS = range(1, 6)


def patch_play(seed: int, rows: int, cols: int, n: int = 300, patch=PATCH):
    beatmap = make_map(seed=200 + seed, n=n, gap_ms=300)
    player = Player(miss_rate=0.02, aim_noise_mm=0.4, hz=250, patch=patch, patch_short_mm=2.0 if patch else 0.0)
    return po.analyze_play(beatmap, simulate(beatmap, player, seed=seed), rows=rows, cols=cols)


def inside_patch(spot: Hotspot, margin: float = 0.06) -> bool:
    x0, y0, x1, y1 = PATCH
    return x0 - margin <= spot.fx <= x1 and y0 - margin <= spot.fy <= y1


class LocalisationTests(unittest.TestCase):
    def test_fine_grids_find_a_small_patch_where_it_is(self) -> None:
        for rows, cols in (ANALYSIS_DETAILS["fine"], ANALYSIS_DETAILS["very_fine"]):
            found = 0
            for seed in SEEDS:
                report = patch_play(seed, rows, cols)
                spots = [s for s in report.hotspots if inside_patch(s)]
                if spots and spots[0].kind == "short" and spots[0].size == "small":
                    found += 1
                    spot = spots[0]
                    self.assertLessEqual(max(spot.width_mm, spot.height_mm), 12.0, (rows, cols, seed))
                    self.assertGreater(spot.err_mm, 0.8)
                    self.assertLess(spot.share, 0.1)
                    self.assertGreater(spot.z, hotspots.SPOT_Z)
                # nothing is reported elsewhere
                self.assertEqual([s for s in report.hotspots if not inside_patch(s, 0.2)], [], (rows, cols, seed))
            self.assertGreaterEqual(found, 4, (rows, cols))

    def test_the_coarse_grid_smears_it(self) -> None:
        for seed in SEEDS:
            report = patch_play(seed, *ANALYSIS_DETAILS["normal"])
            small = [s for s in report.hotspots if inside_patch(s) and s.size == "small"]
            self.assertEqual(small, [], seed)

    def test_no_false_alarms_on_a_well_aimed_play(self) -> None:
        for rows, cols in (ANALYSIS_DETAILS["fine"], ANALYSIS_DETAILS["very_fine"]):
            for seed in range(6):
                report = patch_play(seed, rows, cols, patch=None)
                self.assertEqual(report.hotspots, (), (rows, cols, seed))
                self.assertNotIn("play.find.spot_short", [f.key for f in findings.findings(report)])

    def test_deterministic(self) -> None:
        self.assertEqual(patch_play(2, 6, 8), patch_play(2, 6, 8))


class PooledCellTests(unittest.TestCase):
    def test_cells_are_pooled_and_sparse_ones_say_nothing(self) -> None:
        report = patch_play(3, 9, 12)
        self.assertEqual(len(report.cells), 108)
        self.assertEqual(sum(c.n for c in report.cells), report.n_judged)
        clicked = {}
        for r in report.results:
            if r.err_mm is not None:
                key = (min(int(r.fy * 9), 8), min(int(r.fx * 12), 11))
                clicked[key] = clicked.get(key, 0) + 1
        for cell in report.cells:
            self.assertEqual(cell.enough, clicked.get((cell.row, cell.col), 0) >= hotspots.MIN_CELL_N)
            self.assertTrue(math.isfinite(cell.est_x_mm) and math.isfinite(cell.est_y_mm))
            self.assertTrue(0.0 <= cell.est_miss <= 1.0)
        # pooling pulls a lone noisy cell towards its neighbours: pooled errors vary less than raw ones
        raw = [math.hypot(c.err_x_mm, c.err_y_mm) for c in report.cells if c.enough]
        pooled = [math.hypot(c.est_x_mm, c.est_y_mm) for c in report.cells if c.enough]
        self.assertLessEqual(max(pooled), max(raw) + 1e-9)

    def test_grid_levels(self) -> None:
        self.assertEqual(grid_for("normal"), (3, 4))
        self.assertEqual(grid_for("fine"), (6, 8))
        self.assertEqual(grid_for("very_fine"), (9, 12))
        self.assertEqual(grid_for("nonsense"), (6, 8))
        self.assertEqual(grid_for(None), (6, 8))


class WordsTests(unittest.TestCase):
    def test_place_and_direction_ids(self) -> None:
        self.assertEqual(hotspots.place_id(0.9, 0.9), "bottom.right.corner")
        self.assertEqual(hotspots.place_id(0.6, 0.8), "bottom.center_right")
        self.assertEqual(hotspots.place_id(0.3, 0.5), "middle.center_left")
        self.assertEqual(hotspots.place_id(0.05, 0.5), "middle.left.edge")
        self.assertEqual(hotspots.direction_id(1.0, 0.0), "right")
        self.assertEqual(hotspots.direction_id(0.0, -1.0), "up")         # screen y grows downwards
        self.assertEqual(hotspots.direction_id(-1.0, 1.0), "down_left")

    def test_a_found_patch_is_one_plain_sentence_in_both_languages(self) -> None:
        report = next(r for r in (patch_play(s, 6, 8) for s in SEEDS) if r.hotspots)
        found = findings.findings(report)
        self.assertLessEqual(len(found), 4)
        spot = next(f for f in found if f.key == "play.find.spot_short")
        self.assertEqual(spot.params["place"], "bottom.right.corner")
        self.addCleanup(i18n.set_language, "es")
        i18n.set_language("es")
        text = say(spot)
        self.assertTrue(text.startswith("Abajo, a la derecha (cerca de la esquina): te quedas corto ≈ "), text)
        self.assertTrue(text.endswith("mm (zona pequeña)"), text)
        i18n.set_language("en")
        self.assertIn("Near the bottom, on the right (near the corner): you stop short by", say(spot))
        # the coarse "bottom right corner" edge finding is not repeated next to the finer hotspot
        self.assertNotIn(("play.find.short", "bottom_right"), [(f.key, f.params.get("zone")) for f in found])

    def test_every_word_exists(self) -> None:
        known = set(i18n.load_catalog("es")) & set(i18n.load_catalog("en"))
        for v in ("top", "middle", "bottom"):
            self.assertIn(f"play.place.v.{v}", known)
        for h in ("left", "center_left", "center_right", "right"):
            self.assertIn(f"play.place.h.{h}", known)
        for near in ("corner", "edge"):
            self.assertIn(f"play.place.near.{near}", known)
        for d in ("up", "down", "left", "right", "up_left", "up_right", "down_left", "down_right"):
            self.assertIn(f"play.towards.{d}", known)
        for kind in ("short", "over", "off"):
            self.assertIn(f"play.find.spot_{kind}", known)
        for size in ("small", "medium", "large"):
            self.assertIn(f"play.size.{size}", known)
        for level in ANALYSIS_DETAILS:
            self.assertIn(f"play.detail.{level}", known)


class SpeedTests(unittest.TestCase):
    def test_finest_grid_keeps_the_analysis_fast(self) -> None:
        big = make_map(seed=7, n=600, gap_ms=90.0)
        capture = simulate(big, Player(hz=1000.0), seed=1, t0=10.0, stop_after_s=5.0)
        capture = replace(capture, samples=capture.samples[:5000])
        po.analyze_play(big, capture, rows=9, cols=12)
        best = min(self._time(big, capture) for _ in range(3))
        self.assertLess(best, 0.08, f"{best * 1000:.0f} ms")

    @staticmethod
    def _time(beatmap, capture) -> float:
        start = time.perf_counter()
        po.analyze_play(beatmap, capture, rows=9, cols=12)
        return time.perf_counter() - start


if __name__ == "__main__":
    unittest.main()
