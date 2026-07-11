"""Runtime discovery of Copilot data locations.

GitHub Copilot Chat (the VS Code extension) writes one event-sourced
``<uuid>.jsonl`` per chat session under the *editor's* storage area:

    <product-dir>/User/workspaceStorage/<md5>/chatSessions/*.jsonl   (per workspace)
    <product-dir>/User/globalStorage/emptyWindowChatSessions/*.jsonl (no workspace)

where ``<product-dir>`` depends on the editor build — stock VS Code is
``Code``, but Insiders, VSCodium, Cursor, Windsurf and any company-branded
fork each get their own folder under the same platform config root. Instead
of hardcoding product names, discovery **enumerates every child of the
platform config root(s)** and keeps the ones that actually contain
``User/workspaceStorage`` — so an unknown corporate fork is found at runtime
with zero configuration. Remote development (``~/.vscode-server`` and
friends) is probed too, plus any ``extra_roots`` from config.json.

The GitHub Copilot **CLI** keeps its own session store at
``~/.copilot/session-state/<uuid>/`` — optional, parsed by ``cli_parser``.

Everything here is read-only and cheap: directory listings only, no file
content is touched.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from app import config


@dataclass
class StorageRoot:
    """One discovered editor storage area (a ``User`` directory)."""

    ide: str          # display name of the editor build, e.g. "Code", "Cursor"
    user_dir: Path    # the .../User directory
    origin: str       # "auto" | "config"


def _platform_config_roots() -> List[Path]:
    """Directories whose children are editor product dirs (Code, Cursor, …)."""
    roots: List[Path] = []
    home = Path.home()
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        roots.append(Path(appdata) if appdata else home / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        roots.append(home / "Library" / "Application Support")
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        roots.append(Path(xdg) if xdg else home / ".config")
    return [r for r in roots if r.is_dir()]


def _remote_server_roots() -> Iterable[StorageRoot]:
    """VS Code Remote / tunnel server data dirs in the home directory."""
    home = Path.home()
    for name in (".vscode-server", ".vscode-server-insiders", ".cursor-server"):
        user_dir = home / name / "data" / "User"
        if (user_dir / "workspaceStorage").is_dir():
            yield StorageRoot(ide=name.lstrip("."), user_dir=user_dir, origin="auto")


def _looks_like_user_dir(p: Path) -> bool:
    return (p / "workspaceStorage").is_dir() or (p / "globalStorage").is_dir()


def _coerce_to_user_dirs(root: Path) -> List[Path]:
    """Map an arbitrary configured path onto zero or more ``User`` dirs.

    Accepts the product dir (…/MyEditor), the User dir itself, a
    workspaceStorage dir, or a data dir with User under it — people will
    paste whichever path they found first.
    """
    if not root.is_dir():
        return []
    if root.name == "workspaceStorage":
        return [root.parent]
    if _looks_like_user_dir(root):
        return [root]
    for candidate in (root / "User", root / "data" / "User"):
        if _looks_like_user_dir(candidate):
            return [candidate]
    # Last resort: treat it as a config root full of product dirs.
    found = []
    try:
        for child in root.iterdir():
            cand = child / "User"
            if child.is_dir() and _looks_like_user_dir(cand):
                found.append(cand)
    except OSError:
        pass
    return found


def discover_roots() -> List[StorageRoot]:
    """Return every editor storage area found on this machine."""
    seen: Dict[str, StorageRoot] = {}

    def add(root: StorageRoot) -> None:
        key = str(root.user_dir.resolve()).lower()
        if key not in seen:
            seen[key] = root

    for cfg_root in _platform_config_roots():
        try:
            children = sorted(cfg_root.iterdir())
        except OSError:
            continue
        for child in children:
            user_dir = child / "User"
            if child.is_dir() and (user_dir / "workspaceStorage").is_dir():
                add(StorageRoot(ide=child.name, user_dir=user_dir, origin="auto"))

    for root in _remote_server_roots():
        add(root)

    for extra in config.extra_roots():
        for user_dir in _coerce_to_user_dirs(extra):
            add(StorageRoot(ide=user_dir.parent.name or str(user_dir), user_dir=user_dir, origin="config"))

    return list(seen.values())


def chat_session_files(root: StorageRoot) -> List[dict]:
    """List every chat-session jsonl under one storage root.

    Returns ``[{"path": Path, "workspace_dir": Optional[Path]}]`` where
    ``workspace_dir`` is the ``workspaceStorage/<hash>`` dir (None for
    empty-window sessions from globalStorage).
    """
    out: List[dict] = []
    ws_storage = root.user_dir / "workspaceStorage"
    if ws_storage.is_dir():
        try:
            hash_dirs = [p for p in ws_storage.iterdir() if p.is_dir()]
        except OSError:
            hash_dirs = []
        for hdir in hash_dirs:
            chat_dir = hdir / "chatSessions"
            if not chat_dir.is_dir():
                continue
            try:
                for jf in chat_dir.glob("*.jsonl"):
                    out.append({"path": jf, "workspace_dir": hdir})
            except OSError:
                continue

    empty_dir = root.user_dir / "globalStorage" / "emptyWindowChatSessions"
    if empty_dir.is_dir():
        try:
            for jf in empty_dir.glob("*.jsonl"):
                out.append({"path": jf, "workspace_dir": None})
        except OSError:
            pass
    return out


def copilot_cli_dir() -> Optional[Path]:
    d = Path.home() / ".copilot" / "session-state"
    return d if d.is_dir() else None
