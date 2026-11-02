"""Build identity shown in the page footer: the checkout's short commit and when this process started.

Read straight from the git files (no subprocess) so a machine without git, or a
copy without a .git folder, degrades to "unknown" instead of failing.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent


def _git_dir(root: Path) -> Optional[Path]:
    dot_git = root / ".git"
    if dot_git.is_dir():
        return dot_git
    if dot_git.is_file():  # a linked worktree: ".git" is a "gitdir: <path>" pointer
        text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
        if text.startswith("gitdir:"):
            return Path(text[len("gitdir:"):].strip())
    return None


def _read_ref(git_dir: Path, ref: str) -> Optional[str]:
    # A linked worktree keeps HEAD privately but shares refs via "commondir".
    common = git_dir
    pointer = git_dir / "commondir"
    if pointer.is_file():
        common = (git_dir / pointer.read_text(encoding="utf-8").strip()).resolve()
    loose = common / ref
    if loose.is_file():
        return loose.read_text(encoding="utf-8").strip()
    packed = common / "packed-refs"
    if packed.is_file():
        for line in packed.read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref) and not line.startswith(("#", "^")):
                return line.split(" ", 1)[0]
    return None


def read_git_sha(root: Path = _ROOT) -> str:
    """Short commit of the checkout, or "unknown" when it cannot be read."""
    try:
        git_dir = _git_dir(root)
        if git_dir is None:
            return "unknown"
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        sha = _read_ref(git_dir, head[5:]) if head.startswith("ref:") else head
        return sha[:7] if sha else "unknown"
    except (OSError, ValueError):
        return "unknown"


GIT_SHA = read_git_sha()
# Process start, not commit time: a restart onto the same commit still changes the line.
BUILT_AT = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
