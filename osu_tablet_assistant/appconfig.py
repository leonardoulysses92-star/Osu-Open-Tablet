"""The assistant's own small settings file (never OTD's).

Stored as JSON in ``%APPDATA%\\OsuOpenTablet\\config.json`` on Windows
(``$XDG_CONFIG_HOME/OsuOpenTablet`` or ``~/.config/OsuOpenTablet`` elsewhere).
Writes are atomic; a missing, unreadable or corrupt file gives the defaults
(a corrupt file is kept aside as ``config.corrupt.json`` for inspection), and
every field is validated on load, so a bad value never reaches the program.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .engine.playtypes import ANALYSIS_DETAILS, DEFAULT_DETAIL
from .keys import DEFAULT_CLICK_KEYS, KeyChoice

APP_DIR_NAME = "OsuOpenTablet"
CONFIG_FILE = "config.json"
LANGUAGES = ("es", "en")
POLICIES = ("tune_only", "installed", "allow_new")
DEFAULT_UPDATE_URL = "https://raw.githubusercontent.com/leonardoulysses92-star/Osu-Open-Tablet/main/data/"
DEFAULT_NAME_TEMPLATE = "{tablet} {timestamp}"
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def app_data_dir(system: str | None = None, env: Mapping[str, str] | None = None,
                 home: Path | None = None) -> Path:
    """Folder for this program's config and cached community data."""
    system = system or platform.system()
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    if system == "Windows":
        base = env.get("APPDATA")
        return (Path(base) if base else home / "AppData" / "Roaming") / APP_DIR_NAME
    xdg = env.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else home / ".config") / APP_DIR_NAME


def default_config_path() -> Path:
    """``<app data dir>/config.json``."""
    return app_data_dir() / CONFIG_FILE


@dataclass
class AppConfig:
    """User choices that outlive one run (no telemetry, nothing is ever sent anywhere)."""
    language: str = "es"
    output_dir: str | None = None              # where presets are saved; None = OTD's Presets folder
    preset_name_template: str = DEFAULT_NAME_TEMPLATE   # {tablet} and {timestamp} are filled in
    plugin_policy: str = "installed"
    check_updates_on_start: bool = False       # off by default: no network access unless asked
    advanced_ui: bool = False                  # "Advanced" mode shows the technical cards (off = Simple)
    update_url: str = DEFAULT_UPDATE_URL
    last_update_check: str | None = None       # ISO timestamp of the last successful check
    version: int = 1
    # Play Coach. Click keys: see ``keys.KeyChoice`` (keyboard keys / extra buttons, never "mouse1");
    # pen_clicks adds the pen tip (mouse 1) to the keys that are read. Old files without these fields load fine.
    click_keys: list[str] = field(default_factory=lambda: list(DEFAULT_CLICK_KEYS))
    pen_clicks: bool = False
    analysis_detail: str = DEFAULT_DETAIL      # normal (3x4) | fine (6x8) | very_fine (9x12)

    @property
    def key_choice(self) -> KeyChoice:
        return KeyChoice.make(self.click_keys, self.pen_clicks)

    @property
    def effective_click_keys(self) -> tuple[str, ...]:
        """The keys the Play Coach reads (the pen tip only when ``pen_clicks``)."""
        return self.key_choice.effective

    def set_key_choice(self, choice: KeyChoice) -> None:
        self.click_keys, self.pen_clicks = list(choice.keys), choice.pen_clicks


def _valid_url(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 500:
        return False
    parts = urlparse(value)
    return parts.scheme == "https" and bool(parts.netloc)


def from_dict(data: Any) -> AppConfig:
    """An ``AppConfig`` from decoded JSON; invalid or unknown fields fall back to defaults."""
    cfg = AppConfig()
    if not isinstance(data, dict):
        return cfg
    checks = {
        "language": lambda v: v in LANGUAGES,
        "output_dir": lambda v: v is None or (isinstance(v, str) and 0 < len(v) < 1000),
        "preset_name_template": lambda v: isinstance(v, str) and 0 < len(v) <= 120,
        "plugin_policy": lambda v: v in POLICIES,
        "check_updates_on_start": lambda v: isinstance(v, bool),
        "advanced_ui": lambda v: isinstance(v, bool),
        "update_url": _valid_url,
        "last_update_check": lambda v: v is None or (isinstance(v, str) and len(v) <= 40),
        "version": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "click_keys": lambda v: isinstance(v, list) and len(v) <= 50 and all(isinstance(k, str) for k in v),
        "pen_clicks": lambda v: isinstance(v, bool),
        "analysis_detail": lambda v: v in ANALYSIS_DETAILS,
    }
    for f in fields(AppConfig):
        if f.name in data and checks[f.name](data[f.name]):
            setattr(cfg, f.name, data[f.name])
    cfg.set_key_choice(cfg.key_choice)     # known names only, no duplicates, never zero ways to click
    return cfg


def load_config(path: Path | None = None) -> AppConfig:
    """Read the config; never raises (defaults when missing or corrupt)."""
    path = path or default_config_path()
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return AppConfig()
    try:
        data = json.loads(text)
    except ValueError:
        try:   # keep the broken file for the user; the next save writes a clean one
            shutil.copy2(path, path.with_name("config.corrupt.json"))
        except OSError:
            pass
        return AppConfig()
    return from_dict(data)


def save_config(cfg: AppConfig, path: Path | None = None) -> Path:
    """Atomically write ``cfg`` (temp file + ``os.replace``); returns the path."""
    path = path or default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(asdict(from_dict(asdict(cfg))), indent=2, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return path


def preset_file_name(template: str, tablet: str, now: datetime | None = None) -> str:
    """A safe file name (without ``.json``) from ``template``; default "<tablet> <timestamp>"."""
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H-%M-%S")
    try:
        name = template.format(tablet=tablet, timestamp=stamp)
    except (KeyError, IndexError, ValueError):
        name = DEFAULT_NAME_TEMPLATE.format(tablet=tablet, timestamp=stamp)
    name = _UNSAFE.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(".").strip()
    return name[:120] or f"preset {stamp}"
