"""engine.findings: at most four plain lines, strongest first, no redundant corners."""
from __future__ import annotations

import unittest

from osu_tablet_assistant.engine import findings as fd
from osu_tablet_assistant.engine.playtypes import EdgeFinding, FeelFit, PlayReport, ReachFit


def edge(zone: str, kind: str = "short", severity: float = 0.5, mm: float = 3.0) -> EdgeFinding:
    return EdgeFinding(zone, kind, severity, 10, 0.2, mm)


class FindingsTests(unittest.TestCase):
    def test_good_play_says_so(self) -> None:
        self.assertEqual([r.key for r in fd.findings(PlayReport())], ["play.find.good"])

    def test_strongest_first_and_capped_at_four(self) -> None:
        report = PlayReport(
            edges=(edge("left", severity=0.3), edge("bottom_right", severity=0.9), edge("top", "late", 0.4),
                   edge("right", "over", 0.5), edge("bottom", "noisy", 0.35), edge("center", "late", 0.2)),
            reach=ReachFit(0.9, 1.0, 0, 0, (0.88, 0.92), (0.99, 1.01), 80),
            feel=FeelFit(-2.0, 10, 0.2, 10, 600.0))
        out = fd.findings(report)
        self.assertEqual(len(out), fd.MAX_FINDINGS)
        self.assertEqual(out[0].key, "play.find.short")
        self.assertEqual(out[0].params["zone"], "bottom_right")
        self.assertEqual(out[0].params["mm"], "3")

    def test_corner_is_dropped_when_a_side_says_the_same(self) -> None:
        report = PlayReport(edges=(edge("right", severity=0.9), edge("bottom_right", severity=0.8)))
        zones = [r.params["zone"] for r in fd.findings(report)]
        self.assertEqual(zones, ["right"])

    def test_weak_findings_and_ok_zones_are_ignored(self) -> None:
        report = PlayReport(edges=(edge("left", severity=0.05), edge("right", "ok", 0.0)))
        self.assertEqual([r.key for r in fd.findings(report)], ["play.find.good"])

    def test_reach_needs_an_interval_that_excludes_one(self) -> None:
        sure = PlayReport(reach=ReachFit(0.9, 1.0, 0, 0, (0.88, 0.92), (0.97, 1.03), 80))
        unsure = PlayReport(reach=ReachFit(0.9, 1.0, 0, 0, (0.8, 1.05), (0.97, 1.03), 80))
        self.assertEqual(fd.findings(sure)[0].key, "play.find.reach_short")
        self.assertEqual(fd.findings(unsure)[0].key, "play.find.good")

    def test_lag_and_jitter(self) -> None:
        report = PlayReport(feel=FeelFit(-1.5, 12, 0.1, 9, 600.0), radius_mm=3.0)
        self.assertEqual({r.key for r in fd.findings(report)}, {"play.find.lag", "play.find.jitter"})


if __name__ == "__main__":
    unittest.main()
