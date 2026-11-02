"""The footer's build identity: a readable short sha, or "unknown" — never a crash."""

from __future__ import annotations

from pathlib import Path

from app import build_info


def test_reads_sha_from_a_detached_head(tmp_path: Path) -> None:
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text("0123456789abcdef0123456789abcdef01234567\n", encoding="utf-8")
    assert build_info.read_git_sha(tmp_path) == "0123456"


def test_reads_sha_through_a_branch_ref(tmp_path: Path) -> None:
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "refs" / "heads" / "main").write_text("abcdef0123456789abcdef0123456789abcdef01\n", encoding="utf-8")
    assert build_info.read_git_sha(tmp_path) == "abcdef0"


def test_reads_sha_from_packed_refs(tmp_path: Path) -> None:
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "packed-refs").write_text("# pack-refs\nfedcba9876543210fedcba9876543210fedcba98 refs/heads/main\n", encoding="utf-8")
    assert build_info.read_git_sha(tmp_path) == "fedcba9"


def test_no_git_folder_is_unknown(tmp_path: Path) -> None:
    assert build_info.read_git_sha(tmp_path) == "unknown"
