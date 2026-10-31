"""Live tuning of a running OpenTabletDriver daemon, plus osu! process detection.

``LiveTuner`` keeps a model of the settings that are live in the daemon and
pushes changes with one direct JSON-RPC ``SetSettings`` per send (see
``OtdController.apply_rpc``). Verified against OTD 0.6.x ``DriverDaemon.cs``:
``SetSettings`` only replaces the daemon's in-memory settings and rebuilds the
pipeline; it never writes ``settings.json`` (the file is only *read* when the
daemon starts). So every live change is "live, not saved" until the user
presses Keep, which writes ``settings.json`` through ``settings_io`` (after a
backup). Consequences, documented in ``docs/COMPANION.md``:

* restarting OTD drops unsaved live changes (that is the safe default);
* the OTD GUI keeps its own copy: pressing *Save/Apply* there overwrites ours.

Safety: values the tuner changes are clamped to ``filters.json`` bounds before
they are sent; an area that leaves the tablet is rejected; sends are
rate-limited (``min_interval_s``) and coalesced; a failed send restores the
last settings the daemon acknowledged (auto-revert) and re-sends them once.
Every dependency (sender, clock, backup/writer, process lister) is injectable.
"""
from __future__ import annotations

import copy
import csv
import io
import math
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..knowledge.filters import FINEST_GRID_DIVISOR, FilterSpec, load_catalog
from ..models import ApplyResult, ProfileChanges, ProfileInfo, Reason, Rect, TabletSpec
from . import settings_io

DEFAULT_MIN_INTERVAL_S = 0.15
DEFAULT_RPC_TIMEOUT_S = 1.5
MAX_UNDO = 500

Sender = Callable[[dict], ApplyResult]


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class SendOutcome:
    """One attempt to push the model into the daemon."""
    ok: bool
    message_key: str
    detail: str = ""
    reverted: bool = False        # the model went back to the last acknowledged settings


@dataclass(frozen=True)
class ProposeResult:
    accepted: bool
    reasons: tuple[Reason, ...] = ()
    sent: SendOutcome | None = None   # None: queued (rate limit) or nothing to send


@dataclass(frozen=True)
class KeepResult:
    ok: bool
    message_key: str
    detail: str = ""
    backup_path: Path | None = None


# --------------------------------------------------------------------------
# Bounds
# --------------------------------------------------------------------------
def _profile(settings: dict, tablet_name: str) -> dict | None:
    profiles = settings.get("Profiles") if isinstance(settings, dict) else None
    for prof in profiles if isinstance(profiles, list) else []:
        if isinstance(prof, dict) and prof.get("Tablet") == tablet_name:
            return prof
    return None


def _stores(profile: dict | None) -> list[dict]:
    stores = profile.get("Filters") if isinstance(profile, dict) else None
    return [s for s in stores if isinstance(s, dict)] if isinstance(stores, list) else []


def _values(store: dict) -> dict[str, Any]:
    return {i["Property"]: i.get("Value") for i in store.get("Settings") or []
            if isinstance(i, dict) and isinstance(i.get("Property"), str)}


def enforce_bounds(old: dict, new: dict, tablet_name: str, catalog: Mapping[str, FilterSpec],
                   tablet: TabletSpec | None = None) -> tuple[dict, list[Reason], list[Reason]]:
    """Clamp every filter value that differs from ``old`` to the catalog bounds.

    Returns ``(settings, clamp_notes, violations)``. Values the user already
    had are left alone even if outside our bounds (they were not written by
    us). ``violations`` (area off the tablet, empty area) mean: reject.
    """
    fixed = copy.deepcopy(new)
    notes: list[Reason] = []
    violations: list[Reason] = []
    old_prof, new_prof = _profile(old, tablet_name), _profile(fixed, tablet_name)
    if new_prof is None:
        return fixed, notes, [Reason("livetune.error.no_profile", {"tablet": tablet_name})]
    seen: dict[str, int] = {}
    old_by_path: dict[str, list[dict]] = {}
    for store in _stores(old_prof):
        old_by_path.setdefault(str(store.get("Path")), []).append(store)
    for store in _stores(new_prof):
        path = str(store.get("Path"))
        n = seen.get(path, 0)
        seen[path] = n + 1
        before = old_by_path.get(path, [])
        old_vals = _values(before[n]) if n < len(before) else {}
        spec = catalog.get(path)
        if spec is None:
            continue
        for item in store.get("Settings") or []:
            if not isinstance(item, dict) or "Property" not in item:
                continue
            prop, value = item["Property"], item.get("Value")
            param = spec.param(prop)
            if param is None or (prop in old_vals and old_vals[prop] == value):
                continue
            # bounds always; the grid is the finest one the companion's fine step levels may use
            clamped = param.clamp(value, grid=param.grid(FINEST_GRID_DIVISOR))
            if clamped != value:
                notes.append(Reason("livetune.clamped", {"filter": spec.display_name, "param": prop,
                                                         "value": value, "new": clamped}))
                item["Value"] = clamped
    old_area = ((old_prof or {}).get("AbsoluteModeSettings") or {}).get("Tablet")
    new_area = (new_prof.get("AbsoluteModeSettings") or {}).get("Tablet")
    if new_area != old_area and isinstance(new_area, dict):
        rect = settings_io.rect_from_otd(new_area)
        if rect.width <= 0 or rect.height <= 0:
            violations.append(Reason("livetune.error.area_empty"))
        elif tablet is not None and not area_fits(rect, tablet):
            violations.append(Reason("livetune.error.area_outside", {"w": rect.width, "h": rect.height}))
    return fixed, notes, violations


def area_fits(rect: Rect, tablet: TabletSpec, tolerance_mm: float = 1e-6) -> bool:
    """True when the (rotated) area lies inside the tablet's active area."""
    rad = math.radians(rect.rotation)
    c, s = abs(math.cos(rad)), abs(math.sin(rad))
    ew, eh = rect.width * c + rect.height * s, rect.width * s + rect.height * c
    return (rect.x - ew / 2 >= -tolerance_mm and rect.x + ew / 2 <= tablet.width_mm + tolerance_mm
            and rect.y - eh / 2 >= -tolerance_mm and rect.y + eh / 2 <= tablet.height_mm + tolerance_mm)


# --------------------------------------------------------------------------
# Live tuner
# --------------------------------------------------------------------------
def controller_sender(controller: Any, timeout_s: float = DEFAULT_RPC_TIMEOUT_S) -> Sender:
    """Sender using ``controller.apply_rpc`` (fast path), else ``controller.apply``."""
    fast = getattr(controller, "apply_rpc", None)
    if callable(fast):
        return lambda settings: fast(settings, timeout_s)
    return controller.apply


def dry_run_sender(settings: dict) -> ApplyResult:
    """Sender that applies nothing (dry-run sessions)."""
    return ApplyResult(True, "dry_run", "livetune.dry_run")


class LiveTuner:
    """Model of the daemon's live settings with rate-limited sends, undo and Keep.

    ``meta`` is an opaque value travelling with each settings state (the
    companion stores its adaptation state there), so undo and auto-revert
    restore both together.
    """

    def __init__(self, settings: dict, tablet_name: str, sender: Sender, *,
                 config_dir: Path | None = None, tablet: TabletSpec | None = None,
                 catalog: Mapping[str, FilterSpec] | None = None,
                 backup: Callable[[Path], Path] | None = None,
                 writer: Callable[[Path, dict], None] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
                 meta: Any = None, can_keep: bool = True) -> None:
        if not isinstance(settings, dict):
            raise TypeError("settings must be a dict")
        if _profile(settings, tablet_name) is None:
            raise KeyError(f"no OTD profile for tablet {tablet_name!r}")
        self.tablet_name = tablet_name
        self.tablet = tablet
        self.config_dir = Path(config_dir) if config_dir else None
        self.catalog = dict(catalog) if catalog is not None else load_catalog()
        self.min_interval_s = float(min_interval_s)
        self.can_keep = can_keep
        self._sender = sender
        self._backup = backup or settings_io.backup_settings
        self._writer = writer or settings_io.write_settings
        self._clock = clock
        self.original = copy.deepcopy(settings)
        self._live = copy.deepcopy(settings)
        self._meta = meta
        self._undo: list[tuple[dict, Any]] = []
        # What the daemon is known to run (we assume the given settings are live).
        self._acked: tuple[dict, Any, list[tuple[dict, Any]]] = (copy.deepcopy(settings), meta, [])
        self._saved = copy.deepcopy(settings)
        self._unsent = False
        self._last_send_t = -float("inf")
        self.backup_path: Path | None = None
        self.backup_error: str | None = None
        self._backup_done = False
        self.send_count = 0
        self.last_outcome: SendOutcome | None = None

    # ------------------------------------------------------------ inspection
    @property
    def settings(self) -> dict:
        """Deep copy of the model (what is, or will shortly be, live)."""
        return copy.deepcopy(self._live)

    @property
    def meta(self) -> Any:
        return self._meta

    def replace_meta(self, meta: Any) -> None:
        """Swap the meta of the current state without creating an undo step."""
        if self._acked[0] == self._live and self._acked[1] is self._meta:
            self._acked = (self._acked[0], meta, self._acked[2])
        self._meta = meta

    def profile(self) -> ProfileInfo:
        """The target profile of the model."""
        for prof in settings_io.list_profiles(self._live):
            if prof.tablet_name == self.tablet_name:
                return prof
        raise KeyError(self.tablet_name)

    @property
    def pending(self) -> bool:
        """A change waits for the rate limit before it is sent."""
        return self._unsent

    @property
    def dirty(self) -> bool:
        """Live settings differ from what ``settings.json`` holds (as far as we know)."""
        return self._live != self._saved

    @property
    def change_count(self) -> int:
        return len(self._undo)

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    # --------------------------------------------------------------- changes
    def propose(self, changes: ProfileChanges, meta: Any = None) -> ProposeResult:
        """Apply ``changes`` to the model (bounded) and send if the rate limit allows."""
        try:
            candidate = settings_io.apply_changes(self._live, self.tablet_name, changes)
        except (KeyError, TypeError, ValueError) as exc:
            return ProposeResult(False, (Reason("livetune.error.patch", {"detail": str(exc)}),))
        bounded, notes, violations = enforce_bounds(self._live, candidate, self.tablet_name,
                                                    self.catalog, self.tablet)
        if violations:
            return ProposeResult(False, tuple(violations))
        if bounded == self._live:
            return ProposeResult(False, (Reason("livetune.no_change"),))
        self._ensure_backup()
        self._undo.append((self._live, self._meta))
        del self._undo[:-MAX_UNDO]
        self._live, self._meta = bounded, meta
        self._unsent = True
        return ProposeResult(True, tuple(notes), self.pump())

    def undo(self) -> ProposeResult:
        """Go back one change (exact previous settings dict)."""
        if not self._undo:
            return ProposeResult(False, (Reason("livetune.nothing_to_undo"),))
        self._live, self._meta = self._undo.pop()
        self._unsent = True
        return ProposeResult(True, (Reason("livetune.undone"),), self.pump())

    def revert_all(self) -> ProposeResult:
        """Back to the settings the tuner started with (undo history is cleared)."""
        if self._live == self.original and not self._undo:
            return ProposeResult(False, (Reason("livetune.no_change"),))
        first_meta = self._undo[0][1] if self._undo else self._meta
        self._live, self._meta = copy.deepcopy(self.original), first_meta
        self._undo.clear()
        self._unsent = True
        return ProposeResult(True, (Reason("livetune.reverted_all"),), self.pump())

    # ------------------------------------------------------------------ send
    def pump(self, now: float | None = None) -> SendOutcome | None:
        """Send the latest model if it is unsent and the rate limit has elapsed."""
        if not self._unsent:
            return None
        now = self._clock() if now is None else now
        if now - self._last_send_t < self.min_interval_s:
            return None
        return self._send(now)

    def flush(self) -> SendOutcome | None:
        """Send now, ignoring the rate limit (closing the companion, tests)."""
        return self._send(self._clock()) if self._unsent else None

    def _send(self, now: float) -> SendOutcome:
        self._last_send_t = now
        self._unsent = False
        snapshot = copy.deepcopy(self._live)
        result = self._call_sender(snapshot)
        self.send_count += 1
        if result.ok:
            self._acked = (snapshot, self._meta, list(self._undo))
            outcome = SendOutcome(True, result.message_key, result.detail)
        else:
            acked, meta, stack = self._acked
            self._live, self._meta, self._undo = copy.deepcopy(acked), meta, list(stack)
            retry = self._call_sender(copy.deepcopy(acked))   # make sure the daemon runs the known state
            self.send_count += 1
            detail = f"{result.detail}\nrevert: ok={retry.ok} {retry.detail}".strip()
            key = result.message_key if result.message_key.startswith("livetune.") else "livetune.error.send"
            outcome = SendOutcome(False, key, detail, reverted=True)
        self.last_outcome = outcome
        return outcome

    def _call_sender(self, settings: dict) -> ApplyResult:
        try:
            return self._sender(settings)
        except Exception as exc:  # the sender talks to another process; never crash the GUI
            return ApplyResult(False, "none", "otd.apply.failed", f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ keep
    def _ensure_backup(self) -> None:
        """First-use backup of settings.json (once; failure is recorded, not raised)."""
        if self._backup_done or self.config_dir is None:
            return
        self._backup_done = True
        try:
            self.backup_path = self._backup(self.config_dir)
        except settings_io.OtdSettingsError as exc:
            self.backup_error = exc.key        # e.g. no settings.json yet: nothing to lose
        except OSError as exc:
            self.backup_error = f"os: {exc}"

    def keep(self) -> KeepResult:
        """Persist the live settings into ``settings.json`` (after the backup)."""
        if not self.can_keep:
            return KeepResult(False, "livetune.keep.disabled")
        if self.config_dir is None:
            return KeepResult(False, "livetune.keep.no_config")
        self._ensure_backup()
        if self.backup_error is not None and self.backup_error != "otd.error.settings_missing":
            return KeepResult(False, "livetune.keep.no_backup", self.backup_error)
        if self._unsent:
            out = self.flush()
            if out is not None and not out.ok:
                return KeepResult(False, "livetune.keep.send_failed", out.detail, self.backup_path)
        try:
            self._writer(self.config_dir, copy.deepcopy(self._live))
        except (OSError, TypeError, settings_io.OtdSettingsError) as exc:
            return KeepResult(False, "livetune.keep.write_failed", str(exc), self.backup_path)
        self._saved = copy.deepcopy(self._live)
        return KeepResult(True, "livetune.keep.ok", str(self.config_dir), self.backup_path)


# --------------------------------------------------------------------------
# osu! detection (read-only process listing)
# --------------------------------------------------------------------------
OSU_EXE_NAMES = ("osu!.exe", "osu!", "osu.exe", "osu!lazer.exe", "osu-lazer", "osu!.app")


@dataclass(frozen=True)
class ProcessEntry:
    pid: int
    name: str
    path: str | None = None


@dataclass(frozen=True)
class OsuProcess:
    pid: int
    name: str
    flavor: str            # "stable" | "lazer" | "unknown"
    path: str | None = None


ProcessLister = Callable[[], Sequence[ProcessEntry]]


def classify_osu(entry: ProcessEntry) -> OsuProcess | None:
    """An osu! process, with stable/lazer told apart by name or install folder."""
    name = entry.name.lower()
    if name not in OSU_EXE_NAMES:
        return None
    hint = f"{name} {(entry.path or '').lower()}"
    if "lazer" in hint:
        flavor = "lazer"
    elif entry.path:
        flavor = "stable"
    else:
        flavor = "unknown"
    return OsuProcess(entry.pid, entry.name, flavor, entry.path)


def find_osu(processes: Sequence[ProcessEntry]) -> OsuProcess | None:
    """First osu! process in ``processes`` (lazer preferred when both run)."""
    found = [p for p in (classify_osu(e) for e in processes) if p is not None]
    found.sort(key=lambda p: (p.flavor != "lazer", p.pid))
    return found[0] if found else None


def is_osu_running(lister: ProcessLister | None = None) -> bool:
    """True when an osu! (stable or lazer) process exists."""
    return find_osu((lister or list_processes)()) is not None


def list_processes() -> list[ProcessEntry]:
    """Running processes (Windows: toolhelp snapshot, ``tasklist`` fallback; else ``ps``)."""
    if sys.platform == "win32":
        try:
            return _toolhelp_processes()
        except (OSError, AttributeError, ValueError):
            return _tasklist_processes()
    return _ps_processes()


def _toolhelp_processes() -> list[ProcessEntry]:
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
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
        raise OSError("CreateToolhelp32Snapshot failed")
    out: list[ProcessEntry] = []
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            name = entry.szExeFile
            path = None
            if name.lower() in OSU_EXE_NAMES:   # only resolve the path for candidates
                proc = k32.OpenProcess(0x1000, False, entry.th32ProcessID)   # QUERY_LIMITED_INFORMATION
                if proc:
                    buf = ctypes.create_unicode_buffer(32768)
                    size = wintypes.DWORD(len(buf))
                    if k32.QueryFullProcessImageNameW(proc, 0, buf, ctypes.byref(size)):
                        path = buf.value
                    k32.CloseHandle(proc)
            out.append(ProcessEntry(int(entry.th32ProcessID), name, path))
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


def parse_tasklist_csv(text: str) -> list[ProcessEntry]:
    """Parse ``tasklist /fo csv /nh`` output."""
    out: list[ProcessEntry] = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 2 and row[1].strip().isdigit():
            out.append(ProcessEntry(int(row[1]), row[0]))
    return out


def _tasklist_processes() -> list[ProcessEntry]:
    try:
        proc = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True,
                              timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_tasklist_csv(proc.stdout or "")


def _ps_processes() -> list[ProcessEntry]:
    try:
        proc = subprocess.run(["ps", "-eo", "pid=,comm="], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in (proc.stdout or "").splitlines():
        pid, _, comm = line.strip().partition(" ")
        if pid.isdigit():
            out.append(ProcessEntry(int(pid), Path(comm.strip()).name, comm.strip() or None))
    return out


class OsuWatcher:
    """Polls the process list at most every ``interval_s`` and reports changes."""

    def __init__(self, lister: ProcessLister | None = None, clock: Callable[[], float] = time.monotonic,
                 interval_s: float = 3.0) -> None:
        self._lister = lister or list_processes
        self._clock = clock
        self.interval_s = interval_s
        self._last_t = -float("inf")
        self.process: OsuProcess | None = None

    @property
    def running(self) -> bool:
        return self.process is not None

    def poll(self, force: bool = False) -> bool:
        """Refresh if due; True when osu! started or stopped since the last poll."""
        now = self._clock()
        if not force and now - self._last_t < self.interval_s:
            return False
        self._last_t = now
        try:
            found = find_osu(self._lister())
        except Exception:   # a process listing must never break the companion
            found = None
        changed = (found is None) != (self.process is None) or (
            found is not None and self.process is not None and found.pid != self.process.pid)
        self.process = found
        return changed
