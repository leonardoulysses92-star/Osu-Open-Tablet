"""i18n lookup, fallback and locale-file consistency."""
from __future__ import annotations

import json
import re
import string
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from osu_tablet_assistant.gui import i18n
from osu_tablet_assistant.gui.session import STEP_ORDER
from osu_tablet_assistant.models import MapPreference, PlayStyle, Priority, Reason, SkillLevel, TabletSource

PACKAGE = Path(i18n.__file__).resolve().parent.parent
_KEY = re.compile(r"""\bt\(\s*f?["']((?:gui|core)\.[A-Za-z0-9_.{}\-]+)["']""")


def placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


class LookupTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(i18n._catalogs, {
            "es": {"a": "hola {name}", "only_es": "solo es"},
            "en": {"a": "hello {name}", "only_en": "only en", "plain": "{x} stays"},
        }, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(i18n.set_language, "es")

    def test_current_language_and_format(self) -> None:
        i18n.set_language("es")
        self.assertEqual(i18n.current_language(), "es")
        self.assertEqual(i18n.t("a", name="Ana"), "hola Ana")
        i18n.set_language("en")
        self.assertEqual(i18n.t("a", name="Ana"), "hello Ana")

    def test_falls_back_to_english_then_key(self) -> None:
        i18n.set_language("es")
        self.assertEqual(i18n.t("only_en"), "only en")
        self.assertEqual(i18n.t("nope.nothing"), "nope.nothing")
        self.assertEqual(i18n.t("nope.{x}", x=1), "nope.{x}")

    def test_bad_params_do_not_raise(self) -> None:
        self.assertEqual(i18n.t("a"), "hola {name}")
        self.assertEqual(i18n.t("plain", y=1), "{x} stays")

    def test_unsupported_language_rejected(self) -> None:
        with self.assertRaises(ValueError):
            i18n.set_language("fr")

    def test_tr_translates_reason(self) -> None:
        i18n.set_language("en")
        self.assertEqual(i18n.tr(Reason("a", {"name": "Bo"})), "hello Bo")


class CatalogLoadingTests(unittest.TestCase):
    def test_merges_files_and_skips_broken_ones(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "es").mkdir()
            (root / "es" / "a.json").write_text('{"x": "1"}', encoding="utf-8")
            (root / "es" / "b.json").write_text('{"y": "2"}', encoding="utf-8")
            (root / "es" / "bad.json").write_text("{not json", encoding="utf-8")
            (root / "es" / "list.json").write_text("[1]", encoding="utf-8")
            self.assertEqual(i18n.load_catalog("es", root), {"x": "1", "y": "2"})
            self.assertEqual(i18n.load_catalog("en", root), {})

    def test_missing_keys_reports_differences(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for lang, data in (("es", {"a": "1", "b": "2"}), ("en", {"a": "1", "c": "3"})):
                (root / lang).mkdir()
                (root / lang / "core.json").write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(i18n.missing_keys(root), {"es": {"c"}, "en": {"b"}})


class LocaleFileTests(unittest.TestCase):
    """Checks on the real files shipped in ``locales/``."""

    def test_es_and_en_have_identical_keys(self) -> None:
        self.assertEqual(i18n.missing_keys(), {"es": set(), "en": set()})

    def test_placeholders_match_between_languages(self) -> None:
        es, en = i18n.load_catalog("es"), i18n.load_catalog("en")
        for key in sorted(set(es) & set(en)):
            self.assertEqual(placeholders(es[key]), placeholders(en[key]), key)

    def test_no_empty_text(self) -> None:
        for lang in i18n.SUPPORTED:
            for key, text in i18n.load_catalog(lang).items():
                self.assertTrue(text.strip(), f"{lang}:{key}")

    def test_every_literal_key_used_by_gui_code_exists(self) -> None:
        known = set(i18n.load_catalog("en")) | set(i18n.load_catalog("es"))
        files = [PACKAGE / "gui" / n for n in ("app.py", "wizard.py", "session.py", "widgets.py")]
        used = {m for path in files for m in _KEY.findall(path.read_text(encoding="utf-8"))
                if "{" not in m}
        self.assertGreater(len(used), 50)
        self.assertEqual(sorted(used - known), [])

    def test_dynamic_key_families_exist(self) -> None:
        known = set(i18n.load_catalog("en"))
        expected = {f"gui.step.{s.value}" for s in STEP_ORDER}
        for field, enum in (("play_style", PlayStyle), ("map_preference", MapPreference),
                            ("priority", Priority), ("skill_level", SkillLevel)):
            expected |= {f"core.pref.{field}.{m.value}" for m in enum}
            expected |= {f"gui.questions.{field}.title", f"gui.questions.{field}.why"}
        for name in ("notices_jitter", "notices_input_lag"):
            expected |= {f"gui.questions.{name}.{part}" for part in ("title", "why", "label")}
        expected |= {f"gui.questions.keep_area.{part}" for part in ("title", "why", "label")}
        expected |= {f"gui.test.{kind}.{part}" for kind in ("rate", "stillness", "tracking", "aim")
                     for part in ("name", "desc")}
        expected |= {f"gui.display.{k}{s}" for k in ("width", "height", "left", "top", "refresh")
                     for s in ("", "_help")}
        expected |= {f"gui.tablet.source.{s.value}" for s in TabletSource} | {"gui.tablet.source.profile"}
        expected |= {f"core.error.tablet_{k}" for k in ("name", "size", "rate", "pressure")}
        self.assertEqual(sorted(expected - known), [])


if __name__ == "__main__":
    unittest.main()
