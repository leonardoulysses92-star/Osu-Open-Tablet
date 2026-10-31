"""Read an osu! replay (``.osr``) the player chose: the only way to see what the game itself recorded.

osu!stable writes a replay file after a play. It holds, frame by frame, the cursor position in playfield
pixels and which keys were down, plus the hit counts. That is real data from the game itself, read from a
FILE (osu!'s memory is never touched), and only when the player picks the file.

**Data minimization.** Only what the tablet tuning needs is kept: the map's MD5 (to check it is the right
map), mods (speed and Hard Rock), hit counts, and the cursor/key frames. The player's name, the replay
hash, the online score id, the score, the life graph and the date are read past and **dropped**
(:class:`Replay` has no field for them; a test checks that).

Pure standard library (``lzma``). Never raises on bad files: :func:`parse_replay` returns None.
"""
from __future__ import annotations

import hashlib
import lzma
import math
import struct
from dataclasses import dataclass
from pathlib import Path

from ..models import DisplaySpec, PenSample, Rect, TabletSpec
from .beatmap import playfield_to_desktop
from .playtypes import HIT, Beatmap, ClickEvent, ObjectResult, PlayCapture, PlayReport

MOD_EASY, MOD_HARD_ROCK, MOD_DOUBLE_TIME, MOD_HALF_TIME, MOD_NIGHTCORE = 2, 16, 64, 256, 512
KEY_BITS = ((1, "mouse1"), (2, "mouse2"), (4, "k1"), (8, "k2"))   # M1, M2, K1, K2 (smoke, bit 16, is ignored)
MAX_FILE_BYTES = 40 * 1024 * 1024      # a replay is a few hundred KB; refuse absurd files
MAX_FRAMES = 2_000_000


@dataclass(frozen=True)
class ReplayFrame:
    t_ms: float          # map (audio) time
    x: float             # osu! pixels
    y: float
    keys: int            # bitmask, see KEY_BITS


@dataclass(frozen=True)
class Replay:
    """The minimum kept from a replay file (no player name, hash, score id, score, life graph or date)."""
    beatmap_md5: str
    mods: int
    n300: int
    n100: int
    n50: int
    n_miss: int
    frames: tuple[ReplayFrame, ...]

    @property
    def time_scale(self) -> float:
        if self.mods & (MOD_DOUBLE_TIME | MOD_NIGHTCORE):
            return 1.5
        return 0.75 if self.mods & MOD_HALF_TIME else 1.0

    @property
    def hard_rock(self) -> bool:
        return bool(self.mods & MOD_HARD_ROCK)

    @property
    def accuracy(self) -> float | None:
        """osu!standard accuracy from the hit counts (0..1)."""
        total = self.n300 + self.n100 + self.n50 + self.n_miss
        return (300 * self.n300 + 100 * self.n100 + 50 * self.n50) / (300 * total) if total else None

    @property
    def n_objects(self) -> int:
        return self.n300 + self.n100 + self.n50 + self.n_miss


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data, self.pos = data, 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise ValueError("truncated replay")
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def unpack(self, fmt: str):
        return struct.unpack("<" + fmt, self.take(struct.calcsize("<" + fmt)))[0]

    def string(self) -> str:
        marker = self.take(1)[0]
        if marker == 0:
            return ""
        if marker != 0x0B:
            raise ValueError("bad string marker")
        length, shift = 0, 0
        while True:                               # ULEB128
            byte = self.take(1)[0]
            length |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
            if shift > 35:
                raise ValueError("bad length")
        return self.take(length).decode("utf-8", errors="replace")


def _frames(raw: bytes) -> tuple[ReplayFrame, ...]:
    text = lzma.decompress(raw).decode("ascii", errors="ignore")
    frames, t = [], 0.0
    for chunk in text.split(","):
        parts = chunk.split("|")
        if len(parts) < 4:
            continue
        try:
            w, x, y, z = int(float(parts[0])), float(parts[1]), float(parts[2]), int(float(parts[3]))
        except ValueError:
            continue
        if w == -12345:                           # the random-seed pseudo frame at the end
            continue
        t += w
        if math.isfinite(x) and math.isfinite(y) and len(frames) < MAX_FRAMES:
            frames.append(ReplayFrame(t, x, y, z))
    return tuple(frames)


def parse_replay(data: bytes) -> Replay | None:
    """Parse the bytes of an ``.osr``; None when it is not a readable osu!standard replay."""
    if not data or len(data) > MAX_FILE_BYTES:
        return None
    try:
        r = _Reader(data)
        mode = r.take(1)[0]
        if mode != 0:                              # 0 = osu!standard
            return None
        r.unpack("i")                               # game version
        md5 = r.string()
        r.string()                                  # player name  -> dropped
        r.string()                                  # replay md5   -> dropped
        n300, n100, n50 = r.unpack("H"), r.unpack("H"), r.unpack("H")
        r.unpack("H")                               # geki
        r.unpack("H")                               # katu
        n_miss = r.unpack("H")
        r.unpack("i")                               # total score  -> dropped
        r.unpack("H")                               # max combo
        r.take(1)                                   # perfect
        mods = r.unpack("i")
        r.string()                                  # life graph   -> dropped
        r.unpack("q")                               # timestamp    -> dropped
        size = r.unpack("i")
        frames = _frames(r.take(size))
    except (ValueError, struct.error, lzma.LZMAError, IndexError, UnicodeError):
        return None
    if len(frames) < 10:
        return None
    return Replay(md5, mods, n300, n100, n50, n_miss, frames)


def load_replay(path: str | Path) -> Replay | None:
    """Read and parse a replay file; None on any problem (missing, too big, not a replay)."""
    try:
        p = Path(path)
        if p.stat().st_size > MAX_FILE_BYTES:
            return None
        return parse_replay(p.read_bytes())
    except OSError:
        return None


def beatmap_md5(path: str | Path) -> str | None:
    """MD5 of a ``.osu`` file (what a replay stores to say which map it is)."""
    try:
        return hashlib.md5(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def matches_beatmap(replay: Replay, beatmap: Beatmap) -> bool | None:
    """True/False if the replay is of this exact map file; None when the file is not available to compare."""
    digest = beatmap_md5(beatmap.path) if beatmap.path else None
    return None if digest is None else digest.lower() == replay.beatmap_md5.lower()


def replay_to_capture(replay: Replay, area: Rect, display: DisplaySpec, tablet: TabletSpec | None = None
                      ) -> PlayCapture:
    """The replay as a :class:`PlayCapture` the coach already knows how to judge.

    Times become real seconds (map time divided by the speed), positions become desktop pixels (the same
    approximation the live recorder relies on) and each key going down becomes a click.
    """
    scale = replay.time_scale
    samples = tuple(PenSample(f.t_ms / 1000.0 / scale, *playfield_to_desktop(f.x, f.y, display))
                    for f in replay.frames)
    clicks, down = [], 0
    for f in replay.frames:
        for bit, name in KEY_BITS:
            if f.keys & bit and not down & bit:
                clicks.append(ClickEvent(f.t_ms / 1000.0 / scale, name))
        down = f.keys
    end = samples[-1].t if samples else 0.0
    return PlayCapture(samples, tuple(clicks), area, display, tablet, samples[0].t if samples else 0.0, end)


# --------------------------------------------------------- inferred vs real
@dataclass(frozen=True)
class InferenceCheck:
    """How well a live (inferred) report agrees with the same play seen through the replay."""
    compared: int                 # objects judged in both
    agreement: float              # fraction with the same hit / not-hit verdict
    live_miss_rate: float
    replay_miss_rate: float
    real_accuracy: float | None   # from the replay's hit counts
    offset_ms: float              # difference of the two time alignments (live - replay)


def compare_inference(live: PlayReport, from_replay: PlayReport, replay: Replay | None = None) -> InferenceCheck | None:
    """Compare per-object hit/not-hit between the live inference and the replay-based one (None if no overlap)."""
    by_index = {r.index: r for r in from_replay.results}
    pairs = [(r, by_index[r.index]) for r in live.results if r.index in by_index]
    if not pairs:
        return None
    same = sum(1 for a, b in pairs if (a.verdict == HIT) == (b.verdict == HIT))

    def miss(rows: list[ObjectResult]) -> float:
        return sum(1 for r in rows if r.verdict != HIT) / len(rows)

    return InferenceCheck(len(pairs), same / len(pairs), miss([a for a, _ in pairs]), miss([b for _, b in pairs]),
                          replay.accuracy if replay else None,
                          live.alignment.offset_ms - from_replay.alignment.offset_ms)
