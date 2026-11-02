"""Find the map osu! is playing at once: an index built from ``osu!.db`` (stable) or the hashed store (lazer).

The window title names the map ("Artist - Title [Version]") while it is played. Scanning every
``Songs/*/*.osu`` header on each new title made the coach one map behind on big libraries, so:

* **stable** (:class:`MapIndex`): ``osu!.db`` (``engine.osudb``) gives every difficulty with its exact
  title text (romanised and Unicode), its MD5 and its folder. Lookups are dictionary hits: the exact key
  first, then a loose normalised key. osu!.db is parsed again only when its mtime or size changes, in a
  background thread. Maps imported during the session may not be in it yet (osu! seems to write it
  rarely, perhaps only on exit: unverified), so a cheap **Songs delta** (one ``os.scandir``) reads the
  headers (``engine.beatmap``'s header scan and JSON cache) only of folders osu!.db does not list or that
  changed after it was written. Ambiguous titles return **every** candidate; the replay's MD5 settles it
  after the play (``engine.calibrate``).
* **lazer** (:class:`LazerIndex`): there is no Songs folder; ``%APPDATA%/osu/files/*/*/*`` holds every
  file under its SHA-256. Files whose first bytes say ``osu file format v`` (a UTF-8 BOM allowed) are
  ``.osu`` files; their header and MD5 go to a cache keyed by the file name, which never goes stale (same
  name, same content). The first run reads the start of every file, so it runs incrementally in a
  background thread with a progress count; later runs only look at new names.
* without osu!.db (or an unreadable one) the old full header scan (:class:`~.beatmap.BeatmapIndex`) is used.

:class:`MapFinder` is what the coach calls. Read-only FILES; osu!'s memory is never touched. Nothing here
blocks for long on the caller's thread: the heavy parts run in daemon threads.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping

from ..log import log_exception
from . import osucfg
from .beatmap import (BeatmapIndex, IndexEntry, WindowTitle, _loose, _norm, load_beatmap, parse_window_title,
                      read_header)
from .osudb import OsuDb, load_osudb
from .playtypes import Beatmap

DB_NAME = "osu!.db"
OSU_MAGIC = b"osu file format v"
UTF8_BOM = b"\xef\xbb\xbf"
MAX_OSU_BYTES = 8 * 1024 * 1024
LAZER_SAVE_EVERY = 500          # write the lazer cache after this many newly read files
LAZER_CACHE_VERSION = 1


# --------------------------------------------------------------------- keys
def _keys_of(info: WindowTitle | str) -> tuple[str, str, str]:
    """(exact, normalised, loose) key of a title or of an "Artist - Title [Version]" text."""
    if isinstance(info, str):
        parsed = parse_window_title(info) if info.lower().startswith("osu!") else parse_window_title(
            "osu! - " + info)
        if parsed is None:
            return "", "", ""
        info = parsed
    exact = f"{info.artist_title} [{info.version}]"
    return exact, _norm(exact), _loose(info.artist_title) + "\x00" + _loose(info.version)


@dataclass
class _Tables:
    """Lookups over one set of entries (osu!.db, the Songs delta, or the lazer store)."""
    by_key: dict[str, list[IndexEntry]] = field(default_factory=dict)
    by_norm: dict[str, list[IndexEntry]] = field(default_factory=dict)
    by_loose: dict[str, list[IndexEntry]] = field(default_factory=dict)
    by_md5: dict[str, IndexEntry] = field(default_factory=dict)
    folders: set[str] = field(default_factory=set)
    count: int = 0

    def add(self, entry: IndexEntry, folder: str = "") -> None:
        self.count += 1
        for name in dict.fromkeys(entry.names()):
            exact = f"{name} [{entry.version}]"
            self.by_key.setdefault(exact, []).append(entry)
            self.by_norm.setdefault(_norm(exact), []).append(entry)
            self.by_loose.setdefault(_loose(name) + "\x00" + _loose(entry.version), []).append(entry)
        if entry.md5:
            self.by_md5.setdefault(entry.md5.lower(), entry)
        if folder:
            self.folders.add(folder.casefold())

    def lookup(self, keys: tuple[str, str, str]) -> list[IndexEntry]:
        exact, norm, loose = keys
        if not exact:
            return []
        hits = self.by_key.get(exact)
        if not hits:
            hits = self.by_norm.get(norm)
        if not hits:
            hits = self.by_loose.get(loose)
        return list(dict.fromkeys(hits or ()))


def _unique(entries: Iterable[IndexEntry]) -> list[IndexEntry]:
    seen: dict[str, IndexEntry] = {}
    for e in entries:
        seen.setdefault(os.path.normcase(e.path), e)
    return list(seen.values())


def _newest(entries: list[IndexEntry]) -> IndexEntry | None:
    """Several entries for one title: the most recently modified first (the replay MD5 confirms it later)."""
    return max(entries, key=lambda e: e.mtime) if entries else None


# ------------------------------------------------------------------ stable
class MapIndex:
    """osu!.db + Songs delta for one osu!stable install."""

    def __init__(self, osu_dir: str | Path, songs_dir: str | Path | None = None,
                 cache_path: str | Path | None = None, background: bool = False,
                 loader: Callable[[Path], OsuDb | None] = load_osudb) -> None:
        self.osu_dir = Path(osu_dir)
        self.db_path = self.osu_dir / DB_NAME
        songs = songs_dir if songs_dir is not None else osucfg.songs_dir(self.osu_dir)
        self.songs_dir = Path(songs) if songs is not None else self.osu_dir / "Songs"
        self._headers = BeatmapIndex(self.songs_dir, cache_path)
        self.background = background
        self._loader = loader
        self._lock = threading.Lock()
        self._tables: _Tables | None = None
        self._delta = _Tables()
        self._delta_seen: dict[str, float] = {}    # folder -> mtime its headers were read at
        self._stamp: tuple[float, int] | None = None
        self._db_mtime = 0.0
        self._thread: threading.Thread | None = None
        self.complete = False                      # the last osu!.db read had every entry
        self.generation = 0                        # grows whenever the lookups can give new answers
        self.failed = False                        # osu!.db could not be read at all

    # -- osu!.db
    def stamp(self) -> tuple[float, int] | None:
        try:
            st = self.db_path.stat()
        except OSError:
            return None
        return st.st_mtime, st.st_size

    @property
    def ready(self) -> bool:
        return self._tables is not None

    @property
    def loading(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def refresh(self) -> bool:
        """Parse osu!.db again when it changed (in the background when ``background``). True if a load began."""
        stamp = self.stamp()
        if stamp is None or stamp == self._stamp or self.loading:
            return False
        self._stamp = stamp
        if self.background:
            self._thread = threading.Thread(target=self._load, args=(stamp,), name="OsuDbIndex", daemon=True)
            self._thread.start()
        else:
            self._load(stamp)
        return True

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for a background load; True when an index is available."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.ready

    def _load(self, stamp: tuple[float, int]) -> None:
        try:
            db = self._loader(self.db_path)
        except Exception:      # an unexpected file must never break the coach
            log_exception(f"reading {self.db_path}")
            db = None
        if db is None:
            with self._lock:
                self.failed = self._tables is None
                self.generation += 1
            return
        tables = _Tables()
        songs = self.songs_dir
        for e in db.entries:
            if not e.folder or not e.osu_file:
                continue
            tables.add(IndexEntry(str(songs / e.folder / e.osu_file), e.artist, e.title,
                                  e.artist_unicode or e.artist, e.title_unicode or e.title, e.version,
                                  e.last_modified or 0.0, 0, e.md5, "osudb"), e.folder)
        with self._lock:
            self._tables, self._delta, self._delta_seen = tables, _Tables(), {}
            self._db_mtime = stamp[0]
            self.complete, self.failed = db.complete, False
            self.generation += 1

    # -- Songs delta
    def delta(self, hint: WindowTitle | None = None) -> int:
        """Read headers of folders osu!.db does not cover (new, or changed after it). Returns new entries.

        ``hint`` (the title that was not found) also checks known folders whose name contains its song
        title with a real ``stat``: the listing's copy of a folder's mtime is updated lazily by NTFS, so a
        difficulty just added to a known folder can look unchanged for a while."""
        tables = self._tables
        known = tables.folders if tables is not None else set()
        wanted = _loose(hint.title) if hint is not None and hint.title else ""
        stale: list[Path] = []
        try:
            with os.scandir(self.songs_dir) as listing:
                for item in listing:
                    try:
                        if not item.is_dir():
                            continue
                        mtime = item.stat().st_mtime        # no extra system call on Windows
                    except OSError:
                        continue
                    name = item.name
                    if name.casefold() in known and mtime <= self._db_mtime and not (
                            wanted and wanted in _loose(name)):
                        continue
                    try:     # the few candidates get a real stat: NTFS updates the listing's copy lazily
                        mtime = os.stat(item.path).st_mtime
                    except OSError:
                        continue
                    if (name.casefold() in known and mtime <= self._db_mtime) or self._delta_seen.get(name) == mtime:
                        continue
                    self._delta_seen[name] = mtime
                    stale.append(Path(item.path))
        except OSError:
            return 0
        if not stale:
            return 0
        entries = self._headers.scan_folders(stale)
        with self._lock:
            for e in entries:
                self._delta.add(e, Path(e.path).parent.name)
            if entries:
                self.generation += 1
        return len(entries)

    # -- lookups
    def lookup(self, info: WindowTitle | str) -> list[IndexEntry]:
        """Every entry the title can mean (exact key, then loose); delta entries first (fresher)."""
        keys = _keys_of(info)
        tables, delta = self._tables, self._delta
        hits = delta.lookup(keys) + (tables.lookup(keys) if tables is not None else [])
        return _unique(hits)

    def find(self, info: WindowTitle) -> IndexEntry | None:
        """Same contract as :meth:`BeatmapIndex.find` (so :func:`~.beatmap.find_beatmap` takes either)."""
        return _newest(self.lookup(info))

    def by_md5(self, md5: str) -> IndexEntry | None:
        digest = (md5 or "").lower()
        tables = self._tables
        hit = self._delta.by_md5.get(digest) or (tables.by_md5.get(digest) if tables is not None else None)
        if hit is not None or not digest:
            return hit
        for e in list(self._delta_entries()):          # delta entries get their MD5 only when asked
            if not e.md5 and _md5_of(e.path) == digest:
                return e
        return None

    def _delta_entries(self) -> Iterable[IndexEntry]:
        for group in list(self._delta.by_key.values()):
            yield from group

    @property
    def size(self) -> int:
        tables = self._tables
        return (tables.count if tables is not None else 0) + self._delta.count


def _md5_of(path: str | Path) -> str | None:
    try:
        return hashlib.md5(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


# ------------------------------------------------------------------- lazer
def lazer_files_dir(env: Mapping[str, str] | None = None) -> Path | None:
    """lazer's hashed store: ``%APPDATA%/osu/files`` (or the custom folder named in ``storage.ini``)."""
    env = os.environ if env is None else env
    base = env.get("APPDATA")
    if not base:
        return None
    root = Path(base) / "osu"
    try:
        for line in (root / "storage.ini").read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "FullPath" and value.strip():
                root = Path(value.strip())
                break
    except OSError:
        pass
    return root / "files"


def sniff_osu(head: bytes) -> bool:
    """True when the first bytes of a file are those of a ``.osu`` (optional UTF-8 BOM, then the magic)."""
    if head.startswith(UTF8_BOM):
        head = head[len(UTF8_BOM):]
    return head.lstrip(b" \t\r\n").startswith(OSU_MAGIC)


class LazerIndex:
    """``.osu`` files of osu!lazer's store, found by sniffing, with a cache keyed by the (SHA-256) file name."""

    def __init__(self, files_dir: str | Path, cache_path: str | Path | None = None,
                 save_every: int = LAZER_SAVE_EVERY) -> None:
        self.files_dir = Path(files_dir)
        self.cache_path = Path(cache_path) if cache_path else None
        self.save_every = save_every
        self._tables = _Tables()
        self._osu: dict[str, list] = {}         # name -> [md5, artist, title, artist_u, title_u, version, mtime]
        self._other: set[str] = set()           # names known not to be .osu files
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.done = 0
        self.total = 0
        self.complete = False
        self.generation = 0
        self._load_cache()

    @property
    def progress(self) -> tuple[int, int]:
        """(files looked at, files found) of the current or last run."""
        return self.done, self.total

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def _load_cache(self) -> None:
        if self.cache_path is None:
            return
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or data.get("version") != LAZER_CACHE_VERSION:
            return
        osu, other = data.get("osu"), data.get("other")
        if isinstance(osu, dict):
            for name, row in osu.items():
                if isinstance(row, list) and len(row) == 7:
                    self._osu[name] = row
                    self._add(name, row)
        if isinstance(other, list):
            self._other = {n for n in other if isinstance(n, str)}

    def _save_cache(self) -> None:
        if self.cache_path is None:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": LAZER_CACHE_VERSION, "osu": self._osu,
                                       "other": sorted(self._other)}), encoding="utf-8")
            tmp.replace(self.cache_path)
        except OSError:
            pass    # the cache only saves time

    def _add(self, name: str, row: list) -> None:
        md5, artist, title, artist_u, title_u, version, mtime = row
        path = self.files_dir / name[:1] / name[:2] / name
        self._tables.add(IndexEntry(str(path), artist, title, artist_u or artist, title_u or title, version,
                                    float(mtime or 0.0), 0, md5, "lazer"))

    def _names(self) -> list[tuple[str, Path]]:
        found: list[tuple[str, Path]] = []
        try:
            with os.scandir(self.files_dir) as first:
                tops = [Path(d.path) for d in first if d.is_dir()]
        except OSError:
            return found
        for top in tops:
            try:
                with os.scandir(top) as second:
                    mids = [Path(d.path) for d in second if d.is_dir()]
            except OSError:
                continue
            for mid in mids:
                try:
                    with os.scandir(mid) as third:
                        found.extend((f.name, Path(f.path)) for f in third if f.is_file())
                except OSError:
                    continue
        return found

    def _read_one(self, path: Path) -> list | None:
        try:
            with path.open("rb") as handle:
                head = handle.read(64)
                if not sniff_osu(head):
                    return None
                rest = handle.read(MAX_OSU_BYTES)
            mtime = path.stat().st_mtime
        except OSError:
            return None
        meta = read_header(path) or {}
        return [hashlib.md5(head + rest).hexdigest(), meta.get("Artist", ""), meta.get("Title", ""),
                meta.get("ArtistUnicode", ""), meta.get("TitleUnicode", ""), meta.get("Version", ""), mtime]

    def build(self) -> int:
        """Look at every new file once (blocking); returns how many ``.osu`` files are known."""
        names = self._names()
        new = [(n, p) for n, p in names if n not in self._osu and n not in self._other]
        self.total, self.done = len(names), len(names) - len(new)
        since_save = 0
        for name, path in new:
            if self._stop.is_set():
                break
            row = self._read_one(path)
            if row is None:
                self._other.add(name)
            else:
                self._osu[name] = row
                self._add(name, row)
                self.generation += 1
            self.done += 1
            since_save += 1
            if since_save >= self.save_every:
                self._save_cache()
                since_save = 0
        else:
            self.complete = True
        if since_save or not new:
            self._save_cache()
        self.generation += 1
        return len(self._osu)

    def start(self) -> None:
        """Build in a daemon thread (a no-op while one runs or once complete)."""
        if self.running or self.complete:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="LazerIndex", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self.build()
        except Exception:      # never let a strange file kill the coach
            log_exception("indexing the osu!lazer files")

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def lookup(self, info: WindowTitle | str) -> list[IndexEntry]:
        return _unique(self._tables.lookup(_keys_of(info)))

    def find(self, info: WindowTitle) -> IndexEntry | None:
        return _newest(self.lookup(info))

    def by_md5(self, md5: str) -> IndexEntry | None:
        return self._tables.by_md5.get((md5 or "").lower())


# ------------------------------------------------------------------ finder
class MapFinder:
    """The map named by osu!'s window title, from the fastest source the running install offers.

    stable with ``osu!.db`` -> :class:`MapIndex`; lazer -> :class:`LazerIndex`; otherwise the old
    :class:`BeatmapIndex` full scan (rescanned once per new title, as before). ``background=False`` makes
    every load synchronous (tests). ``candidates`` keeps every entry the last title could mean.
    """

    def __init__(self, cache_path: str | Path | None = None, background: bool = True,
                 lazer_files: str | Path | None = None, wait_s: float = 0.25) -> None:
        self.cache_path = Path(cache_path) if cache_path else None
        self.background, self.wait_s = background, wait_s
        self._lazer_files = Path(lazer_files) if lazer_files else None
        self._db: MapIndex | None = None
        self._lazer: LazerIndex | None = None
        self._scan: BeatmapIndex | None = None
        self._scanned_for: str | None = None
        self._delta_tried: set[str] = set()
        self.candidates: tuple[IndexEntry, ...] = ()
        self.entry: IndexEntry | None = None
        self.source = ""                      # osudb | lazer | songs | "" (nothing found)
        self.pending = False                  # an index is still being built: try again later

    # -- sources
    def _lazer_cache(self) -> Path | None:
        return self.cache_path.with_name(self.cache_path.stem + ".lazer.json") if self.cache_path else None

    def _db_index(self, root: Path) -> MapIndex:
        if self._db is None or self._db.osu_dir != root:
            self._db = MapIndex(root, cache_path=self.cache_path, background=self.background)
            self._delta_tried.clear()
        return self._db

    def _lazer_index(self) -> LazerIndex | None:
        if self._lazer is None:
            folder = self._lazer_files or lazer_files_dir()
            if folder is None:
                return None
            self._lazer = LazerIndex(folder, self._lazer_cache())
        return self._lazer

    @property
    def generation(self) -> int:
        """Grows when an index learned something (a retry of a missed title can then succeed)."""
        return sum(i.generation for i in (self._db, self._lazer) if i is not None)

    @property
    def progress(self) -> tuple[int, int] | None:
        """(done, total) of a lazer store indexing run, None when there is none."""
        return self._lazer.progress if self._lazer is not None and not self._lazer.complete else None

    @property
    def osu_dir(self) -> Path | None:
        return self._db.osu_dir if self._db is not None else None

    @property
    def lazer_files(self) -> Path | None:
        """lazer's store folder (configured, or the default one)."""
        return self._lazer.files_dir if self._lazer is not None else self._lazer_files or lazer_files_dir()

    def by_md5(self, md5: str) -> IndexEntry | None:
        for index in (self._db, self._lazer):
            if index is not None:
                hit = index.by_md5(md5)
                if hit is not None:
                    return hit
        return None

    def close(self) -> None:
        if self._lazer is not None:
            self._lazer.stop()

    # -- the lookup
    def find(self, title: str | None, exe: str | None, flavor: str | None = None) -> Beatmap | None:
        """The map the title names (None: no map in the title, not found, or still indexing -> ``pending``)."""
        info = parse_window_title(title)
        self.pending, self.candidates, self.entry, self.source = False, (), None, ""
        lazer = flavor == "lazer" or "lazer" in (exe or "").lower()
        root = Path(exe).parent if exe else None
        if lazer:
            hits = self._find_lazer(info)
        elif root is not None and (root / DB_NAME).is_file():
            hits = self._find_db(root, info, title)
        else:
            hits = self._find_scan(root, info, title)
        self.candidates = tuple(sorted(hits, key=lambda e: -e.mtime))
        for entry in self.candidates:                       # a stale entry (file gone) gives way to the next
            beatmap = load_beatmap(entry.path)
            if beatmap is not None:
                self.entry, self.source = entry, entry.source
                return beatmap
        return None

    def _find_db(self, root: Path, info: WindowTitle | None, title: str | None) -> list[IndexEntry]:
        index = self._db_index(root)
        index.refresh()
        if not index.ready and index.loading:
            index.wait(self.wait_s)
        if info is None:
            return []
        if not index.ready:
            if index.failed:                               # unreadable osu!.db: the old scan still works
                return self._find_scan(root, info, title)
            self.pending = True
            return []
        hits = index.lookup(info)
        key = _keys_of(info)[0]
        if not hits and key not in self._delta_tried:     # imported this session? one cheap delta per title
            self._delta_tried.add(key)
            index.delta(info)
            hits = index.lookup(info)
        return hits

    def _find_lazer(self, info: WindowTitle | None) -> list[IndexEntry]:
        index = self._lazer_index()
        if index is None:
            return []
        if self.background:
            index.start()
        elif not index.complete:
            index.build()
        self.pending = not index.complete
        return index.lookup(info) if info is not None else []

    def _find_scan(self, root: Path | None, info: WindowTitle | None, title: str | None) -> list[IndexEntry]:
        songs = osucfg.songs_dir(root) if root is not None else None
        if info is None or songs is None:
            return []
        if self._scan is None or self._scan.songs_dir != songs:
            self._scan = BeatmapIndex(songs, self.cache_path)
            self._scanned_for = None
        if self._scanned_for != title:            # rescan once per title (new maps appear while osu! runs)
            self._scan.scan()
            self._scanned_for = title
        hit = self._scan.find(info)
        return [hit] if hit is not None else []
