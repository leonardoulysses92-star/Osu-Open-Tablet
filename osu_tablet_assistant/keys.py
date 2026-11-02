"""Click-key names the Play Coach understands, their Windows virtual-key codes, and the player's choice.
Also the text form of the companion's global hotkeys (parsing, defaults, validation of saved bindings).

Pure module (no Tk, no Windows calls), shared by the recorder (``gui.playwatch``), the coach and the config.

Data model of the player's choice (stored in ``AppConfig``):

* ``click_keys``: the KEYBOARD keys and extra mouse buttons the player clicks with (default Z and X). It never
  holds ``mouse1``.
* ``pen_clicks``: "I click with the pen tip". OpenTabletDriver maps the pen tip to the left mouse button
  (``mouse1``) by default, so ``mouse1`` is counted ONLY when this is on. Off by default: for a keyboard player,
  touching the tablet with the pen is not a click. Pen barrel buttons are usually ``mouse2``/``mouse3`` in OTD;
  they count only if the player adds them on purpose.

``effective_keys`` turns both into the tuple the recorder polls. Only those keys are ever read.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

DEFAULT_CLICK_KEYS: tuple[str, ...] = ("z", "x")
PEN_TIP_KEY = "mouse1"
MAX_KEYS = 6

# ------------------------------------------------------------------ vocabulary
_PUNCTUATION = {        # name: (VK_OEM code, shown as)
    "semicolon": (0xBA, ";"), "equal": (0xBB, "="), "comma": (0xBC, ","), "minus": (0xBD, "-"),
    "period": (0xBE, "."), "slash": (0xBF, "/"), "backtick": (0xC0, "`"), "lbracket": (0xDB, "["),
    "backslash": (0xDC, "\\"), "rbracket": (0xDD, "]"), "quote": (0xDE, "'"),
}
_NAMED = {              # name: (VK code, shown as)
    "space": (0x20, "Space"), "enter": (0x0D, "Enter"), "tab": (0x09, "Tab"), "shift": (0x10, "Shift"),
    "ctrl": (0x11, "Ctrl"), "alt": (0x12, "Alt"), "capslock": (0x14, "Caps Lock"),
    "left": (0x25, "←"), "up": (0x26, "↑"), "right": (0x27, "→"), "down": (0x28, "↓"),
    "mouse1": (0x01, "Mouse 1"), "mouse2": (0x02, "Mouse 2"), "mouse3": (0x04, "Mouse 3"),
    "mouse4": (0x05, "Mouse 4"), "mouse5": (0x06, "Mouse 5"),
}
_ALIASES = {
    ";": "semicolon", "=": "equal", ",": "comma", "-": "minus", ".": "period", "/": "slash", "`": "backtick",
    "[": "lbracket", "\\": "backslash", "]": "rbracket", "'": "quote", "return": "enter", "control": "ctrl",
    "caps": "capslock", "spacebar": "space", "m1": "mouse1", "m2": "mouse2",
    "m3": "mouse3", "m4": "mouse4", "m5": "mouse5", "lmb": "mouse1", "rmb": "mouse2", "mmb": "mouse3",
    "leftclick": "mouse1", "rightclick": "mouse2",
}


def _vk_table() -> dict[str, int]:
    table = {name: code for name, (code, _shown) in {**_NAMED, **_PUNCTUATION}.items()}
    table.update({chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)})
    table.update({str(d): 0x30 + d for d in range(10)})
    table.update({f"f{i}": 0x6F + i for i in range(1, 13)})
    table.update({f"num{d}": 0x60 + d for d in range(10)})
    return table


VK_CODES: dict[str, int] = _vk_table()
KNOWN_KEYS = frozenset(VK_CODES)


def canonical(name: Any) -> str | None:
    """The canonical key name for ``name`` ("Z" -> "z", ";" -> "semicolon", "Mouse 4" -> "mouse4"), or None."""
    if not isinstance(name, str):
        return None
    text = name.strip().lower()
    if not text:
        return None
    if len(text) > 1:
        text = re.sub(r"[\s_]+", "", text)        # "Mouse 4" -> "mouse4", "caps_lock" -> "capslock"
    text = _ALIASES.get(text, text)
    if text.startswith("numpad"):
        text = "num" + text[6:]
    return text if text in KNOWN_KEYS else None


def key_code(name: str) -> int | None:
    """Windows virtual-key code of a key name ("z", "5", "f5", "semicolon", "mouse1"...), or None."""
    key = canonical(name)
    return VK_CODES[key] if key is not None else None


def normalize_keys(keys: Iterable[Any] | None, default: Sequence[str] = DEFAULT_CLICK_KEYS) -> tuple[str, ...]:
    """Canonical, unique (first wins), at most ``MAX_KEYS``; unknown names dropped; empty -> ``default``."""
    out: list[str] = []
    for item in keys or ():
        key = canonical(item)
        if key is not None and key not in out:
            out.append(key)
        if len(out) >= MAX_KEYS:
            break
    return tuple(out) if out else tuple(default)


def effective_keys(click_keys: Iterable[Any] | None, pen_clicks: bool = False) -> tuple[str, ...]:
    """The keys the recorder polls: the chosen keys without ``mouse1``, plus ``mouse1`` only with ``pen_clicks``."""
    chosen = [k for k in normalize_keys(click_keys, default=()) if k != PEN_TIP_KEY]
    if pen_clicks:
        chosen = chosen[:MAX_KEYS - 1] + [PEN_TIP_KEY]
    return tuple(chosen) if chosen else DEFAULT_CLICK_KEYS


def display_name(key: str) -> str:
    """How a key is shown to the player: "Z", "5", "F5", ";", "Space", "Num 5", "Mouse 1"."""
    name = canonical(key) or str(key)
    if name in _NAMED:
        return _NAMED[name][1]
    if name in _PUNCTUATION:
        return _PUNCTUATION[name][1]
    if name.startswith("num") and name[3:].isdigit():
        return f"Num {name[3:]}"
    return name.upper()


def display_list(keys: Sequence[str]) -> str:
    """``"Z, X"``: the keys as the player reads them."""
    return ", ".join(display_name(k) for k in keys)


def parse_text(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Keys typed by hand ("z, x, mouse4" or "z x"): ``(known keys in order, words that were not understood)``."""
    known: list[str] = []
    unknown: list[str] = []
    for word in re.split(r"[,\s]+", text or ""):
        if not word:
            continue
        key = canonical(word)
        if key is None:
            unknown.append(word)
        elif key not in known:
            known.append(key)
    return tuple(known), tuple(unknown)


# --------------------------------------------------------------- Tk events
_TK_KEYSYMS = {
    "space": "space", "return": "enter", "kp_enter": "enter", "tab": "tab", "caps_lock": "capslock",
    "left": "left", "right": "right", "up": "up", "down": "down",
    "semicolon": "semicolon", "colon": "semicolon", "comma": "comma", "less": "comma", "period": "period",
    "greater": "period", "slash": "slash", "question": "slash", "apostrophe": "quote", "quoteright": "quote",
    "quotedbl": "quote", "bracketleft": "lbracket", "braceleft": "lbracket", "bracketright": "rbracket",
    "braceright": "rbracket", "minus": "minus", "underscore": "minus", "equal": "equal", "plus": "equal",
    "backslash": "backslash", "bar": "backslash", "grave": "backtick", "quoteleft": "backtick",
    "asciitilde": "backtick",
    # shifted digits on a US layout
    "exclam": "1", "at": "2", "numbersign": "3", "dollar": "4", "percent": "5", "asciicircum": "6",
    "ampersand": "7", "asterisk": "8", "parenleft": "9", "parenright": "0",
}
_TK_IGNORED = {"escape", "shift_l", "shift_r", "control_l", "control_r", "alt_l", "alt_r", "meta_l", "meta_r",
               "super_l", "super_r", "win_l", "win_r", "app", "menu", "num_lock", "scroll_lock", "iso_level3_shift"}
# Tk button numbers: 1 left, 2 middle, 3 right; 4/5 (Tk 8.6 on Windows) or 8/9 (Tk 8.7+) the side buttons
_TK_BUTTONS = {1: "mouse1", 2: "mouse3", 3: "mouse2", 4: "mouse4", 5: "mouse5", 8: "mouse4", 9: "mouse5"}
_VK_TO_NAME = {code: name for name, code in VK_CODES.items()}


def name_from_tk_event(event: Any, vk_fallback: bool | None = None) -> str | None:
    """Our key name for a Tk ``<KeyPress>`` or ``<ButtonPress>`` event, or None (Escape, a lone modifier, unknown).

    ``vk_fallback`` (default: on Windows) also accepts ``event.keycode`` as a Windows virtual-key code, which
    covers layouts whose keysyms are not in the table.
    """
    num = getattr(event, "num", None)
    if isinstance(num, int) and not isinstance(num, bool) and num > 0:
        return _TK_BUTTONS.get(num)
    keysym = str(getattr(event, "keysym", "") or "")
    low = keysym.lower()
    if not low or low in _TK_IGNORED:
        return None
    if low.startswith("kp_") and low[3:].isdigit():
        return f"num{low[3:]}"
    if low in _TK_KEYSYMS:
        return _TK_KEYSYMS[low]
    if re.fullmatch(r"f([1-9]|1[0-2])", low):
        return low
    if len(low) == 1 and (low.isalpha() and low.isascii() or low.isdigit()):
        return low
    if vk_fallback is None:
        vk_fallback = os.name == "nt"
    code = getattr(event, "keycode", None)
    if vk_fallback and isinstance(code, int):
        name = _VK_TO_NAME.get(code)
        if name is not None and name not in ("shift", "ctrl", "alt"):
            return name
    return None


# --------------------------------------------------------------- the choice
@dataclass(frozen=True)
class KeyChoice:
    """What the player clicks with: keyboard keys / extra buttons, and whether the pen tip counts."""
    keys: tuple[str, ...] = DEFAULT_CLICK_KEYS
    pen_clicks: bool = False

    @classmethod
    def make(cls, keys: Iterable[Any] | None, pen_clicks: bool = False) -> "KeyChoice":
        """Validated choice: ``mouse1`` in ``keys`` turns into ``pen_clicks``; no keys and no pen -> Z and X."""
        cleaned = normalize_keys(keys, default=())
        pen = bool(pen_clicks) or PEN_TIP_KEY in cleaned
        rest = tuple(k for k in cleaned if k != PEN_TIP_KEY)
        if not rest and not pen:
            rest = DEFAULT_CLICK_KEYS
        return cls(rest[:MAX_KEYS - 1] if pen else rest, pen)

    @property
    def effective(self) -> tuple[str, ...]:
        return effective_keys(self.keys, self.pen_clicks)

    @property
    def full(self) -> bool:
        return len(self.keys) >= (MAX_KEYS - 1 if self.pen_clicks else MAX_KEYS)

    def add(self, key: str | None) -> "KeyChoice":
        """With one more key (``mouse1`` is the pen switch, not a chip; unknown keys and duplicates do nothing)."""
        name = canonical(key) if key else None
        if name is None or name == PEN_TIP_KEY or name in self.keys or self.full:
            return self
        return KeyChoice(self.keys + (name,), self.pen_clicks)

    def remove(self, key: str) -> "KeyChoice":
        """Without ``key``; the last key can only go when the pen tip counts (never zero ways to click)."""
        if key not in self.keys or (len(self.keys) == 1 and not self.pen_clicks):
            return self
        return KeyChoice(tuple(k for k in self.keys if k != key), self.pen_clicks)

    def feed(self, key: str | None) -> tuple["KeyChoice", str]:
        """One key pressed while listening: ``(new choice, outcome)``; outcome is added | known | left_click |
        full | ignored. The left button is not added (it is how the player presses "Done"; the pen tip has its
        own switch)."""
        if key is None:
            return self, "ignored"
        if key == PEN_TIP_KEY:
            return self, "left_click"
        if key in self.keys:
            return self, "known"
        if self.full:
            return self, "full"
        added = self.add(key)
        return added, "added" if added != self else "ignored"

    def with_pen(self, on: bool) -> "KeyChoice":
        return KeyChoice.make(self.keys, on)

    def reset(self) -> "KeyChoice":
        """Back to Z and X, pen tip off."""
        return KeyChoice()


# ------------------------------------------------------------ companion hotkeys
# Text form of the companion's global hotkeys ("ctrl+alt+up"). Registering them is ``gui.hotkeys``' job;
# the parsing lives here so the config can validate saved bindings without importing the GUI.
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
HOTKEY_MODIFIERS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT,
                    "win": MOD_WIN}
HOTKEY_VK: dict[str, int] = {
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "pageup": 0x21, "pagedown": 0x22,
    "home": 0x24, "end": 0x23, "insert": 0x2D, "delete": 0x2E, "space": 0x20, "enter": 0x0D,
    "tab": 0x09, "backspace": 0x08, "plus": 0xBB, "minus": 0xBD, "comma": 0xBC, "period": 0xBE,
    **{chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)},
    **{str(d): 0x30 + d for d in range(10)},
    **{f"num{d}": 0x60 + d for d in range(10)},
    **{f"f{n}": 0x6F + n for n in range(1, 25)},
}
HOTKEY_ACTIONS = ("smoother", "responsive", "precise", "looser", "area_bigger", "area_smaller", "undo", "keep",
                  "step")
DEFAULT_BINDINGS: dict[str, str] = {
    "smoother": "ctrl+alt+up", "responsive": "ctrl+alt+down",
    "precise": "ctrl+alt+left", "looser": "ctrl+alt+right",
    "area_bigger": "ctrl+alt+pageup", "area_smaller": "ctrl+alt+pagedown",
    "undo": "ctrl+alt+z", "keep": "ctrl+alt+k",
    "step": "ctrl+alt+p",            # cycles the step size: normal -> fine -> very fine ("Paso")
}
MAX_HOTKEY_TEXT = 40


class HotkeyError(ValueError):
    """A hotkey string could not be parsed."""


@dataclass(frozen=True)
class Hotkey:
    modifiers: int
    vk: int
    text: str


def parse_hotkey(text: str) -> Hotkey:
    """"ctrl+alt+up" -> Hotkey. Needs at least one modifier and exactly one key."""
    parts = [p.strip().lower() for p in str(text).replace(" ", "").split("+") if p.strip()]
    mods, found = 0, []
    for part in parts:
        if part in HOTKEY_MODIFIERS:
            mods |= HOTKEY_MODIFIERS[part]
        elif part in HOTKEY_VK:
            found.append(part)
        else:
            raise HotkeyError(f"unknown key {part!r} in {text!r}")
    if len(found) != 1 or not mods:
        raise HotkeyError(f"{text!r}: use modifier(s) + one key, e.g. ctrl+alt+up")
    names = [n for n, m in (("ctrl", MOD_CONTROL), ("alt", MOD_ALT), ("shift", MOD_SHIFT), ("win", MOD_WIN))
             if mods & m]
    return Hotkey(mods, HOTKEY_VK[found[0]], "+".join(names + found))


def clean_bindings(data: object) -> dict[str, str]:
    """Bindings for every action from untrusted data (a saved config): never invalid, never duplicated.

    Known actions with valid text keep it (normalised, e.g. "Alt+Ctrl+Up" -> "ctrl+alt+up"); "" means
    "no hotkey". Missing or invalid entries get the default unless that combination is already taken,
    in which case the action stays without a hotkey.
    """
    given = data if isinstance(data, Mapping) else {}
    out: dict[str, str] = {}
    used: set[tuple[int, int]] = set()

    def take(text: object) -> str | None:
        if not isinstance(text, str) or len(text) > MAX_HOTKEY_TEXT:
            return None
        if not text.strip():
            return ""
        try:
            hk = parse_hotkey(text)
        except HotkeyError:
            return None
        if (hk.modifiers, hk.vk) in used:
            return None
        used.add((hk.modifiers, hk.vk))
        return hk.text

    pending = []
    for action in HOTKEY_ACTIONS:      # the user's own choices first, so a default never pushes them out
        value = take(given.get(action)) if action in given else None
        if value is None:
            pending.append(action)
        else:
            out[action] = value
    for action in pending:
        out[action] = take(DEFAULT_BINDINGS[action]) or ""
    return {a: out[a] for a in HOTKEY_ACTIONS}
