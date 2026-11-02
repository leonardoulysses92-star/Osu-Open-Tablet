"""Habits (engine.habits), their store (engine.habitstore), the rulebook (engine.rulebook) and the style card.

All synthetic: simulated players with known injected habits (tests/playsim.py); nothing touches osu!, a real
driver or the user's files (stores live in temporary folders or in memory).
"""
from __future__ import annotations

import json
import math
import tempfile
import tkinter as tk
import unittest
from dataclasses import replace
from pathlib import Path

from osu_tablet_assistant.engine import habits as hb, habitstore as hs, optimizer as opt, rulebook as rb
from osu_tablet_assistant.engine.playobserve import analyze_play
from osu_tablet_assistant.engine.recommend import HAWKU, RADIAL_TABLET, RESAMPLER
from osu_tablet_assistant.knowledge.filters import load_catalog
from osu_tablet_assistant.models import FilterConfig, ProfileInfo
from playsim import AREA, TABLET, Player, make_map, simulate

MAP = make_map(seed=3, n=200, gap_ms=300)
CATALOG = load_catalog()
_CACHE: dict = {}


def profile_of(player: Player, seed: int = 5, mods: hb.ModInfo | None = None) -> hb.HabitProfile:
    key = (repr(player), seed, mods)
    if key not in _CACHE:
        capture = simulate(MAP, player, seed=seed)
        _CACHE[key] = hb.analyze_habits(MAP, capture, analyze_play(MAP, capture), mods)
    return _CACHE[key]


def view_of(player: Player, seeds=(5, 6)) -> hs.HabitView:
    """Two plays of the same player pooled in a memory-only store (what the rulebook needs to act)."""
    store = hs.HabitStore(None, clock=lambda: 1_000_000.0)
    setup = hs.Setup.of(AREA)
    for seed in seeds:
        store.add(profile_of(player, seed), setup, "s1")
    return store.view(setup, profile_of(player, seeds[0]).r_mm)


def ctx(**kw) -> rb.Context:
    kw.setdefault("catalog", CATALOG)
    kw.setdefault("tablet", TABLET)
    return rb.Context(AREA, **kw)


NOISY = dict(aim_noise_mm=1.0)          # realistic scatter (~0.3 radii), so a systematic error costs misses


class HabitRecoveryTests(unittest.TestCase):
    """Known injected habits come back with the right sign and size."""

    def test_a_clean_player_has_no_significant_aim_habit(self) -> None:
        p = profile_of(Player())
        self.assertTrue(p.usable)
        for key in (hb.AIM_X, hb.AIM_Y, hb.SIZE, hb.ASPECT, hb.ROTATION, hb.EDGE_STOP):
            self.assertFalse(p.get(key).significant, key)
        self.assertFalse(p.emi)
        self.assertGreater(p.get(hb.TAP_LEAD).value, 0)          # arrives, then taps

    def test_constant_offset(self) -> None:
        p = profile_of(Player(bias_x_mm=1.0, bias_y_mm=-0.6))
        self.assertAlmostEqual(p.get(hb.AIM_X).value, 1.0 / p.r_mm, delta=0.05)
        self.assertAlmostEqual(p.get(hb.AIM_Y).value, -0.6 / p.r_mm, delta=0.05)
        self.assertTrue(p.get(hb.AIM_X).significant)
        self.assertFalse(p.get(hb.SIZE).significant)

    def test_overshoot_proportional_to_distance_is_not_an_edge_stop(self) -> None:
        p = profile_of(Player(overshoot=0.06))
        self.assertAlmostEqual(p.get(hb.SIZE).value, 0.06, delta=0.015)
        self.assertAlmostEqual(p.get(hb.OVERSHOOT).value, 0.06, delta=0.015)
        self.assertFalse(p.get(hb.EDGE_STOP).significant)

    def test_constant_edge_stop_is_separated_from_size(self) -> None:
        p = profile_of(Player(edge_stop_mm=-0.8))
        self.assertAlmostEqual(p.get(hb.EDGE_STOP).value, -0.8 / p.r_mm, delta=0.08)
        self.assertLess(abs(p.get(hb.SIZE).value), 0.01)

    def test_horizontal_only_undershoot(self) -> None:
        p = profile_of(Player(gain_x=0.95))
        self.assertAlmostEqual(p.get(hb.GAIN_X).value, -0.05, delta=0.01)
        self.assertLess(abs(p.get(hb.GAIN_Y).value), 0.015)
        self.assertTrue(p.get(hb.ASPECT).significant)
        self.assertLess(p.get(hb.ASPECT).value, -0.03)

    def test_rotation(self) -> None:
        p = profile_of(Player(rotation_deg=2.5))
        self.assertAlmostEqual(p.get(hb.ROTATION).value, 2.5, delta=0.4)
        self.assertFalse(p.get(hb.AIM_X).significant)

    def test_emi_spikes_are_flagged_and_left_out(self) -> None:
        p = profile_of(Player(spikes=6))
        self.assertTrue(p.emi)
        self.assertEqual(p.get(hb.SPIKES).raw, 6.0)
        self.assertFalse(p.get(hb.AIM_X).significant)        # the clicks near the spikes are not used
        self.assertLess(p.get(hb.EDGE_CLAMP + "top").value, 0.01)

    def test_early_tapping(self) -> None:
        early = profile_of(Player(time_bias_ms=-130))
        normal = profile_of(Player())
        self.assertLess(early.get(hb.TAP_LEAD).value, -20)
        self.assertTrue(early.get(hb.TAP_LEAD).significant)
        self.assertAlmostEqual(normal.get(hb.TAP_LEAD).value - early.get(hb.TAP_LEAD).value, 140, delta=20)

    def test_still_cursor_jitter(self) -> None:
        shaky, calm = profile_of(Player(jitter_px=2.0)), profile_of(Player())
        self.assertGreater(shaky.get(hb.JITTER).raw, 0.08)
        self.assertLess(calm.get(hb.JITTER).value, 0)

    def test_mods(self) -> None:
        self.assertTrue(all(h.confidence != "high" for h in profile_of(Player()).habits))    # inferred: capped
        relax = profile_of(Player(), mods=hb.ModInfo(meaningless=True))
        self.assertFalse(relax.usable)
        known = profile_of(Player(), mods=hb.ModInfo())
        self.assertTrue(known.mods_known and not known.mods_mismatch)
        self.assertIsNotNone(known.get(hb.TIMING))             # the clock is the map's: the lateness is known
        self.assertTrue(profile_of(Player(), mods=hb.ModInfo(hard_rock=True)).mods_mismatch)

    def test_miss_cost(self) -> None:
        self.assertEqual(hb.miss_cost(0.0, 0.3), 0.0)
        self.assertLess(hb.miss_cost(0.1, 0.1), 0.001)
        self.assertGreater(hb.miss_cost(0.6, 0.3), hb.miss_cost(0.3, 0.3))
        self.assertAlmostEqual(hb.hit_chance(0.0, 0.3), 1 - math.exp(-1 / (2 * 0.09)), places=3)

    def test_never_raises(self) -> None:
        from osu_tablet_assistant.engine.playtypes import PlayReport
        capture = simulate(MAP, Player(), seed=1)
        self.assertFalse(hb.analyze_habits(MAP, capture, PlayReport()).usable)          # untrusted: nothing
        self.assertFalse(hb.analyze_habits(MAP, capture, None).usable)                  # odd input: no exception


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="osu-habits-")
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / hs.FILE_NAME

    def test_inverse_variance_pooling(self) -> None:
        setup = hs.Setup.of(AREA)
        recs = [hs.PlayRecord(0, "a", "live", setup, 3.0, habits=(("ur", 100.0, 10.0, 50, ""),)),
                hs.PlayRecord(0, "a", "live", setup, 3.0, habits=(("ur", 130.0, 20.0, 50, ""),))]
        h = hs.pool(recs, setup, 3.0).get("ur")
        self.assertAlmostEqual(h.value, (100 / 100 + 130 / 400) / (1 / 100 + 1 / 400))
        self.assertAlmostEqual(h.se, 1 / math.sqrt(1 / 100 + 1 / 400))
        self.assertEqual((h.n, h.plays), (100, 2))

    def test_one_play_is_never_enough_and_two_agreeing_plays_are(self) -> None:
        player = Player(bias_x_mm=1.0)
        one = view_of(player, seeds=(5,))
        self.assertTrue(one.get(hb.AIM_X).significant)
        self.assertFalse(one.get(hb.AIM_X).actionable)
        self.assertTrue(view_of(player).get(hb.AIM_X).actionable)

    def test_a_change_already_made_is_not_counted_twice(self) -> None:
        store = hs.HabitStore(None)
        prof = profile_of(Player(bias_x_mm=1.0))
        store.add(prof, hs.Setup.of(AREA), "s")
        moved = replace(AREA, x=AREA.x - 1.0)   # +1 mm right in view (180)
        bias = store.view(hs.Setup.of(moved), prof.r_mm).get(hb.AIM_X)
        self.assertLess(abs(bias.value), 0.05)
        bigger = replace(AREA, width=AREA.width * 1.06, height=AREA.height * 1.06)
        store2 = hs.HabitStore(None)
        store2.add(profile_of(Player(overshoot=0.06)), hs.Setup.of(AREA), "s")
        self.assertLess(abs(store2.view(hs.Setup.of(bigger), prof.r_mm).get(hb.SIZE).value), 0.015)

    def test_file_round_trip_corruption_version_and_forget(self) -> None:
        store = hs.HabitStore(self.path)
        store.add(profile_of(Player()), hs.Setup.of(AREA, 2.0, (HAWKU,)), "s")
        self.assertTrue(store.save())
        again = hs.HabitStore(self.path)
        self.assertEqual(again.plays, store.plays)
        text = self.path.read_text(encoding="utf-8")
        for secret in ("Song", "Artist", "sim.osu"):
            self.assertNotIn(secret, text)                    # summary numbers only, no map names
        self.path.write_text("{not json", encoding="utf-8")
        broken = hs.HabitStore(self.path)
        self.assertEqual((broken.plays, broken.load_problem), ([], "corrupt"))
        self.assertTrue(self.path.with_name("habits.corrupt.json").exists())
        self.path.write_text(json.dumps({"version": 99, "plays": []}), encoding="utf-8")
        future = hs.HabitStore(self.path)
        self.assertFalse(future.save())                       # a newer format is never overwritten
        self.path.write_text(json.dumps({"version": 1, "plays": [{"t": "x"}, 5]}), encoding="utf-8")
        self.assertEqual(hs.HabitStore(self.path).plays, [])  # bad plays are skipped
        self.assertTrue(again.forget())
        self.assertFalse(self.path.exists() or self.path.with_name("habits.corrupt.json").exists())
        self.assertEqual(again.plays, [])

    def test_adaptation_window(self) -> None:
        day = 86400.0
        now = [10 * day]
        store = hs.HabitStore(None, clock=lambda: now[0])
        prof = profile_of(Player())
        old = hs.Setup.of(AREA)
        store.add(prof, old, "yesterday")
        new = hs.Setup.of(replace(AREA, width=AREA.width * 0.85, height=AREA.height * 0.85))
        now[0] += day
        self.assertIsNone(store.adaptation(old, "today"))                   # nothing changed
        window = store.adaptation(new, "today")                             # changed outside the coach
        self.assertEqual(window.kind, "area")
        self.assertIsNone(store.adaptation(new, "yesterday"))               # the coach changed it itself
        store.add(prof, new, "today")
        now[0] += day
        self.assertEqual(store.adaptation(new, "tomorrow").kind, "area")    # still inside the window next day
        now[0] += hs.ADAPT_AREA_DAYS * day
        self.assertIsNone(store.adaptation(new, "today"))                   # the window is over
        heavy = hs.HabitStore(None, clock=lambda: 0.0)
        heavy.add(prof, hs.Setup.of(AREA, 12.0, (HAWKU,)), "a")
        self.assertEqual(heavy.adaptation(hs.Setup.of(AREA), "b").kind, "smoothing")


class RulebookTests(unittest.TestCase):
    def first_change(self, view: hs.HabitView, **kw) -> rb.Suggestion | None:
        return rb.next_change(rb.suggest(view, ctx(**kw)))

    def test_offset_moves_the_area_the_same_way_within_the_cap(self) -> None:
        s = self.first_change(view_of(Player(bias_x_mm=1.0, **NOISY)))
        self.assertEqual((s.rule, s.action), (4, rb.SHIFT))
        self.assertGreater(s.dx, 0)
        self.assertLessEqual(math.hypot(s.dx, s.dy), rb.SHIFT_CAP_MM + 1e-9)
        big = self.first_change(view_of(Player(bias_x_mm=4.0, **NOISY)))
        self.assertAlmostEqual(math.hypot(big.dx, big.dy), rb.SHIFT_CAP_MM)
        self.assertIn("S21", s.sources)

    def test_overshoot_makes_the_area_bigger(self) -> None:
        s = self.first_change(view_of(Player(overshoot=0.06, **NOISY)))
        self.assertEqual((s.rule, s.action, s.axis, s.sign), (1, rb.SIZE, "both", 1))
        self.assertLessEqual(s.step, rb.SIZE_CAP)

    def test_horizontal_undershoot_narrows_one_axis_only_with_the_free_proportion(self) -> None:
        view = view_of(Player(gain_x=0.94, **NOISY))
        free = self.first_change(view, aspect_lock=False)
        self.assertEqual((free.rule, free.axis, free.sign), (5, "x", -1))
        locked = self.first_change(view, aspect_lock=True)
        self.assertEqual((locked.axis, locked.sign), ("both", -1))

    def test_rotation_turns_the_area_at_most_two_degrees(self) -> None:
        s = self.first_change(view_of(Player(rotation_deg=4.0, **NOISY)))
        self.assertEqual((s.rule, s.action, s.sign), (6, rb.ROTATE, 1))
        self.assertEqual(s.step, rb.ROTATE_CAP_DEG)

    def test_noise_keeps_the_setting(self) -> None:
        out = rb.suggest(view_of(Player(**NOISY)), ctx())
        self.assertIsNone(rb.next_change(out))
        self.assertIn(rb.KEEP, [s.action for s in out])

    def test_emi_blocks_everything(self) -> None:
        out = rb.suggest(view_of(Player(bias_x_mm=1.0, **NOISY)), ctx(spikes_now=6))
        self.assertEqual(out[0].action, rb.EMI)
        self.assertIsNone(rb.next_change(out))

    def test_adaptation_window_says_wait(self) -> None:
        out = rb.suggest(view_of(Player(bias_x_mm=1.0, **NOISY)), ctx(adaptation=hs.Adaptation("area", 2.2)))
        self.assertEqual([s.action for s in out], [rb.WAIT])
        self.assertEqual(out[0].reason.params["days"], 3)

    def test_early_tapping_never_changes_the_size(self) -> None:
        out = rb.suggest(view_of(Player(time_bias_ms=-130, **NOISY)), ctx())
        self.assertFalse(any(s.action == rb.SIZE for s in out))
        self.assertIn("early_tap", [s.reason.key.rsplit(".", 1)[-1] for s in out])

    @staticmethod
    def synthetic(**values: float) -> hs.HabitView:
        habits = {k: hs.PooledHabit(k, v, abs(v) / 10 + 1e-6, 400, 6, 6, 0, False) for k, v in values.items()}
        habits[hb.SCATTER] = hs.PooledHabit(hb.SCATTER, 0.3, 0.01, 400, 6, 6, 0, False)
        return hs.HabitView(habits, 6, 3.0, 25.0, 1.0)

    def test_caps_hold_for_huge_habits(self) -> None:
        view = self.synthetic(aim_bias_x=3.0, aim_bias_y=-2.0, gain_x=0.4, gain_y=0.1, size=0.25, aspect=0.3,
                              rotation=-15.0)
        for s in rb.suggest(view, ctx(aspect_lock=False)):
            if s.action == rb.SHIFT:
                self.assertLessEqual(math.hypot(s.dx, s.dy), rb.SHIFT_CAP_MM + 1e-9)
            if s.action == rb.SIZE:
                self.assertLessEqual(s.step, rb.SIZE_CAP + 1e-9)
            if s.action == rb.ROTATE:
                self.assertLessEqual(s.step, rb.ROTATE_CAP_DEG)

    def test_trail_lightens_the_smoothing_before_the_area(self) -> None:
        view = self.synthetic(trail=-0.6, size=-0.05)
        on = (FilterConfig(HAWKU, True, {"Latency": 6.0}),)
        out = rb.suggest(view, ctx(filters=on))
        self.assertEqual(out[0].rule, 3)
        self.assertEqual(out[0].filter_change[0], HAWKU)
        self.assertEqual(rb.suggest(view, ctx(filters=on, player_filters=frozenset({HAWKU})))[0].rule, 2)

    def test_never_prediction_and_never_two_smoothing_filters(self) -> None:
        shaky = self.synthetic(jitter=0.03)
        for filters in ((), (FilterConfig(HAWKU, True, {"Latency": 3.0}),),
                        (FilterConfig(HAWKU, True, {"Latency": 3.0}), FilterConfig(RESAMPLER, True, {"frameShift": 0.5}))):
            for s in rb.suggest(shaky, ctx(filters=filters, installed=frozenset(CATALOG))):
                if s.filter_change is None:
                    continue
                path, store = s.filter_change
                if store is not None and store.enabled:
                    self.assertFalse(rb.is_prediction(path, CATALOG), path)
                    self.assertEqual(path, RADIAL_TABLET)
                after = opt.replace_filter(filters, path, store)
                self.assertLessEqual(len(rb.smoothing_on(after, CATALOG)), 1)
        added = [s for s in rb.suggest(shaky, ctx(installed=frozenset(CATALOG))) if s.action == rb.ADD_LIGHT]
        self.assertEqual(len(added), 1)
        self.assertTrue(rb.is_prediction(RESAMPLER, CATALOG) or RESAMPLER != RADIAL_TABLET)

    def test_the_rules_are_all_there_with_sources(self) -> None:
        self.assertEqual([r.id for r in rb.RULES], list(range(1, 19)))
        self.assertTrue(all(r.sources for r in rb.RULES))

    def test_proposals_from_rules(self) -> None:
        state = opt.new_state()
        prof = ProfileInfo("t", "mode", AREA, AREA, True, ())
        shift = rb.next_change(rb.suggest(view_of(Player(bias_x_mm=1.0, **NOISY)), ctx()))
        p = opt.rule_proposal(state, prof, shift, TABLET)
        self.assertEqual((p.kind, p.rule), ("shift", 4))
        self.assertGreater(p.move[0], 0)
        turn = rb.next_change(rb.suggest(view_of(Player(rotation_deg=4.0, **NOISY)), ctx()))
        p = opt.rule_proposal(state, prof, turn, TABLET)
        self.assertEqual(p.kind, "rotate")
        self.assertAlmostEqual((p.area.rotation - AREA.rotation) % 360, 2.0)
        undone = opt.mark_undone(state, "rotate", (2.0, 0.0))
        self.assertIsNone(opt.rule_proposal(undone, prof, turn, TABLET))

    def test_describe_is_neutral_and_marks_unsure_habits(self) -> None:
        view = view_of(Player(bias_x_mm=1.0, edge_stop_mm=-0.8, **NOISY))
        style = rb.describe(view, rb.suggest(view, ctx()))
        self.assertIn("aim_bias", [i.key for i in style.acted])
        self.assertIn(hb.EDGE_STOP, [i.key for i in style.fine])
        learning = rb.describe(view_of(Player(bias_x_mm=1.0, **NOISY), seeds=(5,)), ())
        self.assertFalse(learning.acted)
        self.assertTrue(all(i.confidence == "low" for i in learning.unsure))
        self.assertEqual(learning.headline.key, "play.style.headline.learning")


class WordingTests(unittest.TestCase):
    """The style texts describe; they never ask the player to change how they play."""

    FORBIDDEN = {"es": ("deberías", "debes", "corrige", "corregir", "intenta", "mejora tu", "error tuyo"),
                 "en": ("you should", "you must", "correct your", "fix your", "try to", "improve your")}

    def test_no_technique_advice_and_both_languages(self) -> None:
        from osu_tablet_assistant.gui import i18n
        cats = {lang: i18n.load_catalog(lang) for lang in i18n.SUPPORTED}
        keys = [k for k in cats["es"] if k.startswith(("play.style.", "play.rule."))]
        self.assertGreater(len(keys), 80)
        for lang, words in self.FORBIDDEN.items():
            for key in keys:
                text = cats[lang][key].lower()
                for word in words:
                    self.assertNotIn(word, text, f"{lang}:{key}")


def _display_available() -> bool:
    try:
        root = tk.Tk()
    except tk.TclError:
        return False
    root.destroy()
    return True


class CoachHabitTests(unittest.TestCase):
    def test_rounds_build_the_style_and_forget_clears_it(self) -> None:
        from test_coach import Env
        env = Env(self)
        env.play_round()
        self.assertIsNotNone(env.core.style)
        self.assertEqual(env.core.style.plays, 1)
        env.core.result = None
        env.play_round()
        self.assertEqual(env.core.style.plays, 2)
        self.assertEqual(len(env.core.habit_store.plays), 2)
        fb = env.core.forget_habits()
        self.assertTrue(fb.ok)
        self.assertEqual((env.core.habit_store.plays, env.core.style.plays), ([], 0))


@unittest.skipUnless(_display_available(), "no display")
class StyleCardTests(unittest.TestCase):
    def test_card_in_both_languages(self) -> None:
        from osu_tablet_assistant.gui import i18n, stylecard
        from osu_tablet_assistant.gui.widgets import apply_theme
        self.addCleanup(i18n.set_language, "es")
        view = view_of(Player(bias_x_mm=1.0, edge_stop_mm=-0.8, **NOISY))
        style = rb.describe(view, rb.suggest(view, ctx()))
        for lang in i18n.SUPPORTED:
            i18n.set_language(lang)
            root = tk.Tk()
            apply_theme(root)
            forgot = []
            card = stylecard.StyleCard(root, style, i18n.t, lambda: forgot.append(1), {})
            card.pack(fill="both")
            root.update()
            self.assertTrue(card.is_open)                      # it opens by itself when the setup adapts

            def texts(w):
                out = []
                for child in w.winfo_children():
                    try:
                        out.append(str(child.cget("text")))
                    except tk.TclError:
                        pass
                    out.append(texts(child))
                return " ".join(out)
            shown = texts(card)
            for key in ("play.style.title", "play.style.acted_title", "play.style.fine_title", "play.style.forget",
                        "play.style.privacy"):
                self.assertIn(i18n.t(key), shown)
            self.assertNotIn("play.", shown)
            def find(w):
                for child in w.winfo_children():
                    try:
                        if str(child.cget("text")) == i18n.t("play.style.forget"):
                            return child
                    except tk.TclError:
                        pass
                    hit = find(child)
                    if hit is not None:
                        return hit
                return None
            find(card.details).invoke()
            self.assertEqual(forgot, [1])
            card.toggle_open()
            self.assertFalse(card.is_open)
            unsure = stylecard.style_sections(rb.describe(view_of(Player(**NOISY), seeds=(5,)), ()), i18n.t)
            for _title, items in unsure:
                self.assertTrue(all(sure == i18n.t("play.style.conf.low") for _t, _a, sure in items))
            root.destroy()


if __name__ == "__main__":
    unittest.main()
