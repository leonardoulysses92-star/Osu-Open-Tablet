# Architecture

osu-open-tablet is a Tkinter desktop program that reads and writes OpenTabletDriver (OTD) 0.6.x settings
to find a good tablet area and filter chain for an osu! player. Python 3.10+, standard library only.
Windows is the target; on other systems path detection must not crash, and the Windows-only parts
(`ctypes` calls) return "not available" instead of failing on import.

## Packages

```
osu_tablet_assistant/
  __main__.py      entry point: arguments, language, log file, the main window
  appconfig.py     the program's own config.json (atomic writes, every field validated on load)
  profile.py       saved player profile (tablet, screen, answers, last saved settings)
  keys.py          click-key names and codes, the player's key choice, companion hotkey text
  log.py           local rotating log for errors the program recovers from
  models.py        dataclasses shared across packages (Rect, TabletSpec, Candidate, Reason...)
  otd/             everything that touches OTD
  knowledge/       data: tablets, filter catalog, community presets, data updater
  engine/          pure logic, no Tk
  gui/             windows; the logic behind each window lives in a module without Tk
  locales/es, en/  interface texts (same keys in both languages)
tests/             unittest, synthetic data only
tools/             refresh_data.py, screenshots.py, build_exe.ps1, release.ps1
data/              generated copy of the community data that the in-app updater downloads
```

### otd

| Module | Role |
|---|---|
| `settings_io` | find OTD (installed or portable), read, patch, back up and write `settings.json`, save presets |
| `plugins` | installed plugins and the filter paths they provide (`KNOWN_PLUGINS`) |
| `controller` | apply settings to a running daemon: Console `loadsettings`, else JSON-RPC over the named pipe, else a preset file |
| `livetune` | `LiveTuner` (live sends, undo, keep, first-use backup) and osu! process detection |
| `winproc` | read-only Toolhelp process listing and executable paths (`ctypes`, private `kernel32` handle) |

### knowledge

| Module | Role |
|---|---|
| `filters` + `filters.json` | filter catalog: OTD paths, parameters, ranges, policy flags |
| `tablets` + `tablets_builtin.json` | tablet list (built-in, OTD `Configurations`, typed by hand) |
| `presets` + `community_presets.json` | community setups with a strict validator |
| `updater` | manifest + SHA-256 data bundle: download, validate, cache; a cache is used only when newer than the packaged data |

### engine

| Module | Role |
|---|---|
| `area` | area size, aspect and position from the tablet, screen and answers |
| `recommend` | candidates, scoring, ranking, one-step refinement; filter order (`RESEARCH_ORDER`) |
| `community` | community presets as candidates, adapted to the player's area, and comparisons |
| `metrics` | pen samples to `MetricsReport` (rate, stillness, tracking, aim) |
| `judge` | repeated runs to one grade in words, reliability and ties |
| `adapt` | live tuning: an intent ("smoother", area +2 %) to small parameter steps, convergence |
| `passive` | cursor statistics per live setting (jitter, path roughness) |
| `smoothing` | smoothing filters the player can try in the coach, gentle values, five-level ladder |
| `playtypes` | plain data shared by the coach pipeline |
| `beatmap` | `.osu` parsing, map lookup by window title, playfield coordinates |
| `osudb`, `mapindex` | `osu!.db` reader and the map index (stable, lazer store, header scan fallback) |
| `osucfg` | the few `osu!.<user>.cfg` keys that matter (raw input, sensitivity, frame limit) and the osu! folder |
| `replay`, `calibrate` | `.osr` reader; live recording checked against the replay (clock offset, transform, map MD5) |
| `areafit` | area geometry in the player's view and in native tablet millimetres |
| `playobserve` | judge a play: alignment, per-object verdicts, reach, edges, feel |
| `hotspots`, `findings` | fine miss grid with pooled cells and small zones; plain-language findings |
| `habits`, `habitstore`, `rulebook` | the player's habits per play, pooled over plays (`habits.json`), and the rules that pick one change |
| `optimizer` | the coach's round loop: cost, stop rule, one proposal per round, area trials |

### gui

| Module | Role |
|---|---|
| `app`, `wizard`, `session` | main window; wizard pages; `Session` is the state machine behind them, reaching every service through `Services` so tests inject fakes |
| `widgets`, `palette`, `raster`, `i18n` | theme, reusable widgets, antialiased bitmaps, translations |
| `live_tests`, `pen_sampler` | the pen test windows and the cursor sampler (`GetCursorPos`, back-fill from `GetMouseMovePointsEx`) |
| `companion`, `hotkeys` | live tuning window (`CompanionCore` has no Tk), global hotkeys and beeps |
| `coach`, `coachcore` | Play Coach window and its logic without Tk |
| `playwatch` | play recorder: cursor, the chosen click keys only, osu!'s window title |
| `keycard`, `smoothcard`, `stylecard`, `tabletmap`, `mapgeometry` | coach cards and the tablet map |
| `winutil` | `companion_args_from_session` and `make_click_through`, shared by companion and coach |

Import direction: `gui` uses `engine`, `otd` and `knowledge`; `engine` does not import `gui` (only
`engine.osucfg` uses `otd.winproc` for an executable path). `appconfig` only imports `engine` and `keys`.

## Data flow of the guided setup

```
startup     appconfig.load_config, log.setup, updater.activate_cache
detect      settings_io.config_locations / find_install_dir, plugins, version notice
choose      tablet, display, questions (plugin policy) -> UserPreferences
baseline    live_tests (rate, stillness) -> metrics
candidates  recommend.generate_candidates (+ community.select_presets, saved settings, keep current)
test        apply each candidate live -> stillness, tracking, aim -> judge / recommend.rank -> refine
result      reasons, comparison with community presets, plugin suggestions
save        full OTD settings file (backup first), optional apply
```

The welcome screen also opens live tuning (`gui.companion`) and the Play Coach (`gui.coach`) after the
display step, through `wizard.COMPANION_LAUNCHER` / `wizard.COACH_LAUNCHER`.

## Rules that are easy to break

- Nothing is applied without `confirmed=True` and one settings backup per session; tests never touch the
  real OTD folder or a running daemon.
- Coordinates: OTD rects are centre-based (tablet in mm, display in px). OTD measures the display from the
  top-left of the whole virtual desktop, Tk and `GetCursorPos` from the primary monitor;
  `Session.otd_display()` converts.
- In OTD 0.6 filters inside the pre-transform group run in the order of the profile's `Filters` list. New
  chains follow `recommend.RESEARCH_ORDER`; a player's chain is never re-sorted.
- `Frequency` of async filters stays at the catalog default (1000 Hz).
- Prediction is never switched on and filters with `auto_recommend: false` are never picked automatically
  (osu! notice of 2026-10-14, see `docs/filtros.md`).
- Plugin policy: `tune_only` keeps the chain and moves one value; `installed` uses detected plugins;
  `allow_new` also suggests plugins. Nothing is ever installed.
- Community area adaptation is a heuristic (Radial Follow radii scale with the area, see `docs/comunidad.md`).
- Every user-visible string is an i18n key; `Reason(key, params)` carries explanations from the engine to the GUI.
- Errors the program recovers from go to `log.log_exception`, never silently away.

Details per feature: [PLAY_COACH.md](PLAY_COACH.md), [COMPANION.md](COMPANION.md), [DESIGN.md](DESIGN.md),
[research/filters.md](research/filters.md), [research/players.md](research/players.md).
