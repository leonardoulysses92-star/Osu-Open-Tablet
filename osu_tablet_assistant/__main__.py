"""Command line entry point: ``python -m osu_tablet_assistant``."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .gui import i18n


def build_parser() -> argparse.ArgumentParser:
    """Argument parser for the launcher."""
    parser = argparse.ArgumentParser(
        prog="osu_tablet_assistant",
        description="Guided OpenTabletDriver setup assistant for osu! players.")
    parser.add_argument("--lang", choices=i18n.SUPPORTED, default=None,
                        help="interface language (default: the saved choice, else es)")
    parser.add_argument("--config-dir", type=Path,
                        help="OpenTabletDriver settings folder (the one holding settings.json)")
    parser.add_argument("--install-dir", type=Path,
                        help="OpenTabletDriver program folder (the one holding the Console/Daemon exe)")
    parser.add_argument("--dry-run", action="store_true",
                        help="never apply anything to OpenTabletDriver; only export a preset")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Launch the wizard; returns the process exit code."""
    args = build_parser().parse_args(argv)
    from .appconfig import app_data_dir, load_config
    app_dir = app_data_dir()
    i18n.set_language(args.lang or load_config(app_dir / "config.json").language)
    try:
        import tkinter
    except ImportError as exc:
        print(f"tkinter is required to run the assistant: {exc}", file=sys.stderr)
        return 1
    from .gui.app import App, prepare_session
    from .gui.session import Session
    session = Session(dry_run=args.dry_run)
    prepare_session(session, app_dir)
    try:
        app = App(session, args.config_dir, args.install_dir)
    except tkinter.TclError as exc:
        print(f"cannot open a window (no display?): {exc}", file=sys.stderr)
        return 1
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
