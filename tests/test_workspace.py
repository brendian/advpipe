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


import pytest  # noqa: E402

from advpipe.workspace import slugify  # noqa: E402


@pytest.mark.parametrize(
    ("text", "slug"),
    [
        ("add a clamp(x, lo, hi) function", "add-clamp-x-lo-hi-function"),
        (
            "Create the SQLite storage layer for the homeinv server in a new module `db.py`.\n\n"
            "- details that must be ignored",
            "create-sqlite-storage-layer-homeinv",
        ),
        ("\n\n  Fix the refund rounding bug  \n", "fix-refund-rounding-bug"),
        ("The", "the"),  # only filler words: keep them rather than return nothing
        ("!!!", "work"),
        ("x" * 60, "x" * 40),
        ("Ümlaut café support", "mlaut-caf-support"),
    ],
)
def test_slugify(text: str, slug: str) -> None:
    assert slugify(text) == slug
    assert len(slug) <= 40


def test_branch_named_from_work_item_with_collision_suffix(target_repo: Path) -> None:
    first = Workspace.create(target_repo, "w1", "Fix the refund rounding bug")
    second = Workspace.create(target_repo, "w2", "Fix the refund rounding bug")
    third = Workspace.create(target_repo, "w3", "fix refund rounding bug!")
    assert first.branch == "advpipe/fix-refund-rounding-bug"
    assert second.branch == "advpipe/fix-refund-rounding-bug-2"
    assert third.branch == "advpipe/fix-refund-rounding-bug-3"
    assert first.path.name == "w1"  # worktree dirs stay keyed by run id


def test_branch_defaults_to_run_id_without_name(target_repo: Path) -> None:
    assert Workspace.create(target_repo, "r42").branch == "advpipe/r42"
