# Architecture

**osu-open-tablet** is a desktop assistant that guides an osu! player through
configuring [OpenTabletDriver](https://opentabletdriver.net) (OTD) and finds
good settings *for that player's tablet and hand*: area, aspect ratio and
filter chain (Hawku smoothing, Devocub antichatter, Temporal Resampler,
RadialFollow, Saturn, noise reduction...). It works for any tablet OTD supports,
including tablets the project has never seen (manual entry).

## Hard constraints

* Python 3.10+, **standard library only** (tkinter for the GUI). No pip installs
  at runtime, so anyone can run it with a plain Python install.
* Primary target Windows 10/11; Linux/macOS best-effort (path detection must not crash).
* Spanish and English UI. No user-visible string is hard-coded outside `locales/`.
* **Never modify the user's real OTD files during development.** Read-only
  access to `%LOCALAPPDATA%\OpenTabletDriver` is allowed for format discovery.
  Never kill, restart or send state-changing commands to the user's running OTD
  daemon. Tests use synthetic fixtures, not the real `settings.json`.
* At runtime the program always writes a **backup** before touching anything and
  asks for confirmation before applying.
* Do not claim a "scientific optimum". Output is a measured, explainable starting
  point; every recommendation carries `Reason`s the user can read.

## Data flow

```
startup: appconfig.load_config (%APPDATA%\OsuOpenTablet\config.json)
         knowledge.updater.activate_cache (validated cached data, else the packaged JSON)
         [optional, user-enabled or button] updater.update -> manifest + sha256 -> cache -> activate

detect OTD (installed + portable locations, OTD version notice)
     -> pick tablet -> display -> questionnaire (+ plugin policy)          -> UserPreferences
     -> [slot] starting preset  (engine.community.starting_options over knowledge.presets)
     -> baseline measurements (gui.live_tests -> engine.metrics)
     -> engine.recommend.generate_candidates(..., community=select_presets(...))
          standard options (or tune-less/tune-more with tune_only) + community:<id> + keep-current
     -> apply candidate -> measure -> rank / refine -> best
     -> result: comparison with closest presets (community.compare_with_best),
                plugin suggestions (allow_new), "Companion / live tuning" hook
     -> save a FULL OTD settings file to the user's folder (or OTD Presets/), backup first,
        dated copy in <folder>/Backup before overwriting; optional apply; report
```

Community data (`knowledge/community_presets.json`, `knowledge/filters.json`) is
data only. `tools/refresh_data.py` copies both into the repo-level `data/` with a
`manifest.json` (version/date from the presets file, SHA-256 and size per file);
that folder is what the in-app updater downloads from raw GitHub
(`leonardoulysses92-star/Osu-Open-Tablet`, branch `main`). A test fails when `data/` is stale.
`.gitattributes` keeps those bytes exact (`data/** -text`).

## Layout and ownership

```
osu-open-tablet/
  osu_tablet_assistant/
    models.py                   shared contracts (FROZEN, integrator only)
    __main__.py                 entry point: main()                          [gui]
    otd/                        everything that touches OTD                  [otd]
      __init__.py
      settings_io.py            locate, load, parse, patch, backup, preset
      plugins.py                installed plugin discovery
      controller.py             apply settings to a running OTD (Console/RPC)
    appconfig.py                the assistant's own config (atomic, corruption-tolerant)
    knowledge/
      filters.json + filters.py filter catalog (paths, properties, ranges)   [engine]
      tablets.py + tablets_builtin.json   tablet database                    [gui]
      community_presets.json + presets.py  curated community setups + strict validator
      updater.py                manifest/sha256 data bundle: fetch, validate, cache, activate
    engine/
      area.py                   area / aspect-ratio recommendation           [engine]
      recommend.py              candidates, scoring, ranking, refinement     [engine]
      community.py              presets -> candidates, area adaptation, comparison, suggestions
      metrics.py                pure functions: samples -> MetricsReport     [tests]
      judge.py                  repeated runs -> median, grade in words, reliability, ties
      playtypes.py beatmap.py areafit.py playobserve.py optimizer.py findings.py
                                Play Coach engine, see docs/PLAY_COACH.md
    gui/
      app.py wizard.py session.py widgets.py i18n.py                         [gui]
      live_tests.py             tkinter pen tests that yield samples         [tests]
      playwatch.py coachcore.py coach.py tabletmap.py mapgeometry.py palette.py
                                Play Coach (recorder, logic without Tk, window, tablet map)
    locales/{es,en}/*.json      i18n, one file per owner (see below)
  tests/                        unittest, one test module per source module
  docs/
```

Workstreams: **otd**, **engine**, **tests** (live tests + metrics), **gui**.
Each owns only its files. Cross-module needs go through the signatures below;
if a signature is not enough, keep your side compatible and report the gap.

### i18n

`gui/i18n.py` exposes `t(key: str, **params) -> str` and `set_language("es"|"en")`.
It loads **every** `locales/<lang>/*.json` and merges them (flat `{"key": "text"}`,
`str.format` placeholders). Owners use separate files so nobody edits the same one:

* gui: `core.json` · engine: `engine.json` · tests: `tests.json` · otd: `otd.json`

Key naming: `<area>.<thing>`, e.g. `engine.reason.area_wrist`, `tests.stillness.title`.
Every key must exist in both `es` and `en`. A unit test must fail if a key is
missing in either language.

### Module contracts

**otd** (`osu_tablet_assistant.otd`)

```python
# settings_io.py
def find_config_dir() -> Path | None            # dir holding settings.json, Plugins/, Presets/
def find_install_dir() -> Path | None           # dir with OpenTabletDriver.Console.exe / Daemon.exe
def load_settings(config_dir: Path) -> dict     # raw settings.json
def list_profiles(settings: dict) -> list[ProfileInfo]
def apply_changes(settings: dict, tablet_name: str, changes: ProfileChanges) -> dict  # pure, deep copy
def backup_settings(config_dir: Path) -> Path   # copy into <config>/Backup/ with timestamp
def save_preset(config_dir: Path, name: str, settings: dict, overwrite=False,
                destination: Path | None = None) -> Path
    # into <config>/Presets/ or <destination>/; overwriting first copies the old file
    # to <folder>/Backup/<name>-<stamp>.json (backup_file)
def write_settings(config_dir: Path, settings: dict) -> None           # atomic write
def is_full_settings(doc) -> bool               # Revision, Profiles, LockUsableArea*, Tools
def version_notice(settings) -> (key, params) | None   # info when Revision < 0.6.8.1, never forced
def config_locations(system=None, env=None, home=None, extra=()) -> list[ConfigLocation]
    # %LOCALAPPDATA%\OpenTabletDriver + portable folders (Daemon.exe or convert_to_portable.bat
    # next to a settings document or userdata/); read-only probing
# plugins.py
def list_installed_plugins(config_dir: Path) -> list[PluginInfo]
def available_filter_paths(config_dir: Path) -> set[str]   # built-in + plugin-provided
# controller.py
class OtdController:
    def __init__(self, install_dir: Path | None, config_dir: Path | None): ...
    def is_daemon_running(self) -> bool
    def apply(self, settings: dict) -> ApplyResult   # tries Console, then RPC, then reports preset-only
```

**engine** (`osu_tablet_assistant.engine`, `knowledge.filters`)

```python
# knowledge/filters.py
@dataclass(frozen=True) class FilterParam: name, kind("float"|"int"|"bool"|"enum"), default, min, max, step, unit
@dataclass(frozen=True) class FilterSpec: path, display_name, plugin_name, params, added_latency_note,
                                          best_for(tuple[str,...]), summary_key, sources(tuple[str,...]), confidence
def load_catalog() -> dict[str, FilterSpec]     # keyed by OTD Path
# engine/area.py
def recommend_area(tablet: TabletSpec, display: DisplaySpec, prefs: UserPreferences) -> tuple[Rect, tuple[Reason, ...]]
# engine/recommend.py
def generate_candidates(tablet, display, prefs, available_filters: set[str],
                        baseline: list[MetricsReport] | None, current: ProfileInfo | None,
                        community: Sequence[CommunityPreset] | None = None) -> list[Candidate]
    # honours prefs.plugin_policy (models.PluginPolicy: tune_only | installed | allow_new);
    # filters with auto_recommend=False are never picked; Frequency always stays at 1000 Hz
# engine/community.py
def select_presets(presets, tablet, area, current, policy, available, catalog, limit=3, include=()) -> list
def adapt_preset(preset, area, catalog) -> (filters, reasons)      # HEURISTIC area scaling, see below
def compare_with_best(best, presets, tablet, current, catalog, limit=3) -> list[PresetComparison]
def plugin_suggestions(presets, tablet, area, current, available, catalog, wanted=()) -> list
# knowledge/presets.py
def parse_presets(doc, catalog=None) -> PresetBundle   # strict; bad entries skipped with a warning
def load_presets() -> PresetBundle                     # active data, never raises
# knowledge/updater.py
def update(base_url, cache_dir, fetcher=urllib_fetch) -> UpdateResult   # never raises
def activate_cache(cache_dir) -> Bundle | None
def score_reports(reports: Sequence[MetricsReport], prefs: UserPreferences, user_rating: int | None) -> float
def rank(results: list[CandidateResult], prefs: UserPreferences) -> list[CandidateResult]  # sets .score, best first
def refine(best: Candidate, results: list[CandidateResult], prefs: UserPreferences,
           available_filters: set[str]) -> Candidate | None   # next small tuning step, None when converged
```

**tests** (`engine.metrics`, `gui.live_tests`)

```python
# engine/metrics.py  (pure, no tkinter)
def effective_rate(samples: Sequence[PenSample]) -> float | None
def stillness_report(samples, mm_per_px: float) -> MetricsReport
def tracking_report(samples, target_path: Sequence[PenSample], mm_per_px: float) -> MetricsReport
def aim_report(activations: Sequence[tuple[PenSample, tuple[float, float]]], mm_per_px: float, *,
               appear_times=None, samples=None, radius_px=None, center_fraction=0.35,
               settle_s=0.25, overshoot_s=0.12) -> MetricsReport   # all keywords optional
def segment_circle_entry(p0, p1, center, radius) -> float | None   # first touch, fraction of the segment
# gui/live_tests.py
def run_test(parent, kind: str, mm_per_px: float, display: DisplaySpec | None,
             on_done: Callable[[MetricsReport], None], on_cancel: Callable[[], None] | None = None,
             *, activation: str = "touch", center_fraction: float = 0.35) -> None
# kinds: "rate" | "stillness" | "tracking" | "aim"; samples come from OS cursor position
# (OTD moves the cursor): use a high-frequency poll on Windows (ctypes GetCursorPos) and Tk
# <Motion> events as fallback. `display` is the monitor the tablet is mapped to (desktop px,
# center-based like OTD): the test window must cover exactly that monitor. None = primary.
# mm_per_px = tablet_area.width_mm / display.width_px (computed by the caller).
# activation (aim only): "touch" | "center" | "click"
```

**Aim test: activation without clicking.** Many players keep the pen on the
surface, others hover, so a click-based test measures the wrong thing. By
default (`activation="touch"`) a target fires the moment the cursor touches its
circle; `"center"` fires on a center zone of `center_fraction * radius`;
`"click"` is the legacy mouse-button mode. Mouse buttons are ignored in
touch/center modes. `AimSession.observe` is fed every `PenSampler` sample live
(`PenSampler.snapshot`); consecutive samples form a segment and the activation
is its first intersection with the circle, time interpolated along it, so a
flick that crosses the circle between two reports counts at the right moment.
Guards: no target fires in its first 100 ms (a cursor still on it then fires
when the debounce ends); a target that spawns with the cursor on it must be
left and re-entered; a single step longer than half the shorter display side
is a teleport (pen re-entering range), only its end point is tested. Targets
are planned at run start at least `max(20 % of the shorter side, 3 radii)`
from the previous one, the first one from the current cursor. After the last
activation the window keeps sampling `AIM_SETTLE_S` before reporting.

**Aim metrics** (`aim_report`, mm and ms; `appear` = when the target showed up,
which is the previous activation):

* `aim_time_ms_mean`: mean time to activation (`activation.t - appear`);
  extras `aim_time_ms_median`, `aim_time_ms_p95`.
* `aim_error_mean_mm`: mean closest approach, the smallest cursor-to-center
  distance from activation until the cursor leaves the circle or 250 ms pass
  (lower is better; `recommend` keeps ranking on it). With hover activation the
  distance at the activation itself is ~1 radius by construction
  (`activation_error_mean_mm`, kept as an extra). Without trajectory samples it
  falls back to the activation point, i.e. the old click error.
* `center_time_ms_mean` / `_median`, `center_reach_rate`: appear -> first entry
  into the center zone (until 250 ms after activation).
* `overshoot_mean_mm` / `_max_mm`: largest excursion past the center along the
  approach axis (cursor at appearance -> center) within 120 ms of activation,
  short enough to exclude the move to the next target.
* `settle_rms_mm`: exact time-weighted RMS distance to the center over the
  closest-approach window.
* `path_efficiency_mean`: straight distance / travelled distance, appearance
  position -> activation point.
* `bias_x_mm` / `bias_y_mm`: mean offset of the closest-approach point.

Integration notes (verified when wiring the modules together):

* `mm_per_px` uses the areas OTD applies *during* the test: the current profile's
  tablet area and its own display area for a baseline, the candidate's for a candidate.
* Coordinates: `DisplaySpec` (and the test window / `GetCursorPos`) use OS desktop
  px with the primary monitor at 0,0. OTD measures its display area from the
  top-left of the whole virtual desktop (a real 0.6.7 setup with a monitor left of
  the primary stores the primary's center as X = 960 + 1080). `Session.otd_display()`
  shifts the display by that origin before anything is written into OTD.
* `Frequency` is always left at the catalog default (1000 Hz). The former
  "Saturn Multifilter Frequency = report rate" rule was dropped after the
  2026-10-30 audit (MultifilterCore compares it with the *update* interval; no
  Saturn doc asks for the report rate).
* Filter order: in OTD 0.6 `PipelinePosition.Raw` is an alias of PreTransform,
  so inside the pre-transform group filters run in the order of the profile's
  `Filters` list. New chains follow `recommend.RESEARCH_ORDER` (noise and
  reconstruction, then smoothing, then resampling, then screen-space); a user's
  chain is never re-sorted by stage. `recommend.CHAIN_ORDER` is the deprecated
  pre-audit order kept only because `engine.adapt` (companion mode) imports it.
* Community area adaptation (HEURISTIC): tablet-space Radial Follow
  `OuterRadius` scales with the area width ratio and `InnerRadius` with the
  height ratio (the Mixmg calculator computes 0.00894 x width and 0.0065 x
  height); `SmoothingCoefficient` scales only for calculator-signature entries
  (inner == coefficient); knee/leak, px and raw-unit values are kept; within 5 %
  nothing changes; unknown source area = values as published. Prediction is
  forced off, Frequency reset, values clamped to catalog bounds; every change
  is a Reason.
* Plugin policy: `tune_only` keeps the current chain (order, on/off) and moves
  one knob (`tune-less` / `tune-more`, each change old -> new); `installed`
  uses detected plugins; `allow_new` adds `PluginSuggestion`s on the result
  screen (name, reason, homepage, install hint). The program never installs
  plugins and never builds an unmeasurable candidate.
* osu! policy (2026-10-14, forum topic 2254092): prediction plugins and
  non-smoothing filters have `auto_recommend: false` and a `policy_note`.
* Extension points for the companion mode: `wizard.COMPANION_LAUNCHER`
  (callable taking the `ResultsView`; shows a "Companion / live tuning" button),
  `wizard.RESULT_SECTIONS` / `ResultsView.add_section`.
* A rotated current area (e.g. 180 deg, left-handed) keeps its rotation.
* Per candidate the wizard runs `stillness`, `tracking` and `aim` while that
  candidate is live in OTD (jitter is part of the score).
* Repeating a test never overwrites it: `Session.record_report` appends to `CandidateResult.runs` and
  `CandidateResult.reports` is the aggregate (`engine.judge.aggregate_by_kind`: field-wise median, the most
  extreme run dropped from 4 runs). `recommend.rank(..., required_kinds)` flags a result that lacks a kind as
  `complete=False` and always ranks it after the complete ones; `judge.summarize` adds the grade
  (Excellent/Good/Fair/Weak from the objective score, thresholds in `judge.GRADE_*`), the reliability and the
  tie group (scores within the noise of the best are all starred). Noise sources: the aim targets used an
  unseeded RNG (fixed now: `live_tests.TEST_SEEDS`, one seed per test kind), the first aims of a run were
  slower (the first 2 are played but not scored), and 20 aim targets give a noisy mean (not changed: human
  variability, fatigue and order effects between options cannot be removed, only averaged by repeating).
* Welcome paths (`Session.goal`: guided / live / coach). Live and coach end at the display step, where
  "Next" opens the tool through `wizard.COMPANION_LAUNCHER` / `wizard.COACH_LAUNCHER` (same extension
  points as the result screen). `AppConfig.advanced_ui` (header switch) shows or hides the technical cards.

**gui** (`osu_tablet_assistant.gui`, `knowledge.tablets`)

```python
# knowledge/tablets.py
def list_known_tablets(install_dir: Path | None) -> list[TabletSpec]   # OTD config if present + built-in
def custom_tablet(name, width_mm, height_mm, report_rate_hz, ...) -> TabletSpec
# __main__.py
def main(argv: list[str] | None = None) -> int
```

## Definition of done per module

* Type-hinted, documented with short docstrings, no dead code, no third-party deps.
* `python -m unittest discover -s tests` passes on Windows with Python 3.12.
* Own tests included, covering happy path and malformed input.
* Report back: files created, public API, known gaps, anything in the contracts that did not fit.
