"""The "calm dark glass" design: contrast of the tokens, the antialiased bitmaps, the image cache,
rounded containers at several DPI scales and the no-pictogram rule for on-screen text."""
from __future__ import annotations

import ast
import json
import tkinter as tk
import unittest
from pathlib import Path

from osu_tablet_assistant.gui import raster
from osu_tablet_assistant.gui.palette import COLORS, contrast_ratio, mix, relative_luminance
from osu_tablet_assistant.gui.widgets import NOTICE_KINDS, Chip, ImageCache

PACKAGE = Path(__file__).resolve().parents[1] / "osu_tablet_assistant"
SURFACES = ("bg", "field", "surface", "surface_2", "surface_3")


def _display_available() -> bool:
    try:
        root = tk.Tk()
    except tk.TclError:
        return False
    root.destroy()
    return True


class ContrastTests(unittest.TestCase):
    def test_wcag_formula(self) -> None:
        self.assertAlmostEqual(contrast_ratio("#000000", "#ffffff"), 21.0, places=2)
        self.assertAlmostEqual(contrast_ratio("#777777", "#777777"), 1.0)
        self.assertAlmostEqual(relative_luminance("#ffffff"), 1.0)

    def test_reading_text_is_at_least_4_5_to_1_on_every_surface(self) -> None:
        for fg in ("text", "muted", "accent", "good", "warn", "error", "info"):
            for bg in SURFACES:
                ratio = contrast_ratio(getattr(COLORS, fg), getattr(COLORS, bg))
                self.assertGreaterEqual(ratio, 4.5, f"{fg} on {bg}: {ratio:.2f}")

    def test_notices_chips_and_buttons_are_readable(self) -> None:
        for fg_attr, bg_attr, _icon in NOTICE_KINDS.values():
            soft = getattr(COLORS, bg_attr)
            for fg in (COLORS.text, COLORS.muted, getattr(COLORS, fg_attr)):
                self.assertGreaterEqual(contrast_ratio(fg, soft), 4.5, f"{fg} on {bg_attr}")
        for kind in ("neutral", "accent", *NOTICE_KINDS):
            fg, bg = Chip.colors(kind)
            self.assertGreaterEqual(contrast_ratio(fg, bg), 4.5, kind)
        for fill in (COLORS.accent, COLORS.accent_hover, COLORS.accent_pressed):
            self.assertGreaterEqual(contrast_ratio(COLORS.on_accent, fill), 4.5, fill)
        self.assertGreaterEqual(contrast_ratio(COLORS.text, COLORS.accent_soft), 4.5)   # selected rows

    def test_disabled_text_is_visible_but_clearly_distinct(self) -> None:
        for bg in SURFACES:
            self.assertGreaterEqual(contrast_ratio(COLORS.subtle, getattr(COLORS, bg)), 2.5, bg)
        self.assertGreaterEqual(contrast_ratio(COLORS.text, COLORS.subtle), 3.0)
        self.assertGreaterEqual(contrast_ratio(COLORS.muted, COLORS.subtle), 1.8)

    def test_glass_layers_step_up_from_the_ground(self) -> None:
        levels = [relative_luminance(getattr(COLORS, n)) for n in ("bg", "field", "surface", "surface_2", "surface_3")]
        self.assertEqual(levels, sorted(levels))
        self.assertGreater(relative_luminance(COLORS.highlight), relative_luminance(COLORS.border))
        self.assertEqual(mix("#000000", "#ffffff", 0.5), "#808080")


class RasterTests(unittest.TestCase):
    def test_rounded_rect_blends_corners_into_the_background(self) -> None:
        rows = raster.rounded_rect(40, 30, 10, "#202020", "#000000", border="#808080")
        self.assertEqual((len(rows), len(rows[0])), (30, 40))
        self.assertEqual(rows[0][0], "#000000")                 # outside the corner: background
        self.assertEqual(rows[15][20], "#202020")               # inside: fill
        self.assertEqual(rows[0][20], "#808080")                # straight edge: the hairline
        corner = {p for row in rows[:10] for p in row[:10]}     # along the arc: blends, not a hard step
        self.assertTrue(corner - {"#000000", "#808080", "#202020"})

    def test_highlight_brightens_only_the_top_edge(self) -> None:
        rows = raster.rounded_rect(40, 40, 8, "#202020", "#000000", border="#404040", highlight="#c0c0c0")
        self.assertEqual(rows[0][20], "#c0c0c0")
        self.assertEqual(rows[39][20], "#404040")

    def test_compact_nine_slice_source_has_a_uniform_centre(self) -> None:
        edge = 9
        rows = raster.rounded_rect(2 * edge + 1, 2 * edge + 1, 8, "#202020", "#000000", border="#606060",
                                   ring="#ff0000", ring_width=2)
        centre_row, centre_col = rows[edge], [row[edge] for row in rows]
        self.assertEqual(len(set(centre_row[edge - 1:edge + 2])), 1)
        self.assertEqual(len(set(centre_col[edge - 1:edge + 2])), 1)

    def test_icons_and_helpers(self) -> None:
        for name in raster.ICONS:
            rows = raster.icon(name, 16, "#ffffff", "#000000")
            self.assertEqual((len(rows), len(rows[0])), (16, 16), name)
            self.assertTrue(any(p != "#000000" for row in rows for p in row), name)
        base = raster.rounded_rect(10, 10, 5, "#ff0000", "#000000")
        top = raster.icon("dot", 10, "#ffffff", "#ff0000")
        merged = raster.overlay(base, top, "#ff0000")
        self.assertEqual(merged[0][0], "#000000")
        self.assertEqual(merged[5][5], "#ffffff")
        self.assertEqual(len(raster.pad_sides(base, 2, 3, "#000000")[0]), 15)
        quarters = raster.quadrants(raster.rounded_rect(8, 8, 4, "#ff0000", "#000000"))
        self.assertEqual([len(q) for q in quarters], [4, 4, 4, 4])
        self.assertEqual(raster.tcl_data((("#000000", "#ffffff"),)), "{#000000 #ffffff}")

    def test_results_are_memoised(self) -> None:
        a = raster.rounded_rect(20, 20, 6, "#101010", "#000000")
        self.assertIs(a, raster.rounded_rect(20, 20, 6, "#101010", "#000000"))


class ImageCacheTests(unittest.TestCase):
    def test_same_key_same_object_and_bounded(self) -> None:
        cache = ImageCache(limit=3)
        made: list[int] = []

        def factory(n: int):
            return lambda: made.append(n) or object()
        first = cache.get(("a", 1), factory(1))
        self.assertIs(cache.get(("a", 1), factory(2)), first)
        self.assertEqual(made, [1])
        for n in range(10):
            cache.get(("b", n), factory(n))
        self.assertEqual(len(cache), 3)


# Pictographs and symbol glyphs that would act as icons (typographic marks such as • · — … × ≈ are fine).
_ICON_RANGES = ((0x2190, 0x21FF), (0x25A0, 0x25FF), (0x2600, 0x27BF), (0x2B00, 0x2BFF), (0x1F000, 0x1FAFF))


def _icon_chars(text: str, allowed: str = "") -> set[str]:
    return {ch for ch in text if ch not in allowed and any(lo <= ord(ch) <= hi for lo, hi in _ICON_RANGES)}


class NoPictogramTests(unittest.TestCase):
    def test_gui_string_literals_have_no_icon_glyphs(self) -> None:
        for path in sorted((PACKAGE / "gui").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                          if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) and node.body
                          and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                    self.assertEqual(_icon_chars(node.value), set(), f"{path.name}:{node.lineno}")

    def test_locale_texts_have_no_icon_glyphs(self) -> None:
        for path in sorted((PACKAGE / "locales").glob("*/*.json")):
            for key, text in json.loads(path.read_text(encoding="utf-8")).items():
                self.assertEqual(_icon_chars(text, allowed="→"), set(), f"{path.parent.name}/{path.name}: {key}")


@unittest.skipUnless(_display_available(), "no display")
class RoundedFrameTests(unittest.TestCase):
    def make_root(self, scaling: float | None = None) -> tk.Tk:
        from osu_tablet_assistant.gui.widgets import apply_theme
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        if scaling:
            root.tk.call("tk", "scaling", scaling)
        apply_theme(root)
        return root

    def test_card_resizes_and_keeps_its_content_inside(self) -> None:
        from osu_tablet_assistant.gui.widgets import Card, WrapLabel, button, px
        for scale in (1.0, 1.25, 1.5):
            root = self.make_root(scaling=scale * 96 / 72)
            card = Card(root, "Title", "A subtitle that wraps when the card gets narrow enough to need it.")
            card.pack(fill="both", expand=True)
            WrapLabel(card.body, "Body text " * 20).pack(fill="x")
            btn = button(card.body, "Action", lambda: None, kind="primary")
            btn.pack(anchor="w")
            style = str(card.cget("style"))
            root.deiconify()             # a withdrawn window is never laid out; park it off screen instead
            for width, height in ((900, 500), (420, 600), (300, 400), (1200, 300)):
                root.geometry(f"{width}x{height}+-4000+-4000")
                root.update()
                self.assertEqual(str(card.cget("style")), style)        # resizing never rebuilds anything
                pad = px(card, 18)                                       # Card's default padding
                for child in (card.header, card.body):
                    self.assertGreaterEqual(child.winfo_x(), pad - 1)
                    self.assertLessEqual(child.winfo_x() + child.winfo_width(), card.winfo_width() - pad + 1)
            card.set_selected(True)
            card.set_hover(True)
            root.update()
            self.assertTrue(card.selected)

    def test_button_keeps_its_surface_when_restyled(self) -> None:
        from osu_tablet_assistant.gui.widgets import Card, button
        root = self.make_root()
        card = Card(root, "x")
        card.pack()
        btn = button(card.body, "Run", lambda: None, kind="primary")
        self.assertEqual(str(btn.cget("style")), "Card.Primary.TButton")
        btn.configure(style="Secondary.TButton")
        self.assertEqual(str(btn.cget("style")), "Card.Secondary.TButton")
        page_btn = button(root, "Next", lambda: None, kind="primary")
        self.assertEqual(str(page_btn.cget("style")), "Primary.TButton")
        btn.set_icon("chevron_right")
        root.update()

    def test_widgets_inside_cards_pick_the_card_variant_automatically(self) -> None:
        from tkinter import ttk

        from osu_tablet_assistant.gui.widgets import Card
        root = self.make_root()
        card = Card(root, "x")
        card.pack()
        self.assertEqual(str(ttk.Entry(card.body).cget("style")), "Card.TEntry")
        self.assertEqual(str(ttk.Combobox(card.body).cget("style")), "Card.TCombobox")
        self.assertEqual(str(ttk.Entry(root).cget("style")), "")

    def test_every_screen_builds_at_larger_scales(self) -> None:
        from osu_tablet_assistant.gui import i18n
        from osu_tablet_assistant.gui.app import App
        from osu_tablet_assistant.gui.session import STEP_ORDER
        from test_integration import Harness
        self.addCleanup(i18n.set_language, "es")
        for scale, lang in ((1.25, "en"), (1.5, "es")):
            i18n.set_language(lang)
            harness = Harness(self, dry_run=False)
            session = harness.drive_to_testing(self)
            app = App(session)
            app.root.withdraw()
            self.addCleanup(app.root.destroy)
            app.root.tk.call("tk", "scaling", scale * 96 / 72)
            for step in STEP_ORDER:
                session.step = step
                app.wizard.show_current()
                app.root.update()


if __name__ == "__main__":
    unittest.main()
