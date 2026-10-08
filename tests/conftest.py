from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from advpipe.config import Config, Gates, Limits

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def target_repo(tmp_path: Path) -> Path:
    """A fresh git copy of tests/fixtures/sample_repo."""
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo, ignore=shutil.ignore_patterns("__pycache__", ".*_cache"))
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "baseline")
    return repo


@pytest.fixture
def config() -> Config:
    return Config(
        gates=Gates(
            test=[sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            types=[],
            lint=[],
            security=[],
        ),
        limits=Limits(budget_usd_per_task=5.0),
    )
