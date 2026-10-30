"""Plain data shared by the Play Coach pipeline (no logic, no tkinter).

Pipeline: ``gui.playwatch`` records a :class:`PlayCapture`; ``engine.beatmap``
supplies the :class:`Beatmap`; ``engine.playobserve`` turns both into a
:class:`PlayReport`; ``engine.optimizer`` turns reports into rounds and
proposals. Field names are part of the contract between those modules.

Coordinate systems
------------------
* desktop pixels: what ``GetCursorPos`` returns (``PenSample.x/.y``);
* osu! pixels: the 512x384 playfield of a beatmap;
* normalized area ``(u, v)``: 0 = left/top edge of the screen (and of the
  active area), 1 = right/bottom edge, as the player sees it, whatever the
  area rotation. Area millimetres in the player's view are ``(u * w, v * h)``;
* native tablet millimetres: those of ``Rect.x/.y`` (see ``engine.areafit``).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..models import DisplaySpec, PenSample, Reason, Rect, TabletSpec

GRID_ROWS = 3
GRID_COLS = 4

# Verdicts of one judged object
HIT = "hit"
NO_CLICK = "no_click"
TOO_FAR = "too_far"
TOO_EARLY = "too_early"
TOO_LATE = "too_late"
VERDICTS = (HIT, NO_CLICK, TOO_FAR, TOO_EARLY, TOO_LATE)

# Zones that edge findings talk about
ZONES = ("left", "right", "top", "bottom", "top_left", "top_right", "bottom_left", "bottom_right", "center")
# Kinds of finding for a zone
SHORT, OVER, LATE, NOISY, OK = "short", "over", "late", "noisy", "ok"
FINDING_KINDS = (SHORT, OVER, LATE, NOISY, OK)

# Convergence verdicts
START, IMPROVING, PLATEAU, WORSE, INSUFFICIENT = "start", "improving", "plateau", "worse", "insufficient"
CONVERGENCE_VERDICTS = (START, IMPROVING, PLATEAU, WORSE, INSUFFICIENT)


# ------------------------------------------------------------------- beatmap
@dataclass(frozen=True)
class HitObject:
    """One object of a map. ``kind``: ``circle`` | ``slider`` | ``spinner``."""
    time_ms: float
    x: float                 # osu! pixels
    y: float
    kind: str = "circle"
    new_combo: bool = False

    @property
    def judged(self) -> bool:
        """Only circles and slider heads are judged (no bodies, ends or spinners)."""
        return self.kind in ("circle", "slider")


@dataclass(frozen=True)
class Beatmap:
    title: str = ""
    artist: str = ""
    version: str = ""
    creator: str = ""
    title_unicode: str = ""
    artist_unicode: str = ""
    cs: float = 4.0
    od: float = 5.0
    ar: float = 5.0
    hp: float = 5.0
    objects: tuple[HitObject, ...] = ()
    path: str = ""

    @property
    def judged_objects(self) -> tuple[HitObject, ...]:
        return tuple(o for o in self.objects if o.judged)

    @property
    def display_name(self) -> str:
        return f"{self.artist} - {self.title} [{self.version}]"


# ------------------------------------------------------------------- capture
@dataclass(frozen=True)
class ClickEvent:
    """A press of one of the configured click keys. ``t`` shares the clock of ``PenSample.t``."""
    t: float
    key: str = ""


@dataclass(frozen=True)
class PlayCapture:
    """What the recorder saw: cursor samples, click times, and the setup they were taken under."""
    samples: tuple[PenSample, ...]
    clicks: tuple[ClickEvent, ...]
    area: Rect                           # native tablet mm, centre based, with rotation
    display: DisplaySpec                 # desktop px, centre based
    tablet: TabletSpec | None = None
    started_at: float = 0.0
    ended_at: float | None = None


# ------------------------------------------------------------------- analysis
@dataclass(frozen=True)
class Alignment:
    """How clicks were matched to objects."""
    offset_ms: float = 0.0               # click time minus object time (median residual)
    time_scale: float = 1.0              # 1.0 normal, 1.5 DT, 0.75 HT
    flip_y: bool = False                 # HR
    quality: float = 0.0                 # 0..1; below ALIGN_TRUST the report should not be trusted
    matched: int = 0
    spread_ms: float = 0.0               # median absolute deviation of the residuals


@dataclass(frozen=True)
class ObjectResult:
    index: int                           # index in Beatmap.judged_objects
    time_ms: float                       # object time (map time, ms)
    verdict: str
    click_t: float | None = None         # capture clock, seconds
    time_error_ms: float | None = None   # click - object
    err_x_mm: float | None = None        # cursor minus object centre, player's view axes
    err_y_mm: float | None = None
    err_mm: float | None = None          # Euclidean
    along_mm: float | None = None        # <0 = cursor stopped short of the object
    lateral_mm: float | None = None
    u: float = 0.5                       # object position, normalized area coordinates (whole screen)
    v: float = 0.5
    fx: float = 0.5                      # object position inside the playfield, 0..1 (zones use these)
    fy: float = 0.5
    cursor_u: float | None = None
    cursor_v: float | None = None
    speed_mm_s: float | None = None      # cursor speed just before the click


@dataclass(frozen=True)
class CellStat:
    row: int
    col: int
    n: int = 0
    misses: int = 0
    err_x_mm: float = 0.0                # mean signed error
    err_y_mm: float = 0.0
    err_mm: float = 0.0                  # mean distance
    along_mm: float = 0.0

    @property
    def miss_rate(self) -> float:
        return self.misses / self.n if self.n else 0.0


@dataclass(frozen=True)
class ReachFit:
    """``cursor = gain * object + bias`` per axis, in area millimetres from the area centre."""
    gain_x: float = 1.0
    gain_y: float = 1.0
    bias_x_mm: float = 0.0
    bias_y_mm: float = 0.0
    ci_x: tuple[float, float] = (1.0, 1.0)     # bootstrap interval of gain_x
    ci_y: tuple[float, float] = (1.0, 1.0)
    n: int = 0


@dataclass(frozen=True)
class EdgeFinding:
    zone: str
    kind: str                            # short | over | late | noisy | ok
    severity: float                      # 0..1
    n: int
    miss_rate: float = 0.0
    err_mm: float = 0.0                  # typical size of the problem
    dx_mm: float = 0.0                   # mean signed error in the player's view
    dy_mm: float = 0.0


@dataclass(frozen=True)
class FeelFit:
    lag_mm: float | None = None          # mean error along the path on fast jumps (<0 = behind)
    n_fast: int = 0
    jitter_mm: float | None = None       # RMS wobble of the slow parts
    n_slow: int = 0
    fast_speed_mm_s: float = 0.0


@dataclass(frozen=True)
class PlayReport:
    alignment: Alignment = field(default_factory=Alignment)
    n_objects: int = 0
    n_judged: int = 0                    # objects whose time already passed in this (maybe partial) capture
    results: tuple[ObjectResult, ...] = ()
    cells: tuple[CellStat, ...] = ()
    reach: ReachFit | None = None
    edges: tuple[EdgeFinding, ...] = ()
    feel: FeelFit | None = None
    miss_rate: float = 0.0
    mean_err_ratio: float | None = None  # mean error / circle radius (mm)
    time_spread_ms: float | None = None
    radius_mm: float | None = None
    warnings: tuple[Reason, ...] = ()
    partial: bool = False
    cost: float | None = None            # filled by optimizer.record_round
    cost_ci: tuple[float, float] | None = None

    @property
    def trusted(self) -> bool:
        return self.alignment.quality >= 0.6


# ------------------------------------------------------------------- rounds
@dataclass(frozen=True)
class RoundRecord:
    index: int
    cost: float | None
    cost_ci: tuple[float, float] | None
    matched: int
    area: Rect
    filters: tuple = ()                  # tuple[FilterConfig, ...]
    change: tuple[Reason, ...] = ()      # what was applied BEFORE this round was played
    accepted: bool = True                # counted for convergence (enough data, trusted alignment)
    move_mm: float = 0.0                 # size of that change in area millimetres (0 for filters)
    kind: str = "start"                  # start | shift | scale | filter | revert


@dataclass(frozen=True)
class Convergence:
    verdict: str
    message: Reason
    rounds: int = 0
    last_gain: float | None = None       # relative improvement of the last accepted round (+ = better)
