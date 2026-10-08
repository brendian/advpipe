"""A failing test that the code critic says is itself wrong goes back to the test author once."""

from __future__ import annotations

from pathlib import Path

from conftest import git
from fakes import (
    CLAMP_WRONG,
    CORE_HEADER,
    IMPL_OK,
    TEST_CLAMP,
    TEST_CLAMP_DEFECT,
    FakeAgentRunner,
    defect_finding,
    finding,
    happy_scripts,
    verdict_fail,
    verdict_pass,
    writes,
)

from advpipe.config import Config
from advpipe.models import RunState, Status
from advpipe.orchestrator import Orchestrator
from advpipe.runner import Role


def defect_scripts() -> dict[Role, list[object]]:
    scripts: dict[Role, list[object]] = dict(happy_scripts())  # type: ignore[arg-type]
    scripts[Role.TEST_AUTHOR] = [
        writes({"tests/test_clamp.py": TEST_CLAMP_DEFECT}),
        writes({"tests/test_clamp.py": TEST_CLAMP}, "fixed the expected value"),
    ]
    return scripts


async def run(
    config: Config, repo: Path, scripts: dict[Role, list[object]]
) -> tuple[RunState, FakeAgentRunner]:
    runner = FakeAgentRunner(scripts)  # type: ignore[arg-type]
    state = await Orchestrator(config, runner, repo, "add clamp", run_id="d1", name="d1").run()
    return state, runner


def branch_file(repo: Path, path: str) -> str:
    return git(repo, "show", f"advpipe/d1:{path}")


async def test_defective_test_goes_back_to_test_author(config: Config, target_repo: Path) -> None:
    scripts = defect_scripts()
    scripts[Role.CODE_CRITIC] = [verdict_fail(defect_finding())]
    state, runner = await run(config, target_repo, scripts)

    assert state.status is Status.COMPLETE, state.notes
    assert state.rounds_used["code"] == 1
    assert len(runner.calls_for(Role.CODER)) == 1
    fix_call = runner.calls_for(Role.TEST_AUTHOR)[1]
    assert "Test fix pass" in fix_call.prompt and "expects 6" in fix_call.prompt
    assert branch_file(target_repo, "tests/test_clamp.py") == TEST_CLAMP
    log = git(target_repo, "log", "--format=%s", "main..advpipe/d1")
    assert "advpipe: test fix (stage-code round-1)" in log
    # The claim is recorded, never sent to the coder as something to fix.
    (recorded,) = [f for f in state.minor_findings if f.category == "test-defect"]
    assert recorded.id == "C1-F1" and recorded.severity == "minor"
    assert any("fixed defective tests" in n for n in state.notes)


async def test_test_fix_survives_later_coder_rounds(config: Config, target_repo: Path) -> None:
    scripts = defect_scripts()
    scripts[Role.CODER] = [writes(IMPL_OK), "{}"]
    scripts[Role.CODE_CRITIC] = [
        verdict_fail(defect_finding("F1"), finding("F2", line=3, criterion="AC3")),
        verdict_pass(),
    ]
    state, runner = await run(config, target_repo, scripts)

    assert state.status is Status.COMPLETE, state.notes
    assert state.rounds_used["code"] == 2
    # The guard's baseline moved to the test-fix commit, so the fix wasn't reverted.
    assert "coder modified test files" not in runner.calls_for(Role.CODER)[1].prompt
    assert branch_file(target_repo, "tests/test_clamp.py") == TEST_CLAMP


async def test_defect_claim_ignored_when_tests_pass(config: Config, target_repo: Path) -> None:
    scripts = happy_scripts()
    scripts[Role.CODE_CRITIC] = [verdict_fail(defect_finding())]
    state, runner = await run(config, target_repo, scripts)  # type: ignore[arg-type]

    assert state.status is Status.COMPLETE
    assert len(runner.calls_for(Role.TEST_AUTHOR)) == 1  # no fix pass: nothing is failing
    assert [f.category for f in state.minor_findings] == ["test-defect"]


async def test_only_one_test_fix_pass_per_stage(config: Config, target_repo: Path) -> None:
    scripts = defect_scripts()
    # The fix pass doesn't actually fix it, and the critic keeps reporting the defect.
    scripts[Role.TEST_AUTHOR][1] = writes({"tests/test_clamp.py": TEST_CLAMP_DEFECT}, "tried")
    scripts[Role.CODER] = [writes(IMPL_OK), "{}", "{}", "{}"]
    scripts[Role.CODE_CRITIC] = [verdict_fail(defect_finding()) for _ in range(3)]
    state, runner = await run(config, target_repo, scripts)

    assert len(runner.calls_for(Role.TEST_AUTHOR)) == 2
    assert state.status is Status.NEEDS_HUMAN  # the failing gate is still ground truth
    assert [f.id for f in state.open_findings] == ["G-test"]


async def test_fix_pass_cannot_touch_implementation(config: Config, target_repo: Path) -> None:
    scripts = defect_scripts()
    scripts[Role.TEST_AUTHOR][1] = writes(
        {
            "tests/test_clamp.py": TEST_CLAMP,
            "mathutils/core.py": CORE_HEADER + CLAMP_WRONG,
            "mathutils/extra.py": "x = 1\n",
        }
    )
    scripts[Role.CODE_CRITIC] = [verdict_fail(defect_finding())]
    state, _ = await run(config, target_repo, scripts)

    assert state.status is Status.COMPLETE, state.notes
    assert branch_file(target_repo, "mathutils/core.py") == IMPL_OK["mathutils/core.py"]
    files = git(target_repo, "ls-tree", "-r", "--name-only", "advpipe/d1").split()
    assert "mathutils/extra.py" not in files
    assert any("reverted: mathutils/core.py, mathutils/extra.py" in n for n in state.notes)
