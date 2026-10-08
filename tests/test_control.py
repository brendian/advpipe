"""advpipe run --detach, cancel and clean. Detached runs use tests/advpipe_fake_child.py."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import git
from fakes import IMPL_OK, FakeAgentRunner, happy_scripts, verdict_pass, writes
from typer.testing import CliRunner

import advpipe.control as control
from advpipe.cli import app
from advpipe.config import Config
from advpipe.models import RunState, Status
from advpipe.orchestrator import Orchestrator
from advpipe.runlog import RunLog, pid_alive
from advpipe.runner import Role

cli = CliRunner()
FAKE_CHILD = Path(__file__).parent / "advpipe_fake_child.py"


def wait_for(condition: Callable[[], bool], timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting")
        time.sleep(0.05)


def test_pid_alive() -> None:
    assert pid_alive(os.getpid())
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert not pid_alive(proc.pid)  # reaped, so the pid is free


def events_text(runlog: RunLog) -> str:
    path = runlog.root / "events.jsonl"
    return path.read_text() if path.is_file() else ""


@pytest.fixture
def detachable(target_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """target_repo with a pytest gate; detached runs use the fake child, blocked at the coder
    until the returned release file exists."""
    (target_repo / "pipeline.toml").write_text(
        f'[gates]\ntest = ["{sys.executable}", "-m", "pytest", "-q", "-p", "no:cacheprovider"]\n'
        "types = []\nlint = []\nsecurity = []\n"
    )
    release = tmp_path / "release"
    monkeypatch.setattr(control, "child_command", lambda: [sys.executable, str(FAKE_CHILD)])
    monkeypatch.setenv("ADVPIPE_FAKE_BLOCK", "coder")
    monkeypatch.setenv("ADVPIPE_FAKE_RELEASE", str(release))
    return release


def start(repo: Path, *args: str) -> RunLog:
    started = time.monotonic()
    result = cli.invoke(app, ["run", *args, "--repo", str(repo), "--detach"])
    assert result.exit_code == 0, result.output
    assert time.monotonic() - started < 10  # returns at once; the run takes longer
    run_id = result.stdout.strip()
    assert "advpipe cancel " + run_id in result.stderr
    return RunLog.for_run(repo, run_id)


def test_detach_returns_while_run_continues(target_repo: Path, detachable: Path) -> None:
    runlog = start(target_repo, "add clamp")
    assert runlog.lock_holder() is not None  # the child holds the lock straight away
    wait_for(lambda: "coder working" in events_text(runlog))
    assert runlog.read_state().status is Status.RUNNING

    detachable.touch()  # let the coder finish
    wait_for(lambda: runlog.lock_holder() is None)
    state = runlog.read_state()
    assert state.status is Status.COMPLETE, state.notes
    console = (runlog.root / "console.log").read_text()
    assert "== SPEC" in console and "coder done in" in console


def test_detach_with_item_records_the_file(target_repo: Path, detachable: Path) -> None:
    detachable.touch()
    item = target_repo.parent / "s01.md"
    item.write_text("---\nname: s01-clamp\n---\n- add clamp\n")
    runlog = start(target_repo, "--item", str(item))
    wait_for(lambda: runlog.lock_holder() is None)
    state = runlog.read_state()
    assert state.status is Status.COMPLETE, state.notes
    assert state.branch == "advpipe/s01-clamp"
    assert state.work_item == "- add clamp"  # passed after "--", so not taken as an option
    assert state.work_item_file == str(item.resolve())


def test_detach_rejects_several_items(target_repo: Path) -> None:
    items = target_repo.parent / "items.txt"
    items.write_text("a\nb\n")
    result = cli.invoke(
        app, ["run", "--from-file", str(items), "--repo", str(target_repo), "--detach"]
    )
    assert result.exit_code != 0 and "single work item" in result.output


def code_onward() -> FakeAgentRunner:
    return FakeAgentRunner(
        {
            Role.CODER: [writes(IMPL_OK)],
            Role.CODE_CRITIC: [verdict_pass()],
            Role.STANDARDS_REVIEWER: [verdict_pass()],
            Role.SECURITY_REVIEWER: [verdict_pass()],
        }
    )


def test_cancel_leaves_a_resumable_run(target_repo: Path, detachable: Path) -> None:
    runlog = start(target_repo, "add clamp")
    wait_for(lambda: "coder working" in events_text(runlog))

    result = cli.invoke(app, ["cancel", runlog.root.name, "--repo", str(target_repo)])
    assert result.exit_code == 0, result.output
    assert "advpipe resume" in result.output
    wait_for(lambda: runlog.lock_holder() is None)

    state = runlog.read_state()
    assert state.status is Status.RUNNING
    assert "Interrupted during CODE" in state.notes
    assert set(state.commits) == {"spec", "tests"}
    last = json.loads(events_text(runlog).splitlines()[-1])
    assert last["final"] and last["status"] == "running"
    assert "interrupted; resume with" in (runlog.root / "console.log").read_text()

    again = cli.invoke(app, ["cancel", runlog.root.name, "--repo", str(target_repo)])
    assert again.exit_code == 2 and "not active" in again.stderr

    resumed = Orchestrator.resume(code_onward(), target_repo, runlog.root.name)
    assert asyncio.run(resumed.run()).status is Status.COMPLETE


def test_resume_detach_continues_in_the_background(target_repo: Path, detachable: Path) -> None:
    runlog = start(target_repo, "add clamp")
    wait_for(lambda: "coder working" in events_text(runlog))
    run_id = runlog.root.name
    assert cli.invoke(app, ["cancel", run_id, "--repo", str(target_repo)]).exit_code == 0
    wait_for(lambda: runlog.lock_holder() is None)

    detachable.touch()
    started = time.monotonic()
    result = cli.invoke(
        app, ["resume", run_id, "--repo", str(target_repo), "--detach", "--budget", "12.5"]
    )
    assert result.exit_code == 0, result.output
    assert time.monotonic() - started < 10 and result.stdout.strip() == run_id
    assert "resumed in the background" in result.stderr
    assert runlog.lock_holder() is not None  # the child's pid, straight away
    wait_for(lambda: runlog.lock_holder() is None)
    state = runlog.read_state()
    assert state.status is Status.COMPLETE, state.notes
    config = runlog.read_config()
    assert config is not None and config.limits.budget_usd_per_task == 12.5  # --budget passed on


def test_resume_detach_refuses_before_starting_anything(target_repo: Path) -> None:
    runlog = RunLog.for_run(target_repo, "r1")
    runlog.write_state(
        RunState(run_id="r1", repo=str(target_repo), work_item="x", status=Status.COMPLETE)
    )
    result = cli.invoke(app, ["resume", "r1", "--repo", str(target_repo), "--detach"])
    assert result.exit_code == 2 and "nothing to resume" in result.stderr
    assert not (runlog.root / "console.log").exists()

    # A live process holds the lock: refused, even when it's this one (only the detached child
    # itself, told so with the hidden --detached-child, accepts its own pid in run.lock).
    runlog.write_state(
        RunState(run_id="r1", repo=str(target_repo), work_item="x", status=Status.ERROR)
    )
    runlog.acquire_lock(os.getpid())
    result = cli.invoke(app, ["resume", "r1", "--repo", str(target_repo), "--detach"])
    assert result.exit_code == 2 and "still being driven" in result.stderr


def test_cancel_unknown_run(target_repo: Path) -> None:
    result = cli.invoke(app, ["cancel", "nope", "--repo", str(target_repo)])
    assert result.exit_code == 2 and "no run nope" in result.stderr


@pytest.mark.skipif(not Path("/proc/self/cmdline").exists(), reason="needs /proc")
def test_cancel_refuses_a_pid_that_isnt_advpipe(target_repo: Path) -> None:
    # A stale run.lock whose pid was reused by an unrelated process (e.g. after a reboot).
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        runlog = RunLog.for_run(target_repo, "r1")
        runlog.acquire_lock(other.pid)
        result = cli.invoke(app, ["cancel", "r1", "--repo", str(target_repo)])
        assert result.exit_code == 2 and "isn't an advpipe process" in result.stderr
        assert other.poll() is None  # not signalled
    finally:
        other.kill()
        other.wait()


# --------------------------------------------------------------------------- clean


async def dead_run(config: Config, repo: Path, run_id: str = "r1", *, commits: bool) -> RunState:
    """A needs_human run with no commits, or a budget_exceeded run with the spec committed."""
    if commits:
        runner = FakeAgentRunner(happy_scripts(), 2.0)  # 3rd call crosses the $5 budget
    else:
        runner = FakeAgentRunner({Role.SPEC_WRITER: ["wrote nothing"]})  # no task.md
    state = await Orchestrator(config, runner, repo, "add clamp", run_id, name=run_id).run()
    assert state.status is (Status.BUDGET_EXCEEDED if commits else Status.NEEDS_HUMAN)
    return state


def branches(repo: Path) -> list[str]:
    return git(repo, "branch", "--format=%(refname:short)").split()


def clean(repo: Path, *args: str) -> tuple[int, str]:
    result = cli.invoke(app, ["clean", *args, "--repo", str(repo)])
    return result.exit_code, result.output + result.stderr


async def test_clean_removes_worktree_and_branch(config: Config, target_repo: Path) -> None:
    state = await dead_run(config, target_repo, commits=False)
    assert Path(state.worktree).is_dir()

    code, out = clean(target_repo, "r1")
    assert code == 0, out
    assert "removed worktree" in out and "deleted branch advpipe/r1" in out
    assert not Path(state.worktree).exists()
    assert "advpipe/r1" not in branches(target_repo)
    kept = RunLog.for_run(target_repo, "r1")
    assert any(n.startswith("Cleaned up") for n in kept.read_state().notes)
    assert "Cleaned up" in (kept.root / "report.md").read_text()

    code, out = clean(target_repo, "r1", "--logs")  # second time: only the log is left
    assert code == 0, out
    assert not kept.root.exists()


async def test_clean_refuses_unmerged_commits_without_force(
    config: Config, target_repo: Path
) -> None:
    state = await dead_run(config, target_repo, commits=True)
    code, out = clean(target_repo, "r1")
    assert code == 2 and "1 commit(s) that no other branch has" in out
    assert "advpipe/r1" in branches(target_repo) and Path(state.worktree).is_dir()

    code, out = clean(target_repo, "r1", "--force", "--logs")
    assert code == 0, out
    assert "advpipe/r1" not in branches(target_repo)
    assert not RunLog.for_run(target_repo, "r1").root.exists()


async def test_clean_allows_commits_kept_on_another_branch(
    config: Config, target_repo: Path
) -> None:
    await dead_run(config, target_repo, commits=True)
    git(target_repo, "branch", "keep", "advpipe/r1")
    code, out = clean(target_repo, "r1")
    assert code == 0, out
    assert "keep" in branches(target_repo)


async def test_clean_refuses_complete_and_active_runs(config: Config, target_repo: Path) -> None:
    await Orchestrator(config, FakeAgentRunner(happy_scripts()), target_repo, "w", "ok").run()
    code, out = clean(target_repo, "ok", "--force")
    assert code == 2 and "is complete" in out

    await dead_run(config, target_repo, "busy", commits=False)
    RunLog.for_run(target_repo, "busy").acquire_lock(os.getpid())
    code, out = clean(target_repo, "busy", "--force")
    assert code == 2 and "advpipe cancel busy" in out
    assert "advpipe/busy" in branches(target_repo)

    assert clean(target_repo, "nope")[0] == 2


async def test_clean_only_touches_its_own_branch_and_worktree(
    config: Config, target_repo: Path, tmp_path: Path
) -> None:
    await dead_run(config, target_repo, commits=False)
    runlog = RunLog.for_run(target_repo, "r1")
    state = runlog.read_state()

    runlog.write_state(state.model_copy(update={"branch": "main"}))
    code, out = clean(target_repo, "r1", "--force")
    assert code == 2 and "isn't an advpipe/ branch" in out
    assert "main" in branches(target_repo)

    outside = tmp_path / "precious"
    outside.mkdir()
    runlog.write_state(state.model_copy(update={"worktree": str(outside)}))
    code, out = clean(target_repo, "r1", "--force")
    assert code == 2 and "isn't under .advpipe/worktrees" in out
    assert outside.is_dir()


@pytest.mark.parametrize("bad", ["..", "../r1", "r1/../r1", "/etc", "-x", ".hidden", "%2e%2e", ""])
def test_run_ids_cannot_escape_the_runs_dir(target_repo: Path, bad: str) -> None:
    sentinel = target_repo / ".advpipe" / "keep.txt"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("x")
    for command in (["clean", "--logs", "--force"], ["cancel"], ["status"], ["report"]):
        result = cli.invoke(app, [*command, "--repo", str(target_repo), "--", bad])
        assert result.exit_code != 0, (command, bad)
    assert sentinel.is_file()
    with pytest.raises(ValueError, match="invalid run id"):
        RunLog.for_run(target_repo, bad)
