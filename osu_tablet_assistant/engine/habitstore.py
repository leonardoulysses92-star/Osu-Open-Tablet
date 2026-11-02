"""The player's habits across rounds and plays, kept on this PC (stdlib only, no tkinter).

``engine.habits`` measures one play; this module keeps those measurements (``habits.json`` in the assistant's
own config folder, next to ``config.json``), pools them and answers "is this habit real?". Only summary
numbers are stored: the habit values with their standard errors, the setup they were measured on, the date,
an anonymous session id and whether the play came from a replay. Never positions, clicks, map names, scores
or anything that identifies the player. :meth:`HabitStore.forget` deletes all of it ("Olvidar mis hábitos").

Pooling. Plays are combined by **inverse-variance weighting** (``value = sum(v_i / se_i^2) / sum(1 / se_i^2)``,
``se = 1 / sqrt(sum(1 / se_i^2))``). Habits that depend on the setup are first carried over to the current one,
so a change the coach already made is not counted twice:

* aim bias (mm at the area centre): minus the shift of the area since that play;
* gains: ``(1 + g) * old_size / new_size - 1`` per axis; ``size`` and ``aspect`` are then derived from them;
* rotation: minus the rotation added since;
* trail and jitter depend on the filters: only plays with the same smoothing count.

Plays flagged for EMI are kept only for the spike count (research rule 10: do not tune on them). Views:
``session`` (this coach session) and ``long`` (the last ``LONG_TERM_DAYS`` days).

When a habit is worth acting on (the thresholds are heuristics, see the constants): significant (``> 2 SE``),
**repeated** (pointing the same way, by more than its own SE, in at least ``MIN_PLAYS_AGREE`` plays) and backed
by at least ``MIN_SAMPLES`` objects / jumps in total (S20 uses 5 replays and 50 jumps; with 5 plays or more the
confidence may reach "high"). Whether it also costs hits is the rulebook's question.

The file is versioned (``STORE_VERSION``), written atomically, and a corrupt or foreign file never breaks
anything: it is set aside as ``habits.corrupt.json`` and the store starts empty.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ..models import Rect
from . import areafit
from . import habits as hb

STORE_VERSION = 1
FILE_NAME = "habits.json"
MAX_PLAYS = 300            # older plays are forgotten
LONG_TERM_DAYS = 90.0      # the long-term view only looks this far back (heuristic)
MIN_PLAYS_AGREE = 2        # heuristic: a habit must show up the same way in at least this many plays ...
MIN_SAMPLES = 50           # ... over at least this many objects / jumps in total (S20: 50 usable jumps) ...
HIGH_PLAYS = 5             # ... and "high" confidence needs at least this many plays (S20: 5 replays)
SE_FLOOR = 1e-4            # a reported SE below this is treated as this (keeps one play from dominating)
# Setups this close count as the same one (heuristic): a few coach steps do not make a "new setup".
SAME_SIZE = 0.05           # relative width / height
SAME_CENTER_MM = 3.0
SAME_ROTATION_DEG = 3.0
SAME_SMOOTHING_MS = 2.0
# Adaptation window (research rule 11; forum anecdotes: hours to days for an area, days to two weeks after
# leaving heavy smoothing). Heuristics.
ADAPT_AREA_DAYS = 3.0
ADAPT_SMOOTHING_DAYS = 10.0
HEAVY_SMOOTHING_MS = 8.0   # the smoothing card's "quite a lot" warning
AREA_HABITS = (hb.AIM_X, hb.AIM_Y, hb.GAIN_X, hb.GAIN_Y, hb.ROTATION)
SMOOTHING_HABITS = (hb.TRAIL, hb.JITTER, hb.JITTER_HOVER)
DERIVED = (hb.SIZE, hb.ASPECT)                       # recomputed from the pooled gains
_UNITS = {hb.AIM_X: "r", hb.AIM_Y: "r", hb.GAIN_X: "frac", hb.GAIN_Y: "frac", hb.SIZE: "frac", hb.ASPECT: "frac",
          hb.ROTATION: "deg", hb.OVERSHOOT: "frac", hb.TRAIL: "r_per_m_s", hb.UR: "ur", hb.TIMING: "ms",
          hb.KEY_GAP: "ms", hb.TAP_LEAD: "ms", hb.DRIFT_X: "r_per_min", hb.DRIFT_Y: "r_per_min",
          hb.PEN_LIFT: "frac", hb.SPIKES: "per_min"}


def unit_of(key: str) -> str:
    if key.startswith(hb.EDGE_CLAMP):
        return "frac"
    return _UNITS.get(key, "r")


def new_session_id() -> str:
    """An anonymous id for one coach session (random; says nothing about the player)."""
    return uuid.uuid4().hex[:12]


# ------------------------------------------------------------------- data
@dataclass(frozen=True)
class Setup:
    """The setup a play was measured on: the area as OTD stores it and the smoothing that was on."""
    width: float
    height: float
    x: float
    y: float
    rotation: float = 0.0
    smoothing_ms: float = 0.0                 # estimated extra delay of the enabled filters
    smoothing: tuple[str, ...] = ()           # enabled smoothing filters (paths)

    @classmethod
    def of(cls, area: Rect, smoothing_ms: float = 0.0, smoothing: Iterable[str] = ()) -> "Setup":
        return cls(area.width, area.height, area.x, area.y, area.rotation, round(float(smoothing_ms), 2),
                   tuple(sorted(smoothing)))

    @property
    def area(self) -> Rect:
        return Rect(self.width, self.height, self.x, self.y, self.rotation)

    @property
    def smoothing_key(self) -> str:
        return ",".join(self.smoothing) + f"@{round(self.smoothing_ms * 2) / 2:g}"

    def close_to(self, other: "Setup") -> bool:
        return (abs(self.width / other.width - 1) <= SAME_SIZE and abs(self.height / other.height - 1) <= SAME_SIZE
                and math.hypot(self.x - other.x, self.y - other.y) <= SAME_CENTER_MM
                and abs(_wrap(self.rotation - other.rotation)) <= SAME_ROTATION_DEG
                and abs(self.smoothing_ms - other.smoothing_ms) <= SAME_SMOOTHING_MS)


@dataclass(frozen=True)
class PlayRecord:
    """One play as stored: summary numbers only."""
    t: float                                  # when (epoch seconds)
    session: str
    source: str                               # live | replay
    setup: Setup
    r_mm: float
    reach_mm: float = 0.0
    fast_speed: float = 0.0
    mods_known: bool = False
    emi: bool = False
    habits: tuple[tuple[str, float, float, int, str], ...] = ()   # key, value, se, n, detail

    def habit(self, key: str) -> tuple[float, float, int, str] | None:
        return next(((v, se, n, d) for k, v, se, n, d in self.habits if k == key), None)


@dataclass(frozen=True)
class PooledHabit:
    """A habit pooled over plays (see the module docstring)."""
    key: str
    value: float
    se: float
    n: int
    plays: int
    agree: int = 0                            # plays pointing the same way by more than their own SE
    replays: int = 0
    capped: bool = True                       # some play had unknown mods
    detail: str = ""

    @property
    def unit(self) -> str:
        return unit_of(self.key)

    @property
    def z(self) -> float:
        return abs(self.value) / self.se if self.se > 0 else (math.inf if self.value else 0.0)

    @property
    def significant(self) -> bool:
        return self.n >= hb.MIN_N and self.z > hb.SIG_Z

    @property
    def repeated(self) -> bool:
        return self.agree >= MIN_PLAYS_AGREE and self.n >= MIN_SAMPLES

    @property
    def confidence(self) -> str:
        level = hb.confidence(self.z, self.n, self.capped, self.plays)
        return "low" if level != "low" and not self.repeated else level

    @property
    def actionable(self) -> bool:
        """Significant, repeated and not "low": worth changing the setup for (if it also costs hits)."""
        return self.significant and self.repeated and self.confidence != "low"


@dataclass(frozen=True)
class HabitView:
    """Pooled habits, carried over to one setup."""
    habits: Mapping[str, PooledHabit] = field(default_factory=dict)
    plays: int = 0
    r_mm: float = 0.0
    reach_mm: float = 0.0
    fast_speed: float = 0.0
    scope: str = "long"

    def get(self, key: str) -> PooledHabit | None:
        return self.habits.get(key)

    def with_prefix(self, prefix: str) -> list[PooledHabit]:
        return [h for k, h in self.habits.items() if k.startswith(prefix)]


@dataclass(frozen=True)
class Adaptation:
    """The setup changed recently outside the coach: give the hands time before tuning again (rule 11)."""
    kind: str                                 # area | smoothing
    days_left: float


def _wrap(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


# ------------------------------------------------------------------- pooling
def _carry(key: str, value: float, se: float, rec: PlayRecord, setup: Setup, r_now: float) -> tuple[float, float]:
    """A play's habit expressed on the current setup (module docstring); radii of the current map."""
    if key in (hb.AIM_X, hb.AIM_Y):
        dx, dy = areafit.tablet_to_view(setup.x - rec.setup.x, setup.y - rec.setup.y, setup.rotation)
        mm = value * rec.r_mm - (dx if key == hb.AIM_X else dy)
        return mm / r_now, se * rec.r_mm / r_now
    if key in (hb.GAIN_X, hb.GAIN_Y):
        old_w, old_h = areafit.view_size(rec.setup.area)
        new_w, new_h = areafit.view_size(setup.area)
        ratio = (old_w / new_w) if key == hb.GAIN_X else (old_h / new_h)
        return (1.0 + value) * ratio - 1.0, se * ratio
    if key == hb.ROTATION:
        return value - _wrap(setup.rotation - rec.setup.rotation), se
    return value, se


def pool(records: Iterable[PlayRecord], setup: Setup, r_now: float, scope: str = "long") -> HabitView:
    """Pool ``records`` onto ``setup`` (inverse-variance weighting, see the module docstring)."""
    recs = list(records)
    rows: dict[str, list[tuple[float, float, int, str, PlayRecord]]] = {}
    for rec in recs:
        for key, value, se, n, detail in rec.habits:
            if key in DERIVED or (rec.emi and key != hb.SPIKES):
                continue
            if key in SMOOTHING_HABITS and rec.setup.smoothing_key != setup.smoothing_key:
                continue
            if not (math.isfinite(value) and math.isfinite(se)) or n <= 0:
                continue
            v, s = _carry(key, value, se, rec, setup, r_now) if r_now > 0 else (value, se)
            rows.setdefault(key, []).append((v, max(s, SE_FLOOR), n, detail, rec))
    out: dict[str, PooledHabit] = {}
    for key, items in rows.items():
        weights = [1.0 / (s * s) for _v, s, _n, _d, _r in items]
        total = sum(weights)
        value = sum(w * v for w, (v, _s, _n, _d, _r) in zip(weights, items)) / total
        sign = 1 if value > 0 else -1 if value < 0 else 0
        agree = sum(1 for v, s, _n, _d, _r in items if sign and v * sign > s)
        out[key] = PooledHabit(key, value, 1.0 / math.sqrt(total), sum(n for _v, _s, n, _d, _r in items), len(items),
                               agree, sum(1 for *_x, r in items if r.source == "replay"),
                               any(not r.mods_known for *_x, r in items), items[-1][3])
    for derived, sign in ((hb.SIZE, 1.0), (hb.ASPECT, -1.0)):
        gx, gy = out.get(hb.GAIN_X), out.get(hb.GAIN_Y)
        if gx is None or gy is None:
            continue
        # The two gains come from separate equations, so their covariance is ignored (an approximation).
        value = (gx.value + gy.value) / 2.0 if derived == hb.SIZE else gx.value - gy.value
        se = math.hypot(gx.se, gy.se) * (0.5 if derived == hb.SIZE else 1.0)
        out[derived] = PooledHabit(derived, value, se, min(gx.n, gy.n), min(gx.plays, gy.plays),
                                   _agree_derived(recs, derived, setup, r_now, value),
                                   min(gx.replays, gy.replays), gx.capped or gy.capped)
    usable = [r for r in recs if not r.emi]
    last = usable[-1] if usable else (recs[-1] if recs else None)
    return HabitView(out, len(recs), r_now, last.reach_mm if last else 0.0, last.fast_speed if last else 0.0, scope)


def _agree_derived(recs: Iterable[PlayRecord], key: str, setup: Setup, r_now: float, pooled: float) -> int:
    """How many plays show a derived habit (size, aspect) the same way, by more than their own SE."""
    sign = 1 if pooled > 0 else -1 if pooled < 0 else 0
    count = 0
    for rec in recs:
        gx, gy = rec.habit(hb.GAIN_X), rec.habit(hb.GAIN_Y)
        if rec.emi or gx is None or gy is None or not sign:
            continue
        vx, sx = _carry(hb.GAIN_X, gx[0], gx[1], rec, setup, r_now)
        vy, sy = _carry(hb.GAIN_Y, gy[0], gy[1], rec, setup, r_now)
        v, s = ((vx + vy) / 2.0, math.hypot(sx, sy) / 2.0) if key == hb.SIZE else (vx - vy, math.hypot(sx, sy))
        count += v * sign > s
    return count


# ------------------------------------------------------------------- the store
class HabitStore:
    """Plays of this player on this PC. ``path=None`` keeps them in memory only (tests, no config folder)."""

    def __init__(self, path: Path | None = None, clock: Callable[[], float] = time.time) -> None:
        self.path, self._clock = path, clock
        self.plays: list[PlayRecord] = []
        self.load_problem: str | None = None      # "corrupt" | "version" when the file could not be used
        if path is not None:
            self._load()

    # ------------------------------------------------------------- file
    def _load(self) -> None:
        assert self.path is not None
        try:
            text = self.path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return
        except (OSError, UnicodeDecodeError):
            self.load_problem = "corrupt"
            return
        try:
            data = json.loads(text)
        except ValueError:
            self._set_aside()
            return
        if not isinstance(data, dict) or not isinstance(data.get("plays"), list):
            self._set_aside()
            return
        if data.get("version") != STORE_VERSION:      # a newer (or unknown) format: keep it, do not use it
            self.load_problem = "version"
            return
        self.plays = [rec for rec in (_record_from(raw) for raw in data["plays"]) if rec is not None][-MAX_PLAYS:]

    def _set_aside(self) -> None:
        self.load_problem = "corrupt"
        try:
            shutil.copy2(self.path, self.path.with_name("habits.corrupt.json"))
        except OSError:
            pass

    def save(self) -> bool:
        """Write the file atomically (temp file + ``os.replace``). False when it could not be written."""
        if self.path is None:
            return True
        if self.load_problem == "version":            # never overwrite a file this version does not understand
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps({"version": STORE_VERSION, "plays": [_record_to(r) for r in self.plays]},
                              ensure_ascii=False, separators=(",", ":"))
            fd, tmp = tempfile.mkstemp(prefix=".habits-", suffix=".tmp", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(text)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
        except OSError:
            return False
        self.load_problem = None
        return True

    def forget(self) -> bool:
        """"Olvidar mis hábitos": every stored play is dropped and the file deleted. False if it could not be."""
        self.plays.clear()
        self.load_problem = None
        if self.path is None:
            return True
        ok = True
        for target in (self.path, self.path.with_name("habits.corrupt.json")):
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                ok = False
        return ok

    # ------------------------------------------------------------- plays
    def add(self, profile: hb.HabitProfile, setup: Setup, session: str) -> PlayRecord | None:
        """Keep one play's habits (nothing when the play was not usable)."""
        if not profile.usable or not profile.habits:
            return None
        rec = PlayRecord(round(self._clock(), 0), session, "replay" if profile.from_replay else "live", setup,
                         profile.r_mm, profile.reach_mm, profile.fast_speed, profile.mods_known, profile.emi,
                         tuple((h.key, h.value, h.se, h.n, h.detail) for h in profile.habits))
        self.plays.append(rec)
        del self.plays[:-MAX_PLAYS]
        return rec

    def records(self, scope: str = "long", session: str | None = None, now: float | None = None) -> list[PlayRecord]:
        now = self._clock() if now is None else now
        if scope == "session":
            return [r for r in self.plays if r.session == session]
        return [r for r in self.plays if now - r.t <= LONG_TERM_DAYS * 86400.0]

    def view(self, setup: Setup, r_now: float, scope: str = "long", session: str | None = None,
             now: float | None = None) -> HabitView:
        return pool(self.records(scope, session, now), setup, r_now, scope)

    def adaptation(self, setup: Setup, session: str, now: float | None = None) -> Adaptation | None:
        """Did the setup change outside the coach recently (rule 11)? Changes the coach made in one of its own
        sessions do not count, nor going back to a setup played in the previous session."""
        now = self._clock() if now is None else now
        plays = sorted(self.plays, key=lambda r: r.t)
        first_same: PlayRecord | None = None
        before: PlayRecord | None = None
        for rec in reversed(plays):
            if rec.setup.close_to(setup):
                first_same = rec
            else:
                before = rec
                break
        if before is None:
            return None
        start_session = first_same.session if first_same is not None else session
        start_t = first_same.t if first_same is not None else now
        if before.session == start_session:
            return None                               # the coach changed it inside one of its sessions
        if any(r.session == before.session and r.setup.close_to(setup) for r in plays):
            return None                               # back to a setup of that session (e.g. not kept)
        old, new = before.setup, setup
        if old.smoothing_ms >= HEAVY_SMOOTHING_MS and new.smoothing_ms <= old.smoothing_ms / 2.0:
            kind, days = "smoothing", ADAPT_SMOOTHING_DAYS
        else:
            kind, days = "area", ADAPT_AREA_DAYS
        left = days - (now - start_t) / 86400.0
        return Adaptation(kind, round(left, 1)) if left > 0 else None


# ------------------------------------------------------------------- (de)serialising
def _record_to(rec: PlayRecord) -> dict[str, Any]:
    s = rec.setup
    return {"t": rec.t, "session": rec.session, "source": rec.source,
            "setup": {"w": s.width, "h": s.height, "x": s.x, "y": s.y, "rot": s.rotation, "smooth_ms": s.smoothing_ms,
                      "smooth": list(s.smoothing)},
            "r_mm": rec.r_mm, "reach_mm": rec.reach_mm, "fast": rec.fast_speed, "mods": rec.mods_known,
            "emi": rec.emi, "habits": [[k, v, se, n, d] for k, v, se, n, d in rec.habits]}


def _num(value: Any, default: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return float(value)


def _record_from(raw: Any) -> PlayRecord | None:
    """One stored play, validated; None when anything essential is wrong (the play is skipped)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("setup"), dict) or not isinstance(raw.get("habits"), list):
        return None
    s = raw["setup"]
    nums = [_num(s.get(k)) for k in ("w", "h", "x", "y")]
    t, r_mm = _num(raw.get("t")), _num(raw.get("r_mm"))
    if any(v is None for v in nums) or t is None or not r_mm or r_mm <= 0 or nums[0] <= 0 or nums[1] <= 0:
        return None
    smooth = tuple(str(p) for p in s.get("smooth", []) if isinstance(p, str)) if isinstance(s.get("smooth"), list) else ()
    setup = Setup(nums[0], nums[1], nums[2], nums[3], _num(s.get("rot"), 0.0), _num(s.get("smooth_ms"), 0.0), smooth)
    rows = []
    for h in raw["habits"]:
        if not isinstance(h, list) or len(h) != 5 or not isinstance(h[0], str):
            continue
        v, se, n = _num(h[1]), _num(h[2]), _num(h[3])
        if v is None or se is None or n is None or se < 0 or n <= 0:
            continue
        rows.append((h[0], v, se, int(n), str(h[4]) if isinstance(h[4], str) else ""))
    return PlayRecord(t, str(raw.get("session", "")), "replay" if raw.get("source") == "replay" else "live", setup,
                      r_mm, _num(raw.get("reach_mm"), 0.0), _num(raw.get("fast"), 0.0), raw.get("mods") is True,
                      raw.get("emi") is True, tuple(rows))
