# Player and community tablet configurations (research draft)

Retrieved 2026-10-30. The curated result is `osu_tablet_assistant/knowledge/community_presets.json` (31 entries). The raw research list (33 entries, `docs/research/community_presets.draft.json`) was removed from the tree to keep the project tidy; it is still in the history at tag `v0.8.0`.
This is research input for the engine, not product text. Numbers are copied as published, never rounded.

## 1. Methodology

1. **osu! rankings → profiles.** Pulled the top 100 of the osu!standard performance ranking (2026-10-30) and searched each player's own profile "me!" page for setup text (tablet, area, filter, OTD...). Where a profile embeds OTD screenshots, they were opened through osu!'s image proxy (`i.ppy.sh`) and read by eye. 33 of 100 profiles mention setup; 4 contain filter data.
2. **setup.moe** (formerly osusettings.top). It is a player-settings database where users log in with osu! and edit their own profile (`PUT /me/settings` in its client). Its public API (`api.setup.moe/users?mode=std&sort=rank`) was read for the 500 best-ranked registered players (ranks #2 to #3163). Of those, 356 have a tablet and 132 have a filter section. The site's form supports only six filters: Radial Follow (tablet coordinates only), Devocub Antichatter, Temporal Resampler, Kuuube RAW/SMOOTH and Reconstructor. Its field names are camelCase and were mapped to OTD `settings.json` names using our `filters.json` (e.g. `initialSmoothingCoefficient` → `SmoothingCoefficient`, `predictionRatio` → `frameShift`, `reverseEMA` → `reverseSmoothing`, `maximizeFrequency` → `extraFrames`).
3. **Liquipedia** (`List_of_player_tablet_settings` and per-player pages). Read through the MediaWiki API/raw wikitext because the HTML page returns 403. It covers area only, with a per-row date and a `ref` field that is often empty.
4. **Plugin sources** (GitHub raw C#) to verify paths, property names and defaults.
5. **Mixmg's Radial Follow calculator** was run in a browser with known areas to identify calculator-generated values.
6. Forums: osu! forum threads; web search for Reddit, YouTube and blogs (mostly unproductive, see section 6).

Confidence: **high** = the player's own profile or self-entered setup.moe entry; **medium** = wiki/secondary (Liquipedia with empty ref, undated screenshots); **low** = hearsay/unattributed.
setup.moe entries carry no date, so `source_date` is null there. They are current as of retrieval but may be stale.

## 2. Answers to the specific questions

### 2.1 "Initial Smoothing Coefficient" vs `SmoothingCoefficient`

* The JSON property in `settings.json` is still **`SmoothingCoefficient`**. "Initial Smoothing Coefficient" is only the UI label (`[Property("Initial Smoothing Coefficient")]` on `public double SmoothingCoefficient`). Our catalog name is correct; only the display label differs.
* History (AbstractQbit/AbstractOTDPlugins):
  * `RadialFollow-0.1.0` to `0.1.3` (Jun to Aug 2021): single class `RadialFollowSmoothing`, label "Smoothing Coefficient". Defaults were OuterRadius 1 mm, InnerRadius 0.05 mm, SmoothingCoefficient 0.92, SoftKneeScale 3, leak 0.
  * Commit `078dbd1` (2021-12-16, "Add back tablet space coord mode to RadialFollow") split it into ScreenSpace/TabletSpace classes and introduced the label "Initial Smoothing Coefficient". Plugin names: "AbstractQbit's Radial Follow Smoothing (Tablet coordinates)" / "(Screen coordinates)".
  * The first release with the new label is tag **`0.3.0` (2022-01-06)**, after "tweak radial defaults" commits on 2021-12-20 and 2022-01-06. Its defaults are the ones in our catalog: tablet 1.0/0.0 mm, screen 5.0/0.0 px, 0.95, 1.0, 0.0. `v0.3.1` (2022-09-05) is the latest tag and keeps the label.
* Sources: https://raw.githubusercontent.com/AbstractQbit/AbstractOTDPlugins/0.3.0/RadialFollow/RadialFollowSmoothingTabletSpace.cs, https://github.com/AbstractQbit/AbstractOTDPlugins/commit/078dbd1, https://raw.githubusercontent.com/AbstractQbit/AbstractOTDPlugins/RadialFollow-0.1.3/RadialFollow/RadialFollowSmoothing.cs, https://github.com/AbstractQbit/AbstractOTDPlugins/tags

### 2.2 The seed values (0.71 / 0.37 / 0.37 / 0.38 / 0.26), said to be mrekk's

* **Not found published anywhere**, and no public source gives mrekk's filters. The only mrekk data is his area: Liquipedia lists PTK-470 58 x 45.24 mm (dated 2024-08-14, no ref), and 2022 forum hearsay says CTL-472 with "hawku drivers".
* The numbers have the signature of **Mixmg's Radial Follow Smoothing Calculator** (https://mixmg.github.io/smoothing/smoothing-calculator.html, created 2024-11-03). Measured on five areas, its output is OuterRadius = 0.00894 × width, InnerRadius = SmoothingCoefficient = 0.0065 × height, plus derived knee and leak values. The page has an optional toggle that lowers the leak (×0.3 → ×0.17 in its script).
  * The seed matches the calculator for about **79.5 x 57 mm** (gives 0.71073 / 0.37050 / 0.37050 / 0.38326 / 0.26198).
  * mrekk's published area (58 x 45.24) gives 0.51852 / 0.29406 / 0.29406 / 0.43214 / 0.24753. The seed is therefore inconsistent with mrekk's known area.
* Closest real published config: **Dever (#86)**, CTL-472 80 x 60, exactly the calculator output 0.7152 / 0.39 / 0.39 / 0.38357 / 0.25419.
* Conclusion: treat the seed as a calculator-derived preset (confidence low), not as mrekk's setup.

## 3. Configurations found

Global ranks are as of 2026-10-30. RF = Radial Follow (tablet space unless noted), values listed as Outer / Inner / SmoothingCoefficient / SoftKnee / Leak. DC = Devocub Antichatter. TR = Temporal Resampler. CE = Kuuube's Chatter Exterminator.

| Player (rank) | Tablet | Area mm | Filters (exact) | Style | Source | Conf. |
|---|---|---|---|---|---|---|
| cryshina (#2) | CTL-472 | 59 x 46, rot 180 | RF 0.5364 / 0.338 / 0.338 / 0.43045 / 0.23017 (screenshot; enable box looks unticked); CE RAW 3 (unticked) | drag | osu! profile screenshots, undated (≥ 2024-11) | high |
| cryshina (#2) | CTL-472 | 61 x 49 | RF 0.54534 / 0.32175 / 0.42558 / 0.42558 / 0.24091 | drag, raw off | setup.moe/u/93 | high |
| mrekk (#1) | PTK-470 | 58 x 45.24 | unknown | – | Liquipedia 2024-08-14 | medium |
| (seed, unattributed) | ? | ? | RF 0.71 / 0.37 / 0.37 / 0.38 / 0.26 | – | user screenshot; = Mixmg for ~79.5 x 57 | low |
| superi0r (#16) | CTL-472 | 42 x 30 | none (all listed filters disabled) | hybrid | setup.moe/u/1183 | high |
| enri (#22) | CTL-472 | 60 x 48 (Liquipedia) / 71 x 55 (setup.moe) | not stated | hybrid | Liquipedia 2026-08-14 ref=player | medium |
| ferret irl (#24) | CTL-472 | full 152 x 95 + Circular Area (Schwarz-Christoffel) | CE SMOOTH 10 | drag | profile + setup.moe/u/1579 | high |
| saiyku (#26) | Huion H1161 | full 280 x 157 | Reconstructor EMAWeight 0.31 | hover | setup.moe/u/3806 | high |
| ChocoPafe (#28) | CTL-672 | 70 x 36 | TR frameShift 0, reverse 1; RF 0.2 / 0.271 / 0.271 / 0.543 / 0.181 (inner > outer, probably a typo) | hover | setup.moe/u/3031 | high |
| grrmondays (#30) | CTL-480 (same area on PTK-470) | 65.4 x 44.24, rot 0 | DC Latency 6, Strength 1.4, Mult 1, OffX 0, OffY 1, prediction off, Frequency 600 | hybrid | profile screenshot, undated | medium |
| [Karcher] (#43) | CTH-480 | 96 x 54 | DC Latency 1, Strength 5, Mult 1, OffX 0, OffY 1, prediction off, Freq 1000 | – | profile, 2021-09-13 (STALE) | high |
| gwb (#47) | CTL-472 / CTL-4100 | 40 x 22.5 | "opentabletdriver no filter" | drag | profile + setup.moe/u/1542 | high |
| z9a (#49) | PTH-660 | 85 x 47 | RF 0.5 / 0.3 / 0.1 / 0.2 / 0.1 | drag | setup.moe/u/1717 | high |
| Yseri (#55) | CTL-472 | 61 x 47 | TR frameShift 0, reverseSmoothing 0.8 | hybrid | setup.moe/u/236 | high |
| -bouingue (#66) | CTL-472 | 76.3 x 46 | RF 0.682 / 0.299 / 0.299 / 0.385 / 0.293 (Mixmg) | drag, raw on | setup.moe/u/785 | high |
| xootynator (#71) | CTL-680 | full 216 x 135 | not stated | raw off | Liquipedia list + profile | medium |
| Dever (#86) | CTL-472 | 80 x 60 | RF 0.7152 / 0.39 / 0.39 / 0.38357 / 0.25419 (Mixmg) | drag | setup.moe/u/2202 | high |
| big_tatik (#128) | CTL-472 | 50 x 32 | TR frameShift 0; RF 0.633216 / 0.271 / 0.271 / 0.543 / 0.181 | drag | setup.moe/u/1024 | high |
| Fleh (#136) | PTK-470 | 82 x 48 | RF 0.7 / 0.5 / 0.3 / 0.4 / 0.2 (same as 8581210 #208) | drag | setup.moe/u/2364 | high |
| CUPSIZEFAN (#166) | CTL-472 | 80 x 50 | DC Latency 8, Strength 3, defaults otherwise | drag | setup.moe/u/1393 | high |
| origin_ (#175) | XP-Pen G640S | 55 x 41 | DC Latency 4, Strength 4, OffY 0.5 + RF 0.5 / 0.3 / 0.1 / 0.2 / 0.1 | hybrid | setup.moe/u/1673 | high |
| Pabloniichan (#178) | CTL-472 | 91 x 62 | CE RAW 5 + CE SMOOTH 6 | drag | setup.moe/u/2113 | high |
| Exxotl (#178) | CTL-672 | full 216 x 135 | RF 1 / 1 / 0.95 / 1 / 0 | drag | setup.moe/u/422 | medium |
| Kwal1976 (#185) | CTL-470 | 40 x 22 | TR reverseSmoothing 0.85 | drag | setup.moe/u/1772 | high |
| woojungx4 (#232) | XP-Pen Deco Mini7 V2 | 190 x 135 | DC Latency 16, Strength 9 | hover | setup.moe/u/325 | high |
| BossPlays (#300) | CTL-472 | 67 x 49 | CE RAW 2 + DC 1.4 ms / Str 5 / Freq 500 + CE SMOOTH 1 | drag | setup.moe/u/1894 | high |
| Pregnancy (#379) | PTH-860 | 280 x 137.5 | DC Latency 12, Strength 3 | hover | setup.moe/u/1790 | high |
| nixie (#398) | CTL-472 | 53 x 35 | TR (extraFrames false) + DC 1 ms / Str 20 / OffX 0.7 / OffY 0.6 / prediction ON (0.3, 0.7, 2, 0.3) / Freq 500 | drag | setup.moe/u/2583 | high |
| BTMC | PTK-870 | full 216 x 135 | unknown (OTD 0.6.7) | – | Liquipedia 2026-06-19 | medium |
| Rafis | PTK-470 | 95.5 x 56.965 | unknown | – | Liquipedia 2025-10-31 ref=player | medium |
| Vaxei | CTH-480 | 115 x 64.68, rot 0 | unknown (polling 125) | – | Liquipedia 2023-11-30 (STALE) | medium |
| Mathi | CTL-480 | 110 x 61.88, rot 0 | unknown (OTD 0.6.3.0) | – | Liquipedia 2023-11-06 (STALE) | medium |
| forum user yellowbadbeast | ? | ? | RF **screen space** OuterRadius 20 px | – | osu! forum 2024-09-03 | low |

Totals: 33 JSON entries. 24 have actual filter values, 22 are high confidence, 9 medium, 2 low.

Other area-only data points (not in JSON) from setup.moe/Liquipedia: jahkon #12 CTL-472 58 x 36; freezes #31 CTL-472 58 x 38; atlas #40 CTL-472 87 x 54; Akolibed (#4) XP-Pen G640S 68 x 54 (Liquipedia); haxor profile screenshot 78.26 x 59.6 (tablet ambiguous).

## 4. Consensus patterns (with evidence)

**Filter usage among top players (setup.moe, 132 entries with a filter section in the top-500 registered sample):**
* 129 of 132 enable at least one filter. The other 224 tablet entries leave the section empty, which means "not stated", not "no filter". The data is therefore biased toward filter users.
* Enabled counts: Radial Follow (tablet) 57, Temporal Resampler 47, Devocub Antichatter 44, CE SMOOTH 14, CE RAW 9, Reconstructor 2.
* Explicit no-filter top players exist: superi0r #16, gwb #47. mrekk, enri, BTMC and Vaxei: unknown.

**Radial Follow (n=57 enabled):** quartiles Q1 / median / Q3:
* Outer 0.5364 / 0.633 / 1.0 mm
* Inner 0.229 / 0.271 / 0.302 mm
* SmoothingCoefficient 0.263 / 0.2925 / 0.95
* SoftKnee 0.415 / 0.531 / 1.0
* Leak 0 / 0.176 / 0.2

31 of 57 have InnerRadius == SmoothingCoefficient, the Mixmg-calculator signature, so the calculator dominates current practice. Hand-tuned sets recur (0.5/0.3/0.1/0.2/0.1 and 0.7/0.5/0.3/0.4/0.2). Top players use much smaller radii than the plugin default (1.0 mm / 0.95).

**Devocub (n=44 enabled):** quartiles Q1 / median / Q3:
* Latency 2 / 5 / 8 ms
* Strength 2 / 3.5 / 5
* Multiplier 1 (all quartiles)
* OffsetY 0 / 0.6 / 1
* Frequency 500 / 1000 / 1000

Prediction is almost always off. Only nixie has it on, and grrmondays annotates it "never use". The 2021 osu! forum says prediction is "usually kept disabled" (https://osu.ppy.sh/community/forums/topics/1321828). Heavy settings (12 to 16 ms) appear only with hover or large areas.

**Temporal Resampler (n=47 enabled):** median frameShift 0, reverseSmoothing 1, followRadius 0, latency 0. In practice it is used as a plain interpolator for 133 rps tablets (CTL-472/672) on high-refresh monitors, matching the README example "133rps ... 240Hz". The second most common use is undoing hardware smoothing (reverseSmoothing 0.8 to 0.95). Reconstructor guide: start EMA 0.5, raise it if the cursor overshoots (https://github.com/X9VoiD/VoiDPlugins/wiki/Reconstructor, 2021-05-18).

**Chatter Exterminator:** medians RAW 5 / SMOOTH 6. README: RAW 2-3 drag, 5-6 hover; SMOOTH 6-7 drag, 15-16 hover.

**Area by tablet (setup.moe, Q1 / median / Q3 width x height, mm):**
* CTL-472, n=182: W 58 / 65 / 80, H 39 / 45 / 50
* CTL-672, n=42: W 60 / 70 / 100, H 41 / 49 / 58
* CTL-480, n=31: W 64 / 70 / 95, H 42 / 48 / 62
* PTK-470, n=19: W 63 / 82 / 96, H 43 / 49 / 63
* CTH-480, n=13: W 59 / 70 / 80, H 38 / 50 / 54
* Gaomon S620, n=9: median 66 x 51

Full area is a recognisable minority (BTMC, xootynator, saiyku, ferret irl, Exxotl).

**Playstyle and osu!:** among 356 setup.moe tablet entries, playstyle is drag 263, hybrid 52, hover 41. Raw input is off for 228 and on for 85. Almost all use absolute mode.

**Tablet space vs screen space:** every top-player Radial Follow found is tablet space. setup.moe cannot record screen space, so this is partly a site artefact; the only screen-space example is a forum post (20 px). cryshina's profile tells players to configure the filter "manually to your own area", i.e. radii in mm scale with area.

**Advice heard repeatedly:**
* Each filter adds latency, so use one or two at most and none if there is no jitter: KANZAKI / kwll7rse, osu! forum 2026-08-22, https://osu.ppy.sh/community/forums/topics/2237821.
* Devocub strength 1.0 (flow aim) to 1.6 (sharp patterns): grrmondays profile.
* Hawku smoothing: below ~4 ms disable, ~50 ms max for osu!: plugin tooltip, already in `filters.json`.

## 5. Caveats

* setup.moe is self-reported and undated. Some entries look like typos or untouched defaults (ChocoPafe inner > outer; cryshina coefficient == knee; Pabloniichan 5/6 = form defaults; Exxotl near-defaults). Filter order is not recorded.
* The same player can have conflicting sources (cryshina: 59 x 46 vs 61 x 49; enri: 60 x 48 vs 71 x 55; gwb: CTL-472 vs CTL-4100). Both are kept, with notes.
* Screenshot-derived values (cryshina, grrmondays, Karcher, haxor) were read visually from cropped images. The "Enable" checkbox state in cryshina's crops is ambiguous.
* grrmondays's Frequency advice ("x2 monitor refresh") conflicts with our catalog note that Devocub's Frequency is the filter's internal timer. Do not adopt it without checking.
* Ranks change daily. Liquipedia rows with an empty `ref` are unverified.
* Mixmg-derived presets scale with area by construction, so they should be recomputed for each user's area rather than copied.

## 6. Sources that could not be accessed

* https://osuconfigs.com/players/mrekk: HTTP 522 (site down) via fetch and browser.
* https://liquipedia.net/osu/List_of_player_tablet_settings: HTML 403. Worked around through `api.php?action=parse` and `action=raw`.
* osu! Tablet Top List Google Sheet (https://docs.google.com/spreadsheets/d/1oNO5jVTn3N_1OXPcLkculmBnO83s4vJ2V4DUdyUZ-Nk): export returned 410 Gone. The 2022 forum thread exists (https://osu.ppy.sh/community/forums/topics/1561347).
* Reddit (r/osugame, r/OpenTabletDriver): fetch tool and browser both refused.
* Direct i.ibb.co images: hot-link placeholder. Read through the osu! image proxy instead.
* osu! news "The Followpoint: mrekk" (2024-05-06): article body not rendered for the fetcher.
* Not attempted, or no indexable results: Twitter/X posts, Twitch panels, YouTube descriptions, OTD Discord, Kuuube's Discord. No searchable written guide by Kuuube on osu! filter values was found; his site lists hardware buying sheets only.
