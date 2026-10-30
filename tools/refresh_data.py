"""Regenerate the repo-level ``data/`` bundle from ``osu_tablet_assistant/knowledge``.

    python tools/refresh_data.py           # copy the files and rewrite data/manifest.json
    python tools/refresh_data.py --check   # exit 1 if data/ is out of date (used by the tests)

``data/`` is what the in-app "Update community data" button downloads (raw
GitHub, branch main). The manifest's version and date come from
``community_presets.json`` (``version`` / ``generated``): bump them there when
the community data changes, then run this script and commit ``data/``.
The bundle is validated with the same code the app uses before anything is written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from osu_tablet_assistant.knowledge import updater  # noqa: E402

KNOWLEDGE = ROOT / "osu_tablet_assistant" / "knowledge"
DATA = ROOT / "data"


def build(knowledge: Path = KNOWLEDGE) -> tuple[bytes, dict[str, bytes]]:
    """``(manifest bytes, {file name: bytes})`` for the current knowledge files (validated)."""
    files = {name: (knowledge / name).read_bytes() for name in updater.FILES}
    presets = json.loads(files[updater.PRESETS_FILE].decode("utf-8"))
    manifest = {
        "schema": updater.SCHEMA,
        "version": presets["version"],
        "date": presets["generated"],
        "files": {name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                  for name, data in files.items()},
    }
    raw = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    updater.validate_bundle(raw, files)   # raises BundleError if the data is not publishable
    return raw, files


def is_current(data_dir: Path = DATA, knowledge: Path = KNOWLEDGE) -> bool:
    """True when ``data_dir`` holds exactly what :func:`build` produces."""
    raw, files = build(knowledge)
    try:
        return (data_dir / updater.MANIFEST).read_bytes() == raw and all(
            (data_dir / name).read_bytes() == data for name, data in files.items())
    except OSError:
        return False


def write(data_dir: Path = DATA, knowledge: Path = KNOWLEDGE) -> Path:
    """Write the bundle into ``data_dir``; returns the manifest path."""
    raw, files = build(knowledge)
    data_dir.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (data_dir / name).write_bytes(data)
    (data_dir / updater.MANIFEST).write_bytes(raw)
    return data_dir / updater.MANIFEST


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only verify that data/ is up to date")
    args = parser.parse_args(argv)
    if args.check:
        ok = is_current()
        print("data/ is up to date" if ok else "data/ is OUT OF DATE: run python tools/refresh_data.py")
        return 0 if ok else 1
    print(f"wrote {write()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
