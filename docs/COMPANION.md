# Companion / live-tuning mode

The companion runs next to osu!. The player plays, judges how the cursor feels, and
sends one plain intent ("smoother", "more responsive", "steadier at rest", "less
sticky", area ±2%). Each intent is a small, coordinated parameter step applied live
to OpenTabletDriver (OTD). Steps shrink when the feedback goes back and forth, so the
settings converge on a point the player likes. The player then presses **Keep**.

## The window

* **Compact shortcut panel** ("Atajos mientras juegas" / "Shortcuts while you play"): eight
  small buttons, each with its plain name and its key combination on a second line
  (Smoother, More responsive, Steadier at rest, Less sticky, Area -/+, Undo, Keep). A click
  does exactly what the hotkey does. Four per row on a wide window, two per row when narrow.
  Under them, one line with the **step in use** and its hotkey ("Paso: Fino (pasos de unos
  0,4x, área 1%). Ctrl+Alt+P lo cambia.") and one line with the **last change** with old ->
  new values. The live values, cursor hints, quick poll, policy and sound options sit below.
* **Step selector** (Normal / Fino / Muy fino): a small segmented control in the panel, and the
  `step` hotkey (default `Ctrl+Alt+P`) cycles it while playing (its own beep, and the overlay says
  the new step). The default is Normal, so nothing changes for someone who never touches it.
* **"Atajos" / "Shortcuts" button**: opens or closes the hotkey editor. Each action has a
  field and a **Detectar / Detect** button: press it, then the combination in the companion
  window (Esc cancels). While detecting, the global hotkeys are paused, because a combination
  this program registered would be delivered as a hotkey and never reach the window. A
  combination already used by another action is refused and named ("Ctrl+Alt+Up ya es el atajo
  de Más suave"); a key without Ctrl/Alt/Shift asks for a modifier. Typed text is applied with
  **Aplicar** only when every field is valid and no combination repeats (nothing changes
  otherwise). **Restablecer** restores the defaults. An empty field means no hotkey.
* **Saved**: the hotkeys (`companion_hotkeys`) and the step (`companion_step`) live in the
  assistant's own config (`appconfig.AppConfig`). Loading validates them with
  `hotkeys.clean_bindings`: unknown actions are dropped, invalid or repeated combinations fall
  back to the default (or to no key when that default is taken), and actions missing from an
  older file (such as `step`) get their default. Old files load unchanged.
* **Overlay**: unchanged (click-through, non-activating, fading); its last line now also names
  the step in use ("3 cambios · paso: Fino · sin guardar").
* **Layout**: the window opens at a fixed size (900x820 or the screen, whichever is smaller),
  scrolls vertically and reflows to its width (panel 4 or 2 per row, editor 2 or 1 column,
  lower cards 2 or 1 column, every text wraps). A window that followed its requested size let
  wrapping labels and the reflowing grids feed back on each other and froze the UI; a test
  builds it at 640 and 900 px and checks that nothing passes the right edge.

## Step levels

The level **multiplies** the axis gain; it does not replace it. Gain, reversals (halve),
streaks (×1.25) and the convergence rule are the same at every level.

| Level | Gain factor | Float grid | Area per move |
|---|---|---|---|
| Normal | ×1 | catalog step | 2 % |
| Fino | ×0.4 | catalog step / 5 | 1 % |
| Muy fino | ×0.2 | catalog step / 10 | 0.5 % |

* The finer grid never goes below 0.0001 (`knowledge.filters.GRID_FLOOR`); values are rounded
  to the grid's decimals. Ints stay ints and move by whole steps (at least 1), so Noise
  Reduction `Samples` moves ±1 at every level.
* The catalog min/max and the soft limits below apply at every level. `LiveTuner` now clamps
  changed values to the catalog bounds on the finest grid (step / 10) instead of the catalog
  grid, so fine values are not snapped back.
* The "log" seed (the jump from ~0) is scaled by the same factor, so a fine move from 0 is small too.
* Examples (one move at gain 1, from the value shown):

| Filter · parameter (catalog step) | Normal | Fino | Muy fino |
|---|---|---|---|
| Radial Follow · OuterRadius 0.6 mm (0.05) | 0.7 / 0.5 | 0.65 / 0.56 | 0.625 / 0.575 |
| Radial Follow · SmoothingCoefficient 0.88 (0.001) | 0.9 / 0.856 | 0.8888 / 0.8704 | 0.8846 / 0.8752 |
| Radial Follow · InnerRadius 0.04 mm (0.01) | 0.05 / 0.03 | 0.044 / 0.036 | 0.042 / 0.038 |
| Hawku / Devocub · Latency 4 ms (0.5) | 5 / 3 | 4.4 / 3.6 | 4.2 / 3.8 |
| Devocub · AntichatterStrength 3 (0.1) | 3.3 / 2.7 | 3.12 / 2.88 | 3.06 / 2.94 |
| Noise Reduction · DistanceThreshold 0.5 mm (0.05) | 0.6 / 0.4 | 0.55 / 0.45 | 0.525 / 0.475 |
| Noise Reduction · Samples 10 (1) | 11 / 9 | 11 / 9 | 11 / 9 |
| Temporal Resampler · latency 2 ms (0.5) | 2.5 / 1.5 | 2.2 / 1.8 | 2.1 / 1.9 |
| Kuuube (smooth) · strength 6 (0.1) | 7.2 / 5 | 6.48 / 5.56 | 6.24 / 5.77 |

* **Convergence in fine mode**: the same rule (last 4 moves alternate and gain ≤ 0.25) triggers
  after the same number of back-and-forth presses as in Normal, with steps that are already
  smaller; then the UI, the overlay and the chime recommend Keep. The "steps from start" numbers
  are weighted by the level, so a fine move counts less. Changing the level is not an undo step:
  Undo restores values, never the step size, and it does not reset the gain history.

## Modules

| File | Role |
|---|---|
| `otd/livetune.py` | `LiveTuner`: model of the daemon's live settings, rate-limited RPC sends, undo stack, auto-revert, first-use backup, Keep. Also osu! detection (`is_osu_running`, `find_osu`, `OsuWatcher`). |
| `engine/adapt.py` | Pure feel-axis engine: `step()`, `TuningPolicy`, `nudge_area()`, convergence, `accept()`, `poll_to_intents()`. |
| `engine/passive.py` | Pure cursor statistics per window (rest jitter, path roughness, update rate) and before/after comparison. |
| `gui/hotkeys.py` | Global hotkeys (`RegisterHotKey` on a dedicated message-loop thread, with conflict detection) and `Beeper` (one tone pattern per action). |
| `gui/companion.py` | `CompanionCore` (no Tk), `Overlay` (click-through, non-activating, fading), `CompanionWindow`, `open_companion`. |
| `otd/controller.py` | New method `OtdController.apply_rpc(settings, timeout_s=None)`: one `SetSettings` over the pipe. No Console call and no preset fallback. |

No field was added to `models.py`.

## Integration

```python
from osu_tablet_assistant.gui.companion import open_companion
win = open_companion(root, session)   # session: gui.session.Session
```

`companion_args_from_session` reads only public attributes and methods. It takes the
settings that are live right now:

* `session.final_settings()` if `session.applied` is set;
* otherwise the candidate under test (`session.live_candidate_id`);
* otherwise `session.settings`.

It also reads `profile.tablet_name`, `tablet`, `display`, `config_dir` and
`filter_paths` (as installed filters), plus `dry_run` (dry run disables sending and
Keep). It builds a controller with `services.make_controller`. Any argument can be
overridden:

```python
CompanionWindow(parent, settings=..., tablet_name=..., tablet=..., display=..., controller=...,
                config_dir=..., translate=i18n.t, available_filters=..., policy="tune_only",
                on_keep=lambda settings: ..., on_close=lambda: ...)
```

Use `on_keep` to tell the wizard that `settings.json` was replaced. Otherwise, a later
`Session.restore_original` restores the daemon but does not rewrite the file.

## Does `SetSettings` persist? No (verified)

OTD 0.6.x `DriverDaemon.SetSettings` (checked at `v0.6.4.0` source) does three things:

* replaces the in-memory `Settings`;
* rebuilds the output mode and filter pipeline for each device;
* remembers `lastValidSettings`.

It never serializes to disk. `settings.json` is only read, at daemon start
(`LoadUserSettings`). As a result:

* Every companion change is **live, not saved**. The UI says so, and restarting OTD
  drops the changes.
* **Keep** writes `settings.json` through `settings_io.write_settings`, after a
  backup. The backup is taken once per session, before the first change, in
  `<config>/Backup/`.
* The OTD GUI keeps its own copy of the settings. Pressing Save/Apply there
  overwrites the companion's live values. No RPC event tells clients that the
  settings changed.
* If a profile fails to build, OTD itself falls back to `lastValidSettings` and the
  RPC call still succeeds. Our model would then disagree with the daemon. The bounds
  below make this unlikely, but it cannot be detected.

## Safety

* **Bounds:** `adapt` moves values inside the `filters.json` min/max range. It also
  uses narrower soft limits for automatic moves, taken from the tooltip ranges in
  `docs/research/filters.md`, for example:
  * Radial `SmoothingCoefficient` ≤ 0.99;
  * Hawku and Devocub latency ≤ 30 ms;
  * Noise Reduction samples 5–20, threshold 0.2–1.0 mm.

  `LiveTuner` clamps every value it *changes* to the catalog again, so nothing outside
  the bounds is ever sent. Values the user already had are left untouched, even when
  they are outside our bounds.
* **Area:** the area keeps its aspect ratio, center and rotation (180° stays 180°).
  Growth is capped so the rotated area stays on the tablet. A change that would leave
  the tablet is rejected, and so is growth when the tablet size is unknown.
* **Prediction is never added.** Temporal Resampler `frameShift` is only raised up to
  its default of 0.5, and Devocub prediction is never touched. The osu! policy on
  prediction plugins is unresolved (`docs/research/filters.md` §4).
* **Sending:**
  * sends are at least 150 ms apart, and a burst collapses into one send of the latest
    state;
  * a failed send restores the last acknowledged settings and re-sends them once
    (auto-revert);
  * undo restores the exact previous settings dict.
* **Policies:**
  * `tune_only` (the default) changes only values of the filters that are already
    enabled;
  * `installed` may also re-enable a disabled supported filter, or add an installed
    one with gentle values, at its chain position;
  * `allow_new` may also *suggest* a plugin that is not installed. Nothing is ever
    installed, and no filter is ever removed.

## Feel axes

| Axis | + intent / − intent | Radial Follow | Hawku | Devocub | Temporal Resampler | Noise Reduction | Kuuube |
|---|---|---|---|---|---|---|---|
| motion | smoother / more responsive | OuterRadius ×1.2 and (1−SmoothingCoefficient) ÷1.2 | Latency ×1.25 | Latency ×1.25 | latency, then frameShift (≤ 0.5) | Samples ±1 | strength (shared) |
| rest | steadier / less sticky | InnerRadius ×1.3 (≤ outer) | Latency (shared) | AntichatterStrength ±0.3 | followRadius | DistanceThreshold | strength |
| area | area +2% / −2% | scale both sides, keep center, aspect and rotation | | | | | |

Saturn filters and unknown plugins are skipped, with a reason. When a profile has
several filters, one is preferred per axis (Radial first); the next filter is used
once the preferred one reaches its limit.

**Convergence.** Each axis has a gain:

* reversing direction halves it (minimum 0.125);
* two moves in a row in the same direction raise it by ×1.25 (maximum 1);
* every move is at least one grid step (the catalog step in Normal, the finer grid in Fino /
  Muy fino).

An axis has converged when its last 4 moves alternate and the gain is ≤ 0.25. The UI,
the overlay and a chime then recommend Keep. Liking the same values twice in the quick
poll also says "stop here". Every change returns `Reason`s with old → new values.

## Passive hints

From the OS cursor stream (`PenSampler`, polling at 1 kHz), each window covers the time
one setting was live. In each window the monitor measures:

* **rest jitter:** time-weighted RMS, with linear drift removed, inside segments where
  the cursor stays within 0.5 mm for ≥ 0.25 s;
* **path roughness:** at 30–600 mm/s, each point's distance from its neighbours'
  chord, relative to that chord, as a median per 0.25 s chunk;
* **update rate** while moving.

**Comparison.** The before and after windows are compared with Cohen's d on the
per-segment and per-chunk values:

* a verdict needs |d| ≥ 0.3 **and** at least a 10% change in the mean;
* with fewer than 3 rest segments or 8 path chunks the result is "not enough data";
* jitter below half a cursor pixel is flagged as such.

These are hints only, and the UI says so.

## Honest limits (unverified without the user's hardware)

* **Cursor, not game.** The companion sees the OS cursor, not osu! hits, misses or
  accuracy. How osu!stable "raw input" and lazer interact with the OS cursor under OTD
  absolute mode has not been checked. If osu! confines or ignores the OS cursor, the
  passive hints become meaningless; the manual intents keep working.
* **Exclusive fullscreen.** It hides the overlay, so feedback there is audible (beeps).
  Borderless or windowed mode shows the overlay.
* **Fine steps may be below what you can feel.** A "Muy fino" move of 0.0001 in
  `SmoothingCoefficient` or 0.05 ms of latency is real in OTD but probably not noticeable in
  play; the finer levels are for the last touches after Normal got close. Every send (each
  press) still rebuilds OTD's filter pipeline, so many tiny presses mean many rebuilds.
* **Detect only sees keys that reach the window.** A combination owned by another program (for
  example a graphics driver hotkey) never arrives, so nothing happens; type it or pick another.
* **Hotkey conflicts.**
  * `Ctrl+Alt+arrow` can collide with Intel graphics rotation hotkeys. When it does,
    the companion reports it, and the hotkeys can be rebound with the "Atajos" button.
  * On Spanish keyboards `AltGr` equals `Ctrl+Alt`, so registered combinations stop
    reaching osu! chat.
* **Pipeline hitch.** Each send rebuilds OTD's filter pipeline (async filters restart
  their timers). A tiny hitch is possible, so tune during breaks or between maps.
* **Not verified on real hardware.** Real-time RPC latency and stability under
  repeated `SetSettings` with the user's OTD 0.6.7, CTL-472 and plugins were not
  tested here. All tests use fakes, and nothing touched the real daemon or files.
* **Coefficient ceiling.** Automatic moves keep `SmoothingCoefficient` ≤ 0.99 (soft limit) at
  every step level, so the community value 0.995 is not reached by the companion; it is only
  kept when the user already has it.
