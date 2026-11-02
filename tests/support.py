"""Helpers shared by the test modules."""
from __future__ import annotations

import tkinter as tk
from functools import lru_cache


@lru_cache(maxsize=1)
def display_available() -> bool:
    """True when Tk can open a window here (the window tests are skipped otherwise)."""
    try:
        root = tk.Tk()
    except tk.TclError:
        return False
    root.destroy()
    return True
