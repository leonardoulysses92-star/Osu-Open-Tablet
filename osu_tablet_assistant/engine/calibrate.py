"""Check the live recording against the game's own replay: clock offset, desktop -> playfield fit, the map.

After a play osu! writes a replay (``.osr``) with ITS cursor positions in playfield units and its key bits.
Lining the live recording (OS cursor, configured keys, the recorder's clock) up with it measures what the
coach otherwise has to assume:

1. **Clock offset** (:func:`fit_clock`): key-down edges of both are cross-correlated (a vote over every
   pairwise difference, then refined on the matched pairs). Replay times are map times; under DT/NC or HT
   real time is map time / 1.5 or / 0.75; the replay's own mods are tried first, then the other speeds.
2. **Spatial fit** (:func:`fit_affine`): the replay cursor is interpolated at the live sample times and
   ``p_playfield = A * p_desktop + b`` is fitted by least squares (plain normal equations, one robust
   re-fit without outliers). ``A`` should be a scaled identity (1 / desktop px per osu! px). Off-diagonal
   terms or unequal axes mean osu!'s cursor is **transformed** (raw input with "map absolute input to the
   osu! window" or a sensitivity other than 1); a large residual means it does not follow the OS cursor.
   Hard Rock: whether stable stores HR frames flipped is unverified. A free affine fit absorbs a mirror, so
   the sign of ``A``'s y term tells the orientation, and the coach's assumed transform is compared against
   both (``Calibration.alternative_rms_px``).
3. **Map** (:func:`check_play`): the replay's beatmap MD5 against the ``.osu`` that was judged, and the map
   index (``engine.mapindex``) for which map it really was.

:func:`find_new_replay` finds the newest replay written after a given time (``Replays/`` and ``Data/r/`` of
stable, and lazer's hashed store). Nothing here changes any setting: it only measures and reports.
"""
from __future__ import annotations

import bisect
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..log import log_exception
from ..models import Reason
from . import replay as replay_mod
from .beatmap import desktop_to_playfield
from .osucfg import OsuConfig, pointer_trust
from .playtypes import Beatmap, PlayCapture

MATCH_TOL_S = 0.012          # a live key-down and a replay key-down this close are the same press
MIN_MATCHED = 8
MAX_EVENTS = 400             # key-downs per side used for the vote (keeps it ~O(10^5))
MAX_PAIRS = 3000             # cursor samples used for the fit
FRAME_GAP_MS = 60.0          # replay frames further apart than this are not interpolated across
SCALED_IDENTITY_TOL = 0.03   # relative size of off-diagonal / unequal-axis terms still called "identity"
RESIDUAL_LARGE_PX = 6.0      # osu! px; the OS trace vs osu!'s frames differ by a few px at ordinary frame rates
ASSUMED_OK_PX = 4.0          # the coach's assumed transform is "confirmed" when it is this close (osu! px)
SCALE_TOL = 0.03             # fitted vs assumed scale
REPLAY_SUFFIX = ".osr"


# ------------------------------------------------------------------ results
@dataclass(frozen=True)
class ClockFit:
    """``live_t = replay_real_t + offset_s`` (seconds); ``scale`` = map time / real time (1.5 DT, 0.75 HT)."""
    offset_s: float
    scale: float
    matched: int
    n_live: int
    n_replay: int
    spread_ms: float           # median absolute residual of the matched presses


@dataclass(frozen=True)
class AffineFit:
    """``x' = a11 x + a12 y + b1``, ``y' = a21 x + a22 y + b2`` (desktop px -> osu! px)."""
    a11: float
    a12: float
    a21: float
    a22: float
    b1: float
    b2: float
    rms_px: float              # residual, osu! px (Euclidean)
    n: int
    flipped: bool = False      # fitted against the replay's y mirrored (384 - y)

    @property
    def scale(self) -> float:
        return math.sqrt(abs(self.a11 * self.a22 - self.a12 * self.a21))

    @property
    def scaled_identity(self) -> bool:
        s = self.scale
        if s <= 0:
            return False
        return (abs(self.a12) <= SCALED_IDENTITY_TOL * s and abs(self.a21) <= SCALED_IDENTITY_TOL * s
                and abs(self.a11 - self.a22) <= SCALED_IDENTITY_TOL * s and self.a11 > 0 and self.a22 > 0)

    def apply(self, x: float, y: float) -> tuple[float, float]:
        return self.a11 * x + self.a12 * y + self.b1, self.a21 * x + self.a22 * y + self.b2


@dataclass(frozen=True)
class Calibration:
    clock: ClockFit | None
    fit: AffineFit | None
    alternative_rms_px: float | None = None     # the other HR orientation
    assumed_rms_px: float | None = None         # the coach's assumed transform vs the replay
    assumed_scale: float | None = None          # osu! px per desktop px the coach assumed
    shift_px: tuple[float, float] | None = None  # where the fit puts the playfield vs the assumption (osu! px)
    flags: tuple[str, ...] = ()                 # no_clock | few_points | transformed | residual | scale | shifted

    @property
    def verdict(self) -> str:
        """``confirmed`` | ``mismatch`` (osu!'s cursor is not the OS cursor) | ``shifted`` (the playfield is not
        where it was assumed) | ``unknown`` (could not be measured)."""
        if self.fit is None:
            return "unknown"
        if "transformed" in self.flags or "residual" in self.flags:
            return "mismatch"
        if "scale" in self.flags or "shifted" in self.flags:
            return "shifted"
        return "confirmed"


# --------------------------------------------------------------------- time
def replay_key_downs(replay: replay_mod.Replay) -> list[float]:
    """Map times (ms) where a key went down. K1/K2 also set M1/M2 in osu!'s frames, so a mouse bit counts only
    when its key bit is not down with it."""
    out: list[float] = []
    prev = 0
    for f in replay.frames:
        keys = f.keys
        effective = keys & 0b1100
        if keys & 1 and not keys & 4:
            effective |= 1
        if keys & 2 and not keys & 8:
            effective |= 2
        if effective & ~prev:
            out.append(f.t_ms)
        prev = effective
    return out


def _thin(values: Sequence[float], limit: int) -> list[float]:
    if len(values) <= limit:
        return list(values)
    step = len(values) / limit
    return [values[int(i * step)] for i in range(limit)]


def _match(live: Sequence[float], ref: Sequence[float], offset: float, tol: float) -> list[float]:
    """Residuals ``live - (ref + offset)`` of the live presses that have a reference press within ``tol``."""
    out = []
    for t in live:
        want = t - offset
        i = bisect.bisect_left(ref, want)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(ref):
                d = want - ref[j]
                if abs(d) <= tol and (best is None or abs(d) < abs(best)):
                    best = d
        if best is not None:
            out.append(best)
    return out


def fit_clock(live_downs: Sequence[float], replay_downs_ms: Sequence[float], scales: Sequence[float] = (1.0,),
              tol_s: float = MATCH_TOL_S) -> ClockFit | None:
    """Offset (and speed) that lines the live presses (seconds) up with the replay's (map ms). None: no match."""
    live = sorted(live_downs)
    best: ClockFit | None = None
    for scale in scales:
        ref = sorted(t / 1000.0 / scale for t in replay_downs_ms)
        if len(live) < MIN_MATCHED or len(ref) < MIN_MATCHED:
            continue
        votes: dict[int, int] = {}
        a, b = _thin(live, MAX_EVENTS), _thin(ref, MAX_EVENTS)
        for t in a:
            for r in b:
                k = int(math.floor((t - r) / tol_s))
                votes[k] = votes.get(k, 0) + 1
        top = sorted(votes, key=votes.get, reverse=True)[:3]       # type: ignore[arg-type]
        for k in top:
            offset = (k + 0.5) * tol_s
            for _ in range(2):                                     # refine on the matched pairs
                res = _match(live, ref, offset, tol_s * 1.5)
                if not res:
                    break
                offset += sorted(res)[len(res) // 2]
            res = _match(live, ref, offset, tol_s)
            if len(res) < MIN_MATCHED:
                continue
            spread = sorted(abs(r) for r in res)[len(res) // 2] * 1000.0
            fit = ClockFit(offset, scale, len(res), len(live), len(ref), spread)
            if best is None or (fit.matched, -fit.spread_ms) > (best.matched, -best.spread_ms):
                best = fit
    if best is None or best.matched < 0.3 * min(best.n_live, best.n_replay):
        return None
    return best


# ------------------------------------------------------------------- space
def _solve3(m: list[list[float]], v: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting for a 3x3 system; None when singular."""
    a = [row[:] + [v[i]] for i, row in enumerate(m)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            return None
        a[col], a[pivot] = a[pivot], a[col]
        for r in range(col + 1, 3):
            f = a[r][col] / a[col][col]
            for c in range(col, 4):
                a[r][c] -= f * a[col][c]
    x = [0.0, 0.0, 0.0]
    for r in (2, 1, 0):
        x[r] = (a[r][3] - sum(a[r][c] * x[c] for c in range(r + 1, 3))) / a[r][r]
    return x


def _lsq(pairs: Sequence[tuple[float, float, float, float]]) -> AffineFit | None:
    n = len(pairs)
    if n < 6:
        return None
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    sxx = sxy = syy = 0.0
    rx = [0.0, 0.0, 0.0]
    ry = [0.0, 0.0, 0.0]
    for dx, dy, px, py in pairs:          # centred desktop coordinates keep the normal equations well conditioned
        x, y = dx - mx, dy - my
        sxx += x * x
        sxy += x * y
        syy += y * y
        rx[0] += x * px
        rx[1] += y * px
        rx[2] += px
        ry[0] += x * py
        ry[1] += y * py
        ry[2] += py
    m = [[sxx, sxy, 0.0], [sxy, syy, 0.0], [0.0, 0.0, float(n)]]
    cx, cy = _solve3(m, rx), _solve3(m, ry)
    if cx is None or cy is None:
        return None
    a11, a12, c1 = cx
    a21, a22, c2 = cy
    b1, b2 = c1 - a11 * mx - a12 * my, c2 - a21 * mx - a22 * my
    sq = 0.0
    for dx, dy, px, py in pairs:
        ex = a11 * dx + a12 * dy + b1 - px
        ey = a21 * dx + a22 * dy + b2 - py
        sq += ex * ex + ey * ey
    return AffineFit(a11, a12, a21, a22, b1, b2, math.sqrt(sq / n), n)


def fit_affine(pairs: Sequence[tuple[float, float, float, float]]) -> AffineFit | None:
    """Least-squares ``(px, py) = A (dx, dy) + b`` over ``(dx, dy, px, py)`` pairs, re-fitted once without the
    pairs further than ``max(3 RMS, 2 px)`` from the first fit (pen lifts, pauses)."""
    first = _lsq(pairs)
    if first is None:
        return None
    limit = max(3.0 * first.rms_px, 2.0)
    kept = [p for p in pairs if math.hypot(first.apply(p[0], p[1])[0] - p[2], first.apply(p[0], p[1])[1] - p[3])
            <= limit]
    second = _lsq(kept) if len(kept) >= max(6, len(pairs) // 2) else None
    return second or first


def _interp(frames: Sequence[replay_mod.ReplayFrame], times: Sequence[float], t_ms: float
            ) -> tuple[float, float] | None:
    i = bisect.bisect_left(times, t_ms)
    if i <= 0 or i >= len(frames):
        return None
    a, b = frames[i - 1], frames[i]
    span = b.t_ms - a.t_ms
    if span <= 0 or span > FRAME_GAP_MS:
        return None
    k = (t_ms - a.t_ms) / span
    return a.x + k * (b.x - a.x), a.y + k * (b.y - a.y)


def sample_pairs(capture: PlayCapture, replay: replay_mod.Replay, clock: ClockFit, flip: bool = False,
                 limit: int = MAX_PAIRS) -> list[tuple[float, float, float, float]]:
    """(desktop x, y, replay x, y) at the live sample times inside the replay (y mirrored when ``flip``).

    Live samples are kept on change (plus a heartbeat), so a still cursor gives few of them; that is fine."""
    frames = replay.frames
    times = [f.t_ms for f in frames]
    samples = capture.samples
    step = max(1, len(samples) // limit)
    out = []
    for s in samples[::step]:
        t_ms = (s.t - clock.offset_s) * 1000.0 * clock.scale
        p = _interp(frames, times, t_ms)
        if p is None:
            continue
        out.append((s.x, s.y, p[0], 384.0 - p[1] if flip else p[1]))
    return out


def calibrate(capture: PlayCapture, replay: replay_mod.Replay, live_downs: Sequence[float] | None = None
              ) -> Calibration:
    """Clock offset, spatial fit (both HR orientations) and how the coach's assumed transform compares."""
    downs = [c.t for c in capture.clicks] if live_downs is None else list(live_downs)
    scales = [replay.time_scale] + [s for s in (1.0, 1.5, 0.75) if s != replay.time_scale]
    ref = replay_key_downs(replay)
    clock = fit_clock(downs, ref, scales[:1]) or fit_clock(downs, ref, scales[1:])
    if clock is None:
        return Calibration(None, None, flags=("no_clock",))
    pairs = sample_pairs(capture, replay, clock)
    fit = fit_affine(pairs)
    if fit is None:
        return Calibration(clock, None, flags=("few_points",))
    # A free affine fit absorbs a vertical mirror (a22 < 0), so both orientations fit equally well. The one
    # that makes A a positive scaled identity is the replay's orientation: mirrored frames (HR in some
    # clients, unverified) are re-expressed against 384 - y.
    flip = fit.a22 < 0 < fit.a11
    if flip:
        fit = AffineFit(fit.a11, fit.a12, -fit.a21, -fit.a22, fit.b1, 384.0 - fit.b2, fit.rms_px, fit.n, True)
        pairs = [(dx, dy, px, 384.0 - py) for dx, dy, px, py in pairs]

    def assumed_rms(rows: Sequence[tuple[float, float, float, float]]) -> float | None:
        """The coach's assumed transform (full screen on the display, or osu!'s measured window) vs the replay."""
        sq = 0.0
        for dx, dy, px, py in rows:
            ax, ay = desktop_to_playfield(dx, dy, capture.display, capture.playfield)
            sq += (ax - px) ** 2 + (ay - py) ** 2
        return math.sqrt(sq / len(rows)) if rows else None

    assumed = assumed_rms(pairs)
    other = assumed_rms([(dx, dy, px, 384.0 - py) for dx, dy, px, py in pairs])   # the other orientation
    a0 = desktop_to_playfield(0.0, 0.0, capture.display, capture.playfield)
    a1 = desktop_to_playfield(1.0, 0.0, capture.display, capture.playfield)
    assumed_scale = a1[0] - a0[0]
    # The desktop point the coach takes for the playfield centre, as the replay sees it: the real shift.
    shift = None
    if assumed_scale:
        fx, fy = fit.apply((256.0 - a0[0]) / assumed_scale, (192.0 - a0[1]) / assumed_scale)
        shift = (fx - 256.0, fy - 192.0)
    flags = []
    if fit.n < 50:
        flags.append("few_points")
    if not fit.scaled_identity:
        flags.append("transformed")
    if fit.rms_px > RESIDUAL_LARGE_PX:
        flags.append("residual")
    if assumed_scale and abs(fit.scale / assumed_scale - 1.0) > SCALE_TOL:
        flags.append("scale")
    if assumed is not None and assumed > ASSUMED_OK_PX and "scale" not in flags:
        flags.append("shifted")
    return Calibration(clock, fit, other, assumed, assumed_scale, shift, tuple(flags))


# ------------------------------------------------------------------- files
def looks_like_replay(head: bytes) -> bool:
    """First bytes of an ``.osr``: mode 0-3, a plausible game version, then the MD5 string (or an empty one)."""
    if len(head) < 7 or head[0] > 3:
        return False
    version = int.from_bytes(head[1:5], "little", signed=True)
    if not 20000000 <= version <= 40000000:
        return False
    return head[5] == 0 or (head[5] == 0x0B and head[6] == 32)


def _newest(paths: list[tuple[float, Path]]) -> Path | None:
    return max(paths)[1] if paths else None


def find_new_replay(osu_dir: str | Path | None, since: float, lazer_files: str | Path | None = None) -> Path | None:
    """The newest replay written at or after ``since`` (Unix seconds): ``Replays/`` and ``Data/r/`` of stable,
    then lazer's store (only its folders changed since then are listed, and files are sniffed). None: none."""
    found: list[tuple[float, Path]] = []
    if osu_dir:
        root = Path(osu_dir)
        for folder in (root / "Replays", root / "Data" / "r"):
            try:
                with os.scandir(folder) as listing:
                    for item in listing:
                        if item.name.lower().endswith(REPLAY_SUFFIX) and item.is_file():
                            mtime = item.stat().st_mtime
                            if mtime >= since:
                                found.append((mtime, Path(item.path)))
            except OSError:
                continue
    if found:
        return _newest(found)
    if lazer_files:
        for path in _lazer_candidates(Path(lazer_files), since):
            try:
                with path.open("rb") as handle:
                    if looks_like_replay(handle.read(8)):
                        return path
            except OSError:
                continue
    return None


def _lazer_candidates(files: Path, since: float) -> list[Path]:
    out: list[tuple[float, Path]] = []
    try:
        with os.scandir(files) as first:
            tops = [Path(d.path) for d in first if d.is_dir()]
    except OSError:
        return []
    for top in tops:
        try:
            with os.scandir(top) as second:
                mids = [Path(d.path) for d in second if d.is_dir() and d.stat().st_mtime >= since - 1]
        except OSError:
            continue
        for mid in mids:
            try:
                with os.scandir(mid) as third:
                    for f in third:
                        if f.is_file():
                            mtime = f.stat().st_mtime
                            if mtime >= since:
                                out.append((mtime, Path(f.path)))
            except OSError:
                continue
    return [p for _m, p in sorted(out, reverse=True)]


# ------------------------------------------------------------- the report
@dataclass(frozen=True)
class PlayCheck:
    notes: tuple[Reason, ...] = ()
    calibration: Calibration | None = None
    replay_path: str | None = None
    map_confirmed: bool | None = None


def _r1(value: float) -> float:
    return round(float(value), 1)


def calibration_notes(cal: Calibration) -> list[Reason]:
    """The calibration as short factual lines (none when it could not be measured)."""
    fit = cal.fit
    if fit is None:
        return [Reason("play.calib.no_match")] if "no_clock" in cal.flags else []
    verdict = cal.verdict
    if verdict == "confirmed":
        return [Reason("play.calib.confirmed", {"rms": _r1(cal.assumed_rms_px or fit.rms_px)})]
    if verdict == "mismatch":
        why = Reason("play.calib.why.transformed") if "transformed" in cal.flags else \
            Reason("play.calib.why.residual", {"rms": _r1(fit.rms_px)})
        return [Reason("play.calib.mismatch", {"why": why})]
    dx, dy = cal.shift_px or (0.0, 0.0)
    ratio = fit.scale / cal.assumed_scale if cal.assumed_scale else 1.0
    return [Reason("play.calib.shifted", {"dx": _r1(dx), "dy": _r1(dy), "scale": round(ratio * 100.0)})]


def check_play(capture: PlayCapture, beatmap: Beatmap | None, *, osu_dir: str | Path | None = None,
               recorder_now: float | None = None, finder: Any = None, lazer_files: str | Path | None = None,
               cfg: OsuConfig | None = None, stats: Any = None, refresh_hz: float = 60.0,
               replay_path: str | Path | None = None, wall_now: float | None = None) -> PlayCheck:
    """Everything the report says about pointer accuracy after a live play, as short lines. Never raises.

    The newest replay written since the capture started is used (or ``replay_path``). Without one, only
    what ``osu!.<user>.cfg`` says (:func:`~.osucfg.pointer_trust`) and the sampler statistics are reported.
    """
    try:
        return _check_play(capture, beatmap, osu_dir, recorder_now, finder, lazer_files, cfg, stats, refresh_hz,
                           replay_path, wall_now)
    except Exception:          # a report line is never worth breaking the round
        log_exception("checking the play against the replay")
        return PlayCheck()


def _check_play(capture, beatmap, osu_dir, recorder_now, finder, lazer_files, cfg, stats, refresh_hz,
                replay_path, wall_now) -> PlayCheck:
    notes: list[Reason] = []
    wall = time.time() if wall_now is None else wall_now
    elapsed = max(0.0, (recorder_now - capture.started_at)) if recorder_now is not None else 0.0
    path = Path(replay_path) if replay_path else find_new_replay(osu_dir, wall - elapsed - 2.0, lazer_files)
    replay = replay_mod.load_replay(path) if path is not None else None
    cal, confirmed = None, None
    if replay is not None:
        cal = calibrate(capture, replay)
        notes += calibration_notes(cal)
        if beatmap is not None:
            confirmed = replay_mod.matches_beatmap(replay, beatmap)
            if confirmed is False:
                entry = finder.by_md5(replay.beatmap_md5) if finder is not None else None
                name = f"{entry.artist} - {entry.title} [{entry.version}]" if entry is not None else None
                notes.append(Reason("play.calib.map_other", {"name": name}) if name else
                             Reason("play.calib.map_unknown"))
            elif confirmed:
                notes.append(Reason("play.calib.map_ok"))
    if cal is None or cal.fit is None:
        notes += list(pointer_trust(cfg, refresh_hz).reasons)
    if stats is not None and (getattr(stats, "backfilled", 0) > 0 or getattr(stats, "max_gap_ms", 0.0) >= 20.0):
        notes.append(Reason("play.calib.sampling", {"gap": int(round(stats.max_gap_ms)),
                                                     "filled": int(stats.backfilled)}))
    return PlayCheck(tuple(notes), cal, str(path) if path is not None else None, confirmed)
