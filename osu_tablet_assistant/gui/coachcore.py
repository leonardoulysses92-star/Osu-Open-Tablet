"""Play Coach logic without Tk: readiness, rounds, applying/undoing/keeping changes.

:class:`CoachCore` sits on a :class:`~osu_tablet_assistant.otd.livetune.LiveTuner` (live sends, undo,
Keep) and on ``engine.optimizer`` (cost, stop rule, one proposal per round). Everything OS-facing (the
recorder, the osu! window title, the process list) is injected, so it is tested with fakes.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ..engine import adapt, findings as findings_mod, optimizer as opt, replay as replay_mod
from ..engine.beatmap import BeatmapIndex, find_beatmap, load_beatmap, parse_window_title
from ..engine.playobserve import ALIGN_TRUST, analyze_play
from ..engine.playtypes import DEFAULT_DETAIL, Beatmap, Convergence, PlayCapture, PlayReport, grid_for
from ..keys import DEFAULT_CLICK_KEYS, PEN_TIP_KEY, display_list, normalize_keys
from ..knowledge.filters import FilterSpec, load_catalog
from ..models import DisplaySpec, Reason, Rect, TabletSpec, UserPreferences
from ..otd.livetune import LiveTuner, OsuWatcher
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
    """Translate a Reason; zone, place, size and direction parameters become words, adapt's axis/strength too."""
    params = dict(reason.params)
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
            prefs=self.prefs)
        proposal = opt.propose(self.state, profile, report, self.tablet, self.policy, self.aspect_lock,
                               self.allow_filters, self.catalog)
        convergence = opt.convergence(self.state)
        if proposal.kind == "none" and proposal.verdict == opt.PLATEAU:
            convergence = Convergence(opt.PLATEAU, proposal.message or convergence.message,
                                      convergence.rounds, convergence.last_gain)
        self.result = RoundResult(report, tuple(findings_mod.findings(report)) if report.trusted else
                                  (Reason("play.find.untrusted"),), proposal, convergence, self.marks(),
                                  from_replay=from_replay, hint=hint)
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
        outcome = self.tuner.propose(changes)
        if not outcome.accepted:
            return CoachFeedback(False, outcome.reasons, Reason("play.msg.rejected"), "error")
        sent = outcome.sent
        if sent is not None and not sent.ok:
            return CoachFeedback(False, (Reason("play.msg.send_failed"),), Reason("play.msg.send_failed"), "error")
        self._pending = _Pending(proposal.kind, proposal.move, proposal.reasons, proposal.new_adapt)
        self._last_applied = (proposal.kind, proposal.move)
        self.result = None
        self.phase = "prepare"
        return CoachFeedback(True, proposal.reasons, Reason("play.msg.applied"), "keep", live_only=True)

    def skip_proposal(self) -> None:
        """Do not apply this change; remember it so it is not proposed again straight away."""
        proposal = self.result.proposal if self.result else None
        if proposal is not None and proposal.kind in ("shift", "scale", "filter"):
            self.state = opt.mark_undone(self.state, proposal.kind, proposal.move)
        self.skipped += 1
        self._pending = None
        self.result = None
        self.phase = "prepare"

    def undo_last(self) -> CoachFeedback:
        """Take back the last applied change (and do not propose it again)."""
        outcome = self.tuner.undo()
        if not outcome.accepted:
            return CoachFeedback(False, outcome.reasons, Reason("play.msg.nothing_to_undo"), "error")
        if self._last_applied is not None:
            self.state = opt.mark_undone(self.state, *self._last_applied)
        self._pending, self._last_applied = None, None
        return CoachFeedback(True, outcome.reasons, Reason("play.msg.undone"), "undo", live_only=True)

    def restore_original(self) -> CoachFeedback:
        """Back to the settings the coach started with; the rounds start over."""
        outcome = self.tuner.revert_all()
        self.state, self._pending, self._last_applied, self.result = opt.new_state(), None, None, None
        self.phase = "prepare"
        return CoachFeedback(outcome.accepted, outcome.reasons, Reason("play.msg.restored"), "undo", live_only=True)

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


