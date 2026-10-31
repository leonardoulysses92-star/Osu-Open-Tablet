"""Live pen tests: borderless full-display windows that collect cursor samples.

OpenTabletDriver moves the OS cursor, so the window only has to sit under it.
Four tests (``KINDS``): ``rate``, ``stillness``, ``tracking`` and ``aim``.

The module is split in two layers:

* plain functions and classes (geometry, target path, aim scheduling and
  activation, report assembly) that need no display and are unit-tested
  headlessly;
* ``_TestWindow``, the tkinter front-end, created only inside ``run_test``.

Samples and target paths share one clock (``PenSampler.clock``, by default
``time.perf_counter``). Coordinates are desktop pixels; the window assumes the
process and the cursor API use the same coordinate space (call
``SetProcessDpiAwareness`` once at program start on Windows).

Aim activation (``ACTIVATIONS``) does not need a click by default: a target
fires the moment the cursor touches it (``"touch"``) or reaches its center
zone (``"center"``), evaluated on the high-frequency cursor samples with
segment/circle intersection, so pen-on-surface and hovering players are
measured the same way. ``"click"`` keeps the legacy mouse-button behaviour.
"""
from __future__ import annotations

import bisect
import math
import random
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from osu_tablet_assistant.engine import metrics
from osu_tablet_assistant.gui.pen_sampler import PenSampler
from osu_tablet_assistant.models import DisplaySpec, MetricsReport, PenSample

KINDS = ("rate", "stillness", "tracking", "aim")
ACTIVATIONS = ("touch", "center", "click")
COMMON_KEYS = ("tests.common.start_hint", "tests.common.cancel_hint",
               "tests.common.countdown", "tests.common.progress")

COUNTDOWN_S = 3
TRACK_LOOPS = 2.0               # laps of the figure-8 during a tracking run
TRACK_EXTENT = 0.35             # half-size of the figure-8 as a fraction of the display
TRACK_PATH_HZ = 240.0           # sampling rate of the recorded target path
AIM_TARGET_COUNT = 20
AIM_WARMUP_TARGETS = 2          # extra targets at the start of a run that are shown but not scored
# Every run of a test kind uses the same random sequence (targets), whatever the
# option being tested, so differences between options are not target luck.
TEST_SEEDS = {"rate": 11, "stillness": 12, "tracking": 13, "aim": 14}
AIM_MARGIN_FRACTION = 0.1       # keep targets this far from the display border
AIM_MIN_JUMP_FRACTION = 0.2     # consecutive targets are at least this far apart (of the shorter side)
AIM_MIN_JUMP_RADII = 3.0        # ... and at least this many radii, so no target spawns under the cursor
AIM_RADIUS_MM = 3.0
AIM_RADIUS_PX = (14.0, 60.0)    # clamp for the drawn radius
AIM_DEFAULT_RADIUS_PX = 30.0    # AimSession radius when the caller gives none
AIM_DEBOUNCE_S = 0.1            # a new target cannot fire during its first 100 ms
AIM_TELEPORT_FRACTION = 0.5     # one cursor step longer than this (of the shorter side) is a jump, not a flick
AIM_FLASH_S = 0.18              # duration of the activation flash


@dataclass(frozen=True)
class LiveTestSpec:
    """Timing of one test: run length (0 = until finished) and leading seconds discarded."""
    kind: str
    duration_s: float
    ignore_s: float


SPECS = {
    "rate": LiveTestSpec("rate", 5.0, 0.0),
    "stillness": LiveTestSpec("stillness", 5.0, 0.5),
    "tracking": LiveTestSpec("tracking", 8.0, 1.0),
    "aim": LiveTestSpec("aim", 0.0, 0.0),
}


def test_rng(kind: str) -> random.Random:
    """The deterministic generator for ``kind`` (same seed for every candidate and every repeat)."""
    return random.Random(TEST_SEEDS.get(kind, 0))


def kind_keys(kind: str, activation: str = "touch") -> dict[str, str]:
    """i18n keys used by one test: ``title``, ``instructions``, ``running``.

    The aim test has its own instructions/running text per activation mode
    (``touch`` uses the plain keys).
    """
    keys = {part: f"tests.{kind}.{part}" for part in ("title", "instructions", "running")}
    if kind == "aim" and activation != "touch":
        keys["instructions"] = f"tests.aim.instructions_{activation}"
        keys["running"] = f"tests.aim.running_{activation}"
    return keys


def all_keys() -> set[str]:
    """Every i18n key this module can ask for."""
    keys = set(COMMON_KEYS)
    for kind in KINDS:
        for activation in (ACTIVATIONS if kind == "aim" else ("touch",)):
            keys.update(kind_keys(kind, activation).values())
    return keys


def resolve_translate(translate: Callable[..., str] | None) -> Callable[..., str]:
    """``translate`` if given, else ``gui.i18n.t`` if importable, else a key-echo fallback."""
    if translate is not None:
        return translate
    try:
        from osu_tablet_assistant.gui.i18n import t
    except ImportError:
        return lambda key, **_params: key
    return t


def window_rect(display: DisplaySpec | None, primary_size: tuple[int, int]) -> tuple[int, int, int, int]:
    """``(left, top, width, height)`` in desktop px of the monitor to cover.

    ``display`` is center-based like OTD (left = offset_x - width / 2); ``None``
    means the primary monitor, which sits at the desktop origin.
    """
    if display is None:
        return 0, 0, primary_size[0], primary_size[1]
    return (round(display.offset_x - display.width_px / 2), round(display.offset_y - display.height_px / 2),
            display.width_px, display.height_px)


# --------------------------------------------------------------------------
# Tracking target
# --------------------------------------------------------------------------
class TrackingCourse:
    """Figure-8 (1:2 Lissajous) traversed at constant speed, ``loops`` laps in ``duration_s``.

    The curve is tabulated by arc length so speed is exactly constant, which
    keeps the target's jerk comparable between runs.
    """

    TABLE_POINTS = 2048

    def __init__(self, cx: float, cy: float, rx: float, ry: float,
                 duration_s: float, loops: float = TRACK_LOOPS) -> None:
        n = self.TABLE_POINTS
        self._pts = [(cx + rx * math.sin(2 * math.pi * i / n), cy + ry * math.sin(4 * math.pi * i / n))
                     for i in range(n + 1)]
        self._cum = [0.0]
        for (x0, y0), (x1, y1) in zip(self._pts, self._pts[1:]):
            self._cum.append(self._cum[-1] + math.hypot(x1 - x0, y1 - y0))
        self.length = self._cum[-1]
        self.speed = loops * self.length / duration_s     # px / s

    def position(self, t: float) -> tuple[float, float]:
        """Target position ``t`` seconds after the run started (``t < 0`` stays at the start)."""
        s = (max(t, 0.0) * self.speed) % self.length
        i = min(bisect.bisect_right(self._cum, s) - 1, len(self._pts) - 2)
        seg = self._cum[i + 1] - self._cum[i]
        f = (s - self._cum[i]) / seg if seg > 0 else 0.0
        (x0, y0), (x1, y1) = self._pts[i], self._pts[i + 1]
        return x0 + f * (x1 - x0), y0 + f * (y1 - y0)

    def sample_path(self, started_at: float, duration_s: float, hz: float = TRACK_PATH_HZ) -> list[PenSample]:
        """The target as ``PenSample``s on the caller's clock, from ``started_at`` for ``duration_s``."""
        n = int(duration_s * hz)
        return [PenSample(started_at + i / hz, *self.position(i / hz)) for i in range(n + 1)]


def make_tracking_course(rect: tuple[int, int, int, int], duration_s: float) -> TrackingCourse:
    """Figure-8 centered in the window ``rect`` (desktop px), see ``TRACK_EXTENT``."""
    left, top, width, height = rect
    return TrackingCourse(left + width / 2, top + height / 2, width * TRACK_EXTENT, height * TRACK_EXTENT,
                          duration_s)


# --------------------------------------------------------------------------
# Aim scheduling
# --------------------------------------------------------------------------
def plan_aim_targets(rect: tuple[int, int, int, int], margin_px: float, count: int, rng: random.Random,
                     *, start: tuple[float, float] | None = None,
                     min_jump_px: float | None = None) -> list[tuple[float, float]]:
    """``count`` random target centers (desktop px) inside ``rect`` minus ``margin_px``.

    Every target is at least ``min_jump_px`` (default ``AIM_MIN_JUMP_FRACTION``
    of the shorter side) from the previous one, and the first one from
    ``start`` (the cursor position when the run begins), so every aim needs a
    real move. 50 random tries, then the farthest corner of the allowed area;
    since every point is at least half the area's diagonal from its farthest
    corner, the jump is guaranteed whenever ``min_jump_px`` is not larger.
    """
    left, top, width, height = rect
    min_jump = AIM_MIN_JUMP_FRACTION * min(width, height) if min_jump_px is None else min_jump_px
    x0, x1 = left + margin_px, left + width - margin_px
    y0, y1 = top + margin_px, top + height - margin_px
    if x0 > x1:
        x0 = x1 = left + width / 2
    if y0 > y1:
        y0 = y1 = top + height / 2
    corners = ((x0, y0), (x0, y1), (x1, y0), (x1, y1))
    targets: list[tuple[float, float]] = []
    prev = start
    for _ in range(count):
        best: tuple[float, float] | None = None
        best_d = -1.0
        for _ in range(50):
            cand = (rng.uniform(x0, x1), rng.uniform(y0, y1))
            d = math.inf if prev is None else math.dist(cand, prev)
            if d > best_d:
                best, best_d = cand, d
            if d >= min_jump:
                break
        if prev is not None and best_d < min_jump:
            corner = max(corners, key=lambda c: math.dist(c, prev))
            if math.dist(corner, prev) > best_d:
                best = corner
        assert best is not None
        targets.append(best)
        prev = best
    return targets


@dataclass(frozen=True)
class AimHit:
    """One activated target: its center, when it appeared, and where/when it fired."""
    target: tuple[float, float]
    appeared_at: float
    sample: PenSample


class AimSession:
    """Shows targets one at a time and decides when each one fires; no tkinter involved.

    ``activation``:

    * ``"touch"`` (default): fires the moment the cursor path touches the
      circle of ``radius_px``;
    * ``"center"``: fires when it touches the center zone of
      ``radius_px * center_fraction``;
    * ``"click"``: legacy, fires on :meth:`press` (mouse button), wherever it is.

    Cursor samples go through :meth:`observe`. Consecutive samples are joined
    by a straight segment and the activation is its first intersection with
    the circle, with the time interpolated along the segment, so a fast flick
    crossing the circle between two samples counts, at the moment it crossed.
    Mouse buttons (:meth:`press`) are ignored in touch/center modes.

    Guards: a target never fires during its first ``debounce_s``; touching it
    then is ignored, but a cursor that is still in the activation zone when
    the debounce ends fires it at that moment (so a quick player is never
    stuck on a target). A target that appeared with the cursor already on its
    circle (``radius_px``) never fires until the cursor has left that circle
    and come back. A single step longer than
    ``max_step_px`` is a teleport (pen re-entering hover range), not a
    flick: only its end point is tested. Without any known cursor position
    (no ``cursor`` at :meth:`start`, no sample yet) the first sample is taken
    at face value.
    """

    def __init__(self, targets: Sequence[tuple[float, float]], *, activation: str = "touch",
                 radius_px: float = AIM_DEFAULT_RADIUS_PX, center_fraction: float = metrics.AIM_CENTER_FRACTION,
                 debounce_s: float = AIM_DEBOUNCE_S, max_step_px: float | None = None,
                 warmup: int = 0) -> None:
        if activation not in ACTIVATIONS:
            raise ValueError(f"unknown activation mode: {activation!r}")
        if not (math.isfinite(radius_px) and radius_px > 0):
            raise ValueError("radius_px must be a positive number")
        if not (0 < center_fraction <= 1):
            raise ValueError("center_fraction must be in (0, 1]")
        self.targets = list(targets)
        self.activation = activation
        self.radius = float(radius_px)
        self.center_fraction = center_fraction
        self.debounce_s = debounce_s
        self.max_step_px = max_step_px
        self.warmup = max(0, int(warmup))     # leading targets that are played but not scored
        self.index = 0
        self.hits: list[AimHit] = []
        self.trail: list[PenSample] = []      # every cursor sample observed, in order
        self._appeared_at: float | None = None
        self._last: PenSample | None = None
        self._spawn: tuple[float, float] | None = None   # cursor position when the current target appeared
        self._armed: bool | None = None       # None: not decided yet for the current target

    @property
    def current(self) -> tuple[float, float] | None:
        """Center of the visible target, ``None`` when not started or finished."""
        if self._appeared_at is None or self.index >= len(self.targets):
            return None
        return self.targets[self.index]

    @property
    def done(self) -> bool:
        return self.index >= len(self.targets)

    @property
    def zone_radius(self) -> float:
        """Radius that fires the target: its center zone in ``center`` mode, else the circle."""
        return self.radius * self.center_fraction if self.activation == "center" else self.radius

    @property
    def clicks(self) -> list[tuple[PenSample, tuple[float, float]]]:
        """``(activation sample, target center)`` per fired target (name kept for old callers)."""
        return [(h.sample, h.target) for h in self.hits]

    @property
    def appear_times(self) -> list[float]:
        """When each fired target appeared, parallel to :attr:`clicks`."""
        return [h.appeared_at for h in self.hits]

    def start(self, now: float, cursor: tuple[float, float] | None = None) -> None:
        """Show the first target at time ``now``; ``cursor`` is where the cursor is at that moment."""
        self._appeared_at = now
        self._armed = None
        self._spawn = None
        if cursor is not None and all(math.isfinite(v) for v in cursor):
            self._last = PenSample(now, float(cursor[0]), float(cursor[1]))
            self._spawn = (self._last.x, self._last.y)
            self.trail.append(self._last)

    def observe(self, t: float, x: float, y: float) -> bool:
        """Feed one cursor sample (time order); True when it fired at least one target.

        Non-finite samples and samples older than the previous one are ignored.
        """
        if not (math.isfinite(t) and math.isfinite(x) and math.isfinite(y)):
            return False
        if self._last is not None and t < self._last.t:
            return False
        cur = PenSample(t, float(x), float(y))
        prev, self._last = self._last, cur
        self.trail.append(cur)
        if self.activation == "click":
            return False
        fired = False
        while self.current is not None and self._advance(prev, cur):
            fired = True
            prev = self.hits[-1].sample      # the rest of the segment may reach the next target
        return fired

    def press(self, now: float, x: float, y: float) -> bool:
        """Mouse button at ``(x, y)``: fires in ``click`` mode only (after the debounce)."""
        if (self.activation != "click" or self.current is None or self._appeared_at is None
                or now - self._appeared_at < self.debounce_s):
            return False
        self._fire(PenSample(now, float(x), float(y)))
        return True

    def click(self, now: float, x: float, y: float) -> bool:
        """Legacy entry point: :meth:`press` in click mode, otherwise only a cursor sample."""
        return self.press(now, x, y) if self.activation == "click" else self.observe(now, x, y)

    def _advance(self, prev: PenSample | None, cur: PenSample) -> bool:
        """Test the segment ``prev -> cur`` against the current target; True when it fired."""
        center, appeared = self.current, self._appeared_at
        assert center is not None and appeared is not None
        arm_t = appeared + self.debounce_s
        if cur.t < arm_t:
            return False
        start = prev
        if prev is not None and prev.t < arm_t:          # keep only the part after the debounce
            f = (arm_t - prev.t) / (cur.t - prev.t)
            start = PenSample(arm_t, prev.x + f * (cur.x - prev.x), prev.y + f * (cur.y - prev.y))
        if self._armed is None:      # decided once per target, when its debounce ends
            spawned_on = self._spawn is not None and math.dist(self._spawn, center) <= self.radius
            self._armed = not spawned_on
            if self._armed and start is not None and math.dist((start.x, start.y), center) <= self.zone_radius:
                self._fire(start)    # got there during the debounce and stayed: fires as it ends
                return True
        if not self._armed:
            if math.dist((cur.x, cur.y), center) > self.radius:
                self._armed = True   # left the circle; a straight segment cannot come back in
            return False
        teleport = (self.max_step_px is not None and prev is not None
                    and math.dist((prev.x, prev.y), (cur.x, cur.y)) > self.max_step_px)
        if start is None or teleport:
            if math.dist((cur.x, cur.y), center) > self.zone_radius:
                return False
            hit = cur
        else:
            f = metrics.segment_circle_entry((start.x, start.y), (cur.x, cur.y), center, self.zone_radius)
            if f is None:
                return False
            hit = PenSample(start.t + f * (cur.t - start.t), start.x + f * (cur.x - start.x),
                            start.y + f * (cur.y - start.y))
        self._fire(hit)
        return True

    def _fire(self, hit: PenSample) -> None:
        """Log the current target as hit; the next one appears at the same instant."""
        assert self.current is not None and self._appeared_at is not None
        self.hits.append(AimHit(self.current, self._appeared_at, hit))
        self.index += 1
        self._appeared_at = hit.t
        self._spawn = (hit.x, hit.y)
        self._armed = None


def aim_radius_px(mm_per_px: float) -> float:
    """Drawn target radius: ``AIM_RADIUS_MM`` converted to px, clamped to ``AIM_RADIUS_PX``."""
    lo, hi = AIM_RADIUS_PX
    return min(max(AIM_RADIUS_MM / mm_per_px, lo), hi)


def new_aim_session(rect: tuple[int, int, int, int], mm_per_px: float, rng: random.Random, *,
                    cursor: tuple[float, float] | None, activation: str = "touch",
                    center_fraction: float = metrics.AIM_CENTER_FRACTION,
                    count: int = AIM_TARGET_COUNT, warmup: int = 0) -> AimSession:
    """The aim session the test window runs: targets planned away from ``cursor``.

    ``warmup`` extra targets come first and are left out of the report (the
    first aims of a run are slower while the hand settles).
    """
    _, _, width, height = rect
    radius = aim_radius_px(mm_per_px)
    short = min(width, height)
    margin = max(radius * 2, AIM_MARGIN_FRACTION * short)
    min_jump = max(AIM_MIN_JUMP_FRACTION * short, AIM_MIN_JUMP_RADII * radius)
    targets = plan_aim_targets(rect, margin, count + max(0, warmup), rng, start=cursor, min_jump_px=min_jump)
    return AimSession(targets, activation=activation, radius_px=radius, center_fraction=center_fraction,
                      max_step_px=AIM_TELEPORT_FRACTION * short, warmup=warmup)


# --------------------------------------------------------------------------
# Result assembly
# --------------------------------------------------------------------------
def build_report(kind: str, mm_per_px: float, samples: Sequence[PenSample], *, started_at: float,
                 course: TrackingCourse | None = None, aim: AimSession | None = None) -> MetricsReport:
    """Turn what a run collected into the ``MetricsReport`` for ``kind``.

    Samples older than ``started_at + ignore_s`` are discarded (settling time).
    ``tracking`` needs ``course``; ``aim`` needs ``aim`` (activations,
    appearance times, radius). The aim trajectory is ``samples``, or the
    session's own ``trail`` when no samples are given.
    """
    if kind not in SPECS:
        raise ValueError(f"unknown test kind: {kind!r}")
    spec = SPECS[kind]
    kept = [s for s in samples if s.t >= started_at + spec.ignore_s]
    if kind == "rate":
        return metrics.rate_report(kept)
    if kind == "stillness":
        return metrics.stillness_report(kept, mm_per_px)
    if kind == "tracking":
        if course is None:
            raise ValueError("tracking needs a TrackingCourse")
        return metrics.tracking_report(kept, course.sample_path(started_at, spec.duration_s), mm_per_px)
    if aim is None:
        raise ValueError("aim needs an AimSession")
    trajectory = kept or [s for s in aim.trail if s.t >= started_at]
    return metrics.aim_report(aim.clicks[aim.warmup:], mm_per_px, appear_times=aim.appear_times[aim.warmup:],
                              samples=trajectory,
                              radius_px=aim.radius, center_fraction=aim.center_fraction)


# --------------------------------------------------------------------------
# tkinter front-end
# --------------------------------------------------------------------------
BG, FG, MUTED, ACCENT = "#14161a", "#f1f1f1", "#8b919c", "#ff66aa"
FLASH = "#ffffff"
FONT = "Segoe UI"


def blend(color_a: str, color_b: str, k: float) -> str:
    """``#rrggbb`` mix: ``k = 0`` is ``color_a``, ``k = 1`` is ``color_b``."""
    k = min(max(k, 0.0), 1.0)
    a = [int(color_a[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(color_b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(p + (q - p) * k):02x}" for p, q in zip(a, b))


class _TestWindow:
    """Borderless Toplevel running one test: intro -> countdown -> run -> done/cancel."""

    TICK_MS = 8

    def __init__(self, parent: Any, kind: str, mm_per_px: float, display: DisplaySpec | None,
                 on_done: Callable[[MetricsReport], None], on_cancel: Callable[[], None] | None,
                 translate: Callable[..., str], rng: random.Random, sampler: PenSampler,
                 activation: str = "touch", center_fraction: float = metrics.AIM_CENTER_FRACTION) -> None:
        import tkinter as tk

        self._tk = tk
        self.kind, self.mm_per_px = kind, mm_per_px
        self.spec = SPECS[kind]
        self.activation, self.center_fraction = activation, center_fraction
        self._on_done, self._on_cancel, self._t = on_done, on_cancel, translate
        self._rng = rng
        self._sampler, self._clock = sampler, sampler.clock
        self.rect = window_rect(display, (parent.winfo_screenwidth(), parent.winfo_screenheight()))
        left, top, width, height = self.rect
        self.phase = "intro"
        self._after_id: str | None = None
        self._started_at = 0.0
        self._bar = (0.0, 0.0, 0.0, 0.0)     # progress bar x, y, width, height (set when the run starts)
        self._items: dict[str, int] = {}     # canvas item ids, created when the run starts

        self.course = (make_tracking_course(self.rect, self.spec.duration_s) if kind == "tracking" else None)
        self.aim: AimSession | None = None   # created when the run starts (needs the cursor position)
        self._consumed = 0                   # sampler samples already fed to the aim session
        self._shown: tuple[int, tuple[float, float]] | None = None   # target drawn: (index, center)
        self._flash: tuple[float, float, float] | None = None        # activation flash: x, y, start
        self._finish_at: float | None = None

        self.win = tk.Toplevel(parent)
        self.win.overrideredirect(True)
        self.win.geometry(f"{width}x{height}+{left}+{top}")
        self.win.attributes("-topmost", True)
        self.canvas = tk.Canvas(self.win, width=width, height=height, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.win.protocol("WM_DELETE_WINDOW", self.cancel)
        for widget in (self.win, self.canvas):
            widget.bind("<Escape>", lambda _e: self.cancel())
            widget.bind("<ButtonPress-1>", self._on_click)
        self.win.bind("<Return>", lambda _e: self._begin_countdown())
        self.win.bind("<space>", lambda _e: self._begin_countdown())
        if not sampler.threaded:
            self.canvas.bind("<Motion>", lambda e: sampler.record(e.x_root, e.y_root))
        self.win.focus_force()
        self._show_intro()

    # -- drawing helpers --------------------------------------------------
    def _text(self, x: float, y: float, text: str, size: int, color: str = FG, bold: bool = False,
              wrap: int = 0, tag: str = "ui") -> int:
        font = (FONT, size, "bold") if bold else (FONT, size)
        return self.canvas.create_text(x, y, text=text, fill=color, font=font, width=wrap, justify="center",
                                       tag=tag)

    def _local(self, x: float, y: float) -> tuple[float, float]:
        """Desktop px -> canvas px."""
        return x - self.rect[0], y - self.rect[1]

    def _circle(self, item: str, x: float, y: float, r: float) -> None:
        self.canvas.coords(self._items[item], x - r, y - r, x + r, y + r)

    def _show_intro(self) -> None:
        _, _, width, height = self.rect
        keys = kind_keys(self.kind, self.activation)
        self.canvas.delete("all")
        self._text(width / 2, height * 0.25, self._t(keys["title"]), 34, ACCENT, bold=True)
        self._text(width / 2, height * 0.45, self._t(keys["instructions"]), 18, wrap=int(width * 0.6))
        self._text(width / 2, height * 0.72, self._t("tests.common.start_hint"), 16, FG)
        self._text(width / 2, height * 0.78, self._t("tests.common.cancel_hint"), 13, MUTED)

    # -- phases -----------------------------------------------------------
    def _on_click(self, event: Any) -> None:
        if self.phase == "intro":
            self._begin_countdown()
        elif self.phase == "run" and self.aim is not None:
            self.aim.press(self._clock(), event.x_root, event.y_root)   # ignored unless "click" mode

    def _begin_countdown(self) -> None:
        if self.phase != "intro":
            return
        self.phase = "countdown"
        self._countdown(COUNTDOWN_S)

    def _countdown(self, remaining: int) -> None:
        if self.phase != "countdown":
            return
        if remaining == 0:
            self._begin_run()
            return
        _, _, width, height = self.rect
        self.canvas.delete("all")
        self._draw_run_background()
        self._text(width / 2, height / 2, self._t("tests.common.countdown", n=remaining), 48, ACCENT, bold=True)
        self._after_id = self.win.after(1000, self._countdown, remaining - 1)

    def _draw_run_background(self) -> None:
        """Guides drawn behind the countdown and the run (ring for rate, start dot for tracking)."""
        _, _, width, height = self.rect
        cx, cy = width / 2, height / 2
        if self.kind == "rate":
            r = min(width, height) * 0.2
            self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r, outline=MUTED, dash=(6, 8), width=2)
        elif self.kind == "tracking" and self.course is not None:
            x, y = self._local(*self.course.position(0.0))
            self.canvas.create_oval(x - 14, y - 14, x + 14, y + 14, fill=ACCENT, outline="")

    def _cursor(self) -> tuple[float, float] | None:
        """Current cursor position in desktop px (sampler source, else Tk)."""
        pos = self._sampler.position()
        if pos is not None:
            return pos
        try:
            x, y = self.win.winfo_pointerxy()
        except self._tk.TclError:
            return None
        return (float(x), float(y)) if (x, y) != (-1, -1) else None

    def _begin_run(self) -> None:
        _, _, width, height = self.rect
        self.canvas.delete("all")
        self._draw_run_background()
        self._text(width / 2, height * 0.12, self._t(kind_keys(self.kind, self.activation)["running"]), 22)
        bar_w = width * 0.5
        self._bar = (width / 2 - bar_w / 2, height * 0.92, bar_w, 10)
        x, y, w, h = self._bar
        self.canvas.create_rectangle(x, y, x + w, y + h, outline=MUTED)
        self._items["fill"] = self.canvas.create_rectangle(x, y, x, y + h, fill=ACCENT, outline="")
        if self.kind == "aim":
            self._items["flash"] = self.canvas.create_oval(0, 0, 0, 0, outline=FLASH, width=3, state="hidden")
            self._items["ring"] = self.canvas.create_oval(0, 0, 0, 0, outline=ACCENT, width=3, state="hidden")
            self._items["zone"] = self.canvas.create_oval(0, 0, 0, 0, outline=ACCENT, dash=(4, 4), width=1,
                                                          state="hidden")
            self._items["center"] = self.canvas.create_oval(0, 0, 0, 0, fill=ACCENT, outline="", state="hidden")
            self._items["label"] = self._text(width / 2, height * 0.87, "", 14, MUTED)
        else:
            self._items["dot"] = self.canvas.create_oval(0, 0, 0, 0, fill=ACCENT, outline="", state="hidden")
        self._sampler.start()
        self._started_at = self._clock()
        if self.kind == "aim":
            cursor = self._cursor()
            self.aim = new_aim_session(self.rect, self.mm_per_px, self._rng, cursor=cursor,
                                       activation=self.activation, center_fraction=self.center_fraction,
                                       warmup=AIM_WARMUP_TARGETS)
            self._consumed = len(self._sampler.snapshot())
            self.aim.start(self._started_at, cursor)
        self.phase = "run"
        self._tick()

    def _tick(self) -> None:
        if self.phase != "run":
            return
        now = self._clock()
        if self.aim is not None:
            finished = self._tick_aim(self.aim, now)
            progress = len(self.aim.hits) / max(len(self.aim.targets), 1)
        else:
            progress = (now - self._started_at) / self.spec.duration_s
            finished = progress >= 1.0
            if self.course is not None:
                x, y = self._local(*self.course.position(now - self._started_at))
                self._circle("dot", x, y, 14)
                self.canvas.itemconfigure(self._items["dot"], state="normal")
        bx, by, bw, bh = self._bar
        self.canvas.coords(self._items["fill"], bx, by, bx + bw * min(progress, 1.0), by + bh)
        if finished:
            self._finish()
            return
        self._after_id = self.win.after(self.TICK_MS, self._tick)

    def _tick_aim(self, aim: AimSession, now: float) -> bool:
        """Feed new samples, redraw; True once the last target fired and the settle time passed."""
        fresh = self._sampler.snapshot(self._consumed)
        self._consumed += len(fresh)
        before = len(aim.hits)
        for s in fresh:
            aim.observe(s.t, s.x, s.y)
        if len(aim.hits) > before:
            tx, ty = aim.hits[-1].target
            self._flash = (*self._local(tx, ty), now)
        self._draw_aim(aim, now)
        if aim.done and self._finish_at is None:
            self._finish_at = now + metrics.AIM_SETTLE_S     # keep sampling for the last target's settle
        return self._finish_at is not None and now >= self._finish_at

    def _draw_aim(self, aim: AimSession, now: float) -> None:
        """Ring + center dot (+ dashed center zone) for the current target, progress label, flash."""
        r = aim.radius
        target = aim.current
        if self._shown != (aim.index, target):
            self._shown = (aim.index, target)
            label = self._t("tests.common.progress", done=max(0, len(aim.hits) - aim.warmup),
                            total=max(1, len(aim.targets) - aim.warmup))
            self.canvas.itemconfigure(self._items["label"], text=label)
            if target is None:
                for item in ("ring", "zone", "center"):
                    self.canvas.itemconfigure(self._items[item], state="hidden")
            else:
                x, y = self._local(*target)
                self._circle("ring", x, y, r)
                self._circle("center", x, y, max(3.0, 0.12 * r))
                self._circle("zone", x, y, aim.zone_radius)
                self.canvas.itemconfigure(self._items["ring"], state="normal")
                self.canvas.itemconfigure(self._items["center"], state="normal")
                if aim.activation == "center":
                    self.canvas.itemconfigure(self._items["zone"], state="normal")
        if self._flash is not None:
            fx, fy, t0 = self._flash
            k = (now - t0) / AIM_FLASH_S
            item = self._items["flash"]
            if k >= 1.0:
                self.canvas.itemconfigure(item, state="hidden")
                self._flash = None
            else:
                self._circle("flash", fx, fy, r * (1.0 + 0.8 * k))
                color = blend(FLASH, BG, k)
                self.canvas.itemconfigure(item, state="normal", outline=color, fill=blend(ACCENT, BG, 0.4 + 0.6 * k),
                                          width=max(1.0, 5.0 * (1.0 - k)))

    def _finish(self) -> None:
        self._sampler.stop()
        samples = self._sampler.drain()
        report = build_report(self.kind, self.mm_per_px, samples, started_at=self._started_at,
                              course=self.course, aim=self.aim)
        self._close()
        self._on_done(report)

    def cancel(self) -> None:
        """Abort the test (ESC / window close) and notify ``on_cancel``."""
        if self.phase == "closed":
            return
        self._close()
        if self._on_cancel is not None:
            self._on_cancel()

    def _close(self) -> None:
        self.phase = "closed"
        self._sampler.stop()
        try:
            if self._after_id is not None:
                self.win.after_cancel(self._after_id)
            self.win.destroy()
        except self._tk.TclError:
            pass


def run_test(parent: Any, kind: str, mm_per_px: float, display: DisplaySpec | None,
             on_done: Callable[[MetricsReport], None], on_cancel: Callable[[], None] | None = None,
             *, translate: Callable[..., str] | None = None, rng: random.Random | None = None,
             sampler: PenSampler | None = None, activation: str = "touch",
             center_fraction: float = metrics.AIM_CENTER_FRACTION) -> None:
    """Open the full-display test window for ``kind`` and call ``on_done(report)`` at the end.

    ``on_cancel`` runs instead when the user presses ESC or closes the window.
    ``translate(key, **params)`` defaults to ``gui.i18n.t`` (or echoes the key
    when i18n is unavailable). ``rng`` seeds the aim targets (default: the fixed ``TEST_SEEDS`` of the kind, so every run
    and every option gets the same targets) and ``sampler``
    replaces the cursor sampler (both for tests). ``activation`` picks how aim
    targets fire (``ACTIVATIONS``; default ``"touch"``, no click needed) and
    ``center_fraction`` the center-zone radius relative to the target. Returns
    immediately; the caller's Tk main loop drives the window.
    """
    if kind not in SPECS:
        raise ValueError(f"unknown test kind: {kind!r}")
    if not (math.isfinite(mm_per_px) and mm_per_px > 0):
        raise ValueError("mm_per_px must be a positive number")
    if activation not in ACTIVATIONS:
        raise ValueError(f"unknown activation mode: {activation!r}")
    if not (0 < center_fraction <= 1):
        raise ValueError("center_fraction must be in (0, 1]")
    _TestWindow(parent, kind, mm_per_px, display, on_done, on_cancel, resolve_translate(translate),
                rng if rng is not None else test_rng(kind), sampler if sampler is not None else PenSampler(),
                activation, center_fraction)
