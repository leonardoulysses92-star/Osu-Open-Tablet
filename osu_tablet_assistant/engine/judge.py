"""Turn repeated test runs into one stable, readable verdict per option (pure, no tkinter).

The wizard measures each option with several kinds of test and the player may
repeat any of them. This module decides what those runs mean:

* :func:`aggregate_runs` merges the runs of one test kind with the median (and,
  with enough runs, drops the most extreme one) so a single bad run cannot
  overwrite a good one;
* :func:`grade_for` turns the 0-100 objective score into a word
  (excellent / good / normal / weak). The thresholds are constants below;
* :func:`reliability` says how far the numbers can be trusted;
* :func:`summarize` also detects ties: options whose scores differ by less than
  the measurement noise are marked together, not ranked by 0.3 points.

All reference values are estimates, not calibrated against a population of players.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, fields, replace
from typing import Sequence

from ..models import CandidateResult, MetricsReport, UserPreferences

# --------------------------------------------------------------------- constants
MAX_RUNS_KEPT = 9              # older runs of the same kind are forgotten
DROP_EXTREME_FROM = 4          # with >= 4 runs the most extreme one is discarded
# Primary metric per test kind: used to find outliers and to measure agreement.
PRIMARY_METRIC = {"stillness": "jitter_rms_mm", "tracking": "path_error_mean_mm",
                  "aim": "aim_error_mean_mm"}
# Floor added to the median when computing a relative spread, so a near-zero
# median does not make every wobble look huge (same unit as the metric).
SPREAD_FLOOR = {"jitter_rms_mm": 0.02, "path_error_mean_mm": 0.2, "aim_error_mean_mm": 0.3}
RELIABLE_SPREAD = 0.30         # relative MAD at or below this = the runs agree

# Grade thresholds on the objective score (0-100). Heuristic.
GRADE_EXCELLENT = 75.0
GRADE_GOOD = 60.0
GRADE_NORMAL = 45.0

TIE_MIN_POINTS = 3.0           # two options closer than this are always a tie
TIE_SINGLE_RUN_POINTS = 6.0    # assumed noise when a test kind was run only once

_NUMERIC_FIELDS = tuple(f.name for f in fields(MetricsReport)
                        if f.name not in ("test_kind", "sample_count", "duration_s", "extras"))


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# ------------------------------------------------------------------- aggregation
def _extreme_index(runs: Sequence[MetricsReport], kind: str) -> int | None:
    """Index of the run farthest (relatively) from the median primary metric, or None."""
    name = PRIMARY_METRIC.get(kind)
    values = [(i, getattr(r, name)) for i, r in enumerate(runs)] if name else []
    values = [(i, v) for i, v in values if _finite(v)]
    if len(values) < 2:
        return None
    med = statistics.median(v for _i, v in values)
    floor = SPREAD_FLOOR[name]
    return max(values, key=lambda iv: (abs(iv[1] - med) / (abs(med) + floor), -iv[0]))[0]


def aggregate_runs(runs: Sequence[MetricsReport]) -> MetricsReport | None:
    """One report for runs of the same kind: field-wise median (None when ``runs`` is empty).

    With ``DROP_EXTREME_FROM`` or more runs the single most extreme one (by the
    primary metric) is left out first. ``sample_count`` and ``duration_s`` are
    summed over the runs used; ``extras`` gets ``runs`` (how many were merged).
    """
    runs = list(runs)[-MAX_RUNS_KEPT:]
    if not runs:
        return None
    kind = runs[-1].test_kind
    runs = [r for r in runs if r.test_kind == kind]
    if len(runs) >= DROP_EXTREME_FROM:
        drop = _extreme_index(runs, kind)
        if drop is not None:
            runs = runs[:drop] + runs[drop + 1:]
    if len(runs) == 1:
        extras = dict(runs[0].extras)
        extras["runs"] = 1
        return replace(runs[0], extras=extras)
    values: dict[str, float | None] = {}
    for name in _NUMERIC_FIELDS:
        found = [getattr(r, name) for r in runs if _finite(getattr(r, name))]
        values[name] = float(statistics.median(found)) if found else None
    extras: dict[str, object] = {}
    keys = {k for r in runs for k in r.extras}
    for key in sorted(keys):
        found = [r.extras[key] for r in runs if key in r.extras]
        numeric = [v for v in found if _finite(v)]
        extras[key] = float(statistics.median(numeric)) if numeric and len(numeric) == len(found) else found[-1]
    extras["runs"] = len(runs)
    return MetricsReport(test_kind=kind, sample_count=sum(r.sample_count for r in runs),
                         duration_s=sum(r.duration_s for r in runs), extras=extras, **values)


def aggregate_by_kind(runs: Sequence[MetricsReport]) -> tuple[MetricsReport, ...]:
    """One aggregated report per kind, in order of first appearance."""
    order: list[str] = []
    for r in runs:
        if r.test_kind not in order:
            order.append(r.test_kind)
    out = []
    for kind in order:
        merged = aggregate_runs([r for r in runs if r.test_kind == kind])
        if merged is not None:
            out.append(merged)
    return tuple(out)


def runs_by_kind(runs: Sequence[MetricsReport]) -> dict[str, list[MetricsReport]]:
    out: dict[str, list[MetricsReport]] = {}
    for r in runs:
        out.setdefault(r.test_kind, []).append(r)
    return out


# ------------------------------------------------------------ grades & reliability
def grade_for(objective: float | None) -> str | None:
    """``excellent`` | ``good`` | ``normal`` | ``weak`` (None when there is no score)."""
    if objective is None or not math.isfinite(objective):
        return None
    if objective >= GRADE_EXCELLENT:
        return "excellent"
    if objective >= GRADE_GOOD:
        return "good"
    if objective >= GRADE_NORMAL:
        return "normal"
    return "weak"


def relative_spread(runs: Sequence[MetricsReport]) -> float | None:
    """Median absolute deviation of the primary metric over its (floored) median; None with < 2 runs."""
    if not runs:
        return None
    name = PRIMARY_METRIC.get(runs[-1].test_kind)
    values = [getattr(r, name) for r in runs if _finite(getattr(r, name))] if name else []
    if len(values) < 2:
        return None
    med = statistics.median(values)
    mad = statistics.median(abs(v - med) for v in values)
    return mad / (abs(med) + SPREAD_FLOOR[name])


def reliability(runs: Sequence[MetricsReport], required: Sequence[str]) -> str:
    """``few`` (something missing) | ``repeat`` (measured, but once or not in agreement) | ``reliable``."""
    grouped = runs_by_kind(runs)
    if any(not grouped.get(kind) for kind in required):
        return "few"
    for kind in required:
        spread = relative_spread(grouped[kind])
        if len(grouped[kind]) < 2 or spread is None or spread > RELIABLE_SPREAD:
            return "repeat"
    return "reliable"


# ------------------------------------------------------------------ summaries
@dataclass(frozen=True)
class ResultSummary:
    candidate_id: str
    complete: bool
    grade: str | None             # None while incomplete
    objective: float | None
    reliability: str              # few | repeat | reliable
    runs: int                     # fewest runs over the required kinds (0 if something is missing)
    noise: float                  # +/- points the score could move between repeats
    tied: bool = False            # inside the tie group of the best option
    best: bool = False            # in the group marked with a star


@dataclass(frozen=True)
class Verdict:
    summaries: dict[str, ResultSummary]
    top: tuple[str, ...]          # starred ids: the best, plus everyone tied with it
    tie: bool                     # more than one option starred


def score_noise(runs: Sequence[MetricsReport], required: Sequence[str], prefs: UserPreferences) -> float:
    """Points the objective score moves between repeats of the same option.

    One run per kind cannot tell, so ``TIE_SINGLE_RUN_POINTS`` is assumed. With
    repeats, the score is recomputed pairing the i-th run of every kind; half
    the range, but at least ``TIE_MIN_POINTS``.
    """
    from .recommend import score_reports   # recommend imports nothing from here

    grouped = runs_by_kind(runs)
    if any(len(grouped.get(kind, ())) < 2 for kind in required):
        return TIE_SINGLE_RUN_POINTS
    rounds = max(len(grouped[kind]) for kind in required)
    scores = [score_reports([grouped[kind][i % len(grouped[kind])] for kind in required], prefs, None)
              for i in range(rounds)]
    return max(TIE_MIN_POINTS, (max(scores) - min(scores)) / 2)


def summarize(results: Sequence[CandidateResult], prefs: UserPreferences,
              required: Sequence[str]) -> Verdict:
    """Grade, reliability and tie group of every ranked result (``rank`` must have run).

    Only complete results can win or tie. ``top`` lists the best one plus all
    options whose ``score`` is within the larger of the two noise margins.
    """
    from .recommend import score_reports

    summaries: dict[str, ResultSummary] = {}
    noises: dict[str, float] = {}
    for r in results:
        grouped = runs_by_kind(r.runs or r.reports)
        complete = all(grouped.get(kind) for kind in required)
        noises[r.candidate_id] = score_noise(r.runs or r.reports, required, prefs)
        objective = score_reports(r.reports, prefs, None) if complete else None
        counts = [len(grouped.get(kind, ())) for kind in required]
        summaries[r.candidate_id] = ResultSummary(
            r.candidate_id, complete, grade_for(objective), objective,
            reliability(r.runs or r.reports, required), min(counts) if counts else 0, noises[r.candidate_id])
    full = [r for r in results if summaries[r.candidate_id].complete and r.score is not None]
    top: tuple[str, ...] = ()
    if full:
        best = max(full, key=lambda r: r.score)
        group = [best.candidate_id]
        for r in full:
            if r is best:
                continue
            margin = max(noises[best.candidate_id], noises[r.candidate_id])
            if best.score - r.score <= margin:
                group.append(r.candidate_id)
        top = tuple(group)
    tied = len(top) > 1
    for cid in top:
        summaries[cid] = replace(summaries[cid], best=True, tied=tied)
    return Verdict(summaries, top, tied)
