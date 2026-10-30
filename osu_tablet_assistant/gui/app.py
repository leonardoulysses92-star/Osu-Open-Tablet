"""Main window: header with language selector, plus the wizard."""
from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from . import i18n
from .i18n import t
from .session import Session, display_from_tk
from .widgets import px
from .wizard import Wizard, configure_styles

_LANGUAGE_NAMES = {"es": "Español", "en": "English"}


def enable_dpi_awareness() -> None:
    """Ask Windows for real pixels instead of a blurry scaled window (no-op elsewhere)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError, ImportError):
        pass


class App:
    """Owns the Tk root and wires the session to the wizard."""

    def __init__(self, session: Session, config_hint: Path | None = None,
                 install_hint: Path | None = None) -> None:
        enable_dpi_awareness()
        self.session = session
        self.root = tk.Tk()
        configure_styles(self.root)
        session.services.detect_display = lambda: display_from_tk(self.root)
        session.prefs.language = i18n.current_language()
        self._size_window()

        header = ttk.Frame(self.root, padding=(12, 8))
        header.pack(fill="x")
        self._title = ttk.Label(header, style="Title.TLabel")
        self._title.pack(side="left")
        self._lang_var = tk.StringVar(value=_LANGUAGE_NAMES[i18n.current_language()])
        self._lang_label = ttk.Label(header)
        picker = ttk.Combobox(header, textvariable=self._lang_var, state="readonly", width=10,
                              values=[_LANGUAGE_NAMES[c] for c in i18n.SUPPORTED])
        picker.pack(side="right")
        self._lang_label.pack(side="right", padx=6)
        picker.bind("<<ComboboxSelected>>", self._on_language)

        self.wizard = Wizard(self.root, session, config_hint, install_hint, on_close=self.root.destroy)
        self.wizard.pack(fill="both", expand=True)
        self.root.protocol("WM_DELETE_WINDOW", self.wizard.request_close)
        self._retitle()

    def _size_window(self) -> None:
        screen_w, screen_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        width = min(px(self.root, 1000), int(screen_w * 0.9))
        height = min(px(self.root, 760), int(screen_h * 0.88))
        self.root.geometry(f"{width}x{height}+{(screen_w - width) // 2}+{max((screen_h - height) // 3, 0)}")
        self.root.minsize(min(px(self.root, 720), width), min(px(self.root, 520), height))

    def _retitle(self) -> None:
        self.root.title(t("gui.app.title"))
        self._title.configure(text=t("gui.app.title"))
        self._lang_label.configure(text=t("gui.app.language"))

    def _on_language(self, _event: tk.Event) -> None:
        by_name = {name: code for code, name in _LANGUAGE_NAMES.items()}
        code = by_name[self._lang_var.get()]
        if code == i18n.current_language():
            return
        i18n.set_language(code)
        self.session.prefs.language = code
        self._retitle()
        self.wizard.rebuild()

    def run(self) -> None:
        """Enter the Tk main loop."""
        self.root.mainloop()
