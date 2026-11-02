"""Read osu!stable's map list (``osu!.db``): every installed difficulty, with its folder and MD5, in one pass.

osu!stable keeps a binary database of the maps it knows next to ``osu!.exe``. Reading it (read-only, a FILE:
osu!'s memory is never touched) gives the exact "Artist - Title [Version]" text the window title shows, the
``.osu`` file name, its folder under ``Songs`` and its MD5, so the coach finds the map being played with one
dictionary lookup instead of scanning thousands of ``.osu`` headers.

Format (osu! wiki, "Legacy database file structure"):

* header: version (int), folder count (int), account unlocked (bool), unlock date (8 bytes), player name
  (string, read past and **dropped**), number of entries (int);
* per entry: its size in bytes (int) **only if version < 20191106**; nine strings (artist, artist unicode,
  title, title unicode, creator, difficulty, audio file, MD5, ``.osu`` name); status (byte), three shorts,
  last-modified ticks (long); AR / CS / HP / OD as a **byte before 20140609**, a single from then on; slider
  velocity (double); four star-rating tables (from 20140609 on) of an int count and pairs, **Int-Double
  (14 bytes) before 20250107, Int-Float (10 bytes) from then on**; drain, total and preview time; timing
  points (count x 17 bytes); ids, grades, offsets, stack leniency, mode; source, tags, online offset, title
  font; unplayed flag, **last played** (ticks), osz2 flag, **folder name**, and a few more fields.

Strings are 0x00 (empty) or 0x0b + ULEB128 length + UTF-8. The star and timing tables are skipped by
arithmetic (count x fixed size) over a ``memoryview`` with ``struct.unpack_from``, which keeps a pure-Python
parse fast.

Never raises into the GUI: a truncated or corrupt file gives the entries read so far and ``complete=False``.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

V_FLOAT_DIFFICULTY = 20140609      # AR/CS/HP/OD become singles; the star-rating tables appear
V_NO_ENTRY_SIZE = 20191106         # the per-entry size int is gone
V_FLOAT_STARS = 20250107           # star-rating pairs become Int-Float
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_STRING_BYTES = 1 << 20
TICKS_AT_EPOCH = 621_355_968_000_000_000      # .NET ticks (100 ns since 0001-01-01) at 1970-01-01
TIMING_POINT_BYTES = 17                        # double BPM, double offset, bool inherited

_INT = struct.Struct("<i")
_LONG = struct.Struct("<q")
_COUNTS = struct.Struct("<Bhhhq")              # status, circles, sliders, spinners, last modified
_FLOAT4 = struct.Struct("<4f")
_BYTE4 = struct.Struct("<4B")
_TIMES = struct.Struct("<iii")                 # drain s, total ms, preview ms
_AFTER_TIMING = struct.Struct("<iii4BhfB")     # difficulty id, beatmap id, thread id, 4 grades, offset, stack, mode


class _Truncated(ValueError):
    """The data ended (or a field made no sense) in the middle of an entry."""


@dataclass(frozen=True, slots=True)
class OsuDbEntry:
    """One difficulty as osu!.db lists it (what the coach needs, nothing about the player)."""
    artist: str
    artist_unicode: str
    title: str
    title_unicode: str
    creator: str
    version: str                  # difficulty name
    audio_file: str
    md5: str
    osu_file: str                 # the .osu file name inside the folder
    folder: str                   # folder under Songs
    ar: float
    cs: float
    hp: float
    od: float
    mode: int                     # 0 osu!, 1 taiko, 2 catch, 3 mania
    last_modified: float | None   # Unix seconds
    last_played: float | None     # Unix seconds (None: never)

    @property
    def key(self) -> str:
        """``Artist - Title [Version]``, the text the window title shows while it is played."""
        return f"{self.artist} - {self.title} [{self.version}]"


@dataclass(frozen=True)
class OsuDb:
    version: int
    folder_count: int
    expected: int                 # entries the header announced
    entries: tuple[OsuDbEntry, ...]
    complete: bool                # False: the file ended early or was corrupt; ``entries`` is what was read
    error: str = ""


def ticks_to_unix(ticks: int) -> float | None:
    """.NET ticks -> Unix seconds; None for 0 / absurd values."""
    if ticks <= TICKS_AT_EPOCH or ticks > 3_155_378_975_999_999_999:
        return None
    return (ticks - TICKS_AT_EPOCH) / 10_000_000.0


class _Cursor:
    __slots__ = ("mv", "pos", "end")

    def __init__(self, data: bytes | memoryview) -> None:
        self.mv = memoryview(data)
        self.pos, self.end = 0, len(self.mv)

    def need(self, n: int) -> None:
        if n < 0 or self.pos + n > self.end:
            raise _Truncated("truncated")

    def int(self) -> int:
        self.need(4)
        value = _INT.unpack_from(self.mv, self.pos)[0]
        self.pos += 4
        return value

    def unpack(self, fmt: struct.Struct) -> tuple:
        self.need(fmt.size)
        value = fmt.unpack_from(self.mv, self.pos)
        self.pos += fmt.size
        return value

    def skip(self, n: int) -> None:
        self.need(n)
        self.pos += n

    def string(self) -> str:
        self.need(1)
        mv, pos = self.mv, self.pos
        marker = mv[pos]
        pos += 1
        if marker == 0:
            self.pos = pos
            return ""
        if marker != 0x0B:
            raise _Truncated("bad string marker")
        length, shift = 0, 0
        while True:                               # ULEB128
            if pos >= self.end:
                raise _Truncated("truncated")
            byte = mv[pos]
            pos += 1
            length |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
            if shift > 28:
                raise _Truncated("bad length")
        if length > MAX_STRING_BYTES or pos + length > self.end:
            raise _Truncated("bad string")
        self.pos = pos + length
        return bytes(mv[pos:pos + length]).decode("utf-8", errors="replace")

    def skip_string(self) -> None:
        self.string()

    def table(self, item: int) -> None:
        """Skip an int count followed by ``count`` items of ``item`` bytes."""
        count = self.int()
        if count < 0:
            raise _Truncated("bad count")
        self.skip(count * item)


def _entry(c: _Cursor, version: int) -> OsuDbEntry:
    artist, artist_u, title, title_u = c.string(), c.string(), c.string(), c.string()
    creator, diff, audio, md5, osu_file = c.string(), c.string(), c.string(), c.string(), c.string()
    _status, _circles, _sliders, _spinners, modified = c.unpack(_COUNTS)
    if version >= V_FLOAT_DIFFICULTY:
        ar, cs, hp, od = c.unpack(_FLOAT4)
    else:
        ar, cs, hp, od = (float(v) for v in c.unpack(_BYTE4))
    c.skip(8)                                       # slider velocity
    if version >= V_FLOAT_DIFFICULTY:
        pair = 10 if version >= V_FLOAT_STARS else 14
        for _mode in range(4):                      # std, taiko, catch, mania star ratings
            c.table(pair)
    c.unpack(_TIMES)
    c.table(TIMING_POINT_BYTES)
    *_ids_grades, mode = c.unpack(_AFTER_TIMING)
    c.skip_string()                                 # source
    c.skip_string()                                 # tags
    c.skip(2)                                       # online offset
    c.skip_string()                                 # title font
    c.skip(1)                                       # unplayed
    played = c.unpack(_LONG)[0]
    c.skip(1)                                       # osz2
    folder = c.string()
    c.skip(8 + 5)                                   # last online check, five "ignore/disable" flags
    if version < V_FLOAT_DIFFICULTY:
        c.skip(2)                                   # an unknown short of old versions
    c.skip(4 + 1)                                   # last modification (int), mania scroll speed
    return OsuDbEntry(artist, artist_u, title, title_u, creator, diff, audio, md5.lower(), osu_file, folder,
                      float(ar), float(cs), float(hp), float(od), int(mode), ticks_to_unix(modified),
                      ticks_to_unix(played))


def parse_osudb(data: bytes | memoryview, limit: int | None = None) -> OsuDb:
    """Parse the bytes of an ``osu!.db``. Never raises; ``complete`` says whether every entry was read."""
    c = _Cursor(data)
    entries: list[OsuDbEntry] = []
    version = folders = expected = 0
    try:
        version = c.int()
        folders = c.int()
        c.skip(1 + 8)                               # account unlocked, unlock date
        c.skip_string()                             # player name -> dropped
        expected = c.int()
        if expected < 0:
            raise _Truncated("bad count")
        sized = version < V_NO_ENTRY_SIZE
        wanted = expected if limit is None else min(expected, limit)
        for _ in range(wanted):
            if sized:
                size = c.int()
                start = c.pos
                if size < 0:
                    raise _Truncated("bad entry size")
                c.need(size)
                try:
                    entries.append(_entry(c, version))
                except _Truncated:
                    pass                            # an odd entry: its size says where the next one starts
                c.pos = start + size
            else:
                entries.append(_entry(c, version))
    except (_Truncated, struct.error, IndexError) as exc:
        return OsuDb(version, folders, expected, tuple(entries), False, str(exc) or "truncated")
    return OsuDb(version, folders, expected, tuple(entries), len(entries) == expected)


def load_osudb(path: str | Path) -> OsuDb | None:
    """Read and parse an ``osu!.db``; None when the file cannot be read (missing, too big)."""
    try:
        p = Path(path)
        if p.stat().st_size > MAX_FILE_BYTES:
            return None
        data = p.read_bytes()
    except OSError:
        return None
    return parse_osudb(data)
