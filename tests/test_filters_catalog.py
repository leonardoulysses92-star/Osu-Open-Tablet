"""Filter catalog integrity and engine i18n coverage."""
from __future__ import annotations

import itertools
import json
import string
import unittest
from dataclasses import replace
from pathlib import Path

from osu_tablet_assistant.engine.area import recommend_area
from osu_tablet_assistant.engine.recommend import generate_candidates, refine
from osu_tablet_assistant.knowledge.filters import FilterParam, load_catalog, parse_catalog
from osu_tablet_assistant.models import (CandidateResult, DisplaySpec, FilterConfig, MapPreference,
                                         MetricsReport, PlayStyle, Priority, ProfileInfo, Rect,
                                         SkillLevel, TabletSpec, UserPreferences)

LOCALES = Path(__file__).resolve().parent.parent / "osu_tablet_assistant" / "locales"

REQUIRED = {
    "OpenTabletDriver.Plugin.NoiseReduction": {"Samples", "DistanceThreshold"},
    "TabletDriverFilters.Hawku.Smoothing": {"Latency", "Frequency"},
    "TabletDriverFilters.Devocub.Antichatter": {
        "Latency", "AntichatterStrength", "AntichatterMultiplier", "AntichatterOffsetX",
        "AntichatterOffsetY", "PredictionEnabled", "PredictionStrength", "PredictionSharpness",
        "PredictionOffsetX", "PredictionOffsetY", "Frequency"},
    "TemporalResampler": {"frameShift", "followRadius", "latency", "reverseSmoothing",
                          "extraFrames", "loggingEnabled", "Frequency"},
    "RadialFollow.RadialFollowSmoothingTabletSpace": {
        "OuterRadius", "InnerRadius", "SmoothingCoefficient", "SoftKneeScale",
        "SmoothingLeakCoefficient"},
    "RadialFollow.RadialFollowSmoothingScreenSpace": {
        "OuterRadius", "InnerRadius", "SmoothingCoefficient", "SoftKneeScale",
        "SmoothingLeakCoefficient"},
    "Saturn.Multifilter": {"reverseSmoothing", "stockWeight", "rInner", "smoothDist", "sepMult",
                           "aResponse", "dacOuter", "areaScale", "xMod", "interp", "wireMode",
                           "frameShift", "msOverride", "ExGate", "Frequency"},
    "Saturn.AreaRoundingDFC": {"threshold", "dPower", "xDiv", "yDiv", "xMul", "yMul"},
    "Kuuube_s_Chatter_Exterminator.Kuuube_s_CHATTER_EXTERMINATOR_RAW":
        {"Chatter_Extermination_Strength"},
    "Kuuube_s_Chatter_Exterminator.Kuuube_s_CHATTER_EXTERMINATOR_SMOOTH":
        {"Chatter_Extermination_Strength"},
}


def _locale(lang: str) -> dict[str, str]:
    return json.loads((LOCALES / lang / "engine.json").read_text(encoding="utf-8"))


def _fields(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def _all_engine_reasons():
    """Reasons from a broad sweep of the engine's public functions."""
    catalog = load_catalog()
    tablets = (TabletSpec("small", 152, 95, 133), TabletSpec("big", 216, 135, 266),
               TabletSpec("tiny", 60, 40, 200))
    displays = (DisplaySpec(1920, 1080, refresh_hz=60), DisplaySpec(1920, 1200, refresh_hz=240))
    baselines = (None, [MetricsReport("stillness", 100, 2.0, jitter_rms_mm=0.2),
                        MetricsReport("rate", 100, 1.0, effective_rate_hz=60.0)],
                 [MetricsReport("stillness", 100, 2.0, jitter_rms_mm=0.001)])
    plugin_sets = (set(), set(catalog), {"TabletDriverFilters.Hawku.Smoothing"},
                   {"OpenTabletDriver.Plugin.NoiseReduction"})
    current = ProfileInfo("t", "abs", Rect(1920, 1080, 960, 540), Rect(80, 45, 76, 47.5), True,
                          (FilterConfig("Some.Unknown.Filter", True, {}),))
    reasons = []
    for tablet, display, baseline, plugins, style, prio, extra in itertools.product(
            tablets, displays, baselines, plugin_sets, PlayStyle, Priority, (0, 1, 2)):
        prefs = UserPreferences(play_style=style, priority=prio,
                                skill_level=list(SkillLevel)[extra],
                                map_preference=list(MapPreference)[(extra + 1) % 3],
                                notices_jitter=extra == 1, notices_input_lag=extra == 2,
                                preferred_area_width_mm=(None, 5.0, None)[extra])
        reasons += recommend_area(tablet, display, prefs)[1]
        cands = generate_candidates(tablet, display, prefs, plugins, baseline, current)
        for cand in cands:
            reasons += cand.reasons
            refined = refine(cand, [CandidateResult(cand.id)], prefs, plugins)
            if refined is not None:
                reasons += refined.reasons
    rotated = replace(current, tablet_area=Rect(80, 45, 76, 47.5, 90.0))
    for cand in generate_candidates(tablets[0], displays[0], UserPreferences(), set(), None, rotated):
        reasons += cand.reasons
    return reasons


class CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = load_catalog()

    def test_required_filters_and_real_property_names(self) -> None:
        for path, names in REQUIRED.items():
            with self.subTest(path=path):
                self.assertIn(path, self.catalog)
                self.assertEqual({p.name for p in self.catalog[path].params}, names)

    def test_defaults_within_bounds_and_stable_under_clamp(self) -> None:
        for spec in self.catalog.values():
            for p in spec.params:
                with self.subTest(path=spec.path, param=p.name):
                    if p.kind in ("float", "int"):
                        self.assertLessEqual(p.min, p.default)
                        self.assertLessEqual(p.default, p.max)
                        self.assertGreater(p.step, 0)
                    self.assertEqual(p.clamp(p.default), p.default)

    def test_frequency_params_are_in_hz_with_1000_default(self) -> None:
        for path in ("TabletDriverFilters.Hawku.Smoothing", "TabletDriverFilters.Devocub.Antichatter",
                     "TemporalResampler", "Saturn.Multifilter"):
            param = self.catalog[path].param("Frequency")
            self.assertIsNotNone(param)
            self.assertEqual(param.unit, "Hz")
            self.assertEqual(param.default, 1000.0)
            self.assertLessEqual(param.min, 100)
            self.assertGreaterEqual(param.max, 1000)

    def test_metadata_is_honest(self) -> None:
        for spec in self.catalog.values():
            with self.subTest(path=spec.path):
                self.assertTrue(spec.sources)
                self.assertTrue(all(s.startswith("https://") for s in spec.sources))
                self.assertIn(spec.confidence, ("high", "medium", "low"))
                self.assertIn(spec.stage, ("raw", "pre", "post"))
                self.assertTrue(spec.best_for)
        self.assertTrue(self.catalog["Saturn.Multifilter"].advanced)
        self.assertTrue(self.catalog["Saturn.AreaRoundingDFC"].advanced)
        self.assertFalse(self.catalog["TabletDriverFilters.Hawku.Smoothing"].advanced)

    def test_catalog_keys_exist_in_both_languages(self) -> None:
        es, en = _locale("es"), _locale("en")
        for spec in self.catalog.values():
            for key in (spec.summary_key, spec.added_latency_note):
                self.assertIn(key, es)
                self.assertIn(key, en)

    def test_load_catalog_returns_a_fresh_dict(self) -> None:
        self.catalog.clear()
        self.assertTrue(load_catalog())


class ClampTests(unittest.TestCase):
    def test_numeric_clamp(self) -> None:
        p = FilterParam("x", "float", 1.0, 0.0, 10.0, 0.5)
        self.assertEqual(p.clamp(12), 10.0)
        self.assertEqual(p.clamp(-3), 0.0)
        self.assertEqual(p.clamp(2.3), 2.5)
        self.assertEqual(p.clamp(float("nan")), 1.0)
        self.assertEqual(p.clamp(float("inf")), 1.0)
        self.assertEqual(p.clamp("abc"), 1.0)

    def test_int_bool_enum(self) -> None:
        self.assertEqual(FilterParam("n", "int", 5, 2, 20, 1).clamp(7.6), 8)
        self.assertIsInstance(FilterParam("n", "int", 5, 2, 20, 1).clamp(7.6), int)
        self.assertIs(FilterParam("b", "bool", False).clamp(1), True)
        enum = FilterParam("e", "enum", "a", choices=("a", "b"))
        self.assertEqual(enum.clamp("b"), "b")
        self.assertEqual(enum.clamp("zzz"), "a")


class MalformedCatalogTests(unittest.TestCase):
    @staticmethod
    def _doc(**param_overrides) -> dict:
        param = {"name": "Latency", "kind": "float", "default": 2.0, "min": 0.0, "max": 50.0,
                 "step": 0.5, "unit": "ms"}
        param.update(param_overrides)
        return {"filters": [{
            "path": "X.Y", "display_name": "X", "plugin_name": "X", "params": [param],
            "added_latency_note": "k", "best_for": ["smoothing"], "summary_key": "k",
            "sources": ["https://example.org"], "confidence": "high"}]}

    def test_valid_minimal_document(self) -> None:
        self.assertIn("X.Y", parse_catalog(self._doc()))

    def test_rejects_bad_documents(self) -> None:
        bad = [
            {},
            {"filters": [{"path": "X.Y"}]},
            self._doc(default=99.0),
            self._doc(kind="complex"),
            self._doc(min=10.0, max=1.0),
            self._doc(kind="bool", default=3),
            self._doc(kind="enum", default="q", choices=["a"]),
        ]
        no_sources = self._doc()
        no_sources["filters"][0]["sources"] = []
        bad.append(no_sources)
        bad_conf = self._doc()
        bad_conf["filters"][0]["confidence"] = "certain"
        bad.append(bad_conf)
        dup = self._doc()
        dup["filters"].append(dup["filters"][0])
        bad.append(dup)
        for doc in bad:
            with self.subTest(doc=doc), self.assertRaises(ValueError):
                parse_catalog(doc)


class EngineLocaleTests(unittest.TestCase):
    def test_same_keys_and_placeholders_in_both_languages(self) -> None:
        es, en = _locale("es"), _locale("en")
        self.assertEqual(set(es), set(en))
        for key in en:
            self.assertTrue(key.startswith("engine."), key)
            self.assertEqual(_fields(es[key]), _fields(en[key]), key)

    def test_every_reason_key_exists_and_formats(self) -> None:
        es, en = _locale("es"), _locale("en")
        reasons = _all_engine_reasons()
        self.assertTrue(reasons)
        for reason in {(r.key, tuple(sorted(r.params.items()))) for r in reasons}:
            key, params = reason[0], dict(reason[1])
            for lang, catalog in (("es", es), ("en", en)):
                with self.subTest(key=key, lang=lang):
                    self.assertIn(key, catalog)
                    catalog[key].format(**params)   # no missing placeholder
        # No dead reason text: every engine.reason.* entry is reachable.
        used = {r.key for r in reasons}
        self.assertEqual({k for k in en if k.startswith("engine.reason.")} - used, set())

    def test_candidate_label_keys_exist(self) -> None:
        es, en = _locale("es"), _locale("en")
        tablet, display = TabletSpec("t", 152, 95), DisplaySpec(1920, 1080)
        current = ProfileInfo("t", "abs", Rect(1920, 1080, 960, 540), Rect(80, 45, 76, 47.5),
                              True, ())
        for cand in generate_candidates(tablet, display, UserPreferences(), set(), None, current):
            for key in (cand.label_key, cand.summary_key):
                self.assertIn(key, es)
                self.assertIn(key, en)
        self.assertIn("engine.candidate.refined.summary", es)


if __name__ == "__main__":
    unittest.main()
