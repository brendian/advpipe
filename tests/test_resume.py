from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from conftest import git
from fakes import (
    IMPL_OK,
    FakeAgentRunner,
    Step,
    happy_scripts,
    interrupt,
    verdict_pass,
    writes,
)

from advpipe.config import Config
from advpipe.models import Stage, Status
from advpipe.orchestrator import NotResumable, Orchestrator
from advpipe.runlog import RunLog
from advpipe.runner import Role


def code_onward() -> dict[Role, list[Step]]:
    """Scripts for a run resumed at CODE. Any spec/test call would fail the test."""
    return {
        Role.CODER: [writes(IMPL_OK)],
        Role.CODE_CRITIC: [verdict_pass()],
        Role.STANDARDS_REVIEWER: [verdict_pass()],
        Role.SECURITY_REVIEWER: [verdict_pass()],
    }


async def interrupted_at_code(config: Config, repo: Path) -> Orchestrator:
    scripts = happy_scripts()
    scripts[Role.CODE_CRITIC] = [interrupt]
    orch = Orchestrator(config, FakeAgentRunner(scripts), repo, "add clamp", run_id="r1", name="r1")
    with pytest.raises(asyncio.CancelledError):
        await orch.run()
    return orch


async def test_interrupt_leaves_resumable_state(config: Config, target_repo: Path) -> None:
    orch = await interrupted_at_code(config, target_repo)
    state = RunLog.for_run(target_repo, "r1").read_state()
    assert state.status is Status.RUNNING
    assert state.stage is Stage.CODE
    assert set(state.commits) == {"spec", "tests"}
    assert state.cost_usd == pytest.approx(0.04)  # spec, test author, test critic, coder
    assert "Interrupted during CODE" in state.notes
    assert orch.runlog.lock_holder() is None  # lock released


async def test_resume_continues_from_interrupted_stage(config: Config, target_repo: Path) -> None:
    orch = await interrupted_at_code(config, target_repo)
    # Partial work from the interrupted stage is discarded on resume.
    (Path(orch.state.worktree) / "stray.py").write_text("x = 1\n")

    runner = FakeAgentRunner(code_onward())
    resumed = Orchestrator.resume(runner, target_repo, "r1")
    assert resumed.config == config  # the run's saved config, not defaults
    state = await resumed.run()

    assert state.status is Status.COMPLETE, state.notes
    assert "Resumed at stage CODE" in state.notes
    assert state.cost_usd == pytest.approx(0.08)  # 4 before + 4 after
    assert {r.role for r in runner.calls} == set(code_onward())
    files = git(target_repo, "ls-tree", "-r", "--name-only", "advpipe/r1").split()
    assert "stray.py" not in files
    assert "tests/test_clamp.py" in files


async def test_resume_recreates_missing_worktree(config: Config, target_repo: Path) -> None:
    orch = await interrupted_at_code(config, target_repo)
    git(target_repo, "worktree", "remove", "--force", orch.state.worktree)

    runner = FakeAgentRunner(code_onward())
    state = await Orchestrator.resume(runner, target_repo, "r1").run()
    assert state.status is Status.COMPLETE, state.notes


async def test_resume_after_budget_exceeded_with_new_budget(
    config: Config, target_repo: Path
) -> None:
    orch = Orchestrator(
        config, FakeAgentRunner(happy_scripts(), 2.0), target_repo, "w", run_id="r1"
    )
    state = await orch.run()
    assert state.status is Status.BUDGET_EXCEEDED
    assert set(state.commits) == {"spec"}

    scripts = happy_scripts()
    del scripts[Role.SPEC_WRITER]
    runner = FakeAgentRunner(scripts, 0.01)
    state = await Orchestrator.resume(runner, target_repo, "r1", budget_usd=20.0).run()
    assert state.status is Status.COMPLETE, state.notes
    assert state.cost_usd == pytest.approx(6.06)
    assert state.cost_by_stage["spec"] == pytest.approx(2.0)
    saved = RunLog.for_run(target_repo, "r1").read_config()
    assert saved is not None and saved.limits.budget_usd_per_task == 20.0  # what it ran under


async def test_resume_refuses_finished_run(config: Config, target_repo: Path) -> None:
    await Orchestrator(config, FakeAgentRunner(happy_scripts()), target_repo, "w", "r1").run()
    with pytest.raises(NotResumable, match="complete"):
        Orchestrator.resume(FakeAgentRunner({}), target_repo, "r1")


async def test_resume_refuses_active_run(config: Config, target_repo: Path) -> None:
    await interrupted_at_code(config, target_repo)
    (RunLog.for_run(target_repo, "r1").root / "run.lock").write_text(str(os.getpid()))
    with pytest.raises(NotResumable, match="still being driven"):
        Orchestrator.resume(FakeAgentRunner({}), target_repo, "r1")


def test_resume_unknown_run(target_repo: Path) -> None:
    with pytest.raises(NotResumable, match="no run"):
        Orchestrator.resume(FakeAgentRunner({}), target_repo, "nope")
    assert not (target_repo / ".advpipe" / "runs" / "nope").exists()


async def test_stale_lock_is_ignored(config: Config, target_repo: Path) -> None:
    await interrupted_at_code(config, target_repo)
    (RunLog.for_run(target_repo, "r1").root / "run.lock").write_text("999999999")
    runner = FakeAgentRunner(code_onward())
    state = await Orchestrator.resume(runner, target_repo, "r1").run()
    assert state.status is Status.COMPLETE
