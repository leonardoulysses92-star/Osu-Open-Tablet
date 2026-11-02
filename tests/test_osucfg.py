"""engine.osucfg: the user cfg of osu!stable, the pointer verdict and finding the install (synthetic files)."""
from __future__ import annotations

import dataclasses
import os
import tempfile
import time
import unittest
from pathlib import Path

from osu_tablet_assistant.engine import osucfg
from osubuild import make_cfg


class ParseTests(unittest.TestCase):
    def test_reads_the_keys_and_nothing_else(self) -> None:
        cfg = osucfg.parse_cfg(make_cfg(BeatmapDirectory="D:\\Maps", Fullscreen=1, Width=1280, Height=720,
                                        WidthFullscreen=1920, HeightFullscreen=1080, Letterboxing=0,
                                        LetterboxPositionX=-20, LetterboxPositionY=35, RawInput=1,
                                        MouseSpeed="1,25", AbsoluteToOsuWindow=0, FrameSync="Limit2x",
                                        keyOsuLeft="Z", keyOsuRight="X", ConfineMouse="Fullscreen"))
        self.assertEqual(cfg.beatmap_directory, "D:\\Maps")
        self.assertEqual((cfg.fullscreen, cfg.width, cfg.height), (True, 1280, 720))
        self.assertEqual((cfg.width_fullscreen, cfg.height_fullscreen), (1920, 1080))
        self.assertEqual((cfg.letterboxing, cfg.letterbox_x, cfg.letterbox_y), (False, -20, 35))
        self.assertEqual((cfg.raw_input, cfg.mouse_speed, cfg.absolute_to_osu_window), (True, 1.25, False))
        self.assertEqual((cfg.key_left, cfg.key_right, cfg.confine_mouse), ("Z", "X", "Fullscreen"))
        self.assertEqual(cfg.frame_limit_hz(144.0), 288.0)
        text = repr(cfg).lower()
        self.assertNotIn("secret", text)                          # user name and password hash are never kept
        self.assertNotIn("0123456789abcdef", text)

    def test_odd_values_are_none(self) -> None:
        cfg = osucfg.parse_cfg("Width = wide\nRawInput = maybe\nMouseSpeed = \nnot a line\n= 3\n")
        self.assertEqual((cfg.width, cfg.raw_input, cfg.mouse_speed), (None, None, None))
        self.assertEqual(osucfg.OsuConfig(), osucfg.parse_cfg(""))

    def test_frame_limits(self) -> None:
        f = lambda **kw: osucfg.OsuConfig(**kw).frame_limit_hz(60.0)     # noqa: E731
        self.assertEqual(f(frame_sync="VSync"), 60.0)
        self.assertEqual(f(frame_sync="Limit8x"), 480.0)
        self.assertIsNone(f(frame_sync="Unlimited"))
        self.assertEqual(f(frame_sync="LimitCustom", custom_frame_limit=144), 144.0)
        self.assertIsNone(f(frame_sync="LimitCustom"))
        self.assertIsNone(f(frame_sync="SomethingNew"))


class TrustTests(unittest.TestCase):
    def test_direct_by_default(self) -> None:
        self.assertTrue(osucfg.pointer_trust(None).direct)
        self.assertTrue(osucfg.pointer_trust(osucfg.OsuConfig(raw_input=True, mouse_speed=1.0)).direct)
        self.assertTrue(osucfg.pointer_trust(osucfg.OsuConfig(raw_input=False, mouse_speed=1.6,
                                                              absolute_to_osu_window=True)).direct)

    def test_raw_input_transforms_the_cursor(self) -> None:
        window = osucfg.pointer_trust(osucfg.OsuConfig(raw_input=True, absolute_to_osu_window=True))
        self.assertEqual(window.verdict, "needs_calibration")
        self.assertEqual(window.reason.key, "play.pointer.raw_window")
        speed = osucfg.pointer_trust(osucfg.OsuConfig(raw_input=True, mouse_speed=0.8))
        self.assertEqual(speed.reason.key, "play.pointer.raw_speed")
        self.assertEqual(speed.reason.params["pct"], 80)

    def test_low_frame_limit(self) -> None:
        low = osucfg.pointer_trust(osucfg.OsuConfig(frame_sync="VSync"), refresh_hz=60.0)
        self.assertEqual([r.key for r in low.reasons], ["play.pointer.low_fps"])
        self.assertEqual(low.reasons[0].params["fps"], 60)
        self.assertTrue(osucfg.pointer_trust(osucfg.OsuConfig(frame_sync="Limit8x"), refresh_hz=60.0).direct)


class InstallTests(unittest.TestCase):
    def test_find_and_load_the_users_cfg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(osucfg.load_cfg(root))
            (root / "osu!.cfg").write_text("Width = 1\n", encoding="utf-8")          # the global one: ignored
            self.assertIsNone(osucfg.find_cfg(root))
            other = root / "osu!.someone.cfg"
            other.write_text(make_cfg(Width=800), encoding="utf-8")
            mine = root / "osu!.Me.cfg"
            mine.write_text(make_cfg(Width=1600), encoding="utf-8")
            os.utime(other, (time.time() + 50, time.time() + 50))                    # newer, but not mine
            self.assertEqual(osucfg.find_cfg(root, user="me"), mine)
            self.assertEqual(osucfg.load_cfg(root, user="me").width, 1600)
            self.assertEqual(osucfg.find_cfg(root, user="nobody"), other)            # else the newest

    def test_songs_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(osucfg.songs_dir(root, osucfg.OsuConfig()))
            (root / "Songs").mkdir()
            self.assertEqual(osucfg.songs_dir(root, osucfg.OsuConfig()), root / "Songs")
            self.assertEqual(osucfg.songs_dir(root, osucfg.OsuConfig(beatmap_directory="Maps")), root / "Maps")
            absolute = str(root / "Elsewhere")
            self.assertEqual(osucfg.songs_dir(root, osucfg.OsuConfig(beatmap_directory=absolute)), Path(absolute))
        self.assertIsNone(osucfg.songs_dir(None))

    def test_locate_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            mine, local, running = base / "mine", base / "local" / "osu!", base / "running"
            for folder in (mine, local, running):
                folder.mkdir(parents=True)
            (running / "osu!.exe").write_bytes(b"")
            env = {"LOCALAPPDATA": str(base / "local")}
            self.assertEqual(osucfg.locate_osu_dir(None, running / "osu!.exe", env=env), running)
            (local / "osu!.db").write_bytes(b"")
            self.assertEqual(osucfg.locate_osu_dir(None, running / "osu!.exe", env=env), local)
            (mine / "osu!.exe").write_bytes(b"")
            self.assertEqual(osucfg.locate_osu_dir(mine, running / "osu!.exe", env=env), mine)
            self.assertIsNone(osucfg.locate_osu_dir(base / "nothing", None, env={}))

    def test_process_path_fails_soft(self) -> None:
        self.assertIsNone(osucfg.process_image_path(None))
        self.assertIsNone(osucfg.process_image_path(0))
        if os.name == "nt":
            own = osucfg.process_image_path(os.getpid())
            self.assertTrue(own and own.lower().endswith(".exe"))
            self.assertIsNone(osucfg.process_image_path(2 ** 31 - 3))     # no such process

    def test_config_is_frozen(self) -> None:
        with self.assertRaises(dataclasses.FrozenInstanceError):
            osucfg.OsuConfig().width = 3                                            # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
