"""Small reusable ttk widgets and helpers for the wizard."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Sequence

from .i18n import t


def px(widget: tk.Misc, value: float) -> int:
    """Scale a 96-dpi pixel size to the widget's actual screen DPI."""
    return round(value * widget.winfo_fpixels("1i") / 96.0)


def run_in_thread(widget: tk.Misc, work: Callable[[], Any], on_done: Callable[[Any], None],
                  on_error: Callable[[BaseException], None], poll_ms: int = 50) -> None:
    """Run ``work`` off the UI thread; call back on the UI thread with result or error."""
    outcome: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def target() -> None:
        try:
            outcome.put((True, work()))
        except BaseException as exc:  # reported to the UI thread, never lost
            outcome.put((False, exc))

    def poll() -> None:
        try:
            ok, value = outcome.get_nowait()
        except queue.Empty:
            try:
                widget.after(poll_ms, poll)
            except tk.TclError:
                pass  # window closed while working
            return
        try:
            alive = bool(widget.winfo_exists())
        except tk.TclError:
            alive = False
        if alive:
            (on_done if ok else on_error)(value)

    threading.Thread(target=target, daemon=True).start()
    widget.after(poll_ms, poll)


class WrapLabel(ttk.Label):
    """Label whose text re-wraps to the width it is given."""

    def __init__(self, parent: tk.Misc, text: str = "", **kwargs: Any) -> None:
        super().__init__(parent, text=text, justify="left", **kwargs)
        self.bind("<Configure>", self._rewrap)

    def _rewrap(self, event: tk.Event) -> None:
        self.configure(wraplength=max(event.width - 4, 80))


class ScrollFrame(ttk.Frame):
    """Vertically scrollable container; put children into ``body``."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self._canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        bar = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=bar.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.body = ttk.Frame(self._canvas)
        self._window = self._canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._on_body)
        self._canvas.bind("<Configure>", self._on_canvas)
        self._canvas.bind("<Enter>", lambda _e: self._bind_wheel(True))
        self._canvas.bind("<Leave>", lambda _e: self._bind_wheel(False))

    def scroll_top(self) -> None:
        """Scroll back to the top (used when a new page is shown)."""
        self._canvas.yview_moveto(0)

    def _on_body(self, _event: tk.Event) -> None:
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas(self, event: tk.Event) -> None:
        self._canvas.itemconfigure(self._window, width=event.width)

    def _bind_wheel(self, active: bool) -> None:
        if active:
            self._canvas.bind_all("<MouseWheel>", self._on_wheel)
        else:
            self._canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, event: tk.Event) -> None:
        first, last = self._canvas.yview()
        if first > 0.0 or last < 1.0:  # only scroll when content overflows
            self._canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")


class ReadOnlyText(ttk.Frame):
    """Selectable, scrollable, read-only text box."""

    def __init__(self, parent: tk.Misc, height: int = 8) -> None:
        super().__init__(parent)
        self.text = tk.Text(self, height=height, wrap="word", borderwidth=1, relief="solid",
                            state="disabled", padx=6, pady=4)
        bar = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=bar.set)
        self.text.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")

    def set(self, content: str) -> None:
        """Replace the whole content."""
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)
        self.text.configure(state="disabled")


class Expander(ttk.Frame):
    """A "details" section that shows technical text when opened."""

    def __init__(self, parent: tk.Misc, title: str | None = None, height: int = 7) -> None:
        super().__init__(parent)
        self._title = title or t("gui.details.title")
        self._open = False
        self._button = ttk.Button(self, command=self.toggle, style="Toolbutton")
        self._button.pack(anchor="w")
        self._box = ReadOnlyText(self, height=height)
        self._refresh()

    def set(self, content: str) -> None:
        """Replace the technical text."""
        self._box.set(content)

    def toggle(self) -> None:
        """Open or close the section."""
        self._open = not self._open
        self._refresh()

    def _refresh(self) -> None:
        self._button.configure(text=f"{'v' if self._open else '>'} {self._title}")
        if self._open:
            self._box.pack(fill="both", expand=True, pady=(4, 0))
        else:
            self._box.pack_forget()


class StepIndicator(ttk.Frame):
    """Row of step names with the current one emphasised."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self._labels: list[ttk.Label] = []

    def show(self, names: Sequence[str], current: int) -> None:
        """Rebuild the row for ``names``, highlighting index ``current``."""
        for label in self._labels:
            label.destroy()
        self._labels = []
        for index, name in enumerate(names):
            style = "StepCurrent.TLabel" if index == current else (
                "StepDone.TLabel" if index < current else "Step.TLabel")
            label = ttk.Label(self, text=f"{index + 1}. {name}", style=style)
            label.pack(side="left", padx=(0, 12))
            self._labels.append(label)


def parse_float(text: str) -> float:
    """Parse a number typed by the user (accepts a decimal comma)."""
    return float(text.strip().replace(",", "."))
