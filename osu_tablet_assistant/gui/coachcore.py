"""Play Coach logic without Tk: readiness, rounds, applying/undoing/keeping changes.

:class:`CoachCore` sits on a :class:`~osu_tablet_assistant.otd.livetune.LiveTuner` (live sends, undo,
Keep) and on ``engine.optimizer`` (cost, stop rule, one proposal per round). Everything OS-facing (the
recorder, the osu! window title, the process list) is injected, so it is tested with fakes.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ..engine import adapt, areafit, findings as findings_mod, optimizer as opt, replay as replay_mod
from ..engine import smoothing
from ..engine.beatmap import BeatmapIndex, find_beatmap, load_beatmap, parse_window_title
from ..engine.playobserve import ALIGN_TRUST, analyze_play
from ..engine.playtypes import DEFAULT_DETAIL, Beatmap, Convergence, PlayCapture, PlayReport, grid_for
from ..keys import DEFAULT_CLICK_KEYS, PEN_TIP_KEY, display_list, normalize_keys
from ..knowledge.filters import FilterSpec, load_catalog
from ..models import DisplaySpec, FilterConfig, ProfileChanges, Reason, Rect, TabletSpec, UserPreferences
from ..otd import settings_io
from ..otd.livetune import LiveTuner, OsuWatcher
from ..otd.plugins import KNOWN_PLUGINS
from .i18n import t as default_t
from .pen_sampler import PenSampler
from .playwatch import LiveView, PlayRecorder, osu_window_title, songs_dir_for, windows_key_source

ANALYSIS_EVERY_S = 1.5
AUTO_STOP_IDLE_S = 8.0
AUTO_STOP_MIN_CLICKS = 20
NO_CLICKS_HINT_S = 6.0          # recording this long, cursor moving, no click from the chosen keys: ask about them
NO_CLICKS_TRAVEL_SCREENS = 2.0  # ... "moving" = the cursor travelled this many screen heights
PHASES = ("prepare", "observe", "report")
Translate = Callable[..., str]
# Everything the coach and its helpers collect, nothing more (each id has a ``play.data.<id>`` text).
DATA_LEDGER = ("cursor", "keys", "window", "map", "replay", "profile", "never")
# Adjusting the area by hand ("Ajustar el área a mano"): one press = this much, in the player's view.
MANUAL_SIZE_STEP = 0.02         # 2 % bigger / smaller
MANUAL_MOVE_MM = 0.5
MANUAL_MOVES = {"left": (-1.0, 0.0), "right": (1.0, 0.0), "up": (0.0, -1.0), "down": (0.0, 1.0)}
MANUAL_SIZES = {"smaller": (-1, -1), "bigger": (1, 1), "narrower": (-1, 0), "wider": (1, 0),
                "shorter": (0, -1), "taller": (0, 1)}
FREE_ONLY = ("narrower", "wider", "shorter", "taller")       # one axis only: needs the free proportion
# Reasons whose float parameters are numbers to show with the language's decimal mark.
_LOCAL_NUMBERS = ("play.probe.", "play.smooth.", "play.manual.")


def fmt_num(value: float, translate: Translate = default_t, decimals: int = 1) -> str:
    """``76.571`` -> "76,6" in Spanish, "76.6" in English (trailing zeros dropped: 80.0 -> "80")."""
    text = f"{float(value):.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-0"):
        text = "0"
    mark = translate("play.fmt.decimal")
    return text.replace(".", mark if len(mark) == 1 else ".")


def area_summary(area: Rect, translate: Translate = default_t, key: str = "play.area.now") -> str:
    """"Área actual: 80 × 45 mm, centro 76,6 / 45,2 mm, girada 180°" (OTD's own Width/Height/X/Y numbers).

    ``key="play.area.after"`` says the same of a proposed area ("Con el cambio propuesto: ...").
    """
    text = translate(key, w=fmt_num(area.width, translate), h=fmt_num(area.height, translate),
                     x=fmt_num(area.x, translate), y=fmt_num(area.y, translate))
    rotation = area.rotation % 360.0
    if min(rotation, 360.0 - rotation) > 1e-6:
        text += translate("play.area.rotated", rotation=fmt_num(rotation, translate, 0))
    return text


def area_changes(before: Rect, after: Rect, translate: Translate = default_t) -> list[str]:
    """"antes → ahora" for width, height and position; empty while nothing changed."""
    def same(a: float, b: float) -> bool:
        return abs(a - b) < 5e-4

    if all(same(a, b) for a, b in ((before.width, after.width), (before.height, after.height),
                                    (before.x, after.x), (before.y, after.y))):
        return []
    n = lambda v: fmt_num(v, translate)       # noqa: E731
    lines = []
    for name, old, new in (("width", before.width, after.width), ("height", before.height, after.height)):
        lines.append(translate(f"play.area.same.{name}", value=n(new)) if same(old, new)
                     else translate(f"play.area.change.{name}", old=n(old), new=n(new)))
    if same(before.x, after.x) and same(before.y, after.y):
        lines.append(translate("play.area.same.center", value=f"{n(after.x)} / {n(after.y)}"))
    else:
        lines.append(translate("play.area.change.center", old=f"{n(before.x)} / {n(before.y)}",
                               new=f"{n(after.x)} / {n(after.y)}"))
    return lines


def ledger_text(translate: Translate = default_t, keys: Sequence[str] = DEFAULT_CLICK_KEYS) -> str:
    """The plain-language list of what is collected, why, and where it stays (shown in the app)."""
    return "\n".join("• " + translate(f"play.data.{item}", keys=display_list(keys)) for item in DATA_LEDGER)


def place_text(place: str, translate: Translate = default_t) -> str:
    """Words for a ``hotspots.place_id`` ("bottom.center_right.corner" -> "Abajo, a la derecha del centro, ...")."""
    parts = place.split(".")
    if len(parts) < 2:
        return place
    text = translate("play.place.at", v=translate(f"play.place.v.{parts[0]}"), h=translate(f"play.place.h.{parts[1]}"))
    if len(parts) > 2 and parts[2]:
        text += " " + translate(f"play.place.near.{parts[2]}")
    return text


def say(reason: Reason, translate: Translate = default_t) -> str:
    """Translate a Reason; zone, place, size and direction parameters become words, adapt's axis/strength too.

    A parameter that is itself a Reason is translated first (e.g. "the trial" inside "the trial helped").
    """
    params = {k: say(v, translate) if isinstance(v, Reason) else v for k, v in reason.params.items()}
    if reason.key.startswith(_LOCAL_NUMBERS):
        params = {k: fmt_num(v, translate) if isinstance(v, float) else v for k, v in params.items()}
    if "way" in params:
        params["way"] = translate(f"play.probe.way.{params['way']}")
    if "zone" in params:
        params["zone"] = translate(f"play.zone.{params['zone']}")
    if "place" in params:
        params["place"] = place_text(str(params["place"]), translate)
    if "size" in params:
        params["size"] = translate(f"play.size.{params['size']}")
    if "towards" in params:
        params["towards"] = translate(f"play.towards.{params['towards']}")
    if "dir" in params:
        params["dir"] = translate(f"play.dir.{params['dir']}")
    if reason.key.startswith("adapt.") and "axis" in params:
        params["axis"] = translate(f"adapt.axis.{params['axis']}")
    if "strength" in params:
        params["strength"] = translate(f"passive.strength.{params['strength']}")
    return translate(reason.key, **params)


def _travel(capture: PlayCapture) -> float:
    """How far the cursor moved during a capture, in desktop pixels."""
    total, prev = 0.0, None
    for s in capture.samples:
        if prev is not None:
            step = math.hypot(s.x - prev.x, s.y - prev.y)
            if math.isfinite(step):
                total += step
        prev = s
    return total


# --------------------------------------------------------------------------
# Core logic (no Tk)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Readiness:
    """What the prepare screen shows."""
    osu_running: bool
    flavor: str | None = None               # stable | lazer | unknown
    window_title: str | None = None
    beatmap: Beatmap | None = None
    keys_readable: bool = True
    issues: tuple[Reason, ...] = ()
    keys: tuple[str, ...] = DEFAULT_CLICK_KEYS   # the click keys that will be read

    @property
    def can_start(self) -> bool:
        return self.beatmap is not None and self.keys_readable and bool(self.keys)


@dataclass(frozen=True)
class RoundResult:
    """Everything the report screen needs for one finished round."""
    report: PlayReport
    findings: tuple[Reason, ...]
    proposal: opt.RoundProposal
    convergence: Convergence
    marks: tuple[str, ...]
    check: replay_mod.InferenceCheck | None = None    # live inference vs the game's own replay, when one was given
    from_replay: bool = False
    hint: Reason | None = None              # e.g. "no click seen with Z, X: other buttons?" (offer the key card)
    map_changed: bool = False               # this round was played on another map than the previous one


@dataclass(frozen=True)
class ReplayOutcome:
    """Result of reading a replay the player picked."""
    ok: bool
    problem: Reason | None = None
    replay: replay_mod.Replay | None = None
    same_map: bool | None = None            # False: the replay is of another map file (the report may be wrong)
    check: replay_mod.InferenceCheck | None = None


@dataclass(frozen=True)
class CoachFeedback:
    ok: bool
    reasons: tuple[Reason, ...] = ()
    message: Reason | None = None
    sound: str = ""
    live_only: bool = False                 # applied in OTD but not written to settings.json


@dataclass
class _Pending:
    """A change applied before the round that is about to be played (to attribute its effect)."""
    kind: str
    move: tuple[float, float]
    change: tuple[Reason, ...]
    adapt: adapt.AdaptState | None
    probe: opt.ProbeState | None = None


@dataclass
class SmoothTrial:
    """A smoothing filter the player switched on to try (live, not saved)."""
    path: str
    previous: FilterConfig | None        # the store before the trial (None: it was not in the profile)
    base_round: int | None               # last accepted round played without it (what it is compared with)
    result_round: int | None = None      # first accepted round played with it
    decided: bool = False                # the player chose "Keep it"


@dataclass(frozen=True)
class SmoothOption:
    """One row of the smoothing card."""
    path: str
    name: str
    text_id: str                         # play.smooth.what.<id> / play.smooth.feel.<id>
    installed: bool
    on: bool                             # running in OTD right now
    was_on: bool                         # was already on when the coach opened (not a trial)
    trying: bool                         # switched on here, to try
    decided: bool
    delay_ms: float | None               # estimated extra delay with the values it runs (or would run) with


@dataclass(frozen=True)
class SmoothVerdict:
    """"With the smoothing: better / same / worse / unclear" for one trial, after a round played with it."""
    path: str
    name: str
    verdict: str                         # better | same | worse | unclear
    no_base: bool = False                # unclear because no round was played without it


class CoachCore:
    """Round logic on top of a :class:`LiveTuner`."""

    def __init__(self, tuner: LiveTuner, tablet: TabletSpec | None, display: DisplaySpec, *,
                 recorder_factory: Callable[[Rect], PlayRecorder] | None = None,
                 watcher: OsuWatcher | None = None, title_source: Callable[[], str | None] = osu_window_title,
                 cache_path: Path | None = None, prefs: UserPreferences | None = None,
                 policy: adapt.TuningPolicy | None = None, catalog: Mapping[str, FilterSpec] | None = None,
                 aspect_lock: bool = True, allow_filters: bool = True, keys: Sequence[str] = DEFAULT_CLICK_KEYS,
                 analysis_detail: str = DEFAULT_DETAIL, analyze: Callable[..., PlayReport] = analyze_play,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.tuner, self.tablet, self.display = tuner, tablet, display
        self.keys: tuple[str, ...] = normalize_keys(keys, default=())
        self.analysis_detail = analysis_detail
        self.watcher = watcher if watcher is not None else OsuWatcher()
        self._title_source, self._analyze, self._clock = title_source, analyze, clock
        self.cache_path = cache_path
        self.prefs, self.policy = prefs, policy or adapt.TuningPolicy()
        self.catalog = dict(catalog) if catalog is not None else load_catalog()
        self.aspect_lock, self.allow_filters = aspect_lock, allow_filters
        self._recorder_factory = recorder_factory or self._default_recorder
        self.recorder: PlayRecorder | None = None
        self.phase = "prepare"
        self.state = opt.new_state()
        self.beatmap: Beatmap | None = None
        self._manual_map = False
        self._last_title: str | None = None
        self._index: BeatmapIndex | None = None
        self._index_scanned_for: str | None = None
        self.live_report: PlayReport | None = None
        self.result: RoundResult | None = None
        self._pending: _Pending | None = None
        self._last_applied: tuple[str, tuple[float, float]] | None = None
        self._kept_once = False
        self.skipped = 0
        self._round_maps: list[str] = []     # map file of every finished round (to tell when it changed)
        original = self._original_profile()
        self.original_area: Rect = original.tablet_area
        self._original_filters: tuple[FilterConfig, ...] = original.filters
        self.trials: dict[str, SmoothTrial] = {}
        self._touched: set[str] = set()       # smoothing filters the player switched on or off here

    def _original_profile(self):
        for prof in settings_io.list_profiles(self.tuner.original):
            if prof.tablet_name == self.tuner.tablet_name:
                return prof
        return self.tuner.profile()

    # --------------------------------------------------------------- prepare
    def _default_recorder(self, area: Rect) -> PlayRecorder:
        return PlayRecorder(PenSampler(poll_hz=1500.0), self.display, area, self.keys, tablet=self.tablet)

    def set_keys(self, keys: Sequence[str]) -> bool:
        """Use these click keys from the next round on (the recorder is rebuilt with them). False while observing."""
        if self.phase == "observe":
            return False
        self.keys = normalize_keys(keys, default=())
        if self.recorder is not None and not getattr(self.recorder, "running", False):
            self.recorder = None              # the next start() builds one that reads exactly these keys
        return True

    def set_free_aspect(self, free: bool) -> None:
        """``True``: the coach may change the area's proportion per axis (like players who ignore 16:9).

        The proportion of the area against the screen then changes on purpose: circles are drawn a little
        stretched in hand movement, which many experienced players prefer for more room on one axis.
        """
        self.aspect_lock = not free

    def set_analysis_detail(self, detail: str) -> None:
        """``normal`` (3x4) | ``fine`` (6x8) | ``very_fine`` (9x12): grid of the next reports."""
        self.analysis_detail = detail

    @property
    def grid(self) -> tuple[int, int]:
        return grid_for(self.analysis_detail)

    def _full_analysis(self, capture: PlayCapture) -> PlayReport:
        rows, cols = self.grid
        return self._analyze(self.beatmap, capture, rows=rows, cols=cols)

    def no_clicks_hint(self, view: LiveView | None = None) -> Reason | None:
        """Plain hint when the cursor moves a lot but none of the chosen keys was pressed (other buttons?)."""
        view = view if view is not None else self.live_view()
        if view is None or view.clicks > 0 or view.elapsed_s < NO_CLICKS_HINT_S:
            return None
        if view.travel_px < NO_CLICKS_TRAVEL_SCREENS * max(self.display.height_px, 1):
            return None
        return self._keys_hint()

    def _keys_hint(self) -> Reason:
        return Reason("play.keys.none_seen", {"keys": display_list(self.keys)})

    @property
    def profile(self):   # ProfileInfo of what is live right now
        return self.tuner.profile()

    @property
    def area(self) -> Rect:
        return self.profile.tablet_area

    def readiness(self, refresh: bool = True) -> Readiness:
        """Is osu! open, which map is loaded and can it be found. Cheap enough to call every few seconds."""
        if refresh:
            self.watcher.poll()
        osu = self.watcher.process
        issues: list[Reason] = []
        title = self._title_source() if osu is not None else None
        if osu is None:
            issues.append(Reason("play.prep.osu_closed"))
        elif osu.flavor == "lazer":
            issues.append(Reason("play.prep.lazer"))
        if title != self._last_title:
            self._last_title = title
            if parse_window_title(title) is not None:     # osu! loaded another map: look it up again
                self.beatmap, self._manual_map = None, False
        if self.beatmap is None and osu is not None:
            self._find_map(title, osu.path)
        if osu is not None and self.beatmap is None and parse_window_title(title) is None:
            issues.append(Reason("play.prep.no_map_title"))
        elif osu is not None and self.beatmap is None:
            issues.append(Reason("play.prep.map_not_found"))
        keys_ok = self.recorder.keys_available if self.recorder is not None else self._keys_readable()
        if not self.keys:
            issues.append(Reason("play.prep.no_keys"))
        elif not keys_ok:
            issues.append(Reason("play.prep.keys_unreadable"))
        if PEN_TIP_KEY in self.keys:     # cannot be checked from here: explain what has to be true
            issues.append(Reason("play.prep.pen_tip"))
        return Readiness(osu is not None, osu.flavor if osu else None, title, self.beatmap, keys_ok, tuple(issues),
                         self.keys)

    def track_map(self) -> bool:
        """Poll osu!'s window title (cheap) and switch to the map it names. True when the map just changed.

        osu! only shows the map in the title while it is being played, so a menu title ("osu!") says nothing
        and the current map is kept; the next title that names a map is looked up again. Call it every second or so
        in every phase: the map is often only known once the play has started, and a stale one would make the
        whole round be judged against the previous map.
        """
        osu = self.watcher.process
        if osu is None:
            return False
        title = self._title_source()
        if title == self._last_title:
            return False
        self._last_title = title
        if parse_window_title(title) is None:
            return False
        before = self.beatmap.path if self.beatmap is not None else None
        self.beatmap, self._manual_map = None, False
        self._find_map(title, osu.path)
        after = self.beatmap.path if self.beatmap is not None else None
        if after != before:
            self.live_report = None          # the clicks must be matched to the new map from scratch
            return True
        return False

    def _keys_readable(self) -> bool:
        if self._recorder_factory is not self._default_recorder:
            return True
        return bool(self.keys) and windows_key_source(self.keys) is not None

    def _find_map(self, title: str | None, exe: str | None) -> None:
        info = parse_window_title(title)
        songs = songs_dir_for(exe)
        if info is None or songs is None:
            return
        if self._index is None or self._index.songs_dir != songs:
            self._index = BeatmapIndex(songs, self.cache_path)
            self._index_scanned_for = None
        if self._index_scanned_for != title:   # rescan once per title (new maps appear while osu! runs)
            self._index.scan()
            self._index_scanned_for = title
        self.beatmap = find_beatmap(info, self._index)

    def set_beatmap_file(self, path: str | Path) -> bool:
        """The player chose a ``.osu`` by hand. False when it cannot be read."""
        beatmap = load_beatmap(path)
        if beatmap is None:
            return False
        self.beatmap, self._manual_map = beatmap, True
        return True

    # --------------------------------------------------------------- observe
    def start(self) -> bool:
        """Begin recording. False when there is no map yet."""
        if self.beatmap is None or not self.keys:
            return False
        self.recorder = self._recorder_factory(self.area)
        self.recorder.start()
        self.live_report = None
        self.phase = "observe"
        return True

    def live_view(self) -> LiveView | None:
        return self.recorder.live() if self.recorder is not None and self.phase == "observe" else None

    def analyze_live(self) -> PlayReport | None:
        """Judge the partial capture (blocking: the window calls it from a worker thread)."""
        if self.recorder is None or self.beatmap is None:
            return None
        report = self._analyze(self.beatmap, self.recorder.snapshot(), detail=False)
        self.live_report = report
        return report

    def searching(self) -> bool:
        """True until the clicks have been matched to the map well enough to trust the picture."""
        return self.live_report is None or self.live_report.alignment.quality < ALIGN_TRUST

    def should_auto_stop(self) -> bool:
        view = self.live_view()
        return view is not None and view.clicks >= AUTO_STOP_MIN_CLICKS and view.idle_s is not None \
            and view.idle_s >= AUTO_STOP_IDLE_S

    # ---------------------------------------------------------------- report
    def finish(self) -> RoundResult | None:
        """Stop recording, score the round and prepare the ONE proposal."""
        if self.recorder is None or self.beatmap is None:
            return None
        self.recorder.stop()
        capture = self.recorder.snapshot()
        hint = None
        if len(capture.clicks) < 3 and _travel(capture) >= NO_CLICKS_TRAVEL_SCREENS * max(self.display.height_px, 1):
            hint = self._keys_hint()
        return self._complete_round(capture, hint=hint)

    def abort(self) -> None:
        """Stop observing without making a round of it (e.g. to change the click keys)."""
        if self.recorder is not None:
            self.recorder.stop()
        self.recorder, self.live_report = None, None
        self.phase = "prepare"

    def _complete_round(self, capture: PlayCapture, from_replay: bool = False, hint: Reason | None = None
                        ) -> RoundResult:
        assert self.beatmap is not None
        report = self._full_analysis(capture)
        pending, self._pending = self._pending, None
        profile = self.profile
        self.state, report = opt.record_round(
            self.state, report, profile.tablet_area, profile.filters,
            change=pending.change if pending else (), kind=pending.kind if pending else "start",
            move=pending.move if pending else (0.0, 0.0), new_adapt=pending.adapt if pending else None,
            prefs=self.prefs, new_probe=pending.probe if pending else None)
        record = self.state.rounds[-1]
        if pending is not None and not record.accepted:
            self._pending = pending           # nothing could be measured: the next round still tells its effect
        self._note_smoothing_round(record, profile.filters)
        proposal = opt.propose(self.state, profile, report, self.tablet, self.policy, self.aspect_lock,
                               self.allow_filters, self.catalog)
        convergence = opt.convergence(self.state)
        if proposal.kind == "none" and proposal.verdict == opt.PLATEAU:
            convergence = Convergence(opt.PLATEAU, proposal.message or convergence.message,
                                      convergence.rounds, convergence.last_gain)
        this_map = self.beatmap.path
        map_changed = bool(self._round_maps) and self._round_maps[-1] != this_map
        self._round_maps.append(this_map)
        self.result = RoundResult(report, tuple(findings_mod.findings(report)) if report.trusted else
                                  (Reason("play.find.untrusted"),), proposal, convergence, self.marks(),
                                  from_replay=from_replay, hint=hint, map_changed=map_changed)
        self.phase = "report"
        return self.result

    # ---------------------------------------------------------------- replays
    def replay_dir(self) -> Path | None:
        """Where osu!stable keeps replays (``Replays`` next to ``Songs``), if it can be told."""
        osu = self.watcher.process
        songs = songs_dir_for(osu.path) if osu is not None else None
        folder = songs.parent / "Replays" if songs is not None else None
        return folder if folder is not None and folder.is_dir() else None

    def _read_replay(self, path: str | Path) -> ReplayOutcome | tuple[replay_mod.Replay, PlayReport, bool | None]:
        if self.beatmap is None:
            return ReplayOutcome(False, Reason("play.replay.no_map"))
        replay = replay_mod.load_replay(path)
        if replay is None:
            return ReplayOutcome(False, Reason("play.replay.unreadable"))
        capture = replay_mod.replay_to_capture(replay, self.area, self.display, self.tablet)
        return replay, self._full_analysis(capture), replay_mod.matches_beatmap(replay, self.beatmap)

    def round_from_replay(self, path: str | Path) -> ReplayOutcome:
        """Use the game's own replay as the round (exact cursor and keys; works without reading any key)."""
        if self.beatmap is None:
            return ReplayOutcome(False, Reason("play.replay.no_map"))
        replay = replay_mod.load_replay(path)
        if replay is None:
            return ReplayOutcome(False, Reason("play.replay.unreadable"))
        same = replay_mod.matches_beatmap(replay, self.beatmap)
        self._complete_round(replay_mod.replay_to_capture(replay, self.area, self.display, self.tablet),
                             from_replay=True)
        return ReplayOutcome(True, None if same is not False else Reason("play.replay.other_map"), replay, same)

    def verify_with_replay(self, path: str | Path) -> ReplayOutcome:
        """Compare what the coach inferred in the last live round with what the game recorded."""
        if self.result is None or self.result.from_replay:
            return ReplayOutcome(False, Reason("play.replay.nothing_to_check"))
        read = self._read_replay(path)
        if isinstance(read, ReplayOutcome):
            return read
        replay, real, same = read
        check = replay_mod.compare_inference(self.result.report, real, replay)
        if check is None:
            return ReplayOutcome(False, Reason("play.replay.no_overlap"), replay, same)
        self.result = RoundResult(self.result.report, self.result.findings, self.result.proposal,
                                  self.result.convergence, self.result.marks, check, False, self.result.hint)
        return ReplayOutcome(True, None if same is not False else Reason("play.replay.other_map"), replay, same, check)

    def marks(self) -> tuple[str, ...]:
        """One mark per round: start / better / same / worse / unclear (no numbers)."""
        out, previous = [], None
        for rec in self.state.rounds:
            if not rec.accepted or rec.cost is None:
                out.append("unclear")
                continue
            if previous is None:
                out.append("start")
            else:
                gain = (previous.cost - rec.cost) / previous.cost if previous.cost > 1e-9 else 0.0
                limit = opt._threshold(previous, rec)
                out.append("better" if gain > limit else "worse" if gain < -limit else "same")
            previous = rec
        return tuple(out)

    # --------------------------------------------------------------- changes
    def apply_proposal(self) -> CoachFeedback:
        """Send the proposed change to OTD (live, not saved); the next round will measure its effect."""
        proposal = self.result.proposal if self.result else None
        changes = proposal.changes if proposal else None
        if proposal is None or changes is None:
            return CoachFeedback(False, message=Reason("play.msg.nothing_to_apply"), sound="error")
        if changes.filters is not None and self._touched:
            changes = replace(changes, filters=self._keep_user_smoothing(changes.filters))
        if not self.aspect_lock and changes.tablet_area is not None and changes.lock_aspect_ratio is None:
            changes = replace(changes, lock_aspect_ratio=False)    # OTD must not snap the area back to the screen's ratio
        fb = self._send(changes)
        if not fb.ok:
            return fb
        # A change by hand since the report (e.g. smoothing taken out there) means the next round cannot be
        # attributed to this proposal alone.
        hand = self._pending.kind if self._pending is not None and self._pending.kind in ("manual", "smoothing") \
            else None
        self._pending = _Pending(hand or proposal.kind, proposal.move, proposal.reasons, proposal.new_adapt,
                                 None if hand else proposal.new_probe)
        self._last_applied = (proposal.kind, proposal.move)
        self.result = None
        self.phase = "prepare"
        return CoachFeedback(True, proposal.reasons, Reason("play.msg.applied"), "keep", live_only=True)

    def _send(self, changes: ProfileChanges) -> CoachFeedback:
        """One live change through the LiveTuner (bounded, undoable, the first one makes the backup)."""
        outcome = self.tuner.propose(changes)
        if not outcome.accepted:
            return CoachFeedback(False, outcome.reasons, Reason("play.msg.rejected"), "error")
        sent = outcome.sent
        if sent is not None and not sent.ok:
            return CoachFeedback(False, (Reason("play.msg.send_failed"),), Reason("play.msg.send_failed"), "error")
        return CoachFeedback(True, outcome.reasons, Reason("play.msg.applied"), "keep", live_only=True)

    def skip_proposal(self) -> CoachFeedback | None:
        """Do not apply this change; remember it so it is not proposed again straight away.

        Skipping a trial that would also put the previous trial back still puts that area back (a trial that
        did not help is never left on the tablet by skipping); the feedback then says so.
        """
        proposal = self.result.proposal if self.result else None
        feedback = None
        if proposal is not None and proposal.kind in ("shift", "scale", "filter"):
            self.state = opt.mark_undone(self.state, proposal.kind, proposal.move)
        elif proposal is not None and proposal.kind in ("probe", "probe_back"):
            self.state = opt.skip_probe(self.state, proposal.new_probe, back_only=proposal.kind == "probe_back")
            if proposal.undo_area is not None and proposal.undo_area != self.area:
                fb = self._send(self._area_changes(proposal.undo_area))
                if fb.ok:
                    feedback = CoachFeedback(True, (), Reason("play.probe.skip_back"), "undo", live_only=True)
        self.skipped += 1
        self._pending = None
        self.result = None
        self.phase = "prepare"
        return feedback

    def undo_last(self) -> CoachFeedback:
        """Take back the last applied change (and do not propose it again)."""
        outcome = self.tuner.undo()
        if not outcome.accepted:
            return CoachFeedback(False, outcome.reasons, Reason("play.msg.nothing_to_undo"), "error")
        if self._last_applied is not None:
            self.state = opt.mark_undone(self.state, *self._last_applied)
        if self._pending is not None and self._pending.kind == "probe":
            self.state = opt.skip_probe(self.state, self._pending.probe)
        self._pending, self._last_applied = None, None
        self._sync_trials()
        return CoachFeedback(True, outcome.reasons, Reason("play.msg.undone"), "undo", live_only=True)

    def restore_original(self) -> CoachFeedback:
        """Back to the settings the coach started with; the rounds start over."""
        outcome = self.tuner.revert_all()
        self.state, self._pending, self._last_applied, self.result = opt.new_state(), None, None, None
        self.trials.clear()
        self._touched.clear()
        self.phase = "prepare"
        return CoachFeedback(outcome.accepted, outcome.reasons, Reason("play.msg.restored"), "undo", live_only=True)

    # ------------------------------------------------------- changes by hand
    def _area_changes(self, area: Rect) -> ProfileChanges:
        return ProfileChanges(tablet_area=area, lock_aspect_ratio=None if self.aspect_lock else False)

    def _hand_change(self, kind: str) -> None:
        """The player changed something themselves: the next round is not attributed to a proposal."""
        pending = self._pending
        self._pending = _Pending(kind, (0.0, 0.0), (), pending.adapt if pending else None)
        self._last_applied = None

    def manual_area(self, action: str) -> CoachFeedback:
        """One press of "Ajustar el área a mano", in the player's view (left = left of the screen, any rotation).

        ``left/right/up/down`` move it ``MANUAL_MOVE_MM``; ``smaller/bigger`` scale it ``MANUAL_SIZE_STEP``
        keeping its proportion; ``narrower/wider/shorter/taller`` change one axis only (free proportion only).
        Live and undoable like every change, bounded to the tablet, not saved until Keep.
        """
        if self.phase == "observe" or (action not in MANUAL_MOVES and action not in MANUAL_SIZES):
            return CoachFeedback(False, message=Reason("play.manual.not_now"), sound="error")
        if action in FREE_ONLY and self.aspect_lock:
            return CoachFeedback(False, message=Reason("play.manual.needs_free"), sound="error")
        area = self.area
        if action in MANUAL_MOVES:
            ux, uy = MANUAL_MOVES[action]
            new, reasons = areafit.shift_area(area, ux * MANUAL_MOVE_MM, uy * MANUAL_MOVE_MM, self.tablet)
        else:
            gx, gy = MANUAL_SIZES[action]
            new, reasons = areafit.scale_area_axes(area, 1.0 + gx * MANUAL_SIZE_STEP, 1.0 + gy * MANUAL_SIZE_STEP,
                                                   self.tablet, lock_aspect=action not in FREE_ONLY)
        if new == area:
            return CoachFeedback(False, tuple(reasons), Reason("play.manual.no_room"), "error")
        fb = self._send(self._area_changes(new))
        if not fb.ok:
            return fb
        self._hand_change("manual")
        return CoachFeedback(True, tuple(reasons), Reason("play.manual.applied"), "keep", live_only=True)

    # ------------------------------------------------------------- smoothing
    def installed_filters(self) -> set[str]:
        """Filter paths this OTD has: the session's plugin scan, the profile, and the other filters of the same
        plugins (one plugin brings both Radial Follow variants, both Hawku filters...)."""
        paths = set(self.policy.installed) | {fc.path for fc in self._original_filters} \
            | {fc.path for fc in self.profile.filters}
        for known in KNOWN_PLUGINS:
            if paths & set(known.filters):
                paths |= set(known.filters)
        return paths

    def _rate_and_scale(self) -> tuple[float, float]:
        rate = self.tablet.max_report_rate_hz if self.tablet is not None and self.tablet.max_report_rate_hz > 0 \
            else 133.0
        return rate, self.area.width / max(self.display.width_px, 1)

    def smoothing_options(self) -> list[SmoothOption]:
        """The smoothing card: every offered catalog filter, installed ones first."""
        live = self.profile.filters
        installed = self.installed_filters()
        rate, mm_per_px = self._rate_and_scale()
        out = []
        for path in smoothing.offered(self.catalog):
            spec = self.catalog[path]
            idx = smoothing.find(live, path)
            fc = live[idx] if idx is not None else None
            on = fc is not None and fc.enabled
            orig_idx = smoothing.find(self._original_filters, path)
            was_on = orig_idx is not None and self._original_filters[orig_idx].enabled and path not in self.trials
            values = fc.settings if on else smoothing.start_settings(spec, fc.settings if fc else None)[0]
            trial = self.trials.get(path)
            out.append(SmoothOption(path, spec.display_name, smoothing.text_id(path), path in installed, on,
                                    was_on, trial is not None, bool(trial and trial.decided),
                                    smoothing.delay_ms(path, values, rate, mm_per_px)))
        out.sort(key=lambda o: not o.installed)
        return out

    def try_smoothing(self, path: str) -> CoachFeedback:
        """Switch an installed smoothing filter on to try it (gentle values; live, undoable, not saved)."""
        spec = self.catalog.get(path)
        if self.phase == "observe" or spec is None or path not in smoothing.offered(self.catalog):
            return CoachFeedback(False, message=Reason("play.manual.not_now"), sound="error")
        if path not in self.installed_filters():
            return CoachFeedback(False, message=Reason("play.smooth.not_installed", {"name": spec.display_name}),
                                 sound="error")
        live = self.profile.filters
        idx = smoothing.find(live, path)
        if idx is not None and live[idx].enabled:
            return CoachFeedback(False, message=Reason("play.smooth.already_on", {"name": spec.display_name}),
                                 sound="error")
        filters, previous, own = smoothing.enable(live, path, self.catalog)
        fb = self._send(ProfileChanges(filters=filters))
        if not fb.ok:
            return fb
        done = opt.accepted_rounds(self.state)
        self.trials[path] = SmoothTrial(path, previous, done[-1].index if done else None)
        self._touched.add(path)
        self._hand_change("smoothing")
        notes = (Reason("play.smooth.own_values"),) if own else ()
        return CoachFeedback(True, notes, Reason("play.smooth.on", {"name": spec.display_name}), "keep",
                             live_only=True)

    def stop_smoothing(self, path: str) -> CoachFeedback:
        """"Quitarlo": switch the trial off again, with the values it had before (or take it out of the chain)."""
        trial = self.trials.get(path)
        if self.phase == "observe" or trial is None:
            return CoachFeedback(False, message=Reason("play.manual.not_now"), sound="error")
        fb = self._send(ProfileChanges(filters=smoothing.disable(self.profile.filters, path, trial.previous)))
        if not fb.ok:
            return fb
        del self.trials[path]
        self._touched.add(path)
        self._hand_change("smoothing")
        name = self.catalog[path].display_name if path in self.catalog else path
        return CoachFeedback(True, (), Reason("play.smooth.off", {"name": name}), "undo", live_only=True)

    def keep_smoothing(self, path: str) -> CoachFeedback:
        """"Dejarlo": the trial stays on; it is saved with the other settings by "Guardar estos ajustes"."""
        trial = self.trials.get(path)
        if trial is None:
            return CoachFeedback(False, message=Reason("play.manual.not_now"), sound="error")
        trial.decided = True
        name = self.catalog[path].display_name if path in self.catalog else path
        return CoachFeedback(True, (), Reason("play.smooth.kept", {"name": name}), "keep", live_only=True)

    def smoothing_verdicts(self) -> list[SmoothVerdict]:
        """For each undecided trial already played with: better / same / worse than without it (or unclear)."""
        out = []
        for trial in self.trials.values():
            if trial.decided or trial.result_round is None:
                continue
            name = self.catalog[trial.path].display_name if trial.path in self.catalog else trial.path
            rounds = self.state.rounds
            base = rounds[trial.base_round] if trial.base_round is not None and trial.base_round < len(rounds) \
                else None
            res = rounds[trial.result_round] if trial.result_round < len(rounds) else None
            if base is None or res is None or base.cost is None or res.cost is None:
                out.append(SmoothVerdict(trial.path, name, "unclear", no_base=base is None))
                continue
            gain = (base.cost - res.cost) / base.cost if base.cost > 1e-9 else 0.0
            limit = opt._threshold(base, res)
            out.append(SmoothVerdict(trial.path, name, "better" if gain > limit else "worse" if gain < -limit
                                     else "same"))
        return out

    def _note_smoothing_round(self, record, filters: Sequence[FilterConfig]) -> None:
        """A round was played: the first accepted one with a trial switched on is what that trial is judged by."""
        if not record.accepted:
            return
        on = {fc.path for fc in filters if fc.enabled}
        for trial in self.trials.values():
            if trial.result_round is None and not trial.decided and trial.path in on:
                trial.result_round = record.index

    def _sync_trials(self) -> None:
        """After an undo: a trial whose filter is off again is over."""
        on = {fc.path for fc in self.profile.filters if fc.enabled}
        for path in [p for p in self.trials if p not in on]:
            del self.trials[path]

    def _keep_user_smoothing(self, filters: Sequence[FilterConfig]) -> tuple[FilterConfig, ...]:
        """A proposal never switches smoothing the player decided on (on or off): those stores stay as they are
        live now (e.g. "back to the best round" keeps the smoothing the player is trying)."""
        out = list(filters)
        live = self.profile.filters
        for path in self._touched:
            live_idx, idx = smoothing.find(live, path), smoothing.find(out, path)
            if live_idx is None:
                if idx is not None:
                    del out[idx]
            elif idx is None:
                out = list(adapt.insert_in_chain(out, live[live_idx]))
            else:
                out[idx] = live[live_idx]
        return tuple(out)

    def keep(self) -> CoachFeedback:
        """Write the live settings to settings.json (after the backup). Returns the outcome."""
        result = self.tuner.keep()
        self._kept_once = self._kept_once or result.ok
        return CoachFeedback(result.ok, (), Reason("play.msg.kept" if result.ok else result.message_key),
                             "keep" if result.ok else "error")

    def progress_summary(self) -> dict | None:
        """``{"rounds", "gain_pct"}``: rounds played and how much the cost fell from the first to the best one."""
        done = opt.accepted_rounds(self.state)
        if not self.state.rounds:
            return None
        gain = None
        if len(done) >= 2 and done[0].cost:
            gain = (done[0].cost - min(r.cost for r in done)) / done[0].cost * 100.0
        return {"rounds": len(self.state.rounds), "gain_pct": gain}

    @property
    def dirty(self) -> bool:
        return self.tuner.dirty

    def stop_recording(self) -> None:
        if self.recorder is not None:
            self.recorder.stop()


