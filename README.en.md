**[Leer en español](README.md)**

# osu! Tablet Assistant (osu-open-tablet)

A guided assistant for osu! players that helps you configure [OpenTabletDriver](https://opentabletdriver.net) (OTD): the size and aspect ratio of the tablet area, and the smoothing filters. It works with any tablet that OTD supports. It measures your tablet with short live tests and explains every recommendation, so you understand why each suggestion is made.

## What it does

The assistant takes you through eight steps, in this order:

| # | Step (name in the app) | What happens |
|---|---|---|
| 1 | Welcome | You read the notice and accept it with "I understand". |
| 2 | OpenTabletDriver | Finds the OTD settings and program folders, and reads your profiles and plugins. |
| 3 | Tablet | You pick your OTD profile, a tablet from the list, or enter your tablet's details. |
| 4 | Screen | Detects the size of the primary monitor. You confirm or type the values. |
| 5 | About you | Short questions: play style, map type, priority (less delay or smoother cursor), skill level, and whether you notice shaking or lag. |
| 6 | Starting point | Two short tests (report rate and stability) with your current settings. |
| 7 | Try settings | Applies each option to OTD, measures it with stillness, tracking and aim tests, and lets you rate it from 1 to 5. You can refine the best one. |
| 8 | Result | Shows the recommended setup, the reasons for it and the measurements. You can save it as a preset, copy the report or apply it in OTD. |

**What it does not do:**

- It does not guarantee the best possible setting. It gives measured, explainable starting points. Every hand and every tablet is different, so the final setting is yours to tune.
- The area sizes and filter choices come from ranges used by the osu! community. They are heuristics, and the code marks them as such.
- It does not change anything without your confirmation.
- It does not install OTD or plugins, and it never recommends a filter you do not have installed.

## Requirements

| Requirement | Details |
|---|---|
| System | Windows 10 or 11 (the main target). Linux and macOS are best-effort: the code has paths for them, but the project is aimed at Windows. |
| Python | 3.10 or newer, with `tkinter` (part of Python's standard library). Nothing needs to be installed with `pip`. If `tkinter` is missing, the program says so and exits. |
| OpenTabletDriver | 0.6.x installed. The code was checked against the OTD 0.6.7 source code. |

## How to run it

Option 1: double-click `run.bat`.

Option 2: from a terminal, in the project folder:

```
py -3 -m osu_tablet_assistant
```

Command-line options:

| Option | What it does |
|---|---|
| `--lang es` or `--lang en` | Interface language. Default is `es`. You can also change it inside the app. |
| `--config-dir PATH` | OTD settings folder (the one that contains `settings.json`). If you omit it, the assistant looks for it. |
| `--install-dir PATH` | OTD program folder (the one that contains the Console or Daemon executable). If you omit it, the assistant looks for it. |
| `--dry-run` | Dry-run mode: never applies anything to OTD. You can only save a preset and the report. |

`run.bat` passes its arguments to the program, for example: `run.bat --lang en --dry-run`.

## Safety

- **Backup before any change.** Before the first change, the assistant saves a copy of `settings.json` in the `Backup` folder. It does this once per session.
- **Explicit confirmation.** The assistant asks you before the first test that applies settings to OTD, and before saving a preset, applying the final result or restoring your settings.
- **Dry-run mode (`--dry-run`).** It never applies anything to OTD and does not test each option. Use it to review a recommendation without touching anything.

What is written, and where (`<config>` is the OTD settings folder):

| What | Where | When |
|---|---|---|
| Backup of `settings.json` | `<config>\Backup\osu-assistant-settings-YYYYMMDD-HHMMSS.json` | Before the first change |
| Preset with the result | `<config>\Presets\osu-assistant <tablet> YYYYMMDD-HHMM.json` | When you click "Save as preset" |
| Fallback preset | `<config>\Presets\osu-tablet-assistant.json` | Only if the settings could not be applied live. It is overwritten each time |
| `settings.json` | `<config>\settings.json` | Only when you click "Apply to OpenTabletDriver" |
| Test settings | Only in the OTD daemon's memory | During the tests. They are not written to the file |

By default, `<config>` is `%LOCALAPPDATA%\OpenTabletDriver`. If OTD is in portable mode, it is the `userdata` folder next to the program.

**How to undo a change:**

1. Inside the app: "Restore my settings" (in Try settings) or "Restore my original settings" (on the Result page). This sends the settings that were there when you started back to OTD.
2. Load a preset: in OpenTabletDriver, open **File > Presets** and select the saved preset.
3. Restore the file by hand: with OTD closed, copy the backup from `Backup` over `settings.json`. The app has no feature for this step.

If you close the window with a test setting still applied in OTD, the assistant asks whether you want to restore it before it exits.

## Tablets

- The built-in list has **48 tablets** (`osu_tablet_assistant/knowledge/tablets_builtin.json`). For each one it stores the exact OTD profile name, the active area in mm, the report rate, the maximum pressure and the resolution. All of them are marked as verified for area, pressure and resolution against OTD's own configuration files. For 26 of them, the report rate is an estimate based on the manufacturer's specifications.
- Your OTD profile name is matched against the list ignoring case, spaces and punctuation. If your OTD installation has a `Configurations` folder, its tablet definitions are also read, and they replace the built-in ones with the same name.
- If your tablet is not in the list, use the **custom entry**: name, active width and height in mm, report rate in Hz (the field starts at 133 Hz), and maximum pressure (optional).
- If your OTD profile is not in the list, it appears with an unknown size. Type the size by hand.

## Filters it knows

The catalog is in `osu_tablet_assistant/knowledge/filters.json`.

| Filter | What it is for | Recommended automatically |
|---|---|---|
| Hawku Smoothing | Smoothing with a latency value in ms | Yes |
| Devocub Antichatter | Smoothing that acts more on slow movements | Yes |
| Radial Follow (tablet space) | Radius-based smoothing. The cursor never lags behind the pen by more than that radius | Yes |
| Radial Follow (screen space) | The same, with the radius in pixels | Yes |
| Temporal Resampler | Interpolates the position for low-rate tablets on fast monitors | Yes, when the condition applies |
| Noise Reduction | Reduces noise when the pen is almost still | Yes, if noise is present |
| Kuuube's Chatter Exterminator (smooth and raw) | Removes chatter (jitter) | Yes, one of the two |
| Saturn Multifilter | Antichatter, prediction and other options | No (advanced) |
| Saturn Area Rounding | Reshapes the area around the centre of the screen | No (advanced) |

The plugin that provides each filter is listed in the `filters.json` catalog and in `otd/plugins.py`.

Rules:

- **Only installed filters are recommended.** The assistant looks for plugins in `<config>\Plugins\` (by folder name, by the name in `metadata.json`, or by DLL name), and it also counts the filters that already appear in your `settings.json`. If a filter is not found, it is not recommended.
- **"Advanced" means it is never added automatically.** It only appears if you already have it in your profile, where it is kept in the "Your current settings" option.
- **The latency shown for each filter is an estimate** calculated from its algorithm. It is not a measurement.

## How the live tests work

Each test opens a borderless window that covers the chosen monitor. A 3-second countdown runs before it starts. To begin, click (or tap with the pen). Esc cancels.

| Test | Length | What you do | What it measures |
|---|---|---|---|
| Report rate | 5 s | Draw large, continuous circles at your normal playing speed | How many positions per second the cursor receives |
| Stability | 5 s | Rest the pen on the tablet without moving it (or hold it just above the surface) | How much the cursor shakes |
| Tracking | 8 s | Follow a dot that moves in a figure eight at constant speed | Delay, path error and smoothness |
| Aim | 20 circles | Click the centre of each circle, one at a time | Distance from the click to the centre, and time until the click |

**Limits of the tests:**

- They measure only the **system cursor**. They cannot see pressure, tilt or the pen's buttons.
- The delay includes the time the test window takes to draw, not only OTD.
- On Windows, the cursor is polled about 1500 times per second. On other systems, the assistant uses window events instead, as a fallback.
- If you move the pen very slowly, the report rate can read lower than the real rate.
- The score is a heuristic. Each metric becomes a penalty between 0 and 1, and the priority you chose decides how much each one counts. Your 1 to 5 rating adds or subtracts up to 10 points.

**The area size** comes from a range for each play style, in millimetres of width: fingers 40 to 70, wrist 60 to 100, mixed 75 to 120, and arm 100 to 160. It is then adjusted slightly for your map type and skill level, limited to what fits on your tablet, and kept at your screen's aspect ratio. These are heuristic values with low confidence, meant as a starting point. If you prefer to keep your current size, the assistant respects that.

**The options that are tested** are:

| Option | What it aims for |
|---|---|
| No filters (reference) | Your tablet as it is. Used to compare the rest |
| Low latency | The least possible added delay |
| Balanced | Moderate smoothing with little delay |
| Smooth | The steadiest cursor, accepting a bit more delay |
| Your current settings | Only if you already have a profile in OTD, for comparison |

"Refine the best" makes one small adjustment to the most influential parameter of the best option. It stops when no new values are left to try, or after four steps per option.

## Project layout

```
osu-open-tablet/
├── run.bat                     Windows launcher
├── pyproject.toml
├── docs/ARCHITECTURE.md        design and module contracts
└── osu_tablet_assistant/
    ├── __main__.py             entry point and options
    ├── models.py               shared types
    ├── otd/                    everything that touches OTD (settings, plugins, applying)
    ├── knowledge/              tablet database and filter catalog (JSON)
    ├── engine/                 area, recommendations and metrics
    ├── gui/                    the wizard and the live tests
    └── locales/{es,en}/        interface text
tests/                          unit tests (unittest)
```

## Running the tests

From the project folder:

```
py -3 -m unittest discover -s tests -t .
```

## Building the .exe

For an executable that runs without Python installed you need PyInstaller (`py -3 -m pip install pyinstaller`). Then, from the project folder:

```
powershell -ExecutionPolicy Bypass -File build_exe.ps1
```

The result is `dist\Osu Open Tablet.exe`. The script lists every module and data file of the package by hand because the program loads some modules on demand (the pen tests, the recommendation engine, the OTD controller) and PyInstaller cannot detect them. Without that, the `.exe` opens but later fails with `No module named ...`.

## Contributing

- **Adding a tablet:** add an entry to `osu_tablet_assistant/knowledge/tablets_builtin.json`. The `name` field must be the exact OTD profile name. Set `verified: true` only if the area, pressure and resolution were checked against OTD's configuration files. Set `rate_estimated: true` if the report rate comes from the manufacturer's specifications.
- **Adding a filter:** the catalog is `osu_tablet_assistant/knowledge/filters.json` (OTD path, parameters with their bounds, sources and confidence level). Adding it to the catalog is not enough to get it recommended. It also has to be included in `engine/recommend.py`, and if it is a known plugin, in `KNOWN_PLUGINS` in `otd/plugins.py` so it is detected as installed. Its texts must exist in both `locales/es/engine.json` and `locales/en/engine.json`.
- Read `docs/ARCHITECTURE.md` before changing the code.

## Status and limitations

The project is at **version 0.1.0** and is at an early stage.

- Applying settings to a running OTD (with `OpenTabletDriver.Console loadsettings` and with the daemon's pipe) is implemented from OTD's source code, but it is **not yet verified on real hardware**. If the settings cannot be applied, the assistant saves a preset and tells you how to load it.
- You type your tablet's report rate yourself. The assistant does not detect it.
- Only the primary monitor is detected automatically. For another monitor, enter its size and the position of its top-left corner, in desktop pixels.
- The project is aimed at Windows. The high-frequency cursor capture is Windows-specific.
- Only the system cursor is measured: no pressure or tilt.
- Area ranges, latency values and scores are estimates and heuristics.
- The interface is available in Spanish and English.

## License

This project is released under the [MIT](LICENSE) license.
