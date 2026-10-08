from __future__ import annotations

import json
from pathlib import Path

from conftest import git
from fakes import (
    IMPL_OK,
    IMPL_WRONG,
    TASK_MD_BLOCKING,
    TEST_CLAMP,
    TRIVIAL_TEST,
    FakeAgentRunner,
    author_response,
    finding,
    happy_scripts,
    rulings,
    verdict_fail,
    verdict_pass,
    writes,
)

from advpipe.config import Config
from advpipe.models import RunState, Stage, Status
from advpipe.orchestrator import Orchestrator
from advpipe.runner import Role


async def run(config: Config, repo: Path, runner: FakeAgentRunner) -> tuple[RunState, Path]:
    orch = Orchestrator(
        config, runner, repo, "add a clamp(x, lo, hi) function", run_id="t1", name="t1"
    )
    state = await orch.run()
    return state, orch.runlog.root


def branch_file(repo: Path, path: str) -> str:
    return git(repo, "show", f"advpipe/t1:{path}")


async def test_pass_on_first_round(config: Config, target_repo: Path) -> None:
    runner = FakeAgentRunner(happy_scripts())
    state, logdir = await run(config, target_repo, runner)

    assert state.status is Status.COMPLETE, state.notes
    assert state.stage is Stage.DONE
    assert state.rounds_used == {"tests": 1, "code": 1, "review": 1}
    assert not runner.calls_for(Role.ARBITER)
    assert "def clamp" in branch_file(target_repo, "mathutils/core.py")
    # The user's working tree is untouched.
    assert "clamp" not in (target_repo / "mathutils" / "core.py").read_text()
    assert git(target_repo, "status", "--porcelain") == ""
    # One commit per stage; no empty "final" commit when nothing changed after review.
    log = git(target_repo, "log", "--format=%s", "main..advpipe/t1").splitlines()
    assert log == ["advpipe: implementation", "advpipe: tests", "advpipe: spec (task.md)"]
    # Run log and report.
    assert json.loads((logdir / "run.json").read_text())["status"] == "complete"
    report = (logdir / "report.md").read_text()
    assert "**Status:** complete" in report
    assert "Security scanner not run" in report
    for rel in ("task.md", "stage-tests/round-1/critic.json", "stage-code/round-1/gates.json"):
        assert (logdir / rel).is_file(), rel


async def test_critics_get_artifacts_not_reasoning(config: Config, target_repo: Path) -> None:
    runner = FakeAgentRunner(happy_scripts())
    await run(config, target_repo, runner)
    critics = [Role.TEST_CRITIC, Role.CODE_CRITIC, Role.STANDARDS_REVIEWER, Role.SECURITY_REVIEWER]
    for role in critics:
        (call,) = runner.calls_for(role)
        assert "AUTHOR-REASONING" not in call.prompt
        assert "# task.md" in call.prompt
        assert call.tools == ["Read", "Glob", "Grep"]
        assert "task.md b/task.md" not in call.prompt  # the contract is not part of the diff
    # The code critic knows which tests the previous stage wrote.
    (code_critic,) = runner.calls_for(Role.CODE_CRITIC)
    assert "- tests/test_clamp.py" in code_critic.prompt
    # The test author sees task.md, not an implementation.
    (test_call,) = runner.calls_for(Role.TEST_AUTHOR)
    assert "def clamp" not in test_call.prompt


async def test_fail_then_pass(config: Config, target_repo: Path) -> None:
    scripts = happy_scripts()
    scripts[Role.CODER] = [writes(IMPL_OK), author_response(**{"C1-F1": ("fixed", "added guard")})]
    scripts[Role.CODE_CRITIC] = [verdict_fail(finding("F1")), verdict_pass()]
    runner = FakeAgentRunner(scripts)
    state, _ = await run(config, target_repo, runner)

    assert state.status is Status.COMPLETE
    assert state.rounds_used["code"] == 2
    second = runner.calls_for(Role.CODER)[1]
    assert "C1-F1" in second.prompt
    assert not runner.calls_for(Role.ARBITER)


async def test_cap_hit_goes_to_arbiter_which_dismisses(config: Config, target_repo: Path) -> None:
    scripts = happy_scripts()
    scripts[Role.CODER] = [writes(IMPL_OK), "{}", "{}"]
    scripts[Role.CODE_CRITIC] = [
        verdict_fail(finding("F1", line=1)),
        verdict_fail(finding("F1", line=2, criterion="AC1")),
        verdict_fail(finding("F1", line=3, criterion="AC3")),
    ]
    scripts[Role.ARBITER] = [rulings(**{"C3-F1": "dismiss"})]
    runner = FakeAgentRunner(scripts)
    state, logdir = await run(config, target_repo, runner)

    assert state.status is Status.COMPLETE
    assert state.rounds_used["code"] == 3
    assert len(runner.calls_for(Role.ARBITER)) == 1
    assert [r.decision for r in state.rulings] == ["dismiss"]
    assert len(runner.calls_for(Role.CODER)) == 3  # no final pass after a dismissal
    assert (logdir / "arbiter.json").is_file()


async def test_arbiter_fix_then_success(config: Config, target_repo: Path) -> None:
    scripts = happy_scripts()
    scripts[Role.CODER] = [writes(IMPL_OK), "{}", "{}", writes(IMPL_OK, "fixed")]
    scripts[Role.CODE_CRITIC] = [verdict_fail(finding("F1", line=n)) for n in (1, 2, 3)]
    scripts[Role.ARBITER] = [rulings(**{"C3-F1": "fix"})]
    runner = FakeAgentRunner(scripts)
    state, _ = await run(config, target_repo, runner)

    assert state.status is Status.COMPLETE
    coder_calls = runner.calls_for(Role.CODER)
    assert len(coder_calls) == 4
    assert "Final pass" in coder_calls[3].prompt
    assert len(runner.calls_for(Role.CODE_CRITIC)) == 3  # no critic round after the final pass


async def test_arbiter_fix_still_failing_needs_human(config: Config, target_repo: Path) -> None:
    scripts = happy_scripts()
    scripts[Role.CODER] = [writes(IMPL_OK), "{}", "{}", writes(IMPL_WRONG, "broke it")]
    scripts[Role.CODE_CRITIC] = [verdict_fail(finding("F1", line=n)) for n in (1, 2, 3)]
    scripts[Role.ARBITER] = [rulings(**{"C3-F1": "fix"})]
    state, _ = await run(config, target_repo, FakeAgentRunner(scripts))

    assert state.status is Status.NEEDS_HUMAN
    assert [f.id for f in state.open_findings] == ["G-test"]


async def test_disputed_finding_reflagged_goes_to_arbiter(
    config: Config, target_repo: Path
) -> None:
    scripts = happy_scripts()
    scripts[Role.CODER] = [
        writes(IMPL_OK),
        author_response(**{"C1-F1": ("disputed", "AC3 allows this")}),
    ]
    scripts[Role.CODE_CRITIC] = [
        verdict_fail(finding("F1", line=10)),
        verdict_fail(finding("F7", line=12)),  # same file + criterion: the same finding again
    ]
    scripts[Role.ARBITER] = [rulings(**{"C1-F1": "dismiss"})]
    runner = FakeAgentRunner(scripts)
    state, _ = await run(config, target_repo, runner)

    assert state.status is Status.COMPLETE
    assert state.rounds_used["code"] == 2  # not re-debated in a third round
    (arb,) = runner.calls_for(Role.ARBITER)
    assert "AC3 allows this" in arb.prompt


async def test_dispute_accepted_when_critic_drops_it(config: Config, target_repo: Path) -> None:
    scripts = happy_scripts()
    scripts[Role.CODER] = [writes(IMPL_OK), author_response(**{"C1-F1": ("disputed", "no")})]
    scripts[Role.CODE_CRITIC] = [verdict_fail(finding("F1")), verdict_pass()]
    runner = FakeAgentRunner(scripts)
    state, _ = await run(config, target_repo, runner)
    assert state.status is Status.COMPLETE
    assert not runner.calls_for(Role.ARBITER)


async def test_malformed_json_is_retried_once(config: Config, target_repo: Path) -> None:
    scripts = happy_scripts()
    scripts[Role.TEST_CRITIC] = ["I think it's fine!", verdict_pass()]
    runner = FakeAgentRunner(scripts)
    state, _ = await run(config, target_repo, runner)

    assert state.status is Status.COMPLETE
    retry = runner.calls_for(Role.TEST_CRITIC)[1]
    assert "could not be parsed" in retry.prompt


async def test_malformed_json_twice_is_blocking(config: Config, target_repo: Path) -> None:
    scripts = happy_scripts()
    scripts[Role.TEST_AUTHOR].append("{}")
    scripts[Role.TEST_CRITIC] = ["nope", "still nope", verdict_pass()]
    runner = FakeAgentRunner(scripts)
    state, _ = await run(config, target_repo, runner)

    assert state.status is Status.COMPLETE
    second_author = runner.calls_for(Role.TEST_AUTHOR)[1]
    assert "critic output unparseable" in second_author.prompt


async def test_coder_editing_tests_is_reverted_and_flagged(
    config: Config, target_repo: Path
) -> None:
    scripts = happy_scripts()
    gutted = "def test_nothing() -> None:\n    pass\n"
    scripts[Role.CODER] = [
        writes({**IMPL_OK, "tests/test_clamp.py": gutted, "tests/test_extra.py": gutted}),
        "{}",
    ]
    scripts[Role.CODE_CRITIC] = [verdict_pass(), verdict_pass()]
    runner = FakeAgentRunner(scripts)
    state, _ = await run(config, target_repo, runner)

    assert state.status is Status.COMPLETE
    assert branch_file(target_repo, "tests/test_clamp.py") == TEST_CLAMP
    assert "test_extra.py" not in git(target_repo, "ls-tree", "-r", "--name-only", "advpipe/t1")
    second = runner.calls_for(Role.CODER)[1]
    assert "coder modified test files" in second.prompt


async def test_failed_gate_is_blocking_even_if_critic_passes(
    config: Config, target_repo: Path
) -> None:
    scripts = happy_scripts()
    scripts[Role.CODER] = [writes(IMPL_WRONG), writes(IMPL_OK, "{}")]
    scripts[Role.CODE_CRITIC] = [verdict_pass(), verdict_pass()]
    runner = FakeAgentRunner(scripts)
    state, _ = await run(config, target_repo, runner)

    assert state.status is Status.COMPLETE
    assert state.rounds_used["code"] == 2
    second = runner.calls_for(Role.CODER)[1]
    assert "C1-G-test" in second.prompt
    assert "failed" in second.prompt


async def test_budget_exceeded_aborts(config: Config, target_repo: Path) -> None:
    runner = FakeAgentRunner(happy_scripts(), cost_per_call=2.0)  # budget is 5.0
    state, logdir = await run(config, target_repo, runner)

    assert state.status is Status.BUDGET_EXCEEDED
    assert len(runner.calls) == 3
    assert state.cost_usd == 6.0
    assert "budget_exceeded" in (logdir / "report.md").read_text()


async def test_tests_passing_before_implementation_flagged(
    config: Config, target_repo: Path
) -> None:
    scripts = happy_scripts()
    scripts[Role.TEST_AUTHOR] = [
        writes({"tests/test_clamp.py": TRIVIAL_TEST}),
        writes({"tests/test_clamp.py": TEST_CLAMP}, "{}"),
    ]
    scripts[Role.TEST_CRITIC] = [verdict_pass(), verdict_pass()]
    runner = FakeAgentRunner(scripts)
    state, _ = await run(config, target_repo, runner)

    assert state.status is Status.COMPLETE
    second = runner.calls_for(Role.TEST_AUTHOR)[1]
    assert "pass before implementation" in second.prompt


async def test_no_new_tests_flagged(config: Config, target_repo: Path) -> None:
    scripts = happy_scripts()
    scripts[Role.TEST_AUTHOR] = ["did nothing", "{}", "{}"]
    scripts[Role.TEST_CRITIC] = [verdict_pass(), verdict_pass()]
    runner = FakeAgentRunner(scripts)
    state, _ = await run(config, target_repo, runner)

    # Gate findings are auto-ruled `fix` without the arbiter; the final pass adds nothing.
    assert state.status is Status.NEEDS_HUMAN
    assert [f.id for f in state.open_findings] == ["G-tests-missing"]
    assert not runner.calls_for(Role.ARBITER)
    assert state.rulings[0].reason == "failed gate is ground truth"


async def test_blocking_open_questions_stop_before_tests(config: Config, target_repo: Path) -> None:
    scripts = happy_scripts()
    scripts[Role.SPEC_WRITER] = [writes({"task.md": TASK_MD_BLOCKING})]
    runner = FakeAgentRunner(scripts)
    state, _ = await run(config, target_repo, runner)

    assert state.status is Status.NEEDS_HUMAN
    assert state.stage is Stage.SPEC
    assert "swap or raise" in state.open_findings[0].claim
    assert not runner.calls_for(Role.TEST_AUTHOR)


async def test_missing_task_md_needs_human(config: Config, target_repo: Path) -> None:
    scripts = happy_scripts()
    scripts[Role.SPEC_WRITER] = ["I wrote nothing"]
    state, _ = await run(config, target_repo, FakeAgentRunner(scripts))
    assert state.status is Status.NEEDS_HUMAN
    assert "task.md" in state.open_findings[0].claim


async def test_review_blocking_finding_fixed_via_arbiter(config: Config, target_repo: Path) -> None:
    scripts = happy_scripts()
    scripts[Role.SECURITY_REVIEWER] = [verdict_fail(finding("F1", category="security"))]
    scripts[Role.ARBITER] = [rulings(**{"SEC-F1": "fix"})]
    scripts[Role.CODER].append(writes(IMPL_OK, "fixed"))
    runner = FakeAgentRunner(scripts)
    state, _ = await run(config, target_repo, runner)

    assert state.status is Status.COMPLETE
    assert len(runner.calls_for(Role.CODER)) == 2
    assert [r.finding_id for r in state.rulings] == ["SEC-F1"]


async def test_minor_findings_reported_not_fixed(config: Config, target_repo: Path) -> None:
    scripts = happy_scripts()
    scripts[Role.STANDARDS_REVIEWER] = [verdict_pass(finding("F1", severity="minor"))]
    runner = FakeAgentRunner(scripts)
    state, logdir = await run(config, target_repo, runner)

    assert state.status is Status.COMPLETE
    assert [f.id for f in state.minor_findings] == ["STD-F1"]
    assert len(runner.calls_for(Role.CODER)) == 1
    assert "STD-F1" in (logdir / "report.md").read_text()


async def test_worktree_removed_on_success_branch_kept(config: Config, target_repo: Path) -> None:
    state, logdir = await run(config, target_repo, FakeAgentRunner(happy_scripts()))
    assert state.status is Status.COMPLETE and state.worktree_removed
    assert not Path(state.worktree).exists()
    assert "advpipe/t1" in git(target_repo, "branch", "--list", "advpipe/t1")
    assert git(target_repo, "worktree", "list").count("\n") == 1  # only the main tree
    assert "(worktree removed)" in (logdir / "report.md").read_text()


async def test_worktree_kept_when_asked(config: Config, target_repo: Path) -> None:
    runner = FakeAgentRunner(happy_scripts())
    orch = Orchestrator(config, runner, target_repo, "w", run_id="t1", keep_worktree=True)
    state = await orch.run()
    assert state.status is Status.COMPLETE and Path(state.worktree).is_dir()


async def test_worktree_kept_when_not_complete(config: Config, target_repo: Path) -> None:
    scripts = happy_scripts()
    scripts[Role.SPEC_WRITER] = [writes({"task.md": TASK_MD_BLOCKING})]
    state, _ = await run(config, target_repo, FakeAgentRunner(scripts))
    assert state.status is Status.NEEDS_HUMAN
    assert Path(state.worktree).is_dir() and not state.worktree_removed


async def test_report_has_cost_table(config: Config, target_repo: Path) -> None:
    state, logdir = await run(config, target_repo, FakeAgentRunner(happy_scripts()))
    assert state.calls_by_stage == {"spec": 1, "stage-tests": 2, "stage-code": 2, "review": 2}
    report = (logdir / "report.md").read_text()
    assert "| review | 2 | $0.02 | 29% |" in report
    assert "| **total** | **7** | **$0.07** | |" in report


async def test_progress_reports_each_step(config: Config, target_repo: Path) -> None:
    lines: list[str] = []
    orch = Orchestrator(
        config,
        FakeAgentRunner(happy_scripts()),
        target_repo,
        "w",
        run_id="t1",
        name="t1",
        progress=lines.append,
    )
    await orch.run()

    assert lines[0] == "run t1 started; branch advpipe/t1"
    stages = [line for line in lines if line.startswith("== ")]
    assert stages == ["== SPEC", "== TESTS", "== CODE", "== REVIEW", "== FINAL_GATES"]
    text = "\n".join(lines)
    for expected in (
        "spec-writer working (claude-opus-5-5)...",
        "test-critic: PASS (0 blocking, 0 minor)",
        "gates: test FAIL (new tests should fail before implementation)",
        "tests stage passed in 1 round(s)",
        "code-critic: PASS (0 blocking, 0 minor)",
        "security scanner: security skipped",
        "security-reviewer: PASS (0 blocking, 0 minor)",
    ):
        assert expected in text, expected
    assert "coder done in" in text and "(run total $0.04)" in text


async def test_progress_reports_guard_and_arbiter(config: Config, target_repo: Path) -> None:
    scripts = happy_scripts()
    scripts[Role.CODER] = [
        writes({**IMPL_OK, "tests/test_clamp.py": "def test_x() -> None:\n    pass\n"}),
        "{}",
        "{}",
    ]
    scripts[Role.CODE_CRITIC] = [verdict_fail(finding("F1", line=n)) for n in (1, 2, 3)]
    scripts[Role.ARBITER] = [rulings(**{"C3-F1": "dismiss"})]
    lines: list[str] = []
    orch = Orchestrator(
        config, FakeAgentRunner(scripts), target_repo, "w", run_id="t1", progress=lines.append
    )
    await orch.run()
    text = "\n".join(lines)
    assert "test-file guard: coder edited tests" in text
    assert "round 3/3" in text
    assert "round cap reached: 1 open finding(s) go to the arbiter" in text
    assert "  C3-F1: dismiss (r)" in text


async def test_branch_named_after_work_item(config: Config, target_repo: Path) -> None:
    item = "Create the SQLite storage layer for the homeinv server.\n\n- lots of detail"
    state = await Orchestrator(config, FakeAgentRunner(happy_scripts()), target_repo, item).run()
    assert state.status is Status.COMPLETE
    assert state.branch == "advpipe/create-sqlite-storage-layer-homeinv"
    assert state.branch in git(target_repo, "branch", "--list", "advpipe/*")
    report = (target_repo / ".advpipe" / "runs" / state.run_id / "report.md").read_text()
    assert f"`{state.branch}`" in report


async def test_name_overrides_branch(config: Config, target_repo: Path) -> None:
    runner = FakeAgentRunner(happy_scripts())
    orch = Orchestrator(config, runner, target_repo, "anything", name="S01 database")
    state = await orch.run()
    assert state.branch == "advpipe/s01-database"
