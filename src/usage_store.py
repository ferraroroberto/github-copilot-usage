"""Where this app's usage-log database lives.

The vendored ``runtime_data`` helper defaults to a machine-level root (a
top-level folder on Windows) that a fresh clone has no business creating. This
module keeps that helper for the explicit overrides and otherwise puts the file
in the current user's own data folder.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from src import config
from src.runtime_data import FLEET_DATA_ROOT_ENV, app_dir_env_var, runtime_db_path

APP = "github-copilot-usage"
DB_FILENAME = "usage.sqlite3"


def _user_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", "").strip() or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME", "").strip() or str(Path.home() / ".local" / "share")
    return Path(base) / APP


def usage_db_path() -> Path:
    """The usage-log database path; never creates a directory.

    ``<APP>_DATA_DIR`` or ``FLEET_DATA_ROOT`` (from the environment or ``.env``)
    wins; otherwise the file sits in the user's local data folder. The log
    creates its parent folder on first write, so an unwritable location shows up
    as a dropped batch, not as a server that never started.
    """
    config.load()  # .env overrides must count before the environment is read
    if os.environ.get(app_dir_env_var(APP), "").strip() or os.environ.get(FLEET_DATA_ROOT_ENV, "").strip():
        return runtime_db_path(APP, DB_FILENAME)
    return _user_data_dir() / DB_FILENAME
