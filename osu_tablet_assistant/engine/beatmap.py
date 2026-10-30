"""Beatmaps for the Play Coach: parse ``.osu`` files, find the one being played, map coordinates.

Pure standard library. Only the parts of the format the coach needs are read
(metadata, difficulty, hit objects); nothing here touches osu! itself.

Playfield transform (APPROXIMATION)
-----------------------------------
osu!stable draws the 512x384 playfield inside a virtual 640x480 screen at an
offset of (64, 48), scaled by ``screen_height / 480`` and centred horizontally.
That holds for the full-screen / borderless case with the playfield over the
whole screen. osu!lazer and windowed play can differ, so the constants are
exposed and callers should treat the alignment quality as the real check.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ..models import DisplaySpec
from .playtypes import Beatmap, HitObject

PLAYFIELD_W, PLAYFIELD_H = 512.0, 384.0
VIRTUAL_W, VIRTUAL_H = 640.0, 480.0
PLAYFIELD_OFFSET = (64.0, 48.0)

_TYPE_CIRCLE, _TYPE_SLIDER, _TYPE_SPINNER = 1, 2, 8


# ------------------------------------------------------------------ difficulty
def circle_radius_osu_px(cs: float) -> float:
    """Circle radius in osu! pixels (stable formula)."""
    return 54.4 - 4.48 * cs


def hit_window_ms(od: float, window: str = "50") -> float:
    """Half-width (ms) of a hit window at overall difficulty ``od``.

    ``window``: ``"300"`` -> 80 - 6*od, ``"100"`` -> 140 - 8*od, ``"50"`` (default) -> 200 - 10*od.
    The coach matches clicks to objects with the "50" window (the widest one that
    still gives points) and calls a click early or late outside the "100" window.
    """
    base = {"300": (80.0, 6.0), "100": (140.0, 8.0), "50": (200.0, 10.0)}[window]
    return max(base[0] - base[1] * od, 1.0)


# ------------------------------------------------------------------- parsing
def _sections(text: str) -> Iterator[tuple[str, list[str]]]:
    name, lines = "", []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            yield name, lines
            name, lines = line[1:-1], []
        elif line and not line.startswith("//"):
            lines.append(line)
    yield name, lines


def _pairs(lines: list[str]) -> dict[str, str]:
    out = {}
    for line in lines:
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip()] = value.strip()
    return out


def _num(values: dict[str, str], key: str, default: float) -> float:
    try:
        return float(values[key])
    except (KeyError, ValueError):
        return default


def _parse_object(line: str) -> HitObject | None:
    parts = line.split(",")
    if len(parts) < 4:
        return None
    try:
        x, y, time, kind = float(parts[0]), float(parts[1]), float(parts[2]), int(float(parts[3]))
    except ValueError:
        return None
    if kind & _TYPE_SPINNER:
        name = "spinner"
    elif kind & _TYPE_SLIDER:
        name = "slider"
    elif kind & _TYPE_CIRCLE:
        name = "circle"
    else:
        return None
    return HitObject(time, x, y, name, bool(kind & 4))


def parse_osu(text: str, path: str = "") -> Beatmap:
    """Parse the text of a ``.osu`` file; unknown or broken lines are skipped."""
    meta: dict[str, str] = {}
    diff: dict[str, str] = {}
    objects: list[HitObject] = []
    for name, lines in _sections(text):
        if name == "Metadata":
            meta = _pairs(lines)
        elif name == "Difficulty":
            diff = _pairs(lines)
        elif name == "HitObjects":
            objects = [o for o in map(_parse_object, lines) if o is not None]
    objects.sort(key=lambda o: o.time_ms)
    od = _num(diff, "OverallDifficulty", 5.0)
    return Beatmap(
        title=meta.get("Title", ""), artist=meta.get("Artist", ""), version=meta.get("Version", ""),
        creator=meta.get("Creator", ""), title_unicode=meta.get("TitleUnicode", meta.get("Title", "")),
        artist_unicode=meta.get("ArtistUnicode", meta.get("Artist", "")),
        cs=_num(diff, "CircleSize", 4.0), od=od, ar=_num(diff, "ApproachRate", od),
        hp=_num(diff, "HPDrainRate", 5.0), objects=tuple(objects), path=path)


def load_beatmap(path: str | Path) -> Beatmap | None:
    """Read and parse a ``.osu`` file; None when it cannot be read or has no hit objects."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    beatmap = parse_osu(text, str(path))
    return beatmap if beatmap.objects else None


# ------------------------------------------------------------- window title
@dataclass(frozen=True)
class WindowTitle:
    """``osu!  - Artist - Title [Version]`` split apart (the artist/title split is a guess)."""
    artist_title: str
    version: str
    artist: str
    title: str


def parse_window_title(title: str | None) -> WindowTitle | None:
    """Parse osu!stable's window title while a map is loaded; the menu (``osu!``) gives None."""
    if not title:
        return None
    text = title.strip()
    if not text.lower().startswith("osu!"):
        return None
    rest = text[4:].lstrip()
    if not rest.startswith("- "):
        return None
    rest = rest[2:].strip()
    match = re.match(r"^(?P<name>.+?)\s*\[(?P<version>[^\[\]]*)\]\s*$", rest)
    if not match:
        return None
    name, version = match["name"].strip(), match["version"].strip()
    artist, sep, song = name.partition(" - ")
    return WindowTitle(name, version, artist.strip() if sep else "", song.strip() if sep else name)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _loose(text: str) -> str:
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE).casefold()


# ----------------------------------------------------------------- the index
@dataclass(frozen=True)
class IndexEntry:
    path: str
    artist: str
    title: str
    artist_unicode: str
    title_unicode: str
    version: str
    mtime: float = 0.0
    size: int = 0

    def names(self) -> tuple[str, ...]:
        return (f"{self.artist} - {self.title}", f"{self.artist_unicode} - {self.title_unicode}")


def read_header(path: Path) -> dict[str, str] | None:
    """Metadata of a ``.osu`` file, reading only up to the ``[Difficulty]`` section."""
    meta: dict[str, str] = {}
    in_meta = False
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            for number, raw in enumerate(handle):
                if number > 400:
                    break
                line = raw.strip()
                if line.startswith("["):
                    if in_meta:
                        break
                    in_meta = line == "[Metadata]"
                elif in_meta and ":" in line:
                    key, _sep, value = line.partition(":")
                    meta[key.strip()] = value.strip()
    except OSError:
        return None
    return meta or None


class BeatmapIndex:
    """Headers of every ``Songs/*/*.osu`` with an optional on-disk cache keyed by path+mtime+size."""

    def __init__(self, songs_dir: str | Path, cache_path: str | Path | None = None) -> None:
        self.songs_dir = Path(songs_dir)
        self.cache_path = Path(cache_path) if cache_path else None
        self.entries: list[IndexEntry] = []

    def _load_cache(self) -> dict[str, list]:
        if self.cache_path is None:
            return {}
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_cache(self, cache: dict[str, list]) -> None:
        if self.cache_path is None:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(cache), encoding="utf-8")
            tmp.replace(self.cache_path)
        except OSError:
            pass   # the cache only saves time

    def scan(self) -> int:
        """(Re)build :attr:`entries`; returns how many maps were found. Never raises."""
        cache = self._load_cache()
        fresh: dict[str, list] = {}
        entries: list[IndexEntry] = []
        try:
            files = sorted(self.songs_dir.glob("*/*.osu"))
        except OSError:
            files = []
        for path in files:
            try:
                stat = path.stat()
            except OSError:
                continue
            key = str(path)
            cached = cache.get(key)
            if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
                meta = cached[2]
            else:
                meta = read_header(path)
            if not meta:
                continue
            fresh[key] = [stat.st_mtime, stat.st_size, meta]
            entries.append(IndexEntry(
                key, meta.get("Artist", ""), meta.get("Title", ""),
                meta.get("ArtistUnicode", meta.get("Artist", "")), meta.get("TitleUnicode", meta.get("Title", "")),
                meta.get("Version", ""), stat.st_mtime, stat.st_size))
        self.entries = entries
        if fresh != cache:
            self._save_cache(fresh)
        return len(entries)

    def find(self, info: WindowTitle) -> IndexEntry | None:
        """Best entry for a window title: same difficulty name and same "Artist - Title" (case-insensitive,
        romanized or unicode). Several folders for one map: the most recently modified wins."""
        for normalize in (_norm, _loose):
            want_name, want_version = normalize(info.artist_title), normalize(info.version)
            hits = [e for e in self.entries
                    if normalize(e.version) == want_version
                    and any(normalize(n) == want_name for n in e.names())]
            if hits:
                return max(hits, key=lambda e: e.mtime)
        return None


def find_beatmap(info: WindowTitle | str | None, index: BeatmapIndex) -> Beatmap | None:
    """Locate and load the map named by a window title (string or parsed); None when unknown."""
    parsed = parse_window_title(info) if isinstance(info, str) else info
    if parsed is None:
        return None
    entry = index.find(parsed)
    return load_beatmap(entry.path) if entry is not None else None


# ---------------------------------------------------------------- transforms
def playfield_scale(display: DisplaySpec) -> float:
    """Desktop pixels per osu! pixel (screen height / 480)."""
    return display.height_px / VIRTUAL_H


def playfield_to_desktop(x: float, y: float, display: DisplaySpec) -> tuple[float, float]:
    """osu! playfield pixel -> desktop pixel for a display centred at ``(offset_x, offset_y)``."""
    scale = playfield_scale(display)
    left = display.offset_x - display.width_px / 2
    top = display.offset_y - display.height_px / 2
    x0 = left + (display.width_px - VIRTUAL_W * scale) / 2
    return x0 + (x + PLAYFIELD_OFFSET[0]) * scale, top + (y + PLAYFIELD_OFFSET[1]) * scale


def desktop_to_playfield(px: float, py: float, display: DisplaySpec) -> tuple[float, float]:
    """Inverse of :func:`playfield_to_desktop`."""
    scale = playfield_scale(display)
    left = display.offset_x - display.width_px / 2
    top = display.offset_y - display.height_px / 2
    x0 = left + (display.width_px - VIRTUAL_W * scale) / 2
    return (px - x0) / scale - PLAYFIELD_OFFSET[0], (py - top) / scale - PLAYFIELD_OFFSET[1]
