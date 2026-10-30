"""Main window: header (logo, title, language switch) plus the wizard."""
from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from . import i18n
from . import wizard as wizard_module
from .i18n import t
from .session import Session, display_from_tk
from .widgets import COLORS, Chip, Tooltip, apply_theme, button, font, px, run_in_thread
from .wizard import Wizard

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


def window_size(screen: tuple[int, int], wanted: tuple[int, int], minimum: tuple[int, int]
                ) -> tuple[tuple[int, int, int, int], tuple[int, int]]:
    """Window geometry ``(w, h, x, y)`` and min size for a screen, all in real px.

    The window takes ``wanted`` but never more than 92% x 88% of the screen;
    the min size never exceeds the window itself.
    """
    screen_w, screen_h = screen
    width = min(wanted[0], int(screen_w * 0.92))
    height = min(wanted[1], int(screen_h * 0.88))
    x, y = (screen_w - width) // 2, max((screen_h - height) // 3, 0)
    return (width, height, x, y), (min(minimum[0], width), min(minimum[1], height))


def prepare_session(session: Session, app_dir: Path) -> None:
    """Wire the program's own config and community-data cache into ``session`` (real runs only).

    Reads ``<app_dir>/config.json`` (tolerant), applies its plugin policy and
    activates the cached community data if it is still valid (local files
    only; nothing is downloaded here).
    """
    from ..appconfig import CONFIG_FILE, load_config
    from ..models import PluginPolicy
    path = app_dir / CONFIG_FILE
    session.app_config = load_config(path)
    session.app_config_path = path
    session.prefs.plugin_policy = PluginPolicy(session.app_config.plugin_policy)
    session.data_cache_dir = app_dir / "data"
    session.data_bundle = session._optional(session.services.activate_data_cache, session.data_cache_dir)


class App:
    """Owns the Tk root and wires the session to the wizard."""

    def __init__(self, session: Session, config_hint: Path | None = None,
                 install_hint: Path | None = None) -> None:
        enable_dpi_awareness()
        self.session = session
        self.root = tk.Tk()
        apply_theme(self.root)
        session.services.detect_display = lambda: display_from_tk(self.root)
        session.prefs.language = i18n.current_language()
        self._size_window()
        self._build_header()
        if wizard_module.COMPANION_LAUNCHER is None:        # an explicit launcher set by the caller wins
            wizard_module.COMPANION_LAUNCHER = self._open_companion
            self.root.bind("<Destroy>", self._release_companion, add="+")
        self.wizard = Wizard(self.root, session, config_hint, install_hint, on_close=self.root.destroy)
        self.wizard.pack(fill="both", expand=True)
        self.root.protocol("WM_DELETE_WINDOW", self.wizard.request_close)
        self._retitle()
        if session.app_config.check_updates_on_start and session.data_cache_dir is not None:
            self.root.after(500, self._startup_update)   # only because the user enabled it

    def _release_companion(self, event: tk.Event) -> None:
        """Window closed: stop pointing the result screen at this (dead) app."""
        if event.widget is self.root and wizard_module.COMPANION_LAUNCHER == self._open_companion:
            wizard_module.COMPANION_LAUNCHER = None

    def _open_companion(self, view: object) -> None:
        """Result-screen button: open the live-tuning companion on the current session."""
        from tkinter import messagebox
        session = view.session   # type: ignore[attr-defined]

        def kept(_settings: dict) -> None:
            # Keep replaced settings.json: "restore my settings" must write the original back.
            session._wrote_settings = True

        try:
            from .companion import open_companion
            open_companion(self.root, session, on_keep=kept)
        except Exception as exc:  # the window must never take the wizard down with it
            messagebox.showerror(t("gui.app.title"), str(exc), parent=self.root)

    def _startup_update(self) -> None:
        def done(_result: object) -> None:
            if self.session.step.value == "welcome" and self.wizard.slot is None:
                self.wizard.show_current()   # refresh the "community data" card
        run_in_thread(self.root, self.session.update_community_data, done, lambda _exc: None)

    def _size_window(self) -> None:
        r = self.root
        (w, h, x, y), (min_w, min_h) = window_size(
            (r.winfo_screenwidth(), r.winfo_screenheight()), (px(r, 1080), px(r, 820)), (px(r, 860), px(r, 600)))
        r.geometry(f"{w}x{h}+{x}+{y}")
        r.minsize(min_w, min_h)

    def _build_header(self) -> None:
        c = COLORS
        header = tk.Frame(self.root, background=c.surface)
        header.pack(fill="x")
        tk.Frame(self.root, height=1, background=c.border).pack(fill="x")
        inner = tk.Frame(header, background=c.surface)
        inner.pack(fill="x", padx=px(header, 24), pady=px(header, 12))
        size = px(header, 34)
        logo = tk.Canvas(inner, width=size, height=size, background=c.surface, highlightthickness=0)
        logo.create_oval(2, 2, size - 2, size - 2, fill=c.accent, outline="")
        logo.create_oval(size * 0.3, size * 0.3, size * 0.7, size * 0.7, outline=c.surface, width=3)
        logo.pack(side="left")
        titles = tk.Frame(inner, background=c.surface)
        titles.pack(side="left", padx=(px(header, 12), 0))
        self._title = tk.Label(titles, background=c.surface, foreground=c.text, font=font("heading"))
        self._title.pack(anchor="w")
        self._subtitle = tk.Label(titles, background=c.surface, foreground=c.muted, font=font("small"))
        self._subtitle.pack(anchor="w")

        lang = tk.Frame(inner, background=c.surface)
        lang.pack(side="right")
        self._lang_label = tk.Label(lang, background=c.surface, foreground=c.muted, font=font("small"))
        self._lang_label.pack(side="left", padx=(0, px(header, 8)))
        self._lang_var = tk.StringVar(value=i18n.current_language())
        for code in i18n.SUPPORTED:
            option = ttk.Radiobutton(lang, text=code.upper(), value=code, variable=self._lang_var,
                                     command=self._on_language, style="Segment.Toolbutton", takefocus=True)
            option.pack(side="left", padx=(0, 2))
            Tooltip(option, _LANGUAGE_NAMES[code])
        self._settings_button = button(inner, "", lambda: self.wizard.open_settings(), kind="ghost")
        self._settings_button.pack(side="right", padx=(0, px(header, 12)))
        if self.session.dry_run:
            self._dry_chip = Chip(inner, "", "info")
            self._dry_chip.pack(side="right", padx=px(header, 16))
        else:
            self._dry_chip = None

    def _retitle(self) -> None:
        self.root.title(t("gui.app.title"))
        self._title.configure(text=t("gui.app.title"))
        self._subtitle.configure(text=t("gui.app.subtitle"))
        self._lang_label.configure(text=t("gui.app.language"))
        self._settings_button.configure(text="⚙  " + t("gui.settings.open"))
        if self._dry_chip is not None:
            self._dry_chip.set(t("gui.app.dry_run"), "info")

    def _on_language(self) -> None:
        code = self._lang_var.get()
        if code == i18n.current_language():
            return
        i18n.set_language(code)
        self.session.prefs.language = code
        self.session.app_config.language = code
        self.session._optional(self.session.save_app_config)
        self._retitle()
        self.wizard.rebuild()

    def run(self) -> None:
        """Enter the Tk main loop."""
        self.root.mainloop()
