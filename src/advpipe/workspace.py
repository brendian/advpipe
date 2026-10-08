"""Git worktree per run, plus diff and restore helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

GIT_IDENTITY = ["-c", "user.name=advpipe", "-c", "user.email=advpipe@localhost"]
MAX_DIFF_CHARS = 60_000


class GitError(RuntimeError):
    pass


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *GIT_IDENTITY, *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


class Workspace:
    """A git worktree on branch ``advpipe/<run-id>``. The user's working tree is never touched."""

    def __init__(self, repo: Path, path: Path, branch: str) -> None:
        self.repo = repo
        self.path = path
        self.branch = branch

    @classmethod
    def create(cls, repo: Path, run_id: str) -> Workspace:
        """New worktree and branch off the repo's current HEAD."""
        repo = Path(git(repo, "rev-parse", "--show-toplevel").strip())
        _exclude_advpipe_dir(repo)
        path = repo / ".advpipe" / "worktrees" / run_id
        branch = f"advpipe/{run_id}"
        git(repo, "worktree", "add", "-q", "-b", branch, str(path), "HEAD")
        return cls(repo, path, branch)

    @classmethod
    def reopen(cls, repo: Path, path: Path, branch: str) -> Workspace:
        """Reattach to a run's worktree, discarding uncommitted work from an interrupted stage."""
        repo = Path(git(repo, "rev-parse", "--show-toplevel").strip())
        if path.is_dir():
            git(path, "reset", "-q", "--hard", "HEAD")
            git(path, "clean", "-q", "-fd")
        else:
            git(repo, "worktree", "prune")
            git(repo, "worktree", "add", "-q", str(path), branch)
        return cls(repo, path, branch)

    def remove(self) -> None:
        """Delete the worktree directory. The branch (and all committed work) is kept."""
        git(self.repo, "worktree", "remove", "--force", str(self.path))

    def head(self) -> str:
        return git(self.path, "rev-parse", "HEAD").strip()

    def commit(self, message: str) -> str:
        git(self.path, "add", "-A")
        git(self.path, "commit", "-q", "--allow-empty", "--no-verify", "-m", message)
        return self.head()

    def diff(self, base: str, paths: list[str] | None = None) -> str:
        """Diff of the working tree (including untracked files) against ``base``."""
        git(self.path, "add", "-A", "--intent-to-add")
        text = git(self.path, "diff", base, "--", *(paths or []))
        if len(text) > MAX_DIFF_CHARS:
            text = text[:MAX_DIFF_CHARS] + "\n[diff truncated]\n"
        return text

    def changed_files(self, base: str, paths: list[str] | None = None) -> list[str]:
        """Tracked files changed since ``base`` plus untracked files, limited to ``paths``."""
        spec = paths or []
        changed = git(self.path, "diff", "--name-only", base, "--", *spec).split()
        untracked = git(
            self.path, "ls-files", "--others", "--exclude-standard", "--", *spec
        ).split()
        return sorted(set(changed) | set(untracked))

    def restore(self, base: str, files: list[str]) -> None:
        """Put ``files`` back to their state at ``base``; delete files that didn't exist there."""
        existed = set(git(self.path, "ls-tree", "-r", "--name-only", base).split())
        for f in files:
            if f in existed:
                git(self.path, "checkout", base, "--", f)
            else:
                git(self.path, "rm", "-q", "--cached", "--ignore-unmatch", "--", f)
                (self.path / f).unlink(missing_ok=True)


def _exclude_advpipe_dir(repo: Path) -> None:
    """Keep .advpipe/ out of the user's `git status` without editing tracked files."""
    exclude = Path(git(repo, "rev-parse", "--git-path", "info/exclude").strip())
    if not exclude.is_absolute():
        exclude = repo / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text() if exclude.exists() else ""
    if ".advpipe/" not in existing.splitlines():
        exclude.write_text(
            existing + ("" if existing.endswith("\n") or not existing else "\n") + ".advpipe/\n"
        )
