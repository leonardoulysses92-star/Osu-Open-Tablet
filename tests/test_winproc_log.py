"""otd.winproc (read-only process listing) and the local log file."""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

from osu_tablet_assistant import log
from osu_tablet_assistant.otd import winproc


class WinprocTests(unittest.TestCase):
    def test_bad_pids_give_none(self) -> None:
        self.assertIsNone(winproc.image_path(None))
        self.assertIsNone(winproc.image_path(0))

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_lists_this_process_and_finds_its_executable(self) -> None:
        names = winproc.process_names()
        self.assertIn(os.getpid(), {p.pid for p in names})
        path = winproc.image_path(os.getpid())
        self.assertTrue(path and Path(path).name.lower().startswith("py"), path)
        procs = winproc.processes_with_paths({Path(path).name})
        own = [p for p in procs if p[0] == os.getpid()]
        self.assertEqual(own[0][2], path)
        self.assertTrue(all(p[2] is None for p in procs if p[1].lower() != Path(path).name.lower()))

    @unittest.skipIf(sys.platform == "win32", "not Windows")
    def test_elsewhere_listing_raises_oserror(self) -> None:
        with self.assertRaises(OSError):
            winproc.process_names()


class LogTests(unittest.TestCase):
    def test_silent_until_setup_then_writes_a_small_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = log.setup(Path(tmp))
            handlers = [h for h in log.logger.handlers if isinstance(h, logging.FileHandler)]

            def cleanup() -> None:
                for h in handlers:
                    log.logger.removeHandler(h)
                    h.close()
            self.addCleanup(cleanup)
            try:
                raise ValueError("odd file")
            except ValueError:
                log.log_exception("reading a test file")
            for h in handlers:
                h.flush()
            text = path.read_text(encoding="utf-8")
            self.assertIn("reading a test file failed", text)
            self.assertIn("ValueError: odd file", text)
            self.assertEqual(handlers[0].maxBytes, log.MAX_BYTES)
            cleanup()


if __name__ == "__main__":
    unittest.main()
