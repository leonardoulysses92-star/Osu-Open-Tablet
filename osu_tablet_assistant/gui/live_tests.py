"""Live pen tests: borderless full-display windows that collect cursor samples.

OpenTabletDriver moves the OS cursor, so the window only has to sit under it.
Four tests (``KINDS``): ``rate``, ``stillness``, ``tracking`` and ``aim``.

The module is split in two layers:

* plain functions and classes (geometry, target path, aim scheduling, report
  assembly) that need no display and are unit-tested headlessly;
* ``_TestWindow``, the tkinter front-end, created only inside ``run_test``.

Samples and target paths share one clock (``PenSampler.clock``, by default
``time.perf_counter``). Coordinates are desktop pixels; the window assumes the
process and the cursor API use the same coordinate space (call
``SetProcessDpiAwareness`` once at program start on Windows).
"""
from __future__ import annotations

import bisect
import math
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Sequence

from osu_tablet_assistant.engine import metrics
from osu_tablet_assistant.gui.pen_sampler import PenSampler
from osu_tablet_assistant.models import DisplaySpec, MetricsReport, PenSample

if TYPE_CHECKING:  # pragma: no cover
    import tkinter as tk

KINDS = ("rate", "stillness", "tracking", "aim")
COMMON_KEYS = ("tests.common.start_hint", "tests.common.cancel_hint",
               "tests.common.countdown", "tests.common.progress")

COUNTDOWN_S = 3
TRACK_LOOPS = 2.0               # laps of the figure-8 during a tracking run
TRACK_EXTENT = 0.35             # half-size of the figure-8 as a fraction of the display
TRACK_PATH_HZ = 240.0           # sampling rate of the recorded target path
AIM_TARGET_COUNT = 20
AIM_MARGIN_FRACTION = 0.1       # keep targets this far from the display border
AIM_MIN_JUMP_FRACTION = 0.2     # consecutive targets are at least this far apart
AIM_RADIUS_MM = 3.0
AIM_RADIUS_PX = (14.0, 60.0)    # clamp for the drawn radius
AIM_DEBOUNCE_S = 0.1            # ignore a double tap right after a target appears


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


def kind_keys(kind: str) -> dict[str, str]:
    """i18n keys used by one test: ``title``, ``instructions``, ``running``."""
    return {part: f"tests.{kind}.{part}" for part in ("title", "instructions", "running")}


def all_keys() -> set[str]:
    """Every i18n key this module can ask for."""
    keys = set(COMMON_KEYS)
    for kind in KINDS:
        keys.update(kind_keys(kind).values())
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
def plan_aim_targets(rect: tuple[int, int, int, int], margin_px: float, count: int,
                     rng: random.Random) -> list[tuple[float, float]]:
    """``count`` random target centers (desktop px) inside ``rect`` minus ``margin_px``.

    Consecutive targets are kept at least ``AIM_MIN_JUMP_FRACTION`` of the
    shorter side apart (best effort, 50 tries) so every aim needs a real move.
    """
    left, top, width, height = rect
    min_jump = AIM_MIN_JUMP_FRACTION * min(width, height)
    targets: list[tuple[float, float]] = []
    for _ in range(count):
        cand = (rng.uniform(left + margin_px, left + width - margin_px),
                rng.uniform(top + margin_px, top + height - margin_px))
        for _ in range(50):
            if not targets or math.dist(cand, targets[-1]) >= min_jump:
                break
            cand = (rng.uniform(left + margin_px, left + width - margin_px),
                    rng.uniform(top + margin_px, top + height - margin_px))
        targets.append(cand)
    return targets


class AimSession:
    """Shows targets one at a time and logs clicks; no tkinter involved."""

    def __init__(self, targets: Sequence[tuple[float, float]]) -> None:
        self.targets = list(targets)
        self.index = 0
        self.clicks: list[tuple[PenSample, tuple[float, float]]] = []
        self.appear_times: list[float] = []
        self._appeared_at: float | None = None

    @property
    def current(self) -> tuple[float, float] | None:
        """Center of the visible target, ``None`` when not started or finished."""
        if self._appeared_at is None or self.index >= len(self.targets):
            return None
        return self.targets[self.index]

    @property
    def done(self) -> bool:
        return self.index >= len(self.targets)

    def start(self, now: float) -> None:
        """Show the first target at time ``now``."""
        self._appeared_at = now

    def click(self, now: float, x: float, y: float) -> bool:
        """Register a click; returns False when ignored (nothing shown or too soon)."""
        target = self.current
        if target is None or self._appeared_at is None or now - self._appeared_at < AIM_DEBOUNCE_S:
            return False
        self.clicks.append((PenSample(now, x, y), target))
        self.appear_times.append(self._appeared_at)
        self.index += 1
        self._appeared_at = now      # the next target shows up immediately
        return True


def aim_radius_px(mm_per_px: float) -> float:
    """Drawn target radius: ``AIM_RADIUS_MM`` converted to px, clamped to ``AIM_RADIUS_PX``."""
    lo, hi = AIM_RADIUS_PX
    return min(max(AIM_RADIUS_MM / mm_per_px, lo), hi)


# --------------------------------------------------------------------------
# Result assembly
# --------------------------------------------------------------------------
def build_report(kind: str, mm_per_px: float, samples: Sequence[PenSample], *, started_at: float,
                 course: TrackingCourse | None = None, aim: AimSession | None = None) -> MetricsReport:
    """Turn what a run collected into the ``MetricsReport`` for ``kind``.

    Samples older than ``started_at + ignore_s`` are discarded (settling time).
    ``tracking`` needs ``course``; ``aim`` needs ``aim`` (clicks and appearance
    times, which end up in ``aim_time_ms_mean`` and the extras).
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
    return metrics.aim_report(aim.clicks, mm_per_px, appear_times=aim.appear_times)


# --------------------------------------------------------------------------
# tkinter front-end
# --------------------------------------------------------------------------
BG, FG, MUTED, ACCENT = "#14161a", "#f1f1f1", "#8b919c", "#ff66aa"
FONT = "Segoe UI"


class _TestWindow:
    """Borderless Toplevel running one test: intro -> countdown -> run -> done/cancel."""

    TICK_MS = 8

    def __init__(self, parent: Any, kind: str, mm_per_px: float, display: DisplaySpec | None,
                 on_done: Callable[[MetricsReport], None], on_cancel: Callable[[], None] | None,
                 translate: Callable[..., str], rng: random.Random, sampler: PenSampler) -> None:
        import tkinter as tk

        self._tk = tk
        self.kind, self.mm_per_px = kind, mm_per_px
        self.spec = SPECS[kind]
        self._on_done, self._on_cancel, self._t = on_done, on_cancel, translate
        self._sampler, self._clock = sampler, sampler.clock
        self.rect = window_rect(display, (parent.winfo_screenwidth(), parent.winfo_screenheight()))
        left, top, width, height = self.rect
        self.phase = "intro"
        self._after_id: str | None = None
        self._started_at = 0.0
        self._radius = 0.0
        self._bar = (0.0, 0.0, 0.0, 0.0)     # progress bar x, y, width, height (set when the run starts)
        self._fill: int = 0                  # canvas item ids, created when the run starts
        self._dot: int = 0

        self.course = (make_tracking_course(self.rect, self.spec.duration_s) if kind == "tracking" else None)
        self.aim: AimSession | None = None
        if kind == "aim":
            radius = aim_radius_px(mm_per_px)
            margin = max(radius * 2, AIM_MARGIN_FRACTION * min(width, height))
            self.aim = AimSession(plan_aim_targets(self.rect, margin, AIM_TARGET_COUNT, rng))
            self._radius = radius

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
              wrap: int = 0, tag: str = "ui") -> None:
        font = (FONT, size, "bold") if bold else (FONT, size)
        self.canvas.create_text(x, y, text=text, fill=color, font=font, width=wrap, justify="center", tag=tag)

    def _local(self, x: float, y: float) -> tuple[float, float]:
        """Desktop px -> canvas px."""
        return x - self.rect[0], y - self.rect[1]

    def _show_intro(self) -> None:
        _, _, width, height = self.rect
        keys = kind_keys(self.kind)
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
            self.aim.click(self._clock(), event.x_root, event.y_root)

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

    def _begin_run(self) -> None:
        _, _, width, height = self.rect
        self.canvas.delete("all")
        self._draw_run_background()
        self._text(width / 2, height * 0.12, self._t(kind_keys(self.kind)["running"]), 22)
        bar_w = width * 0.5
        self._bar = (width / 2 - bar_w / 2, height * 0.92, bar_w, 10)
        x, y, w, h = self._bar
        self.canvas.create_rectangle(x, y, x + w, y + h, outline=MUTED)
        self._fill = self.canvas.create_rectangle(x, y, x, y + h, fill=ACCENT, outline="")
        self._dot = self.canvas.create_oval(0, 0, 0, 0, fill=ACCENT, outline="", state="hidden")
        self._sampler.start()
        self._started_at = self._clock()
        self.phase = "run"
        if self.aim is not None:
            self.aim.start(self._started_at)
        self._tick()

    def _tick(self) -> None:
        if self.phase != "run":
            return
        now = self._clock()
        if self.aim is not None:
            progress = len(self.aim.clicks) / len(self.aim.targets)
            self._draw_aim(self.aim)
        else:
            progress = (now - self._started_at) / self.spec.duration_s
            if self.course is not None:
                x, y = self._local(*self.course.position(now - self._started_at))
                self.canvas.coords(self._dot, x - 14, y - 14, x + 14, y + 14)
                self.canvas.itemconfigure(self._dot, state="normal")
        bx, by, bw, bh = self._bar
        self.canvas.coords(self._fill, bx, by, bx + bw * min(progress, 1.0), by + bh)
        if progress >= 1.0:
            self._finish()
            return
        self._after_id = self.win.after(self.TICK_MS, self._tick)

    def _draw_aim(self, aim: AimSession) -> None:
        target = aim.current
        if target is None:
            return
        x, y = self._local(*target)
        r = self._radius
        self.canvas.itemconfigure(self._dot, state="normal")
        self.canvas.coords(self._dot, x - r, y - r, x + r, y + r)
        self.canvas.delete("aim_label")
        _, _, width, height = self.rect
        label = self._t("tests.common.progress", done=len(aim.clicks), total=len(aim.targets))
        self._text(width / 2, height * 0.87, label, 14, MUTED, tag="aim_label")

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
             sampler: PenSampler | None = None) -> None:
    """Open the full-display test window for ``kind`` and call ``on_done(report)`` at the end.

    ``on_cancel`` runs instead when the user presses ESC or closes the window.
    ``translate(key, **params)`` defaults to ``gui.i18n.t`` (or echoes the key
    when i18n is unavailable). ``rng`` seeds the aim targets and ``sampler``
    replaces the cursor sampler (both for tests). Returns immediately; the
    caller's Tk main loop drives the window.
    """
    if kind not in SPECS:
        raise ValueError(f"unknown test kind: {kind!r}")
    if not (math.isfinite(mm_per_px) and mm_per_px > 0):
        raise ValueError("mm_per_px must be a positive number")
    _TestWindow(parent, kind, mm_per_px, display, on_done, on_cancel, resolve_translate(translate),
                rng if rng is not None else random.Random(), sampler if sampler is not None else PenSampler())
