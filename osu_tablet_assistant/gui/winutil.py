"""Small helpers shared by the companion and the Play Coach windows."""
from __future__ import annotations

import copy
import sys
import tkinter as tk
from typing import Any

from ..log import log_exception

WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x8, 0x20, 0x80
WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x80000, 0x08000000
GWL_EXSTYLE = -20


def make_click_through(win: tk.Misc) -> bool:
    """Give a Tk toplevel the no-activate / tool / click-through / topmost ex-styles (Windows)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32")
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = wintypes.LONG
        user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
        user32.SetWindowLongW.restype = wintypes.LONG
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, wintypes.UINT]
        hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style |= WS_EX_TOPMOST | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_NOACTIVATE
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ctypes.c_long(style & 0xFFFFFFFF).value)
        # HWND_TOPMOST, SWP_NOSIZE|SWP_NOMOVE|SWP_NOACTIVATE|SWP_FRAMECHANGED
        user32.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x1 | 0x2 | 0x10 | 0x20)
        return True
    except (AttributeError, OSError, ValueError, TypeError):
        return False


def companion_args_from_session(session: Any) -> dict[str, Any]:
    """Window arguments taken from a ``gui.session.Session`` (public attributes only).

    The settings are the ones live in OTD right now: the applied final
    settings, else the candidate currently under test, else the original.
    """
    base = getattr(session, "settings", None)
    profile = getattr(session, "profile", None)
    tablet_name = profile.tablet_name if profile is not None else None
    live = base
    try:
        if getattr(session, "applied", None) is not None:
            live = session.final_settings()
        elif getattr(session, "live_candidate_id", None) and base is not None and tablet_name:
            cand = session.candidate(session.live_candidate_id)
            live = session.services.apply_changes(base, tablet_name, cand.changes)
    except Exception:   # fall back to what was read from settings.json
        log_exception("live settings from the session")
        live = base
    services = getattr(session, "services", None)
    controller = None
    if services is not None and hasattr(services, "make_controller"):
        try:
            controller = services.make_controller(getattr(session, "install_dir", None),
                                                  getattr(session, "config_dir", None))
        except Exception:
            log_exception("OTD controller")
            controller = None
    return {"settings": copy.deepcopy(live) if live is not None else None, "tablet_name": tablet_name,
            "tablet": getattr(session, "tablet", None), "display": getattr(session, "display", None),
            "controller": controller, "config_dir": getattr(session, "config_dir", None),
            "available_filters": set(getattr(session, "filter_paths", ()) or ()),
            "dry_run": bool(getattr(session, "dry_run", False))}
