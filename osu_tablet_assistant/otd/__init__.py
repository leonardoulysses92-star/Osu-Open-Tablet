"""Everything that touches OpenTabletDriver: settings files, plugins, the daemon."""
from __future__ import annotations

from .controller import OtdController
from .plugins import available_filter_paths, list_installed_plugins
from .settings_io import (
    OtdSettingsError,
    apply_changes,
    backup_settings,
    find_config_dir,
    find_install_dir,
    list_profiles,
    load_settings,
    save_preset,
    write_settings,
)

__all__ = [
    "OtdController",
    "OtdSettingsError",
    "apply_changes",
    "available_filter_paths",
    "backup_settings",
    "find_config_dir",
    "find_install_dir",
    "list_installed_plugins",
    "list_profiles",
    "load_settings",
    "save_preset",
    "write_settings",
]
