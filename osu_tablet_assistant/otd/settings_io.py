"""Locate, read, patch, back up and write OpenTabletDriver (OTD 0.6.x) settings.

Format notes (verified against a real OTD 0.6.7 ``settings.json``):

* Top level: ``Revision``, ``Profiles`` (one per tablet), ``Tools``,
  ``LockUsableAreaDisplay``, ``LockUsableAreaTablet``.
* A profile has ``Tablet`` (name), ``OutputMode`` / ``Filters`` (plugin stores:
  ``{"Path", "Settings": [{"Property", "Value"}], "Enable"}``),
  ``AbsoluteModeSettings`` (``Display`` / ``Tablet`` rects, ``EnableClipping``,
  ``EnableAreaLimiting``, ``LockAspectRatio``), ``RelativeModeSettings`` and
  ``Bindings``.
* Rect ``X``/``Y`` are *center* coordinates; display in px, tablet in mm.

Every writer in this module only writes to the directory it is given and never
touches running OTD processes.
"""
from __future__ import annotations

import copy
import json
import os
import platform
import shutil
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import FilterConfig, ProfileChanges, ProfileInfo, Rect

SETTINGS_FILE = "settings.json"
PRESETS_DIR = "Presets"
BACKUP_DIR = "Backup"
PORTABLE_DATA_DIR = "userdata"      # OTD portable mode keeps its config here
BACKUP_PREFIX = "osu-assistant-settings"
_INVALID_NAME_CHARS = set('<>:"/\\|?*') | {chr(c) for c in range(32)}


class OtdSettingsError(Exception):
    """A settings file is missing, unreadable or malformed.

    ``key``/``params`` form a translatable message (``locales/*/otd.json``).
    """

    def __init__(self, key: str, message: str, **params: Any) -> None:
        super().__init__(message)
        self.key = key
        self.params = params


# --------------------------------------------------------------------------
# Locating OTD
# --------------------------------------------------------------------------
def console_exe_name(system: str | None = None) -> str:
    """File name of the OTD console client on ``system`` (default: this OS)."""
    system = system or platform.system()
    return "OpenTabletDriver.Console.exe" if system == "Windows" else "OpenTabletDriver.Console"


def daemon_exe_name(system: str | None = None) -> str:
    """File name of the OTD daemon on ``system`` (default: this OS)."""
    system = system or platform.system()
    return "OpenTabletDriver.Daemon.exe" if system == "Windows" else "OpenTabletDriver.Daemon"


def default_config_dir(system: str, env: Mapping[str, str], home: Path) -> Path:
    """Where an installed (non-portable) OTD keeps its user data on ``system``."""
    if system == "Windows":
        base = env.get("LOCALAPPDATA")
        return Path(base) / "OpenTabletDriver" if base else home / "AppData" / "Local" / "OpenTabletDriver"
    if system == "Darwin":
        return home / "Library" / "Application Support" / "OpenTabletDriver"
    xdg = env.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else home / ".config") / "OpenTabletDriver"


def find_config_dir() -> Path | None:
    """Directory holding ``settings.json``, ``Plugins/`` and ``Presets/``, or None.

    A portable install (``<install>/userdata``) wins over the per-user default,
    like OTD itself. A directory without ``settings.json`` is still returned
    (OTD only writes it once a tablet has been configured).
    """
    candidates: list[Path] = []
    install = find_install_dir()
    if install is not None:
        candidates.append(install / PORTABLE_DATA_DIR)
    candidates.append(default_config_dir(platform.system(), os.environ, Path.home()))
    existing = [c for c in candidates if c.is_dir()]
    for cand in existing:
        if (cand / SETTINGS_FILE).is_file():
            return cand
    return existing[0] if existing else None


def install_candidates(system: str, env: Mapping[str, str], home: Path) -> list[Path]:
    """Directories worth checking for an OTD install, most likely first."""
    if system == "Windows":
        roots = [Path(env[v]) for v in ("ProgramFiles", "ProgramFiles(x86)") if env.get(v)]
        if env.get("LOCALAPPDATA"):
            roots.append(Path(env["LOCALAPPDATA"]) / "Programs")
        roots += [home, home / "Documents", home / "Desktop", home / "Downloads"]
        cands = [root / "OpenTabletDriver" for root in roots]
        for root in roots:   # portable zips get unpacked under arbitrary names
            cands += _subdirs_matching(root, "opentablet")
        return cands
    if system == "Darwin":
        return [Path("/Applications/OpenTabletDriver.app/Contents/MacOS"),
                home / "Applications" / "OpenTabletDriver.app" / "Contents" / "MacOS"]
    return [Path("/usr/lib/opentabletdriver"), Path("/usr/share/OpenTabletDriver"),
            Path("/opt/opentabletdriver"), home / ".local" / "share" / "OpenTabletDriver"]


def find_install_dir() -> Path | None:
    """Directory with ``OpenTabletDriver.Console(.exe)`` / ``Daemon(.exe)``, or None.

    Checks, in order: the folder of a running daemon (Windows), the PATH,
    then the usual install and unzip locations.
    """
    system = platform.system()
    cands = list(_running_daemon_dirs()) if system == "Windows" else []
    on_path = shutil.which(console_exe_name(system)) or shutil.which(daemon_exe_name(system))
    if on_path:
        cands.append(Path(on_path).resolve().parent)
    cands += install_candidates(system, os.environ, Path.home())
    for cand in cands:
        if is_install_dir(cand, system):
            return cand
    return None


def is_install_dir(path: Path, system: str | None = None) -> bool:
    """True when ``path`` contains the OTD console or daemon executable."""
    try:
        return (path / console_exe_name(system)).is_file() or (path / daemon_exe_name(system)).is_file()
    except OSError:
        return False


def _subdirs_matching(root: Path, needle: str) -> list[Path]:
    """Immediate subdirectories of ``root`` whose name contains ``needle`` (any case)."""
    try:
        return sorted(p for p in root.iterdir() if p.is_dir() and needle in p.name.lower())
    except OSError:
        return []


def _running_daemon_dirs() -> Iterable[Path]:
    """Folders of running ``OpenTabletDriver.Daemon.exe`` processes (Windows, read-only)."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.OpenProcess.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)   # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return []
    dirs: list[Path] = []
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(ProcessEntry)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == "opentabletdriver.daemon.exe":
                proc = k32.OpenProcess(0x1000, False, entry.th32ProcessID)  # QUERY_LIMITED_INFORMATION
                if proc:
                    buf = ctypes.create_unicode_buffer(32768)
                    size = wintypes.DWORD(len(buf))
                    if k32.QueryFullProcessImageNameW(proc, 0, buf, ctypes.byref(size)):
                        dirs.append(Path(buf.value).parent)
                    k32.CloseHandle(proc)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return dirs


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------
def load_settings(config_dir: Path) -> dict:
    """Return the raw ``settings.json`` of ``config_dir``.

    Raises ``OtdSettingsError`` when the file is missing, unreadable, not valid
    JSON or not a JSON object.
    """
    path = Path(config_dir) / SETTINGS_FILE
    if not path.is_file():
        raise OtdSettingsError("otd.error.settings_missing", f"settings file not found: {path}", path=str(path))
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError) as exc:
        raise OtdSettingsError("otd.error.settings_unreadable", f"cannot read {path}: {exc}",
                               path=str(path), detail=str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise OtdSettingsError("otd.error.settings_invalid_json", f"invalid JSON in {path}: {exc}",
                               path=str(path), detail=str(exc)) from exc
    if not isinstance(data, dict):
        raise OtdSettingsError("otd.error.settings_not_object", f"{path} is not a JSON object", path=str(path))
    return data


def list_profiles(settings: dict) -> list[ProfileInfo]:
    """One ``ProfileInfo`` per tablet profile; malformed entries are skipped."""
    profiles = settings.get("Profiles") if isinstance(settings, dict) else None
    result: list[ProfileInfo] = []
    for prof in profiles if isinstance(profiles, list) else []:
        if not isinstance(prof, dict) or not isinstance(prof.get("Tablet"), str):
            continue
        abs_mode = _as_dict(prof.get("AbsoluteModeSettings"))
        output = _as_dict(prof.get("OutputMode"))
        filters = prof.get("Filters")
        result.append(ProfileInfo(
            tablet_name=prof["Tablet"],
            output_mode_path=str(output.get("Path") or ""),
            display=rect_from_otd(abs_mode.get("Display")),
            tablet_area=rect_from_otd(abs_mode.get("Tablet")),
            lock_aspect_ratio=bool(abs_mode.get("LockAspectRatio", False)),
            filters=tuple(filter_from_otd(f) for f in (filters if isinstance(filters, list) else [])
                          if isinstance(f, dict) and isinstance(f.get("Path"), str)),
        ))
    return result


def rect_from_otd(data: Any) -> Rect:
    """OTD ``{Width, Height, X, Y, Rotation}`` -> ``Rect``; bad or missing numbers become 0."""
    d = _as_dict(data)
    return Rect(width=_num(d.get("Width")), height=_num(d.get("Height")),
                x=_num(d.get("X")), y=_num(d.get("Y")), rotation=_num(d.get("Rotation")))


def filter_from_otd(store: dict) -> FilterConfig:
    """OTD plugin store -> ``FilterConfig`` (values keep their JSON types)."""
    values: dict[str, Any] = {}
    for item in store.get("Settings") or []:
        if isinstance(item, dict) and isinstance(item.get("Property"), str):
            values[item["Property"]] = item.get("Value")
    return FilterConfig(path=store["Path"], enabled=bool(store.get("Enable", True)), settings=values)


def filter_to_otd(cfg: FilterConfig, base: dict | None = None) -> dict:
    """``FilterConfig`` -> OTD plugin store.

    With ``base`` (the existing store for the same path) unknown keys, the
    property order and properties not mentioned in ``cfg`` are preserved;
    ``cfg`` values win.
    """
    store = copy.deepcopy(base) if base else {}
    items = [copy.deepcopy(i) for i in store.get("Settings") or [] if isinstance(i, dict)]
    seen: set[str] = set()
    for item in items:
        prop = item.get("Property")
        if prop in cfg.settings:
            item["Value"] = copy.deepcopy(cfg.settings[prop])
        seen.add(prop)
    items += [{"Property": k, "Value": copy.deepcopy(v)} for k, v in cfg.settings.items() if k not in seen]
    store["Path"] = cfg.path
    store["Settings"] = items
    store["Enable"] = cfg.enabled
    return store


# --------------------------------------------------------------------------
# Patching (pure)
# --------------------------------------------------------------------------
def apply_changes(settings: dict, tablet_name: str, changes: ProfileChanges) -> dict:
    """Return a deep copy of ``settings`` with ``changes`` applied to one profile.

    The profile is matched by exact ``Tablet`` name (``KeyError`` if absent).
    Only the fields ``changes`` sets are replaced; every other key is kept.
    """
    new = copy.deepcopy(settings)
    profiles = new.get("Profiles") if isinstance(new, dict) else None
    profile = next((p for p in profiles if isinstance(p, dict) and p.get("Tablet") == tablet_name), None) \
        if isinstance(profiles, list) else None
    if profile is None:
        known = [p.get("Tablet") for p in profiles if isinstance(p, dict)] if isinstance(profiles, list) else []
        raise KeyError(f"no OTD profile for tablet {tablet_name!r}; profiles found: {known}")

    if changes.tablet_area is not None or changes.display_area is not None \
            or changes.lock_aspect_ratio is not None:
        abs_mode = _child_dict(profile, "AbsoluteModeSettings")
        if changes.tablet_area is not None:
            _write_rect(_child_dict(abs_mode, "Tablet"), changes.tablet_area)
        if changes.display_area is not None:
            _write_rect(_child_dict(abs_mode, "Display"), changes.display_area)
        if changes.lock_aspect_ratio is not None:
            abs_mode["LockAspectRatio"] = changes.lock_aspect_ratio

    if changes.filters is not None:
        old: dict[str, list[dict]] = {}
        for store in profile.get("Filters") or []:
            if isinstance(store, dict) and isinstance(store.get("Path"), str):
                old.setdefault(store["Path"], []).append(store)
        profile["Filters"] = [filter_to_otd(cfg, (old.get(cfg.path) or [None]).pop(0))
                              for cfg in changes.filters]
    return new


def _write_rect(target: dict, rect: Rect) -> None:
    target.update(Width=float(rect.width), Height=float(rect.height),
                  X=float(rect.x), Y=float(rect.y), Rotation=float(rect.rotation))


def _child_dict(parent: dict, key: str) -> dict:
    """``parent[key]``, replaced by a new dict when missing or not an object."""
    if not isinstance(parent.get(key), dict):
        parent[key] = {}
    return parent[key]


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _num(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------
def backup_settings(config_dir: Path) -> Path:
    """Copy ``settings.json`` to ``<config>/Backup/osu-assistant-settings-<timestamp>.json``.

    Raises ``OtdSettingsError`` if there is no settings file to back up.
    """
    src = Path(config_dir) / SETTINGS_FILE
    if not src.is_file():
        raise OtdSettingsError("otd.error.settings_missing", f"settings file not found: {src}", path=str(src))
    folder = Path(config_dir) / BACKUP_DIR
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = folder / f"{BACKUP_PREFIX}-{stamp}.json"
    n = 1
    while dest.exists():
        n += 1
        dest = folder / f"{BACKUP_PREFIX}-{stamp}-{n}.json"
    shutil.copy2(src, dest)
    return dest


def save_preset(config_dir: Path, name: str, settings: dict, overwrite: bool = False,
                destination: Path | None = None) -> Path:
    """Write ``settings`` as ``<name>.json`` (atomic) and return the path.

    The file goes to ``<config>/Presets/`` or, with ``destination``, straight
    into that folder (e.g. the user's own folder next to a portable OTD). It is
    a complete OTD settings document, loadable with OTD's "Load settings".
    Overwriting needs ``overwrite=True`` and first copies the old file to
    ``<folder>/Backup/<name>-<timestamp>.json``. Raises ``OtdSettingsError``
    for an unsafe name, or when the file exists and ``overwrite`` is False.
    """
    if not is_valid_preset_name(name):
        raise OtdSettingsError("otd.error.preset_name_invalid", f"invalid preset name: {name!r}", name=name)
    folder = Path(destination) if destination is not None else Path(config_dir) / PRESETS_DIR
    path = folder / f"{name}.json"
    if path.exists():
        if not overwrite:
            raise OtdSettingsError("otd.error.preset_exists", f"preset already exists: {path}", path=str(path))
        backup_file(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(path, settings)
    return path


def backup_file(path: Path) -> Path:
    """Copy ``path`` to ``<its folder>/Backup/<stem>-<timestamp>.json`` and return the copy."""
    folder = path.parent / BACKUP_DIR
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = folder / f"{path.stem}-{stamp}{path.suffix}"
    n = 1
    while dest.exists():
        n += 1
        dest = folder / f"{path.stem}-{stamp}-{n}{path.suffix}"
    shutil.copy2(path, dest)
    return dest


FULL_SETTINGS_KEYS = ("Revision", "Profiles", "LockUsableAreaDisplay", "LockUsableAreaTablet", "Tools")


def is_full_settings(doc: Any) -> bool:
    """True for a complete OTD 0.6 settings document (what "Load settings" accepts)."""
    return isinstance(doc, dict) and all(k in doc for k in FULL_SETTINGS_KEYS) \
        and isinstance(doc.get("Profiles"), list)


# --------------------------------------------------------------------------
# OTD version and config locations (read-only)
# --------------------------------------------------------------------------
RECOMMENDED_OTD = (0, 6, 8, 1)


def otd_version(settings: Any) -> tuple[int, ...] | None:
    """OTD version that wrote ``settings`` (its ``Revision``, e.g. "0.6.7"), or None."""
    revision = settings.get("Revision") if isinstance(settings, dict) else None
    if not isinstance(revision, str):
        return None
    parts = revision.strip().split(".")
    if not 2 <= len(parts) <= 4 or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def version_notice(settings: Any) -> tuple[str, dict[str, Any]] | None:
    """Informational (never forced) notice when OTD is older than 0.6.8.1.

    The 2026-10-30 audit: 0.6.8 keeps aux buttons/touch working with an
    interpolator, and 0.6.8.1 fixes a Windows daemon crash with async filters
    in 0.6.8. Returns an ``(i18n key, params)`` pair or None.
    """
    version = otd_version(settings)
    if version is None:
        return None
    padded = version + (0,) * (4 - len(version))
    if padded >= RECOMMENDED_OTD:
        return None
    return "otd.notice.old_version", {"version": ".".join(map(str, version)),
                                      "recommended": ".".join(map(str, RECOMMENDED_OTD))}


PORTABLE_MARKER = "convert_to_portable.bat"
_MAX_PROBE_BYTES = 5_000_000


@dataclass(frozen=True)
class ConfigLocation:
    """A folder OTD may read its settings from (``kind``: "installed" | "portable")."""
    path: Path
    kind: str
    settings_files: tuple[Path, ...] = ()   # full settings documents found there (e.g. Osu.json)


def is_portable_dir(path: Path, system: str | None = None) -> bool:
    """A portable OTD folder: the daemon or ``convert_to_portable.bat`` next to settings/userdata."""
    try:
        if not ((path / PORTABLE_MARKER).is_file() or (path / daemon_exe_name(system)).is_file()):
            return False
        return (path / PORTABLE_DATA_DIR).is_dir() or bool(settings_documents(path))
    except OSError:
        return False


def settings_documents(folder: Path) -> tuple[Path, ...]:
    """``*.json`` files directly in ``folder`` that are complete OTD settings documents."""
    found = []
    try:
        files = sorted(p for p in folder.glob("*.json") if p.is_file())
    except OSError:
        return ()
    for path in files:
        try:
            if path.stat().st_size > _MAX_PROBE_BYTES:
                continue
            if is_full_settings(json.loads(path.read_text(encoding="utf-8-sig"))):
                found.append(path)
        except (OSError, UnicodeDecodeError, ValueError):
            continue
    return tuple(found)


def config_locations(system: str | None = None, env: Mapping[str, str] | None = None,
                     home: Path | None = None, extra: Iterable[Path] = ()) -> list[ConfigLocation]:
    """Every OTD settings location found: the per-user folder and portable installs.

    For a portable install the location is its ``userdata`` folder when it
    exists, else the install folder itself (where a saved ``Osu.json`` may
    live). ``extra`` adds folders to probe (e.g. the user's output folder).
    """
    system = system or platform.system()
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    out: list[ConfigLocation] = []
    seen: set[str] = set()

    def add(path: Path, kind: str, docs_from: Iterable[Path]) -> None:
        key = os.path.normcase(str(path))
        if key in seen:
            return
        seen.add(key)
        docs = tuple(doc for folder in docs_from for doc in settings_documents(folder))
        out.append(ConfigLocation(path, kind, docs))

    default = default_config_dir(system, env, home)
    if default.is_dir():
        add(default, "installed", (default,))
    for cand in list(extra) + install_candidates(system, env, home):
        if is_portable_dir(cand, system):
            data = cand / PORTABLE_DATA_DIR
            add(data if data.is_dir() else cand, "portable", (cand, data) if data.is_dir() else (cand,))
    return out


def is_valid_preset_name(name: str) -> bool:
    """A preset name usable as a single file name on every OS."""
    return (isinstance(name, str) and name.strip() == name and name not in ("", ".", "..")
            and not name.endswith(".") and not (set(name) & _INVALID_NAME_CHARS))


def write_settings(config_dir: Path, settings: dict) -> None:
    """Atomically replace ``<config>/settings.json`` (temp file + ``os.replace``).

    Raises ``OtdSettingsError`` if ``config_dir`` does not exist and
    ``TypeError`` if ``settings`` is not a dict.
    """
    if not isinstance(settings, dict):
        raise TypeError("settings must be a dict")
    folder = Path(config_dir)
    if not folder.is_dir():
        raise OtdSettingsError("otd.error.config_dir_missing", f"config dir not found: {folder}", path=str(folder))
    _atomic_write_json(folder / SETTINGS_FILE, settings)


def dumps_settings(settings: dict) -> str:
    """Serialize like OTD does (2-space indent, non-ASCII kept)."""
    return json.dumps(settings, indent=2, ensure_ascii=False)


def _atomic_write_json(path: Path, data: dict) -> None:
    text = dumps_settings(data)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(5):   # Windows: the target may be briefly open in another process
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.1 * (attempt + 1))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
