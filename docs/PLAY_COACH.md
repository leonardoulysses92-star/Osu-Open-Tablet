# Play Coach

The Play Coach tunes the **active area** and, if needed, the **filters** from a real play. You play a
map; the coach lines your clicks up with the beatmap, works out where you fall short, proposes **one**
change, applies it live in OpenTabletDriver (OTD), and you play again. It repeats until the gain is
practically nil.

Open it from the welcome screen ("Coach de partidas" / "Play Coach"), from the result screen, or from the
live-tuning companion ("Switch to the Play Coach").

## What it sees, and what it does not

| It sees | It does not |
|---|---|
| The OS cursor (the same stream as the pen tests, ~1.5 kHz). | osu!'s memory. It never reads it. |
| **Only** the configured click keys (default Z, X, mouse 1, mouse 2) through `GetAsyncKeyState`. Other keys are never queried. | Hits, misses, score or combo as osu! computes them. |
| The title of osu!'s own window (to know which map). The title of any other window is never read. | Anything about spinners, slider bodies or slider ends. |

Only click **times** are stored (in memory, for the current session). Nothing leaves the PC.

Hits and misses are **inferred**:

1. the `.osu` file is found from the window title (`osu!  - Artist - Title [Difficulty]`) in your `Songs`
   folder, or you pick it by hand;
2. click times are aligned with the hit objects (greedy one-to-one matching inside the hit window);
3. at each click the cursor position is compared with the object centre.

Only circles and slider heads are judged.

## Seeing the real game: replays (`.osr`)

The only way to see what osu! itself recorded, without touching its memory, is the replay file it writes after
a play. `engine/replay.py` reads it **only when the player picks it** ("Use a replay (.osr)" on the prepare
screen, "Check with the replay" on the report):

* **As a round**: positions and clicks are exact (no key polling, no screen-transform guess for timing), so the
  round is as good as the data can be. It also works where the click keys cannot be read.
* **As a check** of a live round: the per-object hit / not-hit verdicts the coach inferred are compared with the
  ones from the replay, and the real accuracy (from the replay's hit counts) is shown. If they agree on 90 % or
  more the inference can be trusted; if not, use the replay as the round.
* The replay's map MD5 is compared with the `.osu` file; a mismatch is shown, not hidden.

Limits: osu! does not keep a replay of every play (for example some fails); the replay positions are mapped with
the same playfield approximation as the live recorder; osu!lazer replays have extra data and were not tested;
the parser was only tested with **synthetic** files written to the documented format, not with real ones.

## What is collected (and what is not)

The same list is shown in the app (prepare screen and Settings):

| Data | Why | Where it stays |
|---|---|---|
| Cursor position while observing | see how far the pen reaches | memory, erased on close |
| Press times of the configured click keys only | match clicks to the map | memory |
| Title of osu!'s own window | find the map | not stored; no other window is read |
| The `.osu` of the map | know where the circles are | not copied |
| A replay, only if you pick it: positions, clicks, mods, hit counts | exact data | memory; name, score, hash and date are discarded |
| Your tablet, screen, answers and last saved settings | not repeat the procedure | `profile.json` on this PC, deletable in Settings |

Never: osu!'s memory, other keys, other windows, your name or score, anything over the internet.

## Your saved profile

`profile.py` keeps, per tablet, what you chose (tablet, screen, answers) and your last 12 saved settings (area
and filters, with date and source). "Continue with my profile" on the welcome screen detects OTD, loads those
choices and lands on "Test settings", skipping the questions and the baseline; your saved settings appear first
as options (skipped if the area no longer fits, a filter is no longer installed, or it equals what OTD runs now)
and can be used without measuring. No play data is ever written there.

## Coordinate systems

* **desktop pixels**: what `GetCursorPos` returns;
* **osu! pixels**: the 512x384 playfield of the beatmap;
* **normalized area `(u, v)`**: 0 = left/top edge of the screen and of the area, 1 = right/bottom, as the
  player sees it. It does not depend on the rotation: the edges are the screen's. Area millimetres in the
  player's view are `(u * width, v * height)`;
* **native tablet millimetres**: those of `Rect.x/.y` in the OTD settings. They come from the area
  millimetres by rotating with the area rotation (`engine/areafit.py`). 0 and 180 are verified; 90/270 use
  the same rotation-matrix convention and are flagged as **unverified** in the proposal.

The playfield transform (`engine/beatmap.py`) assumes osu!stable, full screen or borderless, with the
playfield over the whole screen: scale = `screen_height / 480`, the 640x480 virtual screen centred
horizontally, playfield offset (64, 48). It is an approximation; the alignment quality is the real check.

## Pipeline

```
gui.playwatch.PlayRecorder  -> PlayCapture   samples + clicks + area + display
engine.beatmap              -> Beatmap       parse .osu, find it by window title
engine.playobserve          -> PlayReport    align, judge, grid, reach, edges, feel
engine.optimizer            -> RoundProposal area / filter change + plain-language reasons
otd.livetune.LiveTuner      applied live, undoable; Keep writes settings.json (after a backup)
gui.coach / gui.coachcore   the rounds loop (live tablet map, report, history, stop rule)
```

Shared data types live in `engine/playtypes.py`. `gui/coachcore.py` has all the logic without Tk;
`gui/coach.py` is the window.

### `playobserve` (pure, deterministic, fast)

* **Alignment**: candidate offsets are `click time - object time`; the most voted ones are matched in full
  and compared by quality. Time scales 1.0 / 1.5 (DT) / 0.75 (HT) and a vertical flip (HR) are tried.
  `quality` is the matched fraction weighted by timing precision; **below 0.6 the report is not trusted**.
* **Per object**: cursor interpolated at the click; error in area mm along the screen axes; `along`
  (<0 = the cursor stopped short, along the approach direction) and `lateral`; verdict
  `hit / no_click / too_far / too_early / too_late`.
* **Reach**: robust regression per axis, `cursor = gain * object + bias` (Theil-Sen on sampled pairs, 95 %
  bootstrap interval over the points, seeded). Gain < 1: the cursor stops short on far targets.
* **Edges**: per zone (left, right, top, bottom, four corners, centre; the outer quarter of the playfield)
  compare misses and error with the rest; kinds `short / over / late / noisy / ok`; zones with fewer than 6
  objects are not judged.
* **Feel**: error along the path on fast jumps (lag / over-smoothing) and rest-jitter on slow parts. The
  fast-jump threshold is 600 mm/s as specified, but in a small area few jumps get there, so with fewer than
  8 such jumps the fastest quarter (never below 200 mm/s) is used and the threshold used is reported.
* Works on **partial** captures (it is called every ~1.5 s while playing) and never raises. About 35 ms for
  a 600-object map with ~4400 samples (measured here, not guaranteed on every machine).

### `optimizer`

* **Cost**: ONE number, lower is better, from absolute references (not relative to other rounds):
  misses, mean error in circle radii, timing spread, `|gain - 1|`, lag and jitter. A component that could
  not be measured counts as half-bad so the basis does not change between rounds. A 90 % bootstrap
  interval over the judged objects gives the noise.
* **One physical change per round**, so the effect can be attributed:
  1. **shift** the area by the average cursor offset (the reach `bias`). The cursor error is
     `cursor - object`; moving the area by `+error` moves the cursor by `-error`, so a cursor that lands left
     of the objects is fixed by moving the area **left**, which puts the screen edge where the pen really
     reaches. The brief said "towards the short zones"; this is how that is implemented, because it is what
     reduces the error;
  2. **size** from the per-axis gain (with the aspect locked, the mean of both gains is used and a reason
     says so);
  3. **filters**, only through `adapt.step()` (plugin policy and safety limits of the companion):
     lag on fast jumps -> "more responsive", jitter on slow parts -> "steadier at rest", both -> nothing.
* **Trust region**: halved when a round is worse or a movement reverses; a movement that was undone is not
  proposed again; the area never leaves the tablet; if a round is worse than the best by more than the
  noise, the proposal is to go back to the best round's settings.
* **Stop rule** (`plateau`): the last two accepted rounds each improve the cost by less than
  `max(2 %, noise)`, or nothing useful is left to move. With too little data (few matched clicks, bad
  alignment) the verdict is `insufficient`, nothing changes and the message is "play more of the same map".

## The window

1. **Prepare**: is osu! open, which map, the tablet map with the current area, one big "Start observing"
   button (global hotkey `Ctrl+Alt+P` too).
2. **Observing**: a small always-on-top window with the live tablet map ("Finding the rhythm..." until the
   alignment quality reaches 0.6). It stops by itself after ~8 s without clicks once there are 20 or more.
   An optional click-through mini-map can sit in a screen corner (off by default; it is **not visible in
   exclusive full screen**, use borderless).
3. **Report**: tablet heat map, at most 4 findings in plain words, the single proposed change and the buttons
   "Apply and play again" (main), "Skip this change", "Undo last change".
4. **Progress**: one mark per round (better / same / worse, no numbers), the stop message, "Save these
   settings" and "Restore my original settings".

Applied changes are **live, not saved**, until "Save these settings" (the usual Keep flow, after a backup).
The first change makes the backup, like the companion.

## Limits (be honest with yourself)

* **Never verified against a real pen and a real osu!** here. The synthetic tests prove the logic recovers
  known offsets, gains and shifts from simulated players; they do not prove the alignment works in a real
  game.
* The playfield transform is an approximation; **osu!lazer or windowed play may fail** (the coach warns, and
  the alignment quality tells you). Lazer stores maps differently, so pick the `.osu` by hand there.
* The alignment absorbs your average lateness, so timing errors are relative to your own typical timing.
* The area is assumed to map onto the whole display.
* Rotation 90/270 is unverified.
* Every constant (cost references and weights, thresholds, the 0.6 trust level, step sizes) is an estimate,
  **not calibrated** on real players.
* The coach only reads Windows keys (`GetAsyncKeyState`); elsewhere it cannot judge clicks.
* Each live change rebuilds OTD's filter pipeline (a tiny hitch is possible); the OTD GUI keeps its own copy
  of the settings, and pressing Save there overwrites the coach's live values (see `COMPANION.md`).

## Tests

All synthetic (`tests/playsim.py` simulates a player with a known gain, bias, lag, jitter and timing noise);
nothing touches osu!, a real driver or the user's files: `test_playobserve.py`, `test_optimizer.py`
(including a closed loop over 20 seeds), `test_beatmap.py`, `test_areafit.py`, `test_findings.py`,
`test_coach.py`, `test_coach_gui.py`.
