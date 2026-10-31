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

# Analysis grid over the PLAYFIELD (rows x columns of the 512x384 field). Finer grids get smaller cells; the
# cells are pooled with their neighbours (``engine.hotspots``), so few objects per cell is not noise.
ANALYSIS_DETAILS = {"normal": (3, 4), "fine": (6, 8), "very_fine": (9, 12)}
DEFAULT_DETAIL = "fine"
GRID_ROWS, GRID_COLS = ANALYSIS_DETAILS[DEFAULT_DETAIL]


def grid_for(detail: str | None) -> tuple[int, int]:
    """Rows and columns of an analysis detail level (unknown -> the default)."""
    return ANALYSIS_DETAILS.get(detail or DEFAULT_DETAIL, ANALYSIS_DETAILS[DEFAULT_DETAIL])

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
    # pooled with the neighbouring cells (empirical Bayes); what the map and the hotspots use
    est_x_mm: float = 0.0                # estimated signed error, player's view
    est_y_mm: float = 0.0
    est_miss: float = 0.0                # estimated miss rate
    enough: bool = False                 # False: too few objects here to say anything ("no data")

    @property
    def miss_rate(self) -> float:
        return self.misses / self.n if self.n else 0.0


@dataclass(frozen=True)
class Hotspot:
    """A small region where the aim is off beyond what the whole-play pattern (reach fit) explains.

    Adjacent cells that are each weak but jointly significant are merged. ``u, v`` is the centroid in
    normalized area coordinates, ``fx, fy`` in the playfield; the size is the region's extent on the tablet.
    """
    kind: str                            # short | over | off
    row0: int
    col0: int
    row1: int                            # inclusive
    col1: int
    u: float
    v: float
    fx: float
    fy: float
    width_mm: float
    height_mm: float
    n: int                               # objects clicked inside
    dx_mm: float                         # mean error beyond the whole-play pattern, player's view
    dy_mm: float
    err_mm: float                        # size of that error
    z: float                             # evidence (error / its standard error)
    share: float                         # n / all clicked objects
    width_frac: float = 0.0              # extent as a fraction of the playfield
    height_frac: float = 0.0

    @property
    def size(self) -> str:
        """``small`` | ``medium`` | ``large`` (fraction of the playfield it spans)."""
        return "small" if self.width_frac <= 0.3 and self.height_frac <= 0.3 else \
            "medium" if self.width_frac <= 0.55 and self.height_frac <= 0.55 else "large"


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
    hotspots: tuple[Hotspot, ...] = ()   # strongest first
    grid: tuple[int, int] = (GRID_ROWS, GRID_COLS)

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
