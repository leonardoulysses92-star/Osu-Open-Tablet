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
a play. `engine/replay.py` reads the one the player picks ("Use a replay (.osr)" on the prepare screen, "Check
with the replay" on the report) and, after a live round, the newest one osu! wrote since the play started, only
to check the cursor and the map (see "Map recognition and pointer accuracy"):

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
| Your playing habits, as summary numbers only (value, error, count per habit; the area and smoothing they were measured on; date; a random session id) | adapt the setup to the way you play | `habits.json` on this PC, deleted with "Olvidar mis hábitos" |

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
engine.habits               -> HabitProfile  the player's habits in this play (value, SE, n, confidence)
engine.habitstore           -> HabitView     pooled over plays (habits.json on this PC), carried to this setup
engine.rulebook             -> Suggestion    the 18 community rules: one explained change, or "keep"
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

## Map recognition and pointer accuracy

Everything below reads FILES or window geometry only; osu!'s memory is never read and no tablet setting is
changed by it.

* **Map index** (`engine/osudb.py`, `engine/mapindex.py`). osu!stable's `osu!.db` is parsed once (all format
  generations: byte vs single AR/CS/HP/OD before/after 20140609, the per-entry size before 20191106, Int-Double vs
  Int-Float star tables before/from 20250107; the star and timing tables are skipped arithmetically) into lookups
  by the exact `Artist - Title [Version]` (romanised and Unicode), a loose normalised key, and the MD5. It is parsed
  again (in a background thread) only when its mtime or size changes. A truncated or corrupt file gives what was
  read and `complete=False`, never an exception. Maps missing from it (imported this session) are found by a
  **Songs delta**: one `os.scandir`, headers read (with the existing JSON cache) only for folders osu!.db does not
  list or that changed after it, plus, on a miss, known folders whose name contains the missed song title.
  Ambiguous titles keep every candidate (`MapFinder.candidates`); the newest is used and the replay MD5 confirms it
  afterwards. Without `osu!.db` the old full header scan is used. **lazer**: `%APPDATA%/osu/files/*/*/*` (or
  `storage.ini`'s `FullPath`) is indexed by sniffing `osu file format v` (UTF-8 BOM allowed), with MD5, in a cache
  keyed by the SHA-256 file name, incrementally in a daemon thread with a progress count.
* **Title polling** (`gui/playwatch.py`, `gui/coach.py`): the title is read every 100 ms; the osu! process id is
  cached for 3 s (the process listing is the costly part), so an unchanged title costs one `EnumWindows`. A new
  title is one dictionary lookup. A title that names a map while osu! seems to spectate, show a replay or the
  editor (`title_kind`) keeps the current map and only counts once keys are pressed; otherwise the round says so.
* **Pre-roll** (`PlayRecorder.arm` / `start(since=...)` / `begin_at`): while osu!'s title names a map before
  "Start", the recorder keeps the last 10 s of cursor and key samples in memory, so a late start begins at the
  title change. While observing, a new play title starts the capture there.
* **Sampler back-fill** (`gui/pen_sampler.py`): after a poll gap above 2 ms, `GetMouseMovePointsEx` positions are
  inserted (negative multi-monitor coordinates fixed up as documented, de-duplicated by `(x, y, time stamp)`;
  stamps of uncertain resolution are kept inside the gap or spread evenly). `PenSampler.stats` gives polls, the
  largest gap and the back-filled count; the report mentions them when a gap reached 20 ms or points were filled.
* **Playfield from osu!'s window**: `GetClientRect` + `ClientToScreen` of osu!'s window give the game area; the
  640x480 virtual screen is fitted 4:3 inside it (`PlayCapture.playfield`). Without it, full screen on the display
  is assumed as before.
* **osu!.<user>.cfg** (`engine/osucfg.py`): only `BeatmapDirectory`, `Fullscreen`, sizes, letterboxing,
  `RawInput`, `MouseSpeed`, `AbsoluteToOsuWindow`, `FrameSync`/`CustomFrameLimit`, `keyOsuLeft/Right` and
  `ConfineMouse` are kept (the name and password lines are skipped). `pointer_trust` says "needs_calibration" with
  raw input plus "map to window" or a sensitivity other than 100 %, or a frame limit under 200 fps.
* **Replay calibration** (`engine/calibrate.py`, after each live round): the newest `.osr` written since the play
  started (`Replays/`, `Data/r/`, lazer's store) is lined up with the live recording: clock offset by
  cross-correlating key-down edges (the replay's speed first, then 1.0 / 1.5 / 0.75), then `p_playfield = A *
  p_desktop + b` by least squares. The report gets one factual line: "La transformación se confirmó con el
  replay", "El cursor del juego no coincide con el del sistema: ..." (A not a scaled identity, or a large
  residual), or the measured shift / size of the playfield; plus whether the replay's MD5 confirms the map.
  `engine/replay.py` was checked against the format: the LZMA "alone" stream, the `-12345` seed frame and
  stable's two `(256, -500)` lead frames (now skipped; their time still counts).

**Verified by tests** (synthetic files only; `tests/osubuild.py` writes osu!.db, cfg and .osr bytes):
`test_osudb.py` (every format generation, boundaries, truncated / corrupt / lying counts, 4000 entries well under
a second here), `test_mapindex.py` (exact / Unicode / loose / MD5 lookups, ambiguity, reload only on change, the
delta, background loading, the lazer store and its cache, fallbacks), `test_osucfg.py`, `test_calibrate.py`
(offset, DT with and without the mod flag, raw-input-like and noisy transforms, a shifted playfield, mirrored HR
frames, the newest replay in stable and lazer folders, the report lines), `test_pointer_tracking.py` (pid cache,
title kinds, the ring buffer, back-fill with fine and coarse stamps, the window box, the CoachCore wiring).

**Still needs a real machine** (from the research note's "Unknown / needs testing" list):
1. Whether the stable gameplay playfield has a vertical offset relative to (64, 48) (the replay fit measures it).
2. Stable letterboxing: units and range of `LetterboxPositionX/Y` and how they move the playfield.
3. The `GetMouseMovePointsEx` time stamp resolution, and whether it records SendInput and VMulti / Windows Ink moves.
4. When stable writes `osu!.db` (on exit only?): watch its mtime during a session that imports a map.
5. Whether the stable title uses romanised or Unicode metadata, and its exact spacing; the same for lazer.
6. Whether stable saves an `.osr` in `Data/r` for failed or unranked plays.
7. Whether lazer's files-store replay is a byte-exact `.osr`, and whether sniffing finds every difficulty.
8. The HR replay coordinate convention (flipped or not) in stable and lazer.
9. Real parse time of `osu!.db` and lazer store indexing for 10k+ maps on SSD and HDD.
10. Real polling jitter of the 1.5 kHz thread while osu! and Tk run.
11. Whether the window title is set during spectating or replay watching in stable (the `title_kind` words are
    guesses), and whether osu!'s client rectangle matches the game area in every display mode.

## Limits (be honest with yourself)

* **Never verified against a real pen and a real osu!** here. The synthetic tests prove the logic recovers
  known offsets, gains and shifts from simulated players; they do not prove the alignment works in a real
  game.
* The playfield transform is an approximation; **osu!lazer or windowed play may fail** (the coach warns, and
  the alignment quality tells you). For lazer the map comes from its hashed store, indexed the first time in the
  background (untested with a real lazer install); pick the `.osu` by hand if it is not found.
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

## Playing like a coach: habits and rules

The coach now tries to act like an experienced player who sets up your tablet: it learns your **habits** and
adapts the **setup** to them, so you are comfortable and precise *the way you already play*. It never asks you
to change your technique; the texts describe ("Sueles quedarte un poco corto en saltos largos hacia la
derecha") and say what the setup does about it ("por eso el área baja un 3 %").

### What is measured (`engine/habits.py`, pure, no Tk)

From one round: the aligned clicks, the cursor trace (~1.5 kHz live) and the map. Only circles and slider
heads (never spinners, slider bodies or tails). Every habit has a value, a standard error, a sample count, a
"significant" flag (`|value| > 2 SE`) and a confidence (low / medium / high). Spatial habits are in **circle
radii**.

* **One joint fit** of the click errors, `e = a + M·p + c·d̂ + K·j` (weighted least squares; streams weigh 0.3,
  one pass of outlier removal): `a` = constant aim bias (rule 4), `M` diagonal + `K` = reach gain per axis
  (`gain_x` / `gain_y`, their mean `size`, their difference `aspect`: rule 1/2 and the data-driven route to a
  non-16:9 area, rule 5), `(M21 - M12)/2` = **rotation** (rule 6), `c` = a constant "stop at the circle edge"
  term along the jump that does not grow with distance (rule 2: the area is never changed for it), `K` = the
  distance-proportional overshoot. In the simulations the fit separates them: an injected 0.06 overshoot comes
  back as size 0.055 with no edge stop, an injected -0.8 mm edge stop as -0.80 mm with size < 1 %.
* `long_jump.<dir>` (descriptive), `trail` (along-the-jump error vs jump *speed* at fixed distance: smoothing
  first, rule 3), `ur`, `key_gap` and, from replays only (their clock is the map's), `timing_bias` (rule 15:
  the global offset, never the area), `tap_lead` (aim-before-tap vs tap-before-arrive: a style), `jitter`
  (second-difference, i.e. high-passed, RMS of the still cursor minus a normal 0.05 mm) and `jitter_hover`
  (breaks vs play: a pen held high, rule 8), `drift_*`, `fatigue` (last vs first third), `edge_clamp.<side>`,
  `pen_lift`, `spikes` (EMI-like teleports: impossible speeds that come straight back; those samples and the
  clicks near them are excluded, rule 10) and the hotspots as `zone.<place>`.
* **Mods**: with a replay HR/DT are known and checked against what the alignment inferred (a mismatch is said);
  live they were inferred, so no habit goes above "medium". Relax / Autopilot: nothing is measured.

### Pooling (`engine/habitstore.py`)

Plays (rounds and replays of the same player) are combined by **inverse-variance weighting**. Habits that
depend on the setup are first carried over to the current one (aim bias minus the shift made since, gains times
the size ratio, rotation minus the rotation added), so a change the coach already made is never counted twice;
trail and jitter only pool plays with the same smoothing. Two views: this session, and the last 90 days.
`habits.json` sits next to `config.json`, is versioned, written atomically, a corrupt file is set aside as
`habits.corrupt.json` (a file from a newer version is left untouched), and **"Olvidar mis hábitos"** deletes it.

A change is only recommended when the habit is **significant** (> 2 SE), **repeated** (the same way, by more
than its own SE, in at least **2 plays**, with **50+** objects / jumps in total; "high" needs 5 plays, as S20's 5
replays / 50 jumps), this session does not clearly say the opposite, and it **plausibly costs hits**: a
systematic error of that size on top of your own scatter misses at least 0.5 % more objects (Rice
distribution). Otherwise: keep the current setting (rule 16).

### Rules (`engine/rulebook.py`) and how proposals changed

The 18 rules of the research are data (`RULES`, with their S-numbers) and `suggest()` turns the pooled habits
plus the area, filters and tablet into ordered suggestions, each with a step inside the research caps (about
**3 %** of size, **≤ 1.5 mm** of shift, **≤ 2°** of rotation per round), a coach-voice reason in Spanish and
English ("Un jugador con experiencia haría el área un 3 % más grande: ..."), a confidence and its sources.

Order: interference (10) and the **adaptation window** (11: the area changed outside the coach in the last 3
days, or you left heavy smoothing ≥ 8 ms in the last 10 days: "dale tiempo a tu mano", nothing changes) block
everything; then smoothing before the area (18: two smoothing filters on → the heavier is switched off; 3: the
cursor trails on fast jumps with a filter on → one level lighter, or off); the offset before the size (4, then
13 for one pinned edge); rotation (6); one axis (5, only with the free proportion); size (1/2, never for an
early tapper: "falling short" is then timing); a bigger area for big shake (9); the hover-height note (8)
before the lightest Radial Follow (7, ≤ 3 ms, only when no smoothing is on); notes (15 offset, 12 hardware
smoothing, the edge stop of rule 2); keep (16). Rule 14 (discomfort) needs you to say it and is data only.

In the optimizer the rulebook is **stage 0**, right after "back to the best round" and the stop rule: the first
rule change that can be applied (not just undone, within the trust region, on the tablet) becomes the
round's ONE proposal, as a `shift`, `scale`, `rotate` (new kind) or `filter` proposal carrying `rule` and
`sources`. Without significant, repeated habits it proposes nothing and the old stages (single-round shift /
size / local / filters / area trials) run exactly as before, so the first round of a fresh player behaves as in
0.8.x. Filters you switched on or off yourself in the smoothing card stay yours: the rules never touch them.

**Never prediction.** The only filter the rules ever switch on is Radial Follow (tablet space, lightest
values); lightening only moves down the smoothing ladder, which keeps prediction off. The reason is peppy's
osu! forum notice of **2026-10-14** (S1): smoothing and anti-jitter plugins are fine, plugins that "assist your
input" are cheating and detectable, and prediction filters are not named either way.

### The "Tu estilo de juego" card

In the report, after the proposal: a headline, then (open by default when the setup is adapting) "Lo que el
ajuste hace por ti", "Visto, pero no hace falta cambiar nada" and **"Todavía no seguro"** (low-confidence
habits, e.g. after one play), a privacy line ("se queda en este PC...") and "Olvidar mis hábitos" (asks first).

### Sources (short; reliability as in the research)

| # | What | Reliability |
|---|---|---|
| S1 | peppy, "Important note regarding OpenTabletDriver plugins", 2026-10-14 | official, scope vague |
| S3, S4 | OTD FAQ (EMI teleports; Windows/osu! settings) | official OTD |
| S5 | osu! wiki beginner guide (offset via hit-error bar) | official |
| S6, S8, S9 | forum: comfort / reach / adaptation threads (2014-2020) | forum anecdotes |
| S11, S12, S18 | forum: prediction usually off; no filter without a reason; days to weeks to adapt | forum |
| S14, S15, S16, S17, S19 | Hawku FAQ, Devocub, Radial Follow, Reconstructor, Temporal Resampler docs | author docs |
| S20 | osu-tablet-optimizer (3 % / 2° caps, 5 replays / 50 jumps, edge-bias term) | prior art; README says AI-written |
| S21 | osu-coach (0.15 r offset threshold, 60 % lean) | hobby tool |
| S22 | osu-aim-analyzer (overaim → bigger; fix skew first; rotate the same way) | hobby tool |
| S23, S24 | forum: 4:3 vs 16:9 area ratio | forum, pro numbers second-hand |
| S26 | AreaCoach (≤ 1.5 mm per map, 3 / 15 ms filter caps) | prior art; README says Claude-written |
| S28, S29 | forum: pen held high shakes; bigger area hides shake | forum / snippet only |
| S30 | OTD maintainers: do not stack smoothing | maintainers |

### What is NOT verified (honest list)

* **No top-player or real-player dataset**: setup.moe returned only its title and Reddit could not be fetched,
  so no rule or threshold was checked against how good players actually set up.
* **Every threshold is a heuristic, not calibrated on real plays**: 2 SE, 2 plays, 50 samples, 0.5 % miss cost,
  0.15 r, the 3 % / 1.5 mm / 2° caps (from prior-art tools, two of them AI-written), the stream weight, the spike
  speeds, the jitter "normal" of 0.05 mm, the adaptation windows (3 / 10 days, from forum anecdotes).
* Recovery was only tested on **synthetic** plays (`tests/test_habits.py`). The simulated cursor arrives
  smoothly at each target, so lag-from-smoothing (`trail`) is barely visible there and is tested on synthetic
  habit values only. Real tremor, hover height and EMI were never measured with a real pen.
* Whether a rule-based change *feels* better was not tested with a real player.

## Tests

All synthetic (`tests/playsim.py` simulates a player with a known gain, bias, lag, jitter and timing noise, and
now rotation, distance-proportional overshoot, a constant edge stop and EMI spikes); `test_habits.py` covers the
habits, the store, the rulebook (caps, keep on noise, never prediction, never two smoothing filters), the coach
wiring and the style card;
nothing touches osu!, a real driver or the user's files: `test_playobserve.py`, `test_optimizer.py`
(including a closed loop over 20 seeds, the local-problem rules, the area-trial logic and two noisy closed
loops: an area 5 % too big and 2 mm off, and a well-tuned player that must not drift), `test_coach.py` (area
texts, changes by hand, smoothing trials through a fake `LiveTuner`), `test_coach_gui.py` (the new cards, both
languages), `test_hotspots.py` (a player who stops
short only in a small corner patch: found by the fine grids, smeared by the coarse one, no false alarms,
speed), `test_keys.py` (names, virtual-key codes, Tk events, the choice), `test_beatmap.py`, `test_areafit.py`,
`test_findings.py`, `test_coach.py`, `test_coach_gui.py` (the click-button card with synthetic key events).

## More or less smoothing, with the community's recommendation

Once a smoothing filter is on in the coach ("Suavizado" card), it has five levels from "very little" to "a lot"
(`engine/smoothing.ladder`). The middle one is **what the community uses**: the median of the filter's values in
the five community presets closest to the player's tablet and area (adapted to the player's area, see
`engine/community.adapt_preset`); when fewer than two presets use that filter, cautious fixed values are used and
the card says so. The other levels scale the recommended amount (x0.5, x0.75, x1, x1.4, x2; Radial Follow also
moves its coefficient; the coefficient never goes above 0.99). "Less / More smoothing" moves one level, live,
undoable, and not saved until "Guardar estos ajustes".

**Too much smoothing warning.** The coach estimates the extra delay of all enabled filters together
(`recommend.chain_latency`). From 8 ms the card warns that the player already has quite a lot of smoothing; from
15 ms it refuses to go to a higher level (going down is always allowed) and asks to lower a level or remove a
filter. Both limits are rough heuristics, not calibrated on real players, and the delay is an estimate; the
player can still set anything in OpenTabletDriver itself. Filters without one single "amount" value (for example
Kuuube's chatter filters) have no levels.
