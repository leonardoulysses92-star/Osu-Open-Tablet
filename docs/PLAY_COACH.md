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
| **Only** the click buttons you chose (default **Z and X**; the pen tip only if you say you click with it) through `GetAsyncKeyState`. Other keys are never queried. | Hits, misses, score or combo as osu! computes them. |
| The title of osu!'s own window (to know which map). The title of any other window is never read. | Anything about spinners, slider bodies or slider ends. |

Only click **times** are stored (in memory, for the current session). Nothing leaves the PC.

Hits and misses are **inferred**:

1. the `.osu` file is found from the window title (`osu!  - Artist - Title [Difficulty]`) in your `Songs`
   folder, or you pick it by hand;
2. click times are aligned with the hit objects (greedy one-to-one matching inside the hit window);
3. at each click the cursor position is compared with the object centre.

Only circles and slider heads are judged.

## Your click buttons (and the pen tip)

Not everybody plays with Z and X, so the buttons are a choice ("Your click buttons" card on the prepare screen,
one line with the chosen keys and "Change"):

* **Data model** (`osu_tablet_assistant/keys.py`, stored in the assistant's `config.json`):
  `click_keys` = the keyboard keys and extra buttons (default `["z", "x"]`, at most 6, unknown names dropped,
  never containing `mouse1`) and `pen_clicks` = "I click with the pen tip" (default **off**). The keys that are
  read are `click_keys`, plus `mouse1` only when `pen_clicks` is on. `KeyChoice` keeps the rules (no duplicates,
  never zero ways to click, `mouse1` typed by hand turns into the pen switch). Old config files without these
  fields load with the defaults.
* **Why the pen tip is off by default**: OpenTabletDriver maps the pen tip to the left mouse button (`mouse1`)
  by default. For a keyboard player every touch of the tablet would look like a click and ruin the alignment.
  The pen barrel buttons are usually mouse 2 / 3 in OTD; they only count if you add them.
* **Detect my buttons**: while detection is on, ordinary Tk `<KeyPress>` / `<ButtonPress>` bindings on the
  **coach window only** collect the keys you press (shown as chips); Esc or "Done" ends it. There is no global
  hook and nothing is polled for unknown keys: presses in other programs are never seen. The left button is not
  taken from detection (it is how you press "Done"); use the pen checkbox for it. Right / middle / side mouse
  buttons are `mouse2` / `mouse3` / `mouse4` / `mouse5`. Lone Shift/Ctrl/Alt presses are ignored.
* **Type them**: a small fold-out takes "z, x, mouse4"; nothing changes while a word is not understood.
  Names: letters, digits, `space enter tab shift ctrl alt capslock`, `f1`-`f12`, arrows, punctuation
  (`semicolon comma period slash quote lbracket rbracket minus equal backslash backtick`, or the characters),
  numpad `num0`-`num9`, `mouse1`-`mouse5`.
* Every change applies to the next round and is saved at once (a failed save is ignored: the choice still
  works for this session).
* **"I saw no click with Z, X: do you use other buttons?"**: shown while observing when the cursor has moved
  about two screen heights in 6 s without one click of the chosen keys, and on the report of a round without
  clicks, with a button that goes back to the card in detection mode (what was recorded is dropped).
* The prepare checklist warns when no key is chosen and, with the pen tip on, explains that the tip must be set
  to left click in OTD (the coach cannot check that).

Only key **names** and press times are kept, in memory, never logged elsewhere.

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
| Press times of your chosen click buttons only (named in the app, e.g. "Z, X") | match clicks to the map | memory |
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
* **Grid and hotspots** (`engine/hotspots.py`): the playfield is cut into rows x columns, set by "Detail of
  the miss map": Normal 3x4, **Fine 6x8 (default)**, Very fine 9x12. Fine cells hold few objects, so they are
  not read one by one:
  1. the part of the error the whole play already explains is removed first (the reach fit: gain where its
     interval excludes 1, plus the bias; else the median error). What is left is the *local* error;
  2. each cell is pooled with its 3x3 neighbours (empirical Bayes: weight `spread / (spread + sigma^2/n)`,
     neighbours pulled towards zero when sparse); cells with fewer than 3 clicked objects are "no data"
     (uncoloured on the map, no arrow, never in a sentence);
  3. neighbouring cells whose pooled error is at least 0.35 mm and points the same way are merged, and the
     region is kept only when all its objects together are clearly off: at least 5 objects, at least
     `max(0.8 mm, 20 % of the circle radius)` and 4 standard errors. It gets a centroid (u, v and playfield),
     a size in tablet mm, `small / medium / large`, and a kind: `short` (cursor nearer the centre), `over` or
     `off` (with a direction).
  Findings then say, in plain words, e.g. "Abajo, a la derecha (cerca de la esquina): te quedas corto ≈ 1.3 mm
  (zona pequeña)": thirds (top / halfway / bottom), quarters (left, a little left of centre, a little right
  of centre, right), "near the corner / edge". A hotspot replaces the coarser edge finding it falls in; still
  at most 4 findings.
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
  3. **local** (only when 1 and 2 have nothing to do): the hotspots' errors, each weighted by its share of
     the clicked objects, so one small corner patch moves the area by a fraction of a millimetre, i.e. not
     at all (below the 0.5 mm minimum step); opposite regions cancel; it needs 20 objects inside the hotspots
     and never moves more than 1.5 mm. When nothing is worth changing but a hotspot exists, the proposal is
     "none" with "the problem is only in a small zone (where): play that zone more";
  4. **filters**, only through `adapt.step()` (plugin policy and safety limits of the companion):
     lag on fast jumps -> "more responsive", jitter on slow parts -> "steadier at rest", both -> nothing;
  5. **area trial ("probe")**, only when none of the above has evidence (see below).
* **Area trials** (`optimizer.ProbeState`). A real player's round is noisy, so the stages above often have no
  significant evidence and, before, the coach ended with "nothing to change" while the area never moved. Now
  the point estimates pick a small trial instead:
  * hints: the reach bias per axis (shift, mm in the player's view) and `gain - 1` per axis plus 0.5 % for
    every edge zone that falls short (-) / goes over (+) (size; one "both" hint with the aspect locked, one per
    axis with the free proportion). Hints below 0.1 mm / 0.3 % mean "no hint": nothing is tried. When the only
    problem is a small zone (a hotspot) the local rule still wins ("practise that zone"): a whole-area trial
    cannot fix one zone, so hotspot errors are not used for trials;
  * step: the hint, between 0.5 mm and **1.5 mm** for a shift and between 1 % and **3 %** for a size change,
    times the trust region (0.1 mm / 0.5 % grid). Shifts go through `areafit.shift_area` in the player's view,
    so 180 stays 180 and "right" is the right of the screen; the area never leaves the tablet;
  * the proposal says plainly that it is a trial ("Probamos mover el área 1,2 mm a la derecha; si no mejora, lo
    devolvemos") and the next report says what happened ("...: no mejoró tu ronda, así que lo devolvemos" /
    "...: tu ronda mejoró, así que lo dejamos");
  * judging: the round played on the trial is compared with the round before it. **Kept** when the cost
    improved by more than `max(2 %, noise)` (the same threshold as the stop rule), otherwise **put back**. A
    put-back trial is undone and, in the same step, the next one is tried from the previous area: the opposite
    direction, then both directions at half the step, then the next hint. After a kept trial the same
    direction is tried again (never straight back);
  * bounds: at most **4 trial rounds per family** (shift, size) per coach session; a put-back or skipped trial
    is never repeated; kept trials never add up to more than one cap (1.5 mm per axis, 3 %) from where probing
    started; an evidence-based change, a revert, or a change by hand starts the trials over from a fresh
    estimate (budgets stay used). The plateau rule still applies: in practice, when nothing helps, it stops
    after **two** trials (the trial and its opposite) and the area goes back to where it was ("probe_back");
  * skipping a trial that would also put a failed trial back still puts that area back; undoing a trial
    marks it as tried.
  * In the simulations (`tests/test_optimizer.py`, noisy player with 1.5 mm aim scatter, 90-object map, 10
    seeds): a player whose area is 5 % too big on x and 2 mm off got a changed area after the first round in
    10/10 seeds (mostly by the ordinary shift/scale stages: that error is big enough to be significant) and
    ended better than it started in 10/10; a well-tuned player got 21 trial rounds in total, 20 put back and 1
    kept (by chance, a 2 % size change), ended exactly where it started in 8/10 seeds and never further than
    the caps (largest final change 2 % / 0.3 mm), and the plateau rule stopped every run within 3-6 rounds.
* **Trust region**: halved when a round is worse or a movement reverses; a movement that was undone is not
  proposed again; the area never leaves the tablet; if a round is worse than the best by more than the
  noise, the proposal is to go back to the best round's settings.
* **Stop rule** (`plateau`): the last two accepted rounds each improve the cost by less than
  `max(2 %, noise)`, or nothing useful is left to move. With too little data (few matched clicks, bad
  alignment) the verdict is `insufficient`, nothing changes and the message is "play more of the same map".

## Your area in numbers, and by hand

* The prepare and report screens show the area as OTD stores it ("Área actual: 80 × 45 mm, centro 76,6 /
  45,2 mm, girada 180°", the same Width / Height / X / Y as in OTD) and, once anything changed since the coach
  opened, "antes → ahora" for width, height and centre. The report also says what the proposed change would
  make it. Text helpers: `coachcore.area_summary`, `area_changes`, `fmt_num` (decimal comma in Spanish).
* **"Ajustar el área a mano"** (a fold-out in the area card, its open state remembered): size -/+ 2 % (the
  proportion kept), move left / right / up / down 0.5 mm in the **player's view** (left = left of the
  screen, also with rotation 180, through `areafit`), and with the free proportion on, width and height
  -/+ 2 % separately (then `LockAspectRatio` is set to false, as for proposals). Each press is one live change
  through the `LiveTuner` (bounded to the tablet, undoable with "Deshacer", the first one makes the backup),
  updates the numbers and the outline on the tablet map, and is **not saved** until "Guardar estos ajustes"
  (which now also appears before the first round once something changed). A round played after a change by
  hand is recorded as `manual` (not attributed to a proposal).

## Smoothing: you decide

Many players have smoothing filters installed but switched off (OTD shows them unticked); the coach used to
leave them alone ("tune only what is enabled") and had nothing to tune. Now:

* the prepare screen has a **"Suavizado" card**, collapsed by default (its open state remembered), that lists
  the catalog's ordinary smoothing filters (`engine/smoothing.offered`: not "advanced", not prediction-like;
  i.e. Radial Follow tablet/screen, Hawku Smoothing, Hawku Noise Reduction, Devocub Antichatter, Temporal
  Resampler, Chatter Exterminator) with one plain sentence of what each does, how it feels, the estimated
  extra delay (`recommend.chain_latency`, rough) and a **"Probar"** switch. Installed = the session's plugin
  scan, the filters in the profile and the other filters of the same plugins; the rest are shown greyed with
  "no instalado" and a hint that they are added in OTD's Plugin Manager. Nothing is ever installed;
* "Probar" switches the filter on live (undoable, not saved): a store already in the profile is switched on
  with the player's own values when they are inside the catalog bounds; an installed filter missing from the
  profile is inserted at its chain position (`adapt.insert_in_chain`) with **gentle starting values**
  (heuristic, documented in `engine/smoothing.py`): Radial Follow tablet 0.5 mm / coefficient 0.9, screen 5 px /
  0.9, Hawku 3 ms, Noise Reduction 10 samples / 0.3 mm, Devocub 4 ms, Temporal Resampler without extra latency.
  Prediction is never switched on (Devocub prediction off; Temporal Resampler `frameShift` never above its
  neutral 0.5) and is not mentioned in the texts;
* after a round played **with** it, the report shows **"Con el suavizado: mejor / igual / peor"** (the cost of
  that round against the last accepted round without it, with the same threshold as the round marks; "not
  enough data" or "no round without it to compare with" when that is so) and two buttons: **"Dejarlo"** (it
  stays live and is saved with "Guardar estos ajustes") and **"Quitarlo"** (switched off again with the values
  it had, or taken out of the chain). Nothing about the choice is stored anywhere else;
* the optimizer never switches smoothing on or off by itself for this: its filter stage is unchanged (it may
  tune the values of a filter the player switched on, like any enabled filter), and "back to the best round"
  keeps the smoothing the player chose as it is live.

## The window

1. **Prepare**: is osu! open, which map, your click buttons (one line; "Change" opens detection, reset,
   the pen-tip box, typing by hand and the detail of the miss map), the tablet map with the current area, the
   area card (numbers, "antes → ahora", "Ajustar el área a mano"), the "Suavizado" card, one
   big "Start observing" button (global hotkey `Ctrl+Alt+P` too).
2. **Observing**: a small always-on-top window with the live tablet map ("Finding the rhythm..." until the
   alignment quality reaches 0.6). It stops by itself after ~8 s without clicks once there are 20 or more.
   An optional click-through mini-map can sit in a screen corner (off by default; it is **not visible in
   exclusive full screen**, use borderless).
3. **Report**: tablet heat map, what happened to the trial of the previous round (if any), "Con el
   suavizado" (if a smoothing trial was played), at most 4 findings in plain words, the single proposed change,
   the area card and the buttons "Apply and play again" (main), "Skip this change", "Undo last change".
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
* Whether `GetAsyncKeyState` sees the **clicks OpenTabletDriver injects** for the pen tip / pen buttons is
  **unverified** (it should, as they are sent as ordinary input, but it was not tested with a real tablet). If
  you play with the pen tip and the coach says it saw no click, use a replay (`.osr`) as the round.
* "Detect my buttons" only works while the coach window is focused (by design: nothing global).
* **Fine grids need many objects.** In the simulations (aim scatter 0.3-0.6 mm, a 2 mm undershoot only in
  the bottom-right 15 % x 15 % of the playfield, about 2 % of the objects): with ~300 judged objects the Fine
  and Very fine grids found it in 4-5 of 5-6 seeds, as a "small zone" of about 5-7 mm at the right place
  (each found region held 5-15 objects); with 150 objects it was found only once or twice in six; the Normal
  3x4 grid never isolated it (the patch is diluted in a large cell). Well-aimed simulated plays gave no hotspot
  at any grid (every seed tried, several dozen). Rule of thumb: about 300 circles for "Fine", 600 for "Very fine" before small zones can
  be trusted; with fewer objects the coach simply says nothing about them.
* Each live change rebuilds OTD's filter pipeline (a tiny hitch is possible); the OTD GUI keeps its own copy
  of the settings, and pressing Save there overwrites the coach's live values (see `COMPANION.md`).
* **Area trials are heuristic.** The caps (1.5 mm, 3 %), the minimum steps, the hint floors, the 0.5 % per
  edge zone and the budget of 4 trials per family are estimates, not calibrated. One round is noisy: a trial
  that truly helps a little is often put back (it has to beat the noise of a single round), and now and then
  one that does not help is kept by chance (bounded by the caps). Whether the new area really *feels* better
  was not verified with a real pen and osu!.
* **Smoothing trials are heuristic too.** The gentle starting values, the delay estimates and the
  descriptions are reasonable starting points written from the plugins' documentation, not measured with this
  user's tablet; the "better / same / worse" line compares two single rounds (possibly on different maps) and
  says nothing about how it feels. That is why the player decides.

## Tests

All synthetic (`tests/playsim.py` simulates a player with a known gain, bias, lag, jitter and timing noise);
nothing touches osu!, a real driver or the user's files: `test_playobserve.py`, `test_optimizer.py`
(including a closed loop over 20 seeds, the local-problem rules, the area-trial logic and two noisy closed
loops: an area 5 % too big and 2 mm off, and a well-tuned player that must not drift), `test_coach.py` (area
texts, changes by hand, smoothing trials through a fake `LiveTuner`), `test_coach_gui.py` (the new cards, both
languages), `test_hotspots.py` (a player who stops
short only in a small corner patch: found by the fine grids, smeared by the coarse one, no false alarms,
speed), `test_keys.py` (names, virtual-key codes, Tk events, the choice), `test_beatmap.py`, `test_areafit.py`,
`test_findings.py`, `test_coach.py`, `test_coach_gui.py` (the click-button card with synthetic key events).
