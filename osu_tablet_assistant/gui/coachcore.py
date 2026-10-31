"""Play Coach logic without Tk: readiness, rounds, applying/undoing/keeping changes.

:class:`CoachCore` sits on a :class:`~osu_tablet_assistant.otd.livetune.LiveTuner` (live sends, undo,
Keep) and on ``engine.optimizer`` (cost, stop rule, one proposal per round). Everything OS-facing (the
recorder, the osu! window title, the process list) is injected, so it is tested with fakes.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ..engine import adapt, findings as findings_mod, optimizer as opt
from ..engine.beatmap import BeatmapIndex, find_beatmap, load_beatmap, parse_window_title
from ..engine.playobserve import ALIGN_TRUST, analyze_play
from ..engine.playtypes import Beatmap, Convergence, PlayReport
from ..knowledge.filters import FilterSpec, load_catalog
from ..models import DisplaySpec, Reason, Rect, TabletSpec, UserPreferences
from ..otd.livetune import LiveTuner, OsuWatcher
from .i18n import t as default_t
from .pen_sampler import PenSampler
from .playwatch import DEFAULT_CLICK_KEYS, LiveView, PlayRecorder, osu_window_title, songs_dir_for, windows_key_source

ANALYSIS_EVERY_S = 1.5
AUTO_STOP_IDLE_S = 8.0
AUTO_STOP_MIN_CLICKS = 20
PHASES = ("prepare", "observe", "report")
Translate = Callable[..., str]


def say(reason: Reason, translate: Translate = default_t) -> str:
    """Translate a Reason; zone and direction parameters become words, adapt's axis/strength too."""
    params = dict(reason.params)
    if "zone" in params:
        params["zone"] = translate(f"play.zone.{params['zone']}")
    if "dir" in params:
        params["dir"] = translate(f"play.dir.{params['dir']}")
    if reason.key.startswith("adapt.") and "axis" in params:
        params["axis"] = translate(f"adapt.axis.{params['axis']}")
    if "strength" in params:
        params["strength"] = translate(f"passive.strength.{params['strength']}")
    return translate(reason.key, **params)


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

    @property
    def can_start(self) -> bool:
        return self.beatmap is not None and self.keys_readable


@dataclass(frozen=True)
class RoundResult:
    """Everything the report screen needs for one finished round."""
    report: PlayReport
    findings: tuple[Reason, ...]
    proposal: opt.RoundProposal
    convergence: Convergence
    marks: tuple[str, ...]


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
                 analyze: Callable[..., PlayReport] = analyze_play,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.tuner, self.tablet, self.display = tuner, tablet, display
        self.keys = tuple(keys)
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
        if not keys_ok:
            issues.append(Reason("play.prep.keys_unreadable"))
        return Readiness(osu is not None, osu.flavor if osu else None, title, self.beatmap, keys_ok, tuple(issues))

    def _keys_readable(self) -> bool:
        return windows_key_source(self.keys) is not None or self._recorder_factory is not self._default_recorder

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
        if self.beatmap is None:
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
        report = self._analyze(self.beatmap, self.recorder.snapshot())
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
                                  (Reason("play.find.untrusted"),), proposal, convergence, self.marks())
        self.phase = "report"
        return self.result

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

    @property
    def dirty(self) -> bool:
        return self.tuner.dirty

    def stop_recording(self) -> None:
        if self.recorder is not None:
            self.recorder.stop()


