"""Read-only process listing on Windows (Toolhelp snapshot) and the path of a process's executable.

Used to find a running OpenTabletDriver daemon and osu!. Nothing here opens a process with more than
``PROCESS_QUERY_LIMITED_INFORMATION``: it reads names and paths, never memory. Importing this module
works on any system; the functions raise ``OSError`` (or return None) off Windows.
"""
from __future__ import annotations

import sys
from collections.abc import Callable, Collection
from functools import lru_cache
from typing import Any, NamedTuple

TH32CS_SNAPPROCESS = 0x2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PATH_CHARS = 32768


class ProcessName(NamedTuple):
    pid: int
    name: str            # the executable's file name, e.g. "osu!.exe"


@lru_cache(maxsize=1)
def _api() -> tuple[Any, Any, Any]:
    """Private ``kernel32`` handle with full prototypes (never the shared ``ctypes.windll`` one)."""
    if sys.platform != "win32":
        raise OSError("process listing is only available on Windows")
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    entry_ptr = ctypes.POINTER(PROCESSENTRY32W)
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, entry_ptr]
    k32.Process32FirstW.restype = wintypes.BOOL
    k32.Process32NextW.argtypes = [wintypes.HANDLE, entry_ptr]
    k32.Process32NextW.restype = wintypes.BOOL
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD)]
    k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return k32, PROCESSENTRY32W, ctypes


def process_names() -> list[ProcessName]:
    """Every running process as (pid, exe name); raises ``OSError`` when the snapshot fails."""
    k32, entry_type, ctypes = _api()
    invalid = ctypes.c_void_p(-1).value
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == invalid:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    out: list[ProcessName] = []
    try:
        entry = entry_type()
        entry.dwSize = ctypes.sizeof(entry_type)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append(ProcessName(int(entry.th32ProcessID), entry.szExeFile))
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


def image_path(pid: int | None) -> str | None:
    """Full path of a process's executable, or None (not on Windows, no access, gone, any error)."""
    if not pid:
        return None
    try:
        k32, _entry, ctypes = _api()
        from ctypes import wintypes
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:
            buf = ctypes.create_unicode_buffer(_PATH_CHARS)
            size = wintypes.DWORD(len(buf))
            if not k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return None
            return buf.value or None
        finally:
            k32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError, ImportError):
        return None


def processes_with_paths(wanted: Collection[str] | Callable[[str], bool]) -> list[tuple[int, str, str | None]]:
    """(pid, name, path) of every process; the path is looked up only for names in ``wanted``.

    Raises ``OSError`` when the snapshot fails.
    """
    match = wanted if callable(wanted) else (lambda name, names={w.lower() for w in wanted}: name.lower() in names)
    return [(p.pid, p.name, image_path(p.pid) if match(p.name) else None) for p in process_names()]
