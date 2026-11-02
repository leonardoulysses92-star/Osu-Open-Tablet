"""Local log file for errors the program recovers from on its own.

The program catches some errors on purpose (an odd beatmap, a replay it cannot read, a window that
cannot be measured) so the wizard or the coach keeps working. They are written here so a bug report can
include them. The file lives in the assistant's own folder (``%APPDATA%\\OsuOpenTablet\\osu-open-tablet.log``
on Windows), is capped at about 256 KB plus one older copy, and is never sent anywhere.

Until :func:`setup` is called (``__main__`` does it) nothing is written, so tests and imports stay silent.
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

LOG_FILE = "osu-open-tablet.log"
MAX_BYTES = 256 * 1024
BACKUPS = 1

logger = logging.getLogger("osu_tablet_assistant")
logger.addHandler(logging.NullHandler())      # no output at all until setup()


def setup(folder: Path) -> Path | None:
    """Start writing warnings to ``folder/osu-open-tablet.log``; returns the path, or None when it cannot."""
    path = Path(folder) / LOG_FILE
    if any(isinstance(h, logging.handlers.RotatingFileHandler) for h in logger.handlers):
        return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS,
                                                       encoding="utf-8", delay=True)
    except OSError:
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    return path


def log_exception(what: str) -> None:
    """Record the exception being handled, with its traceback (call it from an ``except`` block)."""
    logger.warning("%s failed", what, exc_info=True)
