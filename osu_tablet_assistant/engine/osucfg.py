"""osu!stable's user settings (``osu!.<user>.cfg``): how far the OS cursor can be trusted, where the maps are.

The file is plain ``Key = Value`` text next to ``osu!.exe``. Only the keys below are read; everything else
(the user name, the password hash, ...) is skipped line by line and never kept. Reading it touches a FILE,
never osu!'s memory.

Why the coach cares (research note "pointer-and-map", section 1): the coach records the OS cursor. That is
osu!'s cursor only while osu! uses it unchanged. With **raw input** on and either **"map absolute raw input
to the osu! window"** or a **sensitivity other than 1**, osu! computes its own cursor from the raw device data,
and desktop pixels no longer map to the playfield by a fixed formula. A low **frame limit** makes osu!'s
positions a step function next to a 1.5 kHz trace. :func:`pointer_trust` turns that into a verdict; the replay
calibration (``engine.calibrate``) is what measures the real transform.

:func:`locate_osu_dir` finds the install from the player's own setting, the usual folder, or the path of
osu!'s running executable (``QueryFullProcessImageNameW``: the path only, nothing inside the process).
"""
from __future__ import annotations

import getpass
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..models import Reason

MAX_CFG_BYTES = 2 * 1024 * 1024
LOW_FRAME_LIMIT_HZ = 200.0      # below this osu!'s positions can lag a fast jump by several osu! px (own estimate)
_KEYS = ("BeatmapDirectory", "Fullscreen", "Width", "Height", "WidthFullscreen", "HeightFullscreen",
         "Letterboxing", "LetterboxPositionX", "LetterboxPositionY", "RawInput", "MouseSpeed",
         "AbsoluteToOsuWindow", "FrameSync", "CustomFrameLimit", "keyOsuLeft", "keyOsuRight", "ConfineMouse")
# FrameSync values of osu!stable -> frames per second as a multiple of the refresh rate (None = no limit).
_FRAME_SYNC = {"vsync": 1.0, "limit2x": 2.0, "limit4x": 4.0, "limit8x": 8.0, "unlimited": None}


@dataclass(frozen=True)
class OsuConfig:
    """The few settings of ``osu!.<user>.cfg`` the coach needs (None: the file does not say)."""
    beatmap_directory: str | None = None
    fullscreen: bool | None = None
    width: int | None = None
    height: int | None = None
    width_fullscreen: int | None = None
    height_fullscreen: int | None = None
    letterboxing: bool | None = None
    letterbox_x: int | None = None        # LetterboxPositionX/Y (units and range unverified)
    letterbox_y: int | None = None
    raw_input: bool | None = None
    mouse_speed: float | None = None      # sensitivity
    absolute_to_osu_window: bool | None = None
    frame_sync: str | None = None         # VSync | Limit2x | Limit4x | Limit8x | LimitCustom | Unlimited | ...
    custom_frame_limit: int | None = None
    key_left: str | None = None           # keyOsuLeft / keyOsuRight (osu!'s own key names, e.g. "Z")
    key_right: str | None = None
    confine_mouse: str | None = None      # Never | Fullscreen | Always

    def frame_limit_hz(self, refresh_hz: float = 60.0) -> float | None:
        """Frames per second osu! is limited to, or None when unlimited / unknown."""
        if not self.frame_sync:
            return None
        mode = self.frame_sync.strip().lower()
        if mode == "limitcustom":
            return float(self.custom_frame_limit) if self.custom_frame_limit and self.custom_frame_limit > 0 else None
        factor = _FRAME_SYNC.get(mode)
        return None if factor is None else factor * refresh_hz


def _bool(text: str | None) -> bool | None:
    if text is None:
        return None
    value = text.strip().lower()
    return True if value in ("1", "true", "yes") else False if value in ("0", "false", "no") else None


def _int(text: str | None) -> int | None:
    try:
        return int(float(text)) if text is not None else None
    except ValueError:
        return None


def _float(text: str | None) -> float | None:
    try:
        return float(text.replace(",", ".")) if text is not None else None
    except ValueError:
        return None


def parse_cfg(text: str) -> OsuConfig:
    """Parse the text of an ``osu!.<user>.cfg``. Unknown keys are dropped without being stored."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and key in _KEYS:
            values[key] = value.strip()
    g = values.get
    return OsuConfig(
        beatmap_directory=g("BeatmapDirectory") or None, fullscreen=_bool(g("Fullscreen")),
        width=_int(g("Width")), height=_int(g("Height")), width_fullscreen=_int(g("WidthFullscreen")),
        height_fullscreen=_int(g("HeightFullscreen")), letterboxing=_bool(g("Letterboxing")),
        letterbox_x=_int(g("LetterboxPositionX")), letterbox_y=_int(g("LetterboxPositionY")),
        raw_input=_bool(g("RawInput")), mouse_speed=_float(g("MouseSpeed")),
        absolute_to_osu_window=_bool(g("AbsoluteToOsuWindow")), frame_sync=g("FrameSync") or None,
        custom_frame_limit=_int(g("CustomFrameLimit")), key_left=g("keyOsuLeft") or None,
        key_right=g("keyOsuRight") or None, confine_mouse=g("ConfineMouse") or None)


def find_cfg(osu_dir: str | Path | None, user: str | None = None) -> Path | None:
    """``osu!.<user>.cfg`` in an install: the one for this Windows user, else the most recently written."""
    if not osu_dir:
        return None
    root = Path(osu_dir)
    try:
        found = [p for p in root.glob("osu!.*.cfg") if p.is_file()]
    except OSError:
        return None
    if not found:
        return None
    if user is None:
        try:
            user = getpass.getuser()
        except Exception:
            user = ""
    own = [p for p in found if user and p.name.casefold() == f"osu!.{user}.cfg".casefold()]
    if own:
        return own[0]

    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    return max(found, key=mtime)


def load_cfg(osu_dir: str | Path | None, user: str | None = None) -> OsuConfig | None:
    """Read the user config of an install; None when there is none or it cannot be read."""
    path = find_cfg(osu_dir, user)
    if path is None:
        return None
    try:
        if path.stat().st_size > MAX_CFG_BYTES:
            return None
        return parse_cfg(path.read_text(encoding="utf-8-sig", errors="replace"))
    except OSError:
        return None


def songs_dir(osu_dir: str | Path | None, cfg: OsuConfig | None = None) -> Path | None:
    """The ``Songs`` folder of an install (``BeatmapDirectory`` when set, relative to the install or absolute)."""
    if not osu_dir:
        return None
    root = Path(osu_dir)
    cfg = cfg if cfg is not None else load_cfg(root)
    if cfg is not None and cfg.beatmap_directory:
        path = Path(cfg.beatmap_directory)
        return path if path.is_absolute() else root / path
    songs = root / "Songs"
    return songs if songs.is_dir() else None


# ------------------------------------------------------------------ the verdict
@dataclass(frozen=True)
class PointerTrust:
    verdict: str                          # "direct" | "needs_calibration"
    reasons: tuple[Reason, ...] = ()

    @property
    def direct(self) -> bool:
        return self.verdict == "direct"

    @property
    def reason(self) -> Reason | None:
        return self.reasons[0] if self.reasons else None


def pointer_trust(cfg: OsuConfig | None, refresh_hz: float = 60.0) -> PointerTrust:
    """Can the OS cursor stand for osu!'s cursor with these settings? ("direct" / "needs_calibration")."""
    if cfg is None:
        return PointerTrust("direct")
    reasons: list[Reason] = []
    if cfg.raw_input:
        if cfg.absolute_to_osu_window:
            reasons.append(Reason("play.pointer.raw_window"))
        if cfg.mouse_speed is not None and abs(cfg.mouse_speed - 1.0) > 1e-3:
            reasons.append(Reason("play.pointer.raw_speed", {"pct": int(round(cfg.mouse_speed * 100))}))
    limit = cfg.frame_limit_hz(refresh_hz)
    if limit is not None and limit < LOW_FRAME_LIMIT_HZ:
        reasons.append(Reason("play.pointer.low_fps", {"fps": int(round(limit))}))
    return PointerTrust("needs_calibration" if reasons else "direct", tuple(reasons))


# --------------------------------------------------------------- the install
def is_osu_dir(path: str | Path | None) -> bool:
    if not path:
        return False
    p = Path(path)
    try:
        return (p / "osu!.exe").is_file() or (p / "osu!.db").is_file()
    except OSError:
        return False


def process_image_path(pid: int | None) -> str | None:
    """Full path of a process's executable (``QueryFullProcessImageNameW``); None off Windows or on error.

    Opens the process with ``PROCESS_QUERY_LIMITED_INFORMATION`` only: it reads the path, nothing inside it.
    """
    if os.name != "nt" or not pid:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                   ctypes.POINTER(wintypes.DWORD)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k32.OpenProcess(0x1000, False, int(pid))     # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            if not k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return None
            return buf.value or None
        finally:
            k32.CloseHandle(handle)
    except (AttributeError, OSError, ImportError, ValueError):
        return None


def locate_osu_dir(configured: str | Path | None = None, process_path: str | Path | None = None,
                   pid: int | None = None, env: Mapping[str, str] | None = None) -> Path | None:
    """The osu!stable folder: the player's own setting, then ``%LOCALAPPDATA%\\osu!``, then the folder of the
    running ``osu!.exe`` (its known path, or asked from the window's process id). None when none is found."""
    env = os.environ if env is None else env
    candidates: list[str | Path] = []
    if configured:
        candidates.append(configured)
    local = env.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "osu!")
    if process_path:
        candidates.append(Path(process_path).parent)
    for candidate in candidates:
        if is_osu_dir(candidate):
            return Path(candidate)
    image = process_image_path(pid) if pid and not process_path else None
    if image and is_osu_dir(Path(image).parent):
        return Path(image).parent
    return None
