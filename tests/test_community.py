"""engine.community + plugin policies, including a profile like the user's real one."""
from __future__ import annotations

import unittest

from osu_tablet_assistant.engine import community as comm
from osu_tablet_assistant.engine import recommend as rec
from osu_tablet_assistant.engine.recommend import generate_candidates, refine
from osu_tablet_assistant.knowledge.filters import load_catalog
from osu_tablet_assistant.knowledge.presets import load_presets
from osu_tablet_assistant.models import (CandidateResult, DisplaySpec, FilterConfig, PluginPolicy,
                                         ProfileInfo, Rect, TabletSpec, UserPreferences)

CATALOG = load_catalog()
BUNDLE = load_presets()
CTL472 = TabletSpec("Wacom CTL-472", 152.0, 95.0, 133.0)
DISPLAY = DisplaySpec(1920, 1080, 960, 540, refresh_hz=240)
USER_RADIAL = FilterConfig(rec.RADIAL_TABLET, True, {
    "OuterRadius": 0.71, "InnerRadius": 0.37, "SmoothingCoefficient": 0.37,
    "SoftKneeScale": 0.38, "SmoothingLeakCoefficient": 0.26})
# Like the user's real setup: CTL-472, 71.66 x 41.44 mm, rotated 180 (left-handed), Radial Follow.
USER_PROFILE = ProfileInfo("Wacom CTL-472", "OpenTabletDriver.Desktop.Output.AbsoluteMode",
                           Rect(1920, 1080, 960, 540), Rect(71.66, 41.44, 76.0, 47.5, 180.0), True,
                           (USER_RADIAL,))
INSTALLED = {rec.RADIAL_TABLET, rec.RADIAL_SCREEN, rec.RESAMPLER, rec.HAWKU, rec.NOISE_REDUCTION,
             rec.ANTICHATTER}
USER_AREA = Rect(71.66, 41.44, 76.0, 47.5, 180.0)


def prefs(policy: PluginPolicy, **kw) -> UserPreferences:
    return UserPreferences(plugin_policy=policy, preferred_area_width_mm=71.66, **kw)


def paths(cand) -> list[str]:
    return [f.path for f in cand.changes.filters]


class AdaptationTests(unittest.TestCase):
    def test_mixmg_preset_scales_with_area(self) -> None:
        dever = BUNDLE.by_id("dever-mixmg")            # 80 x 60, Mixmg output
        (fc,), reasons = comm.adapt_preset(dever, USER_AREA, CATALOG)
        s = fc.settings
        self.assertAlmostEqual(s["OuterRadius"], round(0.7152 * 71.66 / 80, 6))
        self.assertAlmostEqual(s["InnerRadius"], round(0.39 * 41.44 / 60, 6))
        self.assertAlmostEqual(s["SmoothingCoefficient"], round(0.39 * 41.44 / 60, 6))  # signature kept
        self.assertEqual(s["SoftKneeScale"], 0.38357)                                   # dimensionless
        keys = [r.key for r in reasons]
        self.assertIn("engine.reason.community_scaling_rule", keys)
        self.assertEqual(keys.count("engine.reason.community_scaled"), 3)

    def test_hand_tuned_coefficient_not_scaled_and_similar_area_kept(self) -> None:
        z9a = BUNDLE.by_id("z9a-radial")               # 85 x 47, coefficient != inner
        (fc,), _ = comm.adapt_preset(z9a, Rect(42.5, 23.5, 0, 0), CATALOG)
        self.assertEqual(fc.settings["SmoothingCoefficient"], 0.1)
        self.assertAlmostEqual(fc.settings["OuterRadius"], 0.25)
        (fc,), reasons = comm.adapt_preset(z9a, Rect(86, 47.5, 0, 0), CATALOG)
        self.assertEqual(fc.settings["OuterRadius"], 0.5)
        self.assertIn("engine.reason.community_area_similar", [r.key for r in reasons])

    def test_unknown_area_uses_values_as_given(self) -> None:
        user = BUNDLE.by_id("user-provided-radial-follow")
        (fc,), reasons = comm.adapt_preset(user, USER_AREA, CATALOG)
        self.assertEqual(fc.settings, USER_RADIAL.settings)
        self.assertIn("engine.reason.community_area_unknown", [r.key for r in reasons])

    def test_safety_rules(self) -> None:
        nixie = BUNDLE.by_id("nixie-tr-devocub-prediction")
        filters, reasons = comm.adapt_preset(nixie, USER_AREA, CATALOG)
        devocub = next(f for f in filters if f.path == rec.ANTICHATTER)
        self.assertIs(devocub.settings["PredictionEnabled"], False)
        self.assertEqual(devocub.settings["Frequency"], 1000.0)
        self.assertEqual(devocub.settings["AntichatterStrength"], 10.0)
        keys = {r.key for r in reasons}
        self.assertTrue({"engine.reason.community_prediction_off", "engine.reason.community_frequency_reset",
                         "engine.reason.community_clamped", "engine.reason.community_flag_prediction"} <= keys)
        # research order: smoothing before the resampler
        self.assertEqual([f.path for f in filters], [rec.ANTICHATTER, rec.RESAMPLER])

    def test_screen_space_pixels_not_scaled(self) -> None:
        forum = BUNDLE.by_id("forum-screen-space-radial-2024")
        (fc,), reasons = comm.adapt_preset(forum, USER_AREA, CATALOG)
        self.assertEqual(fc.settings["OuterRadius"], 20.0)
        self.assertIn("engine.reason.community_px_unscaled", [r.key for r in reasons])


class UsabilityTests(unittest.TestCase):
    def test_missing_plugins_and_tablet_specific(self) -> None:
        saiyku = BUNDLE.by_id("saiyku-full-area-hover")    # Reconstructor on a Huion
        installed = INSTALLED | {rec.RECONSTRUCTOR}
        self.assertEqual(comm.unusable_reason(saiyku, PluginPolicy.INSTALLED, INSTALLED, USER_PROFILE,
                                              CTL472, CATALOG), "missing_plugin")
        self.assertEqual(comm.unusable_reason(saiyku, PluginPolicy.INSTALLED, installed, USER_PROFILE,
                                              CTL472, CATALOG), "other_tablet")
        huion = TabletSpec("Huion H1161", 279, 157, 266)
        self.assertIsNone(comm.unusable_reason(saiyku, PluginPolicy.INSTALLED, installed, None, huion, CATALOG))
        self.assertEqual(comm.unusable_reason(BUNDLE.by_id("mrekk-liquipedia-2024"), PluginPolicy.INSTALLED,
                                              INSTALLED, None, CTL472, CATALOG), "no_filters")
        self.assertEqual(comm.unusable_reason(BUNDLE.by_id("cupsizefan-devocub"), PluginPolicy.TUNE_ONLY,
                                              INSTALLED, USER_PROFILE, CTL472, CATALOG), "not_in_profile")

    def test_select_includes_requested_first_and_respects_limit(self) -> None:
        chosen = comm.select_presets(BUNDLE.presets, CTL472, USER_AREA, USER_PROFILE, PluginPolicy.INSTALLED,
                                     INSTALLED, CATALOG, limit=3, include=["community:z9a-radial"])
        self.assertEqual(chosen[0].id, "z9a-radial")
        self.assertEqual(len(chosen), 3)
        for preset in chosen:
            self.assertLessEqual(preset.required_paths, INSTALLED)

    def test_models(self) -> None:
        self.assertEqual(comm.tablet_models("Wacom CTL-472 (profile) / Wacom CTL-4100"), {"CTL472", "CTL4100"})
        self.assertIsNone(comm.same_tablet(BUNDLE.by_id("user-provided-radial-follow"), CTL472))


class PolicyTests(unittest.TestCase):
    def test_tune_only_keeps_the_users_chain_and_changes_one_value(self) -> None:
        cands = generate_candidates(CTL472, DISPLAY, prefs(PluginPolicy.TUNE_ONLY), INSTALLED, None,
                                    USER_PROFILE, BUNDLE.presets)
        ids = [c.id for c in cands]
        self.assertNotIn("no-filter", ids)
        self.assertEqual(ids[:2], ["tune-less", "tune-more"])
        self.assertEqual(ids[-1], "keep-current")
        self.assertTrue(any(i.startswith("community:") for i in ids))
        for cand in cands:
            with self.subTest(cand.id):
                self.assertEqual(paths(cand), [rec.RADIAL_TABLET])
                self.assertEqual(cand.changes.tablet_area.rotation, 180.0)
        less = cands[0].changes.filters[0].settings
        changed = {k for k in less if less[k] != USER_RADIAL.settings[k]}
        self.assertEqual(changed, {"OuterRadius"})
        self.assertLess(less["OuterRadius"], 0.71)
        change = next(r for r in cands[0].reasons if r.key == "engine.reason.tune_change")
        self.assertEqual((change.params["old"], change.params["param"]), (0.71, "OuterRadius"))
        dever = next(c for c in cands if c.id == "community:dever-mixmg")
        self.assertIn("engine.reason.community_tune_only", [r.key for r in dever.reasons])

    def test_tune_only_without_profile_still_returns_a_candidate(self) -> None:
        cands = generate_candidates(CTL472, DISPLAY, prefs(PluginPolicy.TUNE_ONLY), INSTALLED, None, None)
        self.assertEqual([c.id for c in cands], ["no-filter"])
        self.assertEqual(cands[0].reasons[0].key, "engine.reason.tune_only_no_profile")

    def test_installed_uses_only_detected_plugins(self) -> None:
        installed = {rec.RADIAL_TABLET, rec.RESAMPLER}
        for policy in (PluginPolicy.INSTALLED, PluginPolicy.ALLOW_NEW):
            cands = generate_candidates(CTL472, DISPLAY, prefs(policy, notices_jitter=True), installed,
                                        None, USER_PROFILE, BUNDLE.presets)
            for cand in cands:
                if cand.id != "keep-current":
                    self.assertLessEqual(set(paths(cand)), installed, cand.id)
            self.assertIn("no-filter", [c.id for c in cands])

    def test_user_only_filters_never_auto_picked(self) -> None:
        everything = set(CATALOG)
        user_only = {p for p, s in CATALOG.items() if not s.auto_recommend}
        self.assertTrue({rec.FRENET, rec.SCUFFED} <= user_only)
        for policy in PluginPolicy:
            for cand in generate_candidates(CTL472, DISPLAY, prefs(policy, notices_jitter=True), everything,
                                            None, USER_PROFILE, BUNDLE.presets):
                self.assertFalse(set(paths(cand)) & user_only, cand.id)
                for fc in cand.changes.filters:
                    if fc.path == rec.ANTICHATTER:
                        self.assertIs(fc.settings.get("PredictionEnabled", False), False)

    def test_allow_new_suggests_missing_plugins_with_sources(self) -> None:
        suggestions = comm.plugin_suggestions(BUNDLE.presets, CTL472, USER_AREA, USER_PROFILE,
                                              {rec.RADIAL_TABLET}, CATALOG, wanted=[rec.RESAMPLER])
        self.assertEqual(suggestions[0].path, rec.RESAMPLER)
        self.assertEqual(suggestions[0].reason.key, "engine.suggest.reason_engine")
        for s in suggestions:
            self.assertTrue(s.homepage.startswith("https://"))
            self.assertNotIn(s.path, {rec.RADIAL_TABLET, rec.FRENET, rec.SCUFFED})

    def test_refine_never_adds_filters(self) -> None:
        for policy in PluginPolicy:
            p = prefs(policy)
            for cand in generate_candidates(CTL472, DISPLAY, p, INSTALLED, None, USER_PROFILE, BUNDLE.presets):
                nxt = refine(cand, [CandidateResult(cand.id)], p, INSTALLED)
                if nxt is not None:
                    self.assertEqual(paths(nxt), paths(cand))
        refined = refine(next(c for c in generate_candidates(
            CTL472, DISPLAY, prefs(PluginPolicy.INSTALLED), INSTALLED, None, USER_PROFILE, BUNDLE.presets)
            if c.id == "community:dever-mixmg"), [], prefs(PluginPolicy.INSTALLED), INSTALLED)
        self.assertTrue(refined.id.startswith("community:dever-mixmg|"))

    def test_community_candidate_label_and_reasons(self) -> None:
        cands = {c.id: c for c in generate_candidates(CTL472, DISPLAY, prefs(PluginPolicy.INSTALLED), INSTALLED,
                                                      None, USER_PROFILE, BUNDLE.presets)}
        dever = cands["community:dever-mixmg"]
        self.assertEqual(comm.label_params(dever), {"name": "Dever"})
        keys = [r.key for r in dever.reasons]
        self.assertEqual(keys[:3], ["engine.reason.community_source_undated",
                                    "engine.reason.community_confidence_high",
                                    "engine.reason.community_starting_point"])
        self.assertIn("community", dever.tags)
        user = cands["community:user-provided-radial-follow"]
        self.assertEqual(user.label_key, "engine.candidate.community_user.label")


class ComparisonTests(unittest.TestCase):
    def test_compare_lists_differences_with_source(self) -> None:
        best = next(c for c in generate_candidates(CTL472, DISPLAY, prefs(PluginPolicy.TUNE_ONLY), INSTALLED,
                                                   None, USER_PROFILE) if c.id == "tune-less")
        rows = comm.compare_with_best(best, BUNDLE.presets, CTL472, USER_PROFILE, CATALOG, limit=4)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows, sorted(rows, key=lambda r: (r.distance, r.preset.id)))
        for row in rows:
            self.assertIn(row.lines[0][0], ("engine.compare.area", "engine.compare.area_unknown"))
        all_keys = {k for row in rows for k, _ in row.lines}
        self.assertTrue(all_keys & {"engine.compare.param", "engine.compare.only_theirs",
                                    "engine.compare.same", "engine.compare.filters_unknown",
                                    "engine.compare.only_mine"})


if __name__ == "__main__":
    unittest.main()
