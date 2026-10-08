from __future__ import annotations

from pathlib import Path

from conftest import git

from advpipe.workspace import Workspace


def test_worktree_isolated_from_user_tree(target_repo: Path) -> None:
    ws = Workspace.create(target_repo, "w1")
    assert ws.branch == "advpipe/w1"
    assert git(ws.path, "branch", "--show-current").strip() == "advpipe/w1"
    (ws.path / "new.py").write_text("x = 1\n")
    ws.commit("add new")
    assert not (target_repo / "new.py").exists()
    assert git(target_repo, "branch", "--show-current").strip() == "main"
    assert git(target_repo, "status", "--porcelain") == ""  # .advpipe/ is excluded


def test_changed_files_and_restore(target_repo: Path) -> None:
    ws = Workspace.create(target_repo, "w2")
    base = ws.head()
    (ws.path / "tests" / "test_core.py").write_text("# gutted\n")
    (ws.path / "tests" / "test_new.py").write_text("# new\n")
    (ws.path / "mathutils" / "core.py").write_text("# changed\n")

    assert ws.changed_files(base, ["tests/"]) == ["tests/test_core.py", "tests/test_new.py"]
    assert ws.diff(base, ["tests/"]).count("diff --git") == 2  # untracked files included

    ws.restore(base, ["tests/test_core.py", "tests/test_new.py"])
    assert ws.changed_files(base, ["tests/"]) == []
    assert not (ws.path / "tests" / "test_new.py").exists()
    assert "def test_mean" in (ws.path / "tests" / "test_core.py").read_text()
    assert ws.changed_files(base) == ["mathutils/core.py"]
