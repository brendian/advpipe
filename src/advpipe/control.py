"""Managing runs from outside the process driving them: detach, cancel, clean up."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from advpipe.models import Status
from advpipe.runlog import RunLog, render_report
from advpipe.workspace import BRANCH_PREFIX, branch_exists, git, unmerged_commits

CONSOLE_LOG = "console.log"


class ControlError(RuntimeError):
    """The requested action isn't safe or possible for this run."""


def child_command() -> list[str]:
    """How a detached run starts advpipe again (tests swap this to use a fake agent runner)."""
    return [sys.executable, "-m", "advpipe"]


def start_detached(runlog: RunLog, args: list[str]) -> int:
    """Start ``advpipe <args>`` as a detached process logging to console.log. Returns its pid.

    The child gets its own session, so closing the terminal or the UI doesn't stop it. Its pid
    goes into run.lock straight away, so `advpipe cancel` works before the child has started
    up. (The child rewrites the same pid when it takes the lock itself.)
    """
    runlog.root.mkdir(parents=True, exist_ok=True)
    flags = 0
    if sys.platform == "win32":
        # No sessions on Windows: a new process group with its own hidden console instead, so
        # Ctrl+C or closing the UI's console doesn't reach it.
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    with (runlog.root / CONSOLE_LOG).open("ab") as log:
        proc = subprocess.Popen(
            [*child_command(), *args],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=sys.platform != "win32",
            creationflags=flags,
        )
    runlog.acquire_lock(proc.pid)
    return proc.pid


def _looks_like_advpipe(pid: int) -> bool:
    """Guard against a stale run.lock whose pid now belongs to an unrelated process (Linux)."""
    cmdline = Path(f"/proc/{pid}/cmdline")
    try:
        return b"advpipe" in cmdline.read_bytes()
    except FileNotFoundError:
        return not Path("/proc/self").exists()  # no /proc (e.g. macOS): can't check
    except OSError:
        return False


def cancel_run(repo: Path, run_id: str) -> int:
    """Send SIGINT to the process driving a run. Returns its pid.

    The run stops at its next await (an agent call or a gate), records "Interrupted during
    <stage>", keeps status `running` and releases its lock, so it can be resumed.
    """
    runlog = RunLog.for_run(repo, run_id)
    if not runlog.root.is_dir():
        raise ControlError(f"no run {run_id}")
    pid = runlog.lock_holder()
    if pid is None:
        raise ControlError(f"run {run_id} is not active; nothing to cancel")
    if not _looks_like_advpipe(pid):
        raise ControlError(f"run.lock names pid {pid}, which isn't an advpipe process")
    os.kill(pid, signal.SIGINT)
    return pid


def clean_run(repo: Path, run_id: str, *, force: bool = False, logs: bool = False) -> list[str]:
    """Remove a dead run's worktree and branch (and its log directory if ``logs``).

    Refuses complete runs (their branch is the result) and active runs. Refuses a branch with
    commits no other branch has unless ``force``. Every check runs before anything is deleted.
    Returns what was done, as lines for the user.
    """
    runlog = RunLog.for_run(repo, run_id)
    if not (runlog.root / "run.json").is_file():
        raise ControlError(f"no run {run_id}")
    state = runlog.read_state()
    if state.status is Status.COMPLETE:
        raise ControlError(
            f"run {run_id} is complete: branch {state.branch} is its result. "
            "Delete it with git once you're done with it."
        )
    holder = runlog.lock_holder()
    if holder is not None:
        raise ControlError(
            f"run {run_id} is active (pid {holder}); stop it first: advpipe cancel {run_id}"
        )

    top = Path(git(repo, "rev-parse", "--show-toplevel").strip())
    worktree = Path(state.worktree) if state.worktree else None
    if worktree is not None and not worktree.resolve().is_relative_to(
        (top / ".advpipe" / "worktrees").resolve()
    ):
        raise ControlError(f"worktree {worktree} isn't under .advpipe/worktrees; not touching it")
    branch = state.branch if state.branch and branch_exists(top, state.branch) else ""
    if branch and not branch.startswith(BRANCH_PREFIX):
        raise ControlError(f"branch {branch} isn't an {BRANCH_PREFIX} branch; not touching it")
    if branch and not force:
        count = unmerged_commits(top, branch)
        if count:
            since = f"{state.base_commit[:12]}.." if state.base_commit else ""
            raise ControlError(
                f"branch {branch} has {count} commit(s) that no other branch has. "
                f"Look at them with: git log --oneline {since}{branch}  "
                "Pass --force to delete them anyway."
            )

    done = []
    if worktree is not None and worktree.exists():
        git(top, "worktree", "remove", "--force", str(worktree))
        done.append(f"removed worktree {worktree}")
    git(top, "worktree", "prune")
    if branch:
        git(top, "branch", "-D", branch)
        done.append(f"deleted branch {branch}")
    if logs:
        shutil.rmtree(runlog.root)
        done.append(f"deleted run log {runlog.root}")
    else:
        state.worktree_removed = True
        state.notes.append("Cleaned up: " + ("; ".join(done) or "nothing left to remove"))
        runlog.write_state(state)
        runlog.write_text("report.md", render_report(state))
    return done or ["nothing to remove"]
