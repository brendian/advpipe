"""Pipeline levels, L1 (docs/LEVELS_PLAN.md): the Tests level. No critics in the tests and
code stages, one review by the code critic at the end, and no arbiter."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from conftest import git
from fakes import (
    IMPL_OK,
    IMPL_WRONG,
    TASK_MD,
    TASK_MD_BLOCKING,
    TEST_CLAMP,
    TRIVIAL_TEST,
    FakeAgentRunner,
    Step,
    defect_finding,
    finding,
    interrupt,
    verdict_fail,
    verdict_pass,
    writes,
)

from advpipe.answers import record_answers
from advpipe.config import Config, Limits, Pipeline
from advpipe.levels import Level, max_agent_calls, roles_for
from advpipe.models import RunState, Stage, Status
from advpipe.orchestrator import Orchestrator
from advpipe.runlog import RunLog
from advpipe.runner import Role
from advpipe.stages import ARBITER_NOTE, NO_ARBITER_NOTE

TEST_FILE = "tests/test_clamp.py"


def at_tests(config: Config, **limits: int) -> Config:
    updates: dict[str, object] = {"pipeline": Pipeline(level=Level.TESTS)}
    if limits:
        updates["limits"] = config.limits.model_copy(update=limits)
    return config.model_copy(update=updates)


def scripts() -> dict[Role, list[Step]]:
    """Every stage passes first time at the Tests level: four agent calls."""
    return {
        Role.SPEC_WRITER: [writes({"task.md": TASK_MD}, "wrote task.md")],
        Role.TEST_AUTHOR: [writes({TEST_FILE: TEST_CLAMP}, "AUTHOR-REASONING tests")],
        Role.CODER: [writes(IMPL_OK, "AUTHOR-REASONING code")],
        Role.CODE_CRITIC: [verdict_pass()],
    }


async def run(
    config: Config, repo: Path, runner: FakeAgentRunner, run_id: str = "r1"
) -> tuple[RunState, Path]:
    orch = Orchestrator(at_tests(config), runner, repo, "add clamp", run_id, name=run_id)
    state = await orch.run()
    return state, orch.runlog.root


def assert_only_level_roles(runner: FakeAgentRunner) -> None:
    allowed = set(roles_for(Level.TESTS))
    assert {c.role for c in runner.calls} <= allowed
    assert not runner.calls_for(Role.TEST_CRITIC) and not runner.calls_for(Role.ARBITER)


# --------------------------------------------------------------------------- happy path


async def test_tests_level_happy_path(config: Config, target_repo: Path) -> None:
    runner = FakeAgentRunner(scripts())
    state, logdir = await run(config, target_repo, runner)

    assert state.status is Status.COMPLETE, (state.notes, state.open_findings)
    assert state.level is Level.TESTS and state.security_scan is False
    assert len(runner.calls) == max_agent_calls(Level.TESTS, Limits())[0] == 4
    assert [c.role for c in runner.calls] == [
        Role.SPEC_WRITER,
        Role.TEST_AUTHOR,
        Role.CODER,
        Role.CODE_CRITIC,
    ]
    assert_only_level_roles(runner)
    assert list(state.commits) == ["spec", "tests", "code", "review"]
    assert state.rulings == [] and state.rounds_used == {"tests": 1, "code": 1, "review": 1}
    # No critic in the tests and code stages: their rounds have no critic.json.
    for stage in ("stage-tests", "stage-code"):
        rdir = logdir / stage / "round-1"
        assert (rdir / "author.txt").is_file() and (rdir / "diff.patch").is_file()
        assert (rdir / "gates.json").is_file() and not (rdir / "critic.json").exists()
    assert (logdir / "review" / "single.json").read_text() == verdict_pass()
    assert not (logdir / "review" / "standards.json").exists()
    assert not (logdir / "arbiter.json").exists()
    # The review fixed nothing, so there's no review commit.
    log = git(target_repo, "log", "--format=%s", "main..advpipe/r1").splitlines()
    assert log == ["advpipe: implementation", "advpipe: tests", "advpipe: spec (task.md)"]
    report = (logdir / "report.md").read_text()
    assert "- **Level:** tests (3 of 5): 4 agents · security scan off" in report
    assert "  - No critics in the test and code steps: one review at the end" in report


async def test_single_review_gets_artifacts_not_reasoning(
    config: Config, target_repo: Path
) -> None:
    runner = FakeAgentRunner(scripts())
    state, logdir = await run(config, target_repo, runner)
    assert state.status is Status.COMPLETE

    (review,) = runner.calls_for(Role.CODE_CRITIC)
    prompt = review.prompt
    assert f"# task.md\n\n{TASK_MD}" in prompt
    assert "This is a single review pass" in prompt
    # The whole change since the base commit: tests and implementation, never task.md.
    diff = (logdir / "review" / "diff.patch").read_text()
    assert f"+++ b/{TEST_FILE}" in diff and "+++ b/mathutils/core.py" in diff
    assert "task.md" not in diff
    assert diff.strip() in prompt
    assert f"# Test files changed in this run (part of the diff below)\n\n- {TEST_FILE}" in prompt
    assert "# Gate output" in prompt and "test: PASS" in prompt
    assert "AUTHOR-REASONING" not in prompt and "wrote task.md" not in prompt
    # The ordinary code critic's prompt (Full) doesn't say "single".
    full = FakeAgentRunner(
        {
            **scripts(),
            Role.TEST_CRITIC: [verdict_pass()],
            Role.CODE_CRITIC: [verdict_pass()],
            Role.STANDARDS_REVIEWER: [verdict_pass()],
            Role.SECURITY_REVIEWER: [verdict_pass()],
        }
    )
    orch = Orchestrator(config, full, target_repo, "add clamp", "f1", name="f1")
    assert (await orch.run()).status is Status.COMPLETE
    assert "single review pass" not in full.calls_for(Role.CODE_CRITIC)[0].prompt


# --------------------------------------------------------------------------- no critics


async def test_tests_stage_without_critic_loops_on_the_red_gate(
    config: Config, target_repo: Path
) -> None:
    """Tests that pass before any implementation fail the red gate; that goes back to the
    test author, with no critic call."""
    s = scripts()
    s[Role.TEST_AUTHOR] = [writes({TEST_FILE: TRIVIAL_TEST}), writes({TEST_FILE: TEST_CLAMP})]
    runner = FakeAgentRunner(s)
    state, logdir = await run(config, target_repo, runner)

    assert state.status is Status.COMPLETE, state.notes
    assert state.rounds_used["tests"] == 2
    second = runner.calls_for(Role.TEST_AUTHOR)[1].prompt
    assert "T1-G-tests-green" in second and "pass before implementation" in second
    assert len(runner.calls_for(Role.CODE_CRITIC)) == 1  # only the review
    assert not (logdir / "stage-tests" / "round-2" / "critic.json").exists()
    assert_only_level_roles(runner)


async def test_code_stage_without_critic_loops_on_gates_and_guard(
    config: Config, target_repo: Path
) -> None:
    """The coder's wrong first attempt (and its test edit, which the guard reverts) go back
    to it; the second attempt passes the gates and the stage, with no critic call."""
    s = scripts()
    tampered = {**IMPL_WRONG, TEST_FILE: "def test_nothing() -> None:\n    pass\n"}
    s[Role.CODER] = [writes(tampered), writes(IMPL_OK)]
    runner = FakeAgentRunner(s)
    state, _ = await run(config, target_repo, runner)

    assert state.status is Status.COMPLETE, state.notes
    assert state.rounds_used["code"] == 2
    second = runner.calls_for(Role.CODER)[1].prompt
    assert "C1-T-guard" in second and "C1-G-test" in second
    assert git(target_repo, "show", f"advpipe/r1:{TEST_FILE}") == TEST_CLAMP
    assert len(runner.calls_for(Role.CODE_CRITIC)) == 1


# --------------------------------------------------------------------------- no arbiter


async def test_settle_without_arbiter(config: Config, target_repo: Path) -> None:
    """At the code stage's cap, every open finding is ruled fix (no arbiter call), with the
    level as the reason, and the coder gets one final pass."""
    s = scripts()
    s[Role.CODER] = [writes(IMPL_WRONG), "r2", "r3", writes(IMPL_OK)]
    runner = FakeAgentRunner(s)
    state, logdir = await run(config, target_repo, runner)

    assert state.status is Status.COMPLETE, state.notes
    assert_only_level_roles(runner)
    assert state.rounds_used["code"] == 3
    assert [(r.finding_id, r.decision, r.reason) for r in state.rulings] == [
        ("C3-G-test", "fix", "no arbiter at level tests")
    ]
    final = runner.calls_for(Role.CODER)[3].prompt
    assert NO_ARBITER_NOTE in final and ARBITER_NOTE not in final
    assert (logdir / "final-pass-code" / "author.txt").is_file()
    assert not (logdir / "arbiter.json").exists()
    # Charged to the code stage: there's no arbiter step at this level.
    assert "arbiter" not in state.cost_by_stage
    assert state.calls_by_stage["stage-code"] == 4
    report = (logdir / "report.md").read_text()
    assert "- C3-G-test: **fix**: no arbiter at level tests" in report
    events = (logdir / "events.jsonl").read_text().splitlines()
    stages = [json.loads(e)["message"] for e in events if json.loads(e)["kind"] == "stage"]
    assert "== ARBITER" not in stages
    assert "round cap reached: 1 open finding(s) are still open" in stages


async def test_settle_without_arbiter_still_red(config: Config, target_repo: Path) -> None:
    """Red gates after the final pass end the run needs_human (decision points come in L3)."""
    s = scripts()
    s[Role.CODER] = [writes(IMPL_WRONG), "r2", "r3", "still wrong"]
    del s[Role.CODE_CRITIC]
    runner = FakeAgentRunner(s)
    state, _ = await run(config, target_repo, runner)

    assert state.status is Status.NEEDS_HUMAN
    assert state.stage is Stage.CODE
    assert [f.criterion for f in state.open_findings] == ["gate:test"]
    assert len(runner.calls) == 1 + 1 + 4
    assert_only_level_roles(runner)


# --------------------------------------------------------------------------- single review


async def test_single_review_blocking_gets_one_coder_pass(
    config: Config, target_repo: Path
) -> None:
    s = scripts()
    s[Role.CODE_CRITIC] = [verdict_fail(finding("F1", claim="no docstring on clamp"))]
    s[Role.CODER].append(writes({"mathutils/extra.py": '"""Extra."""\n'}, "fixed"))
    runner = FakeAgentRunner(s)
    state, logdir = await run(config, target_repo, runner)

    assert state.status is Status.COMPLETE, state.notes
    assert len(runner.calls_for(Role.CODE_CRITIC)) == 1  # no second review round
    fix = runner.calls_for(Role.CODER)[1].prompt
    assert "REV-F1" in fix and "no docstring on clamp" in fix and NO_ARBITER_NOTE in fix
    assert [(r.finding_id, r.reason) for r in state.rulings] == [
        ("REV-F1", "no arbiter at level tests")
    ]
    assert state.calls_by_stage["review"] == 2
    assert (logdir / "final-pass-code" / "gates.json").is_file()
    log = git(target_repo, "log", "--format=%s", "main..advpipe/r1").splitlines()
    assert log[0] == "advpipe: review fixes"
    assert_only_level_roles(runner)


async def test_single_review_minor_findings_are_reported(config: Config, target_repo: Path) -> None:
    s = scripts()
    s[Role.CODE_CRITIC] = [verdict_pass(finding("F1", severity="minor", claim="tiny nit"))]
    state, _ = await run(config, target_repo, FakeAgentRunner(s))
    assert state.status is Status.COMPLETE
    assert [f.id for f in state.minor_findings] == ["REV-F1"]
    assert state.rulings == []


async def test_single_review_test_defect_takes_the_ordinary_path(
    config: Config, target_repo: Path
) -> None:
    """A test-defect finding in the single review goes to the coder like any other blocking
    finding: no test-fix pass, and the test-file guard still protects the tests."""
    s = scripts()
    s[Role.CODE_CRITIC] = [verdict_fail(defect_finding())]
    s[Role.CODER].append(writes({TEST_FILE: TRIVIAL_TEST}, "edited the test"))
    runner = FakeAgentRunner(s)
    state, _ = await run(config, target_repo, runner)

    assert len(runner.calls_for(Role.TEST_AUTHOR)) == 1  # no test-fix pass
    assert "REV-F1" in runner.calls_for(Role.CODER)[1].prompt
    assert state.minor_findings == []
    # The guard reverted the coder's test edit: a blocking finding, so the run stops.
    assert state.status is Status.NEEDS_HUMAN
    assert [f.criterion for f in state.open_findings] == ["guard:tests"]
    assert (target_repo / ".advpipe" / "worktrees" / "r1" / TEST_FILE).read_text() == TEST_CLAMP


async def test_single_review_unparseable_is_blocking(config: Config, target_repo: Path) -> None:
    s = scripts()
    s[Role.CODE_CRITIC] = ["not json", "still not json"]
    s[Role.CODER].append("nothing to change")
    runner = FakeAgentRunner(s)
    state, logdir = await run(config, target_repo, runner)
    assert state.status is Status.COMPLETE, state.notes
    assert len(runner.calls_for(Role.CODE_CRITIC)) == 2  # one JSON retry
    assert (logdir / "review" / "single.json").read_text() == "still not json"
    assert [r.finding_id for r in state.rulings] == ["REV-P1"]


# --------------------------------------------------------------------------- resume, answers


async def test_resume_at_review_reruns_the_gates(config: Config, target_repo: Path) -> None:
    s = scripts()
    s[Role.CODE_CRITIC] = [interrupt]
    orch = Orchestrator(
        at_tests(config), FakeAgentRunner(s), target_repo, "add clamp", "r1", name="r1"
    )
    with pytest.raises(asyncio.CancelledError):
        await orch.run()

    runner = FakeAgentRunner({Role.CODE_CRITIC: [verdict_pass()]})
    state = await Orchestrator.resume(runner, target_repo, "r1").run()
    assert state.status is Status.COMPLETE, state.notes
    assert state.level is Level.TESTS
    assert "test: PASS" in runner.calls_for(Role.CODE_CRITIC)[0].prompt


async def test_blocking_questions_at_the_tests_level(config: Config, target_repo: Path) -> None:
    """Questions stop the run and are answered exactly as at Full."""
    s = scripts()
    s[Role.SPEC_WRITER] = [writes({"task.md": TASK_MD_BLOCKING}, "one blocking question")]
    state, logdir = await run(config, target_repo, FakeAgentRunner(s))
    assert state.status is Status.NEEDS_HUMAN and state.stage is Stage.SPEC
    assert [q.id for q in state.questions] == ["Q1"]
    assert "advpipe answer r1" in (logdir / "report.md").read_text()

    record_answers(target_repo, "r1", {"Q1": "Raise ValueError."})
    runner = FakeAgentRunner(scripts())
    resumed = Orchestrator.resume(runner, target_repo, "r1")
    state = await resumed.run()
    assert state.status is Status.COMPLETE, state.notes
    assert state.level is Level.TESTS and state.spec_passes == 2
    assert "# Your previous task.md" in runner.calls_for(Role.SPEC_WRITER)[0].prompt
    assert "**Answer:** Raise ValueError." in runner.calls_for(Role.CODE_CRITIC)[0].prompt
    assert "**Answer:** Raise ValueError." in git(target_repo, "show", "advpipe/r1:task.md")
    assert_only_level_roles(runner)
    assert RunLog.for_run(target_repo, "r1").read_state().level is Level.TESTS


# --------------------------------------------------------------------------- bounds


def worst_case_scripts() -> dict[Role, list[Step]]:
    """Every stage reaches its cap and needs its final pass; the review's reply is
    unparseable twice. The run still ends complete."""
    return {
        Role.SPEC_WRITER: [writes({"task.md": TASK_MD})],
        # round 1 passes before any code (red gate), round 2 too, the final pass fixes it
        Role.TEST_AUTHOR: [
            writes({TEST_FILE: TRIVIAL_TEST}),
            "r2",
            writes({TEST_FILE: TEST_CLAMP}),
        ],
        Role.CODER: [writes(IMPL_WRONG), "r2", "r3", writes(IMPL_OK), "review fix"],
        Role.CODE_CRITIC: ["not json"] * 2,
    }


async def test_worst_case_agent_calls(config: Config, target_repo: Path) -> None:
    runner = FakeAgentRunner(worst_case_scripts())
    state, _ = await run(config, target_repo, runner)
    assert state.status is Status.COMPLETE, (state.notes, state.open_findings)
    assert len(runner.calls) == max_agent_calls(Level.TESTS, Limits())[1] == 11
    assert all(not steps for steps in runner.scripts.values())  # every scripted call happened
    assert_only_level_roles(runner)


# --------------------------------------------------------------------------- web UI (smoke)


async def test_run_pages_render(config: Config, target_repo: Path) -> None:
    """The run page isn't level-aware until L5, but a Tests-level run must render: a
    complete one, and one stopped after a final pass with no arbiter."""
    pytest.importorskip("fastapi")
    from test_ui import logged_in

    await run(config, target_repo, FakeAgentRunner(scripts()), "t-ok")
    s = scripts()
    s[Role.CODER] = [writes(IMPL_WRONG), "r2", "r3", "still wrong"]
    del s[Role.CODE_CRITIC]
    await run(config, target_repo, FakeAgentRunner(s), "t-red")

    client = logged_in(target_repo)
    ok = client.get("/runs/t-ok")
    assert ok.status_code == 200
    red = client.get("/runs/t-red")
    assert red.status_code == 200
    assert "no arbiter at level tests" in red.text
