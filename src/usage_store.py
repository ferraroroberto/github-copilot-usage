"""Where this app's usage-log database lives.

The path comes from the vendored ``runtime_data`` helper, so the machine-level
runtime-data root, ``GITHUB_COPILOT_USAGE_DATA_DIR`` and ``FLEET_DATA_ROOT``
decide it. Resolving it never creates a directory.
"""

from __future__ import annotations

from pathlib import Path

from src import config
from src.runtime_data import runtime_db_path

APP = "github-copilot-usage"
DB_FILENAME = "usage.sqlite3"


def usage_db_path() -> Path:
    """The usage-log database path; never creates a directory.

    The log creates its parent folder on first write, so an unwritable location
    shows up as a dropped batch, not as a server that never starts.
    """
    config.load()  # .env overrides must count before the environment is read
    return runtime_db_path(APP, DB_FILENAME)
