**[Leer en español](README.md)**

# osu! Tablet Assistant (osu-open-tablet)

A guided assistant for osu! players that helps you configure [OpenTabletDriver](https://opentabletdriver.net) (OTD): the size and aspect ratio of the tablet area, and the smoothing filters. It works with any tablet that OTD supports. It measures your tablet with short live tests, compares the result with setups published by top players and the community (with their sources), and explains every recommendation.

## What it does

The assistant takes you through these steps:

| # | Step (name in the app) | What happens |
|---|---|---|
| 1 | Welcome | You read the notice and accept it. This page also shows the state of the community data and the "Update community data" button. |
| 2 | OpenTabletDriver | Finds the OTD settings and program folders (installed or **portable**), reads your profiles and plugins, and tells you if your OTD is older than 0.6.8.1. |
| 3 | Tablet | You pick your OTD profile, a tablet from the list, or enter your tablet's details. |
| 4 | Screen | Detects the size of the primary monitor. You confirm or type the values. |
| 5 | About you | Play style, map type, priority, skill level, shaking or lag, and **which plugins the assistant may use** (see "Plugin policy"). |
| 5b | Starting preset | Only if there are usable community setups: pick one as a starting point (optional). |
| 6 | Starting point | Two short tests (report rate and stability) with your current settings. |
| 7 | Try settings | Applies each option to OTD, measures it (stability, tracking, aim) and lets you rate it from 1 to 5. You can refine the best one. |
| 8 | Result | The recommended setup, the reasons, the measurements, the **comparison with community setups** and, if you allow it, **plugins you could try**. You save it to your folder, copy the report or apply it in OTD. |

**What it does not do:**

- It does not guarantee the best possible setting. It gives measured, explainable starting points.
- Community values are not "the best": they are one more candidate that is measured like the others and only wins if it measures better for you.
- It does not change anything without your confirmation, and it **never installs plugins** (it only tells you which one, why, where to find it and how to install it yourself).
- It never turns prediction on by itself (see "osu! rules").

## Three paths from the welcome screen

- **Guided setup** (recommended): the steps above.
- **Live tuning**: the companion. You play and press "smoother" / "more responsive" until it feels right (see `docs/COMPANION.md`).
- **Play Coach**: you play a real map; the coach lines your clicks up with the map, tells you in plain words where you fall short (corners, top/bottom, sides) and suggests **one** change per round (move the area, resize it, or one filter). It is applied live, you play again, and it repeats until there is nothing left to gain. What it sees, what it infers and its limits: `docs/PLAY_COACH.md`.

An "Advanced mode" switch (top right) shows the technical data (full report, log, community data). It is off by default.

### The "Options to test" table

Each option shows a **Result in words** (Excellent / Good / Fair / Weak), not points. Repeating a test **does not overwrite** the previous one: the median of all runs is used, so one bad run cannot hide a good one. An option missing any of the three tests shows as "incomplete" and is never ranked above a complete one. When the best options are within the measurement noise they are all starred (★, "tie: choose by feel"). Test targets use one fixed seed per test kind, so every option and every repeat sees the same targets, and the aim test ignores its first 2. The number is under "See technical data".

## Requirements

| Requirement | Details |
|---|---|
| System | Windows 10 or 11 (the main target). Linux and macOS are best-effort. |
| Python | 3.10 or newer with `tkinter` (standard library). Nothing needs to be installed with `pip`. |
| OpenTabletDriver | 0.6.x, installed or portable. Version **0.6.8.1** or newer is recommended if you use interpolators on Windows (0.6.8.1 fixes a daemon crash of 0.6.8 with asynchronous filters). With 0.6.7 the assistant shows a notice; it never forces you to update. |

## How to run it

Double-click `run.bat`, or from a terminal in the project folder:

```
py -3 -m osu_tablet_assistant
```

| Option | What it does |
|---|---|
| `--lang es` or `--lang en` | Interface language. Without it, the last language you chose is used (Spanish the first time). |
| `--config-dir PATH` | OTD settings folder (the one with `settings.json`). |
| `--install-dir PATH` | OTD program folder (the one with the Console or Daemon executable). |
| `--dry-run` | Never applies anything to OTD. You can only save the file and the report. |

## Community data and sources

`osu_tablet_assistant/knowledge/community_presets.json` holds **31 setups** (data of 2026-10-30) from top-ranked players and well-known community members: osu! profiles, self-entered setup.moe entries, Liquipedia and the osu! forum. Every entry keeps its **source link, the source date (or "undated"), the retrieval date and a confidence level** (high = the player's own profile or self-entered entry; medium = wiki/secondary or entries with warnings; low = hearsay or unverified). The full method is in `docs/research/players.md`.

- Entries that look like typos or untouched defaults were **dropped** (ChocoPafe: inner radius larger than outer; Exxotl: plugin defaults with a 1 mm deadzone) or **flagged** (for example "possible typo", "form defaults", "stale", "tablet-specific"). Flags are shown as warnings.
- The values you provided (Radial Follow 0.71 / 0.37 / 0.37 / 0.38 / 0.26, area unknown) are included as **"your own values", unverified, low confidence and not attributed to any player**: the research found no public source tying them to mrekk.
- **Adapting to your area (heuristic):** Radial Follow radii are millimetres on the tablet. The assistant scales `OuterRadius` by the width ratio and `InnerRadius` by the height ratio between your area and the source's area (this is how the popular Mixmg calculator, which most top players use, computes them). `SmoothingCoefficient` is only scaled when the entry has the calculator's signature (inner = coefficient). Knee and leak are kept. Pixel values (screen space) and raw tablet units are not scaled. If the source area is unknown, the values are used as published. Every adjustment is listed in the reasons.
- Prediction is always switched off, an internal `Frequency` value is reset to 1000 Hz, and out-of-range values are limited to the catalog bounds (and that is said too).
- The 3 closest community setups (by tablet, area, confidence and the filters you already use) become candidates; the one you pick as a starting preset is measured first.
- The Result page compares your result with the closest setups: area, tablet and each value that differs, with the source, date and confidence.

## Keeping the data fresh

The **"Update community data"** button (Welcome page and Settings) downloads a small bundle from the project's GitHub page (`data/` folder of `leonardoulysses92-star/Osu-Open-Tablet`, branch `main`; the address can be changed in Settings):

- Only `https` addresses, a timeout and a maximum size per file.
- `manifest.json` lists the SHA-256 and size of each file; both must match, and both files must pass the same strict validation as the built-in data. A downloaded catalog cannot add plugins this version does not know: **it is data only, never code**.
- A valid bundle is saved in `%APPDATA%\OsuOpenTablet\data` and used from then on. If anything fails, the data you already had stays in use (the built-in data as the last fallback).
- The screen shows "Last updated <date> · N setups · M sources" and warns when the data is more than **90 days** old.
- Checking at startup is **off** by default; turn it on in Settings if you want it. There is no telemetry: only those three files are requested, with no identifiers.

For maintainers: after changing `knowledge/community_presets.json` (bump its `version` and `generated`) or `knowledge/filters.json`, run `python tools/refresh_data.py` and commit `data/`. The tests fail if `data/` is out of date.

## Plugin policy

In "About you" (and in Settings) you choose which plugins the assistant may use:

| Choice | What it means |
|---|---|
| **Only tune my current filters** | Your filters and their order stay. Only their values change, **one at a time**, each listed as current value -> proposed value with the reason. There is no "no filters" option. |
| **Use any plugin I have installed** (default) | It picks among the plugins found in OTD's `Plugins` folder. |
| **Also suggest plugins I do not have** | Like the previous one, and the Result page lists missing plugins with the reason, where to find them and how to install them yourself. |

Candidates, refinements and community setups all follow the policy: a community setup that needs a plugin you do not have (or, with "only tune", a filter you do not use) is not tested.

## osu! rules (prediction and input assistance)

On 2026-10-14 osu! said that OTD and smoothing / anti-jitter plugins are fine, but that plugins that assist input in ways "any normal person would consider cheating" will lead to action. It named no plugin ([forum topic](https://osu.ppy.sh/community/forums/topics/2254092)). Therefore:

- Prediction plugins (Frenet-Serret predictor, ScuffedInter/Extrapolator) and non-smoothing filters (Hover Distance Limiter, Saturn Area Rounding) are **never chosen automatically**: user decision only, with an explanatory note.
- Devocub's prediction is never turned on.
- Whether the prediction setting of Temporal Resampler or Saturn (`frameShift`) counts is **unknown**; when an option uses it, the reasons say so.

## Where files are saved

In **Settings** you choose your folder (for example `C:\Users\<you>\Documents\opentablet`, where a portable OTD lives). What the assistant saves is a **complete OTD settings file**, the same format as an `Osu.json` (`Revision`, `Profiles`, `LockUsableAreaDisplay`, `LockUsableAreaTablet`, `Tools`), which you load with OTD's "Load settings".

- When saving you can edit the name; by default "`<tablet> <date time>`" (the template can be changed in Settings).
- If the file already exists (for example `Osu.json`) the assistant asks first, and before replacing it saves a dated copy in `<folder>\Backup\`.
- Without a chosen folder it saves to OTD's `Presets` folder, as before.
- The assistant's own settings (language, folder, file name, plugin policy, updates) live in `%APPDATA%\OsuOpenTablet\config.json`. If that file gets damaged, defaults are used and the damaged copy is kept as `config.corrupt.json`.

**Portable OTD:** besides `%LOCALAPPDATA%\OpenTabletDriver`, the assistant recognises portable folders (with `OpenTabletDriver.Daemon.exe` or `convert_to_portable.bat` next to a settings file or a `userdata` folder). If it finds several, the OpenTabletDriver step lets you choose.

## Safety

- **Backup before any change** to OTD's settings: `<config>\Backup\osu-assistant-settings-YYYYMMDD-HHMMSS.json`, once per session.
- **Explicit confirmation** before the first test that applies settings, before applying the result, before restoring and before replacing a file.
- **Dry-run mode** (`--dry-run`): never applies anything.
- The assistant never closes or restarts OTD.

| What | Where | When |
|---|---|---|
| Backup of `settings.json` | `<config>\Backup\` | Before the first change |
| Your saved settings | Your folder (or `<config>\Presets\`) | When you click "Save as preset" |
| Copy of a replaced file | `<your folder>\Backup\<name>-YYYYMMDD-HHMMSS.json` | Before replacing it |
| Fallback preset | `<config>\Presets\osu-tablet-assistant.json` | Only if the settings could not be applied live |
| `settings.json` | `<config>\settings.json` | Only with "Apply to OpenTabletDriver" |
| Test settings | Only in the OTD daemon's memory | During the tests |
| Assistant settings and cached data | `%APPDATA%\OsuOpenTablet\` | When you change settings or update the data |

**How to undo:** "Restore my settings" in the app; or load a preset/file in OTD; or, with OTD closed, copy the backup over `settings.json`.

## Filters it knows

The catalog (`osu_tablet_assistant/knowledge/filters.json`) was audited against the plugins' source code on 2026-10-30 (`docs/research/filters.md`).

| Filter | Recommended automatically |
|---|---|
| Radial Follow (tablet and screen space), Hawku Smoothing, Devocub Antichatter | Yes |
| Temporal Resampler | Yes, when the screen refreshes much faster than the tablet reports |
| Hawku Noise Reduction, Kuuube's Chatter Exterminator | Yes, if there is shaking |
| Reconstructor, Spectral Reconstructor, Saturn Multifilter / HRRNC, Bezier Interpolator | Not by the standard options (advanced); usable through community setups or your own profile |
| Frenet-Serret predictor, ScuffedInter/Extrapolator, Hover Distance Limiter, Saturn Area Rounding | **Never** (user decision only) |

Corrections applied by the audit: `SmoothingCoefficient` can now be 0.995; Saturn's removed properties (`dacOuter`, `msOverride`) are gone; Saturn's `Frequency` is no longer forced to the report rate (doubtful); and the order of a new filter chain follows the research (noise first, then smoothing, then resampler). In OTD 0.6 the order of the `Filters` list is the execution order, so the assistant **never reorders your chain** by stage.

## How the live tests work

Each test opens a borderless window over the chosen monitor. To start: click, tap with the pen, or press Space/Enter; a short countdown follows. Esc cancels.

| Test | What you do | What it measures |
|---|---|---|
| Report rate | Draw large circles at your normal speed | Positions per second reaching the cursor |
| Stability | Hold the pen still, touching or hovering | How much the cursor shakes |
| Tracking | Follow a moving dot | Delay, path error and smoothness |
| Aim | Move onto each circle: **it counts as soon as the cursor enters it, no click** (works hovering) | Time to each target, closest approach to the centre, overshoot |

Limits: only the system cursor is measured (no pressure or tilt); the delay includes the window's drawing time; on Windows the cursor is polled about 1500 times per second; the score is a heuristic.

## Project layout

```
osu-open-tablet/
├── run.bat, build_exe.ps1
├── data/                       community data bundle for the "Update" button (generated)
├── tools/refresh_data.py       regenerates data/ (manifest + checksums)
├── docs/                       ARCHITECTURE.md and research/
└── osu_tablet_assistant/
    ├── appconfig.py            the assistant's own settings (%APPDATA%\OsuOpenTablet)
    ├── otd/                    everything that touches OTD
    ├── knowledge/              tablets, filter catalog, community presets, updater
    ├── engine/                 area, recommendations, community presets, metrics, verdicts (judge)
                                and the coach engine (beatmap, playobserve, areafit, optimizer, findings)
    ├── gui/                    the wizard and the live tests
    └── locales/{es,en}/        interface text
tests/                          unit tests (unittest)
```

## Tests and .exe

```
py -3 -m unittest discover -s tests
powershell -ExecutionPolicy Bypass -File build_exe.ps1
```

`build_exe.ps1` lists every module and every data file of the package automatically (new modules and JSON files are picked up without editing it).

## Status and limitations (honest)

- Version 0.1.x, early stage. Live apply (Console and daemon pipe) is implemented from OTD's source code but **not verified on real hardware**.
- The area-scaling rule for community values is a heuristic (based on how the Mixmg calculator works), not a rule from the plugin authors.
- setup.moe entries are self-reported and undated; ranks change every day; some setups may be old. Filter order is not recorded by most sources.
- Whether prediction settings count as input assistance for osu! is unresolved.
- You type your tablet's report rate yourself. Only the primary monitor is detected automatically.
- Spanish and English interface.
- **Play Coach**: without a real pen and a real osu! the alignment could not be verified in an actual play; the playfield transform is an approximation (osu!stable, borderless full screen), osu!lazer or windowed play may fail, rotation 90/270 is unverified and the cost constants and thresholds are **uncalibrated** estimates. It reads click keys on Windows only.
- `build_exe.ps1` was not run in this review (Windows only): rebuild the .exe with it before releasing.

## License

[MIT](LICENSE).
