"""Git worktree per run, plus diff and restore helpers."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

GIT_IDENTITY = ["-c", "user.name=advpipe", "-c", "user.email=advpipe@localhost"]
MAX_DIFF_CHARS = 60_000
BRANCH_PREFIX = "advpipe/"
MAX_SLUG_LEN = 40
# Filler words dropped from branch names: "add the X for the Y" -> "add-x-y".
STOP_WORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "as",
        "at",
        "by",
        "for",
        "from",
        "in",
        "into",
        "of",
        "on",
        "or",
        "so",
        "that",
        "the",
        "this",
        "to",
        "with",
    ]
)


class GitError(RuntimeError):
    pass


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *GIT_IDENTITY, *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def slugify(text: str, max_len: int = MAX_SLUG_LEN) -> str:
    """Readable branch-name part from free text: first line, lowercase words joined by '-'.

    Filler words are dropped (unless nothing else is left) and the result is cut at a word
    boundary to ``max_len`` characters. Returns "work" when nothing usable remains.
    """
    first = next((line for line in text.splitlines() if line.strip()), "")
    words = re.findall(r"[a-z0-9]+", first.lower())
    kept = [w for w in words if w not in STOP_WORDS] or words
    slug = ""
    for word in kept:
        candidate = f"{slug}-{word}" if slug else word
        if len(candidate) > max_len:
            break
        slug = candidate
    return slug or (kept[0][:max_len] if kept else "work")


def branch_exists(repo: Path, branch: str) -> bool:
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


def unique_branch(repo: Path, name: str) -> str:
    """``advpipe/<slug of name>``, with -2, -3, ... appended if that branch already exists."""
    base = BRANCH_PREFIX + slugify(name)
    branch, n = base, 1
    while branch_exists(repo, branch):
        n += 1
        branch = f"{base}-{n}"
    return branch


class Workspace:
    """A git worktree on its own ``advpipe/...`` branch; the user's working tree is untouched."""

    def __init__(self, repo: Path, path: Path, branch: str) -> None:
        self.repo = repo
        self.path = path
        self.branch = branch

    @classmethod
    def create(cls, repo: Path, run_id: str, name: str | None = None) -> Workspace:
        """New worktree off the repo's current HEAD, on branch ``advpipe/<slug of name>``.

        ``name`` defaults to the run id. The worktree directory is always named after the run id.
        """
        repo = Path(git(repo, "rev-parse", "--show-toplevel").strip())
        _exclude_advpipe_dir(repo)
        path = repo / ".advpipe" / "worktrees" / run_id
        branch = unique_branch(repo, name or run_id)
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
