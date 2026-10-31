# Filter catalog audit (2026-10-30)

Scope: check `osu_tablet_assistant/knowledge/filters.json` against primary sources (plugin C# source at the tag the OTD Plugin Manager actually installs, READMEs, release notes, the OTD Plugin-Repository, OTD source). The machine-readable list of proposals from this audit (30 changes, 7 new entries; the new entries passed `knowledge.filters.parse_catalog`) was `docs/research/filters_update.draft.json`. It was removed from the tree to keep the project tidy and is still in the history at tag `v0.8.0`; check the catalog itself for what was applied.

Legend: **[src]** = read in C# source. **[prose]** = README, tooltip or release notes only. **[inf]** = my inference. **null** = unknown.

## 0. How OTD really uses settings.json (this decides "is a name right?")

| Fact | Evidence |
|---|---|
| `Path` = the class's `Type.FullName` (namespace.class). The `PluginName` attribute (UI label) is not used for matching. | [src] [PluginSettingStore.cs v0.6.8.1](https://github.com/OpenTabletDriver/OpenTabletDriver/blob/v0.6.8.1/OpenTabletDriver.Desktop/Reflection/PluginSettingStore.cs) |
| Each `Settings[].Property` is matched against the **C# property name** (`PropertyInfo.Name`). Only properties with `[Property]` count. **Unknown names are silently skipped** (no log). A missing setting falls back to `DefaultPropertyValue`. | same file, `ApplySettings` |
| `PipelinePosition` in 0.6.x: `PreTransform = 1`, `PostTransform = 2`, **`Raw = PreTransform`** and **`Pixels = PostTransform`** (aliases). 0.6.8 adds **`Internal = 4`**. | [src] [PipelinePosition.cs v0.6.8.1](https://github.com/OpenTabletDriver/OpenTabletDriver/blob/v0.6.8.1/OpenTabletDriver.Plugin/Output/PipelinePosition.cs), [v0.6.6.2](https://github.com/OpenTabletDriver/OpenTabletDriver/blob/v0.6.6.2/OpenTabletDriver.Plugin/Output/PipelinePosition.cs) |
| Execution order: Internal → PreTransform → (area transform) → PostTransform. Inside one group, filters run in **list order** (`elements.Where(e => e.Position == p)`), which is the order of the profile's `Filters` array. | [src] `ElementsAsPipeline` in [OutputMode.cs v0.6.8.1](https://github.com/OpenTabletDriver/OpenTabletDriver/blob/v0.6.8.1/OpenTabletDriver.Plugin/Output/OutputMode.cs), `GroupElements` in [PipelineManager.cs](https://github.com/OpenTabletDriver/OpenTabletDriver/blob/v0.6.8.1/OpenTabletDriver.Plugin/Output/PipelineManager.cs); Saturn README tells users to reorder the `Filters` array by hand |
| Async filters inherit `Frequency` (`[Property("Frequency")]`, `Unit("hz")`, default 1000, clamped >= 1, warns below 101 Hz). It is the filter's own timer rate. | [src] [AsyncPositionedPipelineElement.cs v0.6.8.1](https://github.com/OpenTabletDriver/OpenTabletDriver/blob/v0.6.8.1/OpenTabletDriver.Plugin/Output/AsyncPositionedPipelineElement.cs) |
| Latest OTD: **v0.6.8.1** (2026-10-29). It fixes a **Windows daemon crash whenever an async filter (interpolator) was used in v0.6.8**. v0.6.8 added `FilterState` so aux buttons and touch keep working while an interpolator is enabled. | [prose] [OTD releases](https://github.com/OpenTabletDriver/OpenTabletDriver/releases) |

Consequences for the engine:
* The catalog's `raw < pre` ordering is **not real in OTD 0.6**. "raw" filters (Temporal Resampler) run wherever they sit in the list among the pre-transform filters. Do not reorder by stage; write `Filters` in the intended order. Proposal: Temporal Resampler `stage` "raw" → "pre". Use "raw" only for the real `Internal` stage (see Hover Distance Limiter), or add `"internal"` to `STAGES`.
* Recommend OTD **>= 0.6.8.1** (or <= 0.6.7) whenever an async filter is part of the chain on Windows.

## 1. Existing catalog entries

### 1.1 AbstractQbit Radial Follow (PRIORITY)

Plugin Manager: "AbstractQbit's Radial Follow Smoothing" **0.3.0.0**, the only build for OTD 0.6.0.0–0.6.8.1 ([listing](https://github.com/OpenTabletDriver/Plugin-Repository/blob/master/Repository/0.6.0.0/AbstractQbit/AbstractOTDPlugins/RadialFollow.json), [opentabletdriver.net/Plugins](https://opentabletdriver.net/Plugins)). Source read at tag `0.3.0` and on master. RadialFollow on master changed last on 2022-01-06 ("tweak radial defaults slightly"). The 2026-09-09 commit 5fc4af9 only removed the per-project `net6.0` TargetFramework ([commit](https://github.com/AbstractQbit/AbstractOTDPlugins/commit/5fc4af9)).

| UI label (what users see) | settings.json `Property` [src] | Tablet-space default | Screen-space default | Code clamp [src] | README semantics |
|---|---|---|---|---|---|
| Outer Radius | `OuterRadius` | 1.0 (mm) | 5.0 (px) | 0..1e6; effective = max(Outer, Inner+0.0001) | Max distance the cursor can lag. With leak > 0 it is where smoothing weakens, not a hard limit. |
| Inner Radius | `InnerRadius` | 0.0 (mm) | 0.0 (px) | 0..1e6 | Deadzone; must be <= Outer |
| Initial Smoothing Coefficient | `SmoothingCoefficient` | 0.95 | 0.95 | 0.0001..1 | Higher = slower descent from outer to inner = more smoothing |
| Soft Knee Scale | `SoftKneeScale` | 1.0 | 1.0 | 0..100 (<= 0.0001 = hard knee) | Softness of the transition at the outer radius, roughly logarithmic |
| Smoothing Leak Coefficient | `SmoothingLeakCoefficient` | 0.0 | 0.0 | 0..1 | Share of smoothing that continues past the outer radius |

* Paths [src]: `RadialFollow.RadialFollowSmoothingTabletSpace` (PluginName "AbstractQbit's Radial Follow Smoothing (Tablet coordinates)", `PreTransform`, mm via digitizer Width/MaxX) and `RadialFollow.RadialFollowSmoothingScreenSpace` ("... (Screen coordinates)", `PipelinePosition.Pixels` = PostTransform).
* **Verdict: every path and property name in our catalog is exactly right, and 0.95 is still the current default.** The new UI label "Initial Smoothing Coefficient" is only the `[Property("...")]` display string. The property is still `SmoothingCoefficient`. Writing the label text would be silently ignored.
* Older 0.1.x builds (OTD 0.5 only) had different defaults: inner 0.05 mm, coefficient 0.92, knee 3 ([README @ RadialFollow-0.1.3](https://github.com/AbstractQbit/AbstractOTDPlugins/blob/RadialFollow-0.1.3/README.md)). That may be where the "outdated" worry comes from. These builds do not apply to OTD 0.6.
* Behaviour [src, [RadialFollowCore.cs](https://github.com/AbstractQbit/AbstractOTDPlugins/blob/master/RadialFollow/RadialFollowCore.cs)]: synchronous (no added timer). The cursor snaps to the pen on NaN or after a >= 50 ms gap between reports. `ResetMs` is declared but unused.
* Discrepancy: our `step` 0.01 cannot represent 0.995, the value the only public osu! preset uses ([pxlwh/otd-osu-preset](https://github.com/pxlwh/otd-osu-preset), CTL-472, Outer 1 mm, coefficient 0.995). Proposal: step 0.001.

### 1.2 Other entries

| Catalog path | Path/props verified? | Discrepancies / new facts | Evidence |
|---|---|---|---|
| `OpenTabletDriver.Plugin.NoiseReduction` | **Yes [src]**. The class lives in **HawkuFilters** with namespace `OpenTabletDriver.Plugin`. PluginName "Hawku Noise Reduction". PreTransform, mm. `Samples` (UI "Buffer") default 10, clamp 0..20. `DistanceThreshold` default 0.5 mm, clamp 0..10. | `plugin_name` is wrong ("legacy built-in" → HawkuFilters). Raise confidence low → high. Tooltip: Samples 5–20, threshold 0.2–1.0 mm, up to 75 ms of latency at 133 rps with buffer 10, extra latency on areas < 20 mm. Threshold 0 divides by zero. | [NoiseReduction.cs](https://github.com/OpenTabletDriver/TabletDriverFilters/blob/master/HawkuFilters/NoiseReduction.cs) |
| `TabletDriverFilters.Hawku.Smoothing` | **Yes [src]**. `Latency` SliderProperty 0..1000, default 2. Weight 1-0.37^(1/steps). Resets after a > 100 ms gap. Tooltip: Wacom 15–25 ms ≈ Wacom-driver smoothing; osu! max ~50 ms; < 4 ms → disable instead. | None. Plugin Manager HawkuFilters 0.3.1.0 (v0.3.1). Repo last commit 2023-05-22 (out-of-range fix). | [Smoothing.cs](https://github.com/OpenTabletDriver/TabletDriverFilters/blob/master/HawkuFilters/Smoothing.cs), [HawkuFilters.json](https://github.com/OpenTabletDriver/Plugin-Repository/blob/master/Repository/0.6.0.0/OpenTabletDriver/TabletDriverFilters/HawkuFilters.json) |
| `TabletDriverFilters.Devocub.Antichatter` | **Yes [src]**. All 10 names and defaults match (PredictionEnabled has no default attribute → false). | `plugin_name` should be **DevocubFilters** (a separate Plugin Manager package, 0.3.1.0). The base weight is only computed on the first `Filter` call, so a Latency change needs re-apply. Tooltip ranges: PredictionStrength 0–2 (3–4 at high latency), PredictionOffsetX 0.5–5..7; prediction needs 10–40 ms latency. | [AntiChatter.cs](https://github.com/OpenTabletDriver/TabletDriverFilters/blob/master/DevocubFilters/AntiChatter.cs) |
| `TemporalResampler` | **Yes [src] for 1.5.1** (Plugin Manager build for OTD >= 0.6.4.0, released 07 Jun; the year is not shown on the releases page). Global namespace, `Raw` (= PreTransform). Names `frameShift, followRadius, latency, reverseSmoothing, extraFrames, loggingEnabled` match. Clamps: frameShift 0..1, latency >= 0, reverseSmoothing 0.001..1. | Stage: see §0. **Version trap:** the manager still lists **1.0.1 for OTD 0.6.3.0**, which had `frameShift`, `weight` (EMA) and `reverseSmoothing` **as a bool**. Writing our float would mismatch. Labels were renamed in 1.2.2 ("Frame Time Shift" → "Prediction Ratio", "Chatter Diameter" → "Follow Radius"). Frequency is only used in logging [src]. | [TemporalResampler.cs v1.5.1](https://github.com/shmkle/TemporalResampler/blob/v1.5.1/TemporalResampler/TemporalResampler.cs), [v1.0.1](https://github.com/shmkle/TemporalResampler/blob/v1.0.1/TemporalResampler/TemporalResampler.cs), [releases](https://github.com/shmkle/TemporalResampler/releases) |
| `Saturn.Multifilter` | **Mostly [src]**. Plugin Manager ships **1.1.0.2** (2026-10-17, OTD >= 0.6.6.2). GitHub latest is 1.1.1.0 (2026-10-23). Both have 12 props: `reverseSmoothing, stockWeight, rInner, smoothDist, sepMult, aResponse, areaScale, xMod, ExGate, interp, wireMode, frameShift`. | **`dacOuter` and `msOverride` no longer exist** (removed after 1.0.0; OTD silently ignores them). Remove both. `interp` default false matches 1.1.x (it was true in 1.0.0). `ExGate` now means "Read Config File" (`SaturnConfig/<Tablet Name>.json`). **Frequency claim is doubtful:** MultifilterCore uses `expect = 1000/Frequency` against the measured *update* interval [src]. No doc says to set it to the report rate, and OTD warns below 101 Hz. Our architecture rule "set Frequency = report rate for Multifilter only" should be dropped or confirmed with the author. | [Multifilter.cs @1.1.0.2](https://github.com/shreddism/TheSaturnCollection/blob/1.1.0.2/TheSaturnCollection/Multifilter.cs), [@1.0.0](https://github.com/shreddism/TheSaturnCollection/blob/1.0.0/TheSaturnCollection/Multifilter.cs), [MultifilterCore.cs](https://github.com/shreddism/TheSaturnCollection/blob/main/TheSaturnCollection/MultifilterCore.cs), [releases](https://github.com/shreddism/TheSaturnCollection/releases) |
| `Saturn.AreaRoundingDFC` | **Yes [src]**. PostTransform. `threshold` (UI "Radius") 720, `dPower` 1, `xDiv/yDiv/xMul/yMul` 1, all clamped >= 0. | None (confidence → high). | [AreaRoundingDFC.cs](https://github.com/shreddism/TheSaturnCollection/blob/main/TheSaturnCollection/AreaRoundingDFC.cs) |
| `Kuuube_s_Chatter_Exterminator.Kuuube_s_CHATTER_EXTERMINATOR_RAW` | **Yes [src]**. PostTransform (screen px). `Chatter_Extermination_Strength` **DefaultPropertyValue 2** (our assumed value is right). | Confidence → high. Formula is in the draft notes. Synchronous per-report gain of ~0.03 inside ~S px, raw passthrough beyond. Plugin Manager 1.1.1.0. | [Chatter_Exterminator_Raw.cs](https://github.com/Kuuuube/Kuuube-s-CHATTER-EXTERMINATOR/blob/main/Kuuube's_Chatter_Exterminator/Chatter_Exterminator_Raw.cs) |
| `..._SMOOTH` | **Yes [src]**. PreTransform. Default **6**. | No mm conversion in the source, so distances are raw tablet units [inf]. | [Chatter_Exterminator_Smooth.cs](https://github.com/Kuuuube/Kuuube-s-CHATTER-EXTERMINATOR/blob/main/Kuuube's_Chatter_Exterminator/Chatter_Exterminator_Smooth.cs) |

## 2. Filters we do not list

Plugin Manager inventory: [Plugin-Repository tree](https://github.com/OpenTabletDriver/Plugin-Repository/tree/master/Repository) and [opentabletdriver.net/Plugins](https://opentabletdriver.net/Plugins) (59 plugins). Full entries for the first seven are in the draft.

| Plugin (version, OTD) | Path [src] | Stage | Properties (default) | Fixes / latency | Recommend? |
|---|---|---|---|---|---|
| **Reconstructor** (VoiD, 0.4.1, OTD 0.6.1–0.6.8.1) | `VoiDPlugins.Filter.Reconstructor` | pre, sync | `EMAWeight` (0.5, clamp 0..1; 0 = div/0) | Undoes hardware EMA smoothing; removes latency. Tune with the wiki's "ram the pen into an edge" overshoot test. No per-tablet values are published. | Only when hardware smoothing is suspected; never together with Reverse EMA < 1 in TR/Saturn [inf]. [src](https://github.com/X9VoiD/VoiDPlugins/blob/0.4.1/src/Filter/Reconstructor/Reconstructor.cs), [wiki](https://github.com/X9VoiD/VoiDPlugins/wiki/Reconstructor) |
| **Spectral Reconstructor** (Vestaia, 1.1.5.0, OTD >= 0.6.7; SUL-1.0) | `TrajectoryEigenFilter.OTD.TrajectoryEigenFilterPlugin` (UI "A Spectral Reconstructor") | pre, async | `Frequency` (UI "Resampling frequency", 1000), `EnableAsyncMode` (true), `WindowMilliseconds` (100), `FilterStrength` (1; 1.5 for 133 Hz), `UseAdaptiveModes` (true), `OutlierThreshold` (6), `LatencySamples` (4 **reports**), `StalenessTimeoutMs` (25), `EnableCsvLogging` (false) | Antichatter "without smoothing" plus a built-in resampler. Latency = LatencySamples / report rate; 0 = zero-latency mode with ~50% rejection [prose]. Defaults are tuned for 700 Hz custom Wacom firmware. | Advanced. First release 2026-09-19, little field data. [src](https://github.com/Vestaia/Spectral-Reconstructor/blob/main/TrajectoryEigenFilter.OTD/TrajectoryEigenFilterPlugin.cs), [README](https://github.com/Vestaia/Spectral-Reconstructor/blob/main/Readme.md) |
| **Saturn HRRNC** (in Saturn 1.1.x) | `Saturn.HRRNC` | pre, sync | `noiseAmount` 5, `posSlack` 1.25, `cStrength` 0.5, `ajResponse` true, `cChain` true, `TimingSeverity` 0, `crossSeverity` 0, `vMult` 1, `timeScale` 1, `dragDist` 0, `maxDist` 0, `maxHMult` 1, `dragConf` 1 | Noise compensation for very high report-rate (modded) tablets. Tooltip: place it first. The last four props were added in 1.1.1.0 and may be absent in the 1.1.0.2 the manager ships (null/unverified). | Only for >= ~500 rps tablets [inf]. [src](https://github.com/shreddism/TheSaturnCollection/blob/main/TheSaturnCollection/HRRNC.cs) |
| **BezierInterpolator** (AbstractQbit, 0.3.1.0) | `BezierInterpolator.BezierInterp` | post, async | `SmoothingFactor` 1, `TiltSmoothingFactor` 1 (EMA weights, 1 = off), `Frequency` | Spline resampler; ~1 report of lag (code estimate). | Alternative to TR; do not stack resamplers [inf]. [src](https://github.com/AbstractQbit/AbstractOTDPlugins/blob/v0.3.1/BezierInterpolator/BezierInterp.cs) |
| **ScuffedInter/Extrapolator** (jaaakb, 1.1.1) | `ScuffedInterpolator.ScuffedInterp` | pre, async | **`_getSetScuffedInterpAmount`** 0.5, `_getScuffedAlwaysExtrap` true, `_getScuffedHalfStepReports` true, `_getScuffedLiftResetDelay` 4, `_getScuffedTabletHz` 133 (clamp 133..266), `Frequency` | Interpolation or prediction for 133–266 rps tablets. Property names are the C# names, not the labels. | Advanced. [src](https://github.com/asrfggafgsdf/scuffed-interp-extrap/blob/main/ScuffedInterp/ScuffedInterp.cs) |
| **Frenet-Serret Frame Predictor** (make-42, 1.1.0, OTD >= 0.6.7) | `FrenetSerretFramePredictor.FrenetSerretFramePredictor` | post, async | `CaptureWindowMs` 20, `LookAheadMs` 8, `D1..D5Strength` 1,1,1,0,0, `Rigidity` 1, `TabFreq` 133, `SplinePointsPerSample` 1, `IntegrationSteps` 10, `Frequency` | Predicts 8 ms + the time since the last report; the README admits extra jitter. Its attribute defaults are inconsistent (D3, IntegrationSteps). | **Never auto-recommend** (osu! policy, §4). [src](https://github.com/make-42/otd-frenet-serret-frame-predictor/blob/HEAD/FrenetSerretFramePredictor/FrenetSerretFramePredictor.cs) |
| **Hover Distance Limiter** (Kuuuube, 0.1.4.0, OTD >= 0.6.8) | `Hover_Distance_Limiter.Hover_Distance_Limiter` | **Internal** (runs before all pre) | `Hover_min` 0, `Hover_max` 255, `NearProximity`, `Pressure_min` 0, `Pressure_max` 16384, `Pen`, `Eraser` (bools have no default → false) | Drops reports outside a hover-height or pressure window. No latency. | Listed so existing profiles can be read. [src](https://github.com/Kuuuube/Hover_Distance_Limiter/blob/main/Hover_Distance_Limiter/Hover_Distance_Limiter.cs) |
| PSSA (jamesbt365, 0.2) | `PSSA.PSSA` | post | `MinPressure` 1, `MaxPressure` 8191, `MinWeight` 1, `MaxWeight` 0.5, `BaseSmoothing`, `ReverseSmoothing` | Pressure-dependent EMA, for drawing. | No: osu! hover play has no pressure. [src](https://github.com/jamesbt365/PSSA/blob/HEAD/PSSA/PSSA.cs) |
| moonlight suite (cfm-miku-en, 2.0.0.0, OTD >= 0.6.6.2) | namespace `MoonlightSuite`; class names and C# property names **not verified** (null) | all pre [prose] | 12 filters: aim, follow, resample, smooth, noise, predict, curve, spring, snap, angle, interpolate, wobble | "snap" overshoots past the pen and "angle" pulls strokes toward fixed directions [prose]. | Do not recommend "snap", "angle" or "predict" (policy risk). The rest are unverified. [FILTERS.md](https://github.com/cfm-miku-en/moonlight-suite/blob/main/FILTERS.md) |
| VelocityGuard (EncyOnCode, v2, **not in the Plugin Manager**) | null (class not read) | post [prose] | Max Dead Zone 4 px, Full Speed Threshold 6 px/ms, Knee Speed 3, Zone At Knee Speed 0.5, Velocity Smooth 4 ms, Output Smooth 0, **Lead 0.75**, Coherence Relief 0.75 (labels only) | "osu!-optimized" velocity deadzone with a lead (prediction) term. | No until the source is read and the policy is clear. [repo](https://github.com/EncyOnCode/VelocityGuard) |
| Adryzz's Custom Filter (2.3.0.0), MeL, Precision Control, Slimy Scylla, SpringInterpolator, Tablet Debounce, Rotation Tools | not audited | — | — | Expression-based, drawing-oriented, or button-related. | Out of scope for osu! cursor recommendations. |

Not found: "Voyager" (no plugin of that name in the Plugin Manager, the GitHub topic, or web search) and maintained Devocub or Temporal Resampler forks. Treat both as null.

## 3. Rules a recommender can use (with strength of evidence)

**Chain and ordering**
1. Order inside a stage = order of the `Filters` array [src §0]. The engine must emit the intended order explicitly.
2. Noise and reconstruction first, then smoothing, then resampling. Evidence: HRRNC tooltip ("should probably be first") [prose]; Spectral Reconstructor renamed with an "A " prefix "so it loads early in the Filters pipeline" [prose, v1.1.2 notes]; the public osu! preset order is CHATTER_SMOOTH → RadialFollow (tablet) → Temporal Resampler [prose, pxlwh].
3. Use **one** reverse-EMA stage: Reconstructor `EMAWeight` or TR `reverseSmoothing` or Saturn `reverseSmoothing`. All three are the same math (Saturn's tooltip says it "matches" the other two) [prose]; stacking them would double the correction [inf].
4. Use **one** resampler or interpolator: Temporal Resampler, Saturn `interp`, Bezier, Scuffed, or Spectral async mode. Each re-times the output on its own timer [inf; no author statement found].
5. Devocub Antichatter *is* a smoother (Latency is its EMA). Its prediction needs 10–40 ms of latency [src tooltip]. TR `frameShift = 1` "works best with some smoothing" [prose].
6. Leave `Frequency` at 1000 on every async filter (Hawku, Devocub, TR, Bezier, Scuffed, Frenet). OTD warns below 101 Hz [src]. Spectral: 125/250/500/1000 only [src tooltip]. Saturn: see the open question below.

**Tablet characteristics**
* **133 rps Wacom (CTL-472/672, also CTL-480/490 class)** → a resampler helps on high-refresh monitors (TR README: "very effective" at 133 rps with a 240 Hz monitor) [prose]. Spectral FilterStrength 1.5 at 133 Hz [prose].
* **No hardware smoothing (CTL-472)** → leave Reverse EMA at 1.0 [prose, community preset]. Hover is noisier than dragging, so use higher antichatter for hover play (Kuuube: RAW 2–3 drag / 5–6 hover; SMOOTH 6–7 drag / 15–16 hover) [src tooltip].
* **Hardware smoothing**: Gaomon S620 is the documented case. Saturn ships S620-specific Reverse EMA and prediction tweaks [prose, 1.0.0 notes]; an osu! forum post says the Hawku/Devocub configs are wrong for the S620 and points to Reconstructor [prose, [forum](https://osu.ppy.sh/community/forums/topics/1162625)]. **No published EMA weight for any specific tablet** (null). The only procedure is the overshoot test.
* **Very high report rate / modded firmware (~700 Hz+)** → HRRNC, Spectral defaults [prose].
* Saturn auto-tunes by tablet name: PTK-x70, PTH-x60, CTL/CTH-x80/x72/x71/x70/x60, PTH-x51/PTK-x40/PTZ, CTL-4100/6100/x90, Gaomon S620 [src TSCutils.cs]. Which of these have hardware smoothing is **not stated** (null).
* Small areas: Hawku Noise Reduction warns of added latency below 20 mm [src tooltip]. Radial Follow screen-space px radii are a different physical size for each area/display ratio [inf]. Prefer the tablet-space variant for area-independent tuning.

**Sane ranges (min / typical / max)**

| Param | Range | Source |
|---|---|---|
| Hawku `Latency` | 0 (off) / 4–25 / 50 ms | [src tooltip] |
| Devocub `Latency` | 0 / 5–50 (Simple preset) / 50; `AntichatterStrength` 1 / 2–3 / 10; `AntichatterMultiplier` 1 / 1 / 1000; `OffsetX` -1 / 0 / 2; `OffsetY` -1 / 1 / 10 | [src tooltip presets] |
| Noise Reduction `Samples` / `DistanceThreshold` | 5 / 10 / 20 · 0.2 / 0.5 / 1.0 mm | [src tooltip] |
| RadialFollow (tablet) `OuterRadius` | null min / 1.0 mm / null max (no author guidance beyond the default; community preset 1 mm; one forum user 20 px in screen space) | [prose] |
| RadialFollow `SmoothingCoefficient` | 0.0001 / 0.95 (default), 0.995 (preset) / 1 | [src clamp, prose] |
| TR `frameShift` | 0 / 0.5 / 1 (1 only with smoothing) | [prose] |
| Reconstructor `EMAWeight` | >0 / start 0.5 / 1 | [prose wiki] |
| Kuuube RAW / SMOOTH strength | >0 / 2–3 (drag), 5–6 (hover) · 6–7, 15–16 | [src tooltip] |
| Saturn `aResponse` | 0 / 0 / ~2 ("above about 2 not recommended") | [src tooltip] |
| Saturn AreaRounding `dPower` | 0.5 / 1 / 2 | [src tooltip] |

## 4. osu! policy (new, important)

On **2026-10-14** peppy posted that OTD and "smoothing or anti-jitter" plugins are fine, but plugins that assist input in ways "any normal person would consider cheating" will lead to action. Affected players should remove them. **No plugin is named** ([forum topic 2254092](https://osu.ppy.sh/community/forums/topics/2254092)). Recommendation: the app should never auto-enable prediction-past-the-pen (Frenet-Serret, moonlight predict/snap/angle, VelocityGuard Lead), and should warn when it finds them in a profile. Whether Temporal Resampler or Saturn prediction (`frameShift`) is affected is **unknown (null)**.

## 5. Open questions

1. Saturn Multifilter `Frequency`: is it meant to be the report rate or the update rate? The code points to the update period. Ask shreddism before keeping the "Frequency = report rate" rule.
2. Saturn 1.1.0.2 vs 1.1.1.0 `HRRNC` property set (hover options): not verified at the 1.1.0.2 tag.
3. Spectral Reconstructor: main is assumed to equal v1.1.5 (not checked against the release zip).
4. Kuuube SMOOTH units: raw tablet units is an inference from the absence of scaling.
5. Should the engine detect the installed plugin version (e.g. TR 1.0.1 vs 1.5.1, Saturn 1.0.0 vs 1.1.x) before writing properties? OTD will not report wrong names.
6. Per-tablet hardware-smoothing facts (which models, which EMA weight): no primary source found. Keep null.
7. TR / Saturn prediction under the osu! notice: unresolved.
