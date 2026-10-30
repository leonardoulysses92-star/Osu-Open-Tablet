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
detect OTD  ->  pick tablet (OTD profile / built-in / custom)  ->  display  ->  questionnaire
     |                                                                              |
 otd.settings_io                                                              UserPreferences
     v                                                                              v
 baseline measurements (gui.live_tests -> engine.metrics)  ->  engine.recommend.generate_candidates
     v                                                                              v
 apply candidate (otd.apply) -> measure again -> engine.recommend.rank / refine  ->  best profile
     v
 export preset to OTD Presets/ + optional apply + human-readable report
```

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
    knowledge/
      filters.json + filters.py filter catalog (paths, properties, ranges)   [engine]
      tablets.py + tablets_builtin.json   tablet database                    [gui]
    engine/
      area.py                   area / aspect-ratio recommendation           [engine]
      recommend.py              candidates, scoring, ranking, refinement     [engine]
      metrics.py                pure functions: samples -> MetricsReport     [tests]
    gui/
      app.py wizard.py session.py widgets.py i18n.py                         [gui]
      live_tests.py             tkinter pen tests that yield samples         [tests]
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
def save_preset(config_dir: Path, name: str, settings: dict) -> Path   # into <config>/Presets/
def write_settings(config_dir: Path, settings: dict) -> None           # atomic write
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
                        baseline: list[MetricsReport] | None, current: ProfileInfo | None) -> list[Candidate]
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
def aim_report(clicks: Sequence[tuple[PenSample, tuple[float, float]]], mm_per_px: float) -> MetricsReport
# gui/live_tests.py
def run_test(parent, kind: str, mm_per_px: float, display: DisplaySpec | None,
             on_done: Callable[[MetricsReport], None], on_cancel: Callable[[], None] | None = None) -> None
# kinds: "rate" | "stillness" | "tracking" | "aim"; samples come from OS cursor position
# (OTD moves the cursor): use a high-frequency poll on Windows (ctypes GetCursorPos) and Tk
# <Motion> events as fallback. `display` is the monitor the tablet is mapped to (desktop px,
# center-based like OTD): the test window must cover exactly that monitor. None = primary.
# mm_per_px = tablet_area.width_mm / display.width_px (computed by the caller).
```

Integration notes (verified when wiring the modules together):

* `mm_per_px` uses the areas OTD applies *during* the test: the current profile's
  tablet area and its own display area for a baseline, the candidate's for a candidate.
* Coordinates: `DisplaySpec` (and the test window / `GetCursorPos`) use OS desktop
  px with the primary monitor at 0,0. OTD measures its display area from the
  top-left of the whole virtual desktop (a real 0.6.7 setup with a monitor left of
  the primary stores the primary's center as X = 960 + 1080). `Session.otd_display()`
  shifts the display by that origin before anything is written into OTD.
* `Frequency` is left at the catalog default (1000 Hz) except for Saturn
  Multifilter, the only filter that reads it as the tablet report rate.
* A rotated current area (e.g. 180 deg, left-handed) keeps its rotation.
* Per candidate the wizard runs `stillness`, `tracking` and `aim` while that
  candidate is live in OTD (jitter is part of the score).

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
