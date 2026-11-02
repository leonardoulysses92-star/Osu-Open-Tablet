"""Builders of synthetic osu! files for the tests: a tiny ``osu!.db``, a user ``.cfg`` and ``.osr`` bytes.

Written from the documented formats (osu! wiki); no real osu! install is needed or touched.
"""
from __future__ import annotations

import lzma
import struct
from dataclasses import dataclass
from typing import Iterable, Sequence

TICKS_AT_EPOCH = 621_355_968_000_000_000


def s(text: str | None) -> bytes:
    """An osu! string: 0x00 (empty) or 0x0b + ULEB128 length + UTF-8."""
    if not text:
        return b"\x00"
    raw = text.encode("utf-8")
    n, out = len(raw), bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            break
    return b"\x0b" + bytes(out) + raw


def ticks(unix_s: float | None) -> int:
    return 0 if unix_s is None else int(unix_s * 10_000_000) + TICKS_AT_EPOCH


@dataclass
class DbMap:
    artist: str = "Artist"
    title: str = "Song"
    version: str = "Hard"
    folder: str = "1 Artist - Song"
    osu_file: str = "Artist - Song (me) [Hard].osu"
    md5: str = "0" * 32
    artist_unicode: str = ""
    title_unicode: str = ""
    creator: str = "me"
    audio: str = "audio.mp3"
    ar: float = 9.0
    cs: float = 4.0
    hp: float = 6.0
    od: float = 8.0
    mode: int = 0
    stars: Sequence[int] = (3, 0, 1, 2)      # pairs per mode table (osu!, taiko, catch, mania)
    timing_points: int = 4
    last_played: float | None = 1_700_000_000.0
    modified: float | None = 1_690_000_000.0


def db_entry(m: DbMap, version: int) -> bytes:
    body = b"".join(s(v) for v in (m.artist, m.artist_unicode, m.title, m.title_unicode, m.creator, m.version,
                                   m.audio, m.md5, m.osu_file))
    body += struct.pack("<Bhhhq", 4, 100, 50, 1, ticks(m.modified))
    if version >= 20140609:
        body += struct.pack("<4f", m.ar, m.cs, m.hp, m.od)
    else:
        body += struct.pack("<4B", int(m.ar), int(m.cs), int(m.hp), int(m.od))
    body += struct.pack("<d", 1.4)
    if version >= 20140609:
        for count in m.stars:
            body += struct.pack("<i", count)
            for i in range(count):
                if version >= 20250107:
                    body += b"\x08" + struct.pack("<i", i) + b"\x0c" + struct.pack("<f", 5.5)
                else:
                    body += b"\x08" + struct.pack("<i", i) + b"\x0d" + struct.pack("<d", 5.5)
    body += struct.pack("<iii", 120, 121000, 40000)
    body += struct.pack("<i", m.timing_points) + struct.pack("<ddB", 300.0, 1000.0, 1) * m.timing_points
    body += struct.pack("<iii4BhfB", 11, 22, 0, 9, 9, 9, 9, 0, 0.7, m.mode)
    body += s("source") + s("tag1 tag2") + struct.pack("<h", 0) + s("")
    body += b"\x00" + struct.pack("<q", ticks(m.last_played)) + b"\x00" + s(m.folder)
    body += struct.pack("<q", ticks(m.modified)) + b"\x00" * 5
    if version < 20140609:
        body += struct.pack("<h", 0)
    body += struct.pack("<i", 0) + b"\x00"
    if version < 20191106:
        return struct.pack("<i", len(body)) + body
    return body


def make_osudb(maps: Iterable[DbMap], version: int = 20250108, player: str = "SecretPlayerName",
               count: int | None = None) -> bytes:
    """An ``osu!.db`` with these maps (``count`` overrides the number the header announces)."""
    maps = list(maps)
    head = struct.pack("<ii", version, len({m.folder for m in maps})) + b"\x01" + struct.pack("<q", 0) + s(player)
    head += struct.pack("<i", len(maps) if count is None else count)
    return head + b"".join(db_entry(m, version) for m in maps) + struct.pack("<i", 0)


def osu_text(artist: str = "Artist", title: str = "Song", version: str = "Hard", n: int = 6,
             artist_unicode: str = "", title_unicode: str = "") -> str:
    """A small but valid ``.osu`` file."""
    meta = [f"Title:{title}", f"Artist:{artist}", f"Version:{version}", "Creator:me"]
    if artist_unicode:
        meta.append(f"ArtistUnicode:{artist_unicode}")
    if title_unicode:
        meta.append(f"TitleUnicode:{title_unicode}")
    objects = [f"{64 + 40 * i},{100 + 20 * i},{1000 + 400 * i},1,0" for i in range(n)]
    return ("osu file format v14\n\n[General]\nAudioFilename: audio.mp3\n\n[Metadata]\n" + "\n".join(meta)
            + "\n\n[Difficulty]\nCircleSize:4\nOverallDifficulty:8\n\n[HitObjects]\n" + "\n".join(objects) + "\n")


def make_cfg(**values) -> str:
    """The text of an ``osu!.<user>.cfg`` (secret-looking keys included to prove they are not kept)."""
    lines = ["# osu! configuration for SecretPlayerName", "# last updated on 2026-01-01", "",
             "Username = SecretPlayerName", "Password = 0123456789abcdef"]
    lines += [f"{key} = {value}" for key, value in values.items()]
    return "\n".join(lines) + "\n"


def make_osr(frames: Sequence[tuple[int, float, float, int]], md5: str = "a" * 32, mods: int = 0,
             version: int = 20260101, mode: int = 0, seed: bool = True, lead: bool = True,
             player: str = "SecretPlayerName") -> bytes:
    """An ``.osr`` from ``(w, x, y, keys)`` frames (w = ms since the previous frame), LZMA "alone" stream.

    ``lead`` adds stable's two leading ``(256, -500)`` frames, ``seed`` the trailing ``-12345`` RNG frame.
    """
    parts = (["0|256|-500|0", "-1|256|-500|0"] if lead else [])
    parts += [f"{w}|{x:.4f}|{y:.4f}|{k}" for w, x, y, k in frames]
    if seed:
        parts.append("-12345|0|0|7364")
    data = lzma.compress(",".join(parts).encode("ascii"), format=lzma.FORMAT_ALONE)
    head = (bytes([mode]) + struct.pack("<i", version) + s(md5) + s(player) + s("deadbeef" * 4)
            + struct.pack("<6H", 300, 20, 3, 5, 2, 4) + struct.pack("<i", 123456) + struct.pack("<H", 99) + b"\x00"
            + struct.pack("<i", mods) + s("1000|0.9,2000|1") + struct.pack("<q", 638000000000000000)
            + struct.pack("<i", len(data)))
    return head + data + struct.pack("<q", 987654321)
