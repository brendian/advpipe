"""The solo levels, Basic and Reviewer (docs/LEVELS_PLAN.md §3.4, milestone L3): one agent
writes task.md, the tests and the code in one pass, and the run stops to ask when something
blocks it or the checks stay red."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import git
from fakes import (
    CORE_HEADER,
    IMPL_OK,
    IMPL_WRONG,
    TASK_MD,
    TASK_MD_BLOCKING,
    TEST_CLAMP,
    FakeAgentRunner,
    Step,
    finding,
    interrupt,
    verdict_fail,
    verdict_pass,
    writes,
)
from test_answers import ANSWER, QUESTION, TASK_MD_REVISED
from test_bypass import AGENT_ANSWER, QUESTION_2, bypass_reply, second_question
from test_decisions import FAIL_SCAN, SCAN_BAD_TXT, scan_cfg
from test_levels import with_pipeline
from typer.testing import CliRunner

import advpipe.cli as cli_module
from advpipe import answers, decisions
from advpipe.answers import record_answers
from advpipe.cli import app
from advpipe.config import Config
from advpipe.decisions import decide
from advpipe.levels import max_agent_calls, roles_for
from advpipe.models import Level, RunState, Stage, Status
from advpipe.orchestrator import Orchestrator
from advpipe.runlog import RunLog
from advpipe.runner import ROLE_MODEL, ROLE_TOOLS, WRITER, Role

cli = CliRunner()
TEST_FILE = "tests/test_clamp.py"

SOLO_OK = writes({"task.md": TASK_MD, TEST_FILE: TEST_CLAMP, **IMPL_OK}, "AUTHOR-REASONING solo")
SOLO_WRONG = writes({"task.md": TASK_MD, TEST_FILE: TEST_CLAMP, **IMPL_WRONG}, "wrong")
SOLO_REVISED = writes({"task.md": TASK_MD_REVISED, TEST_FILE: TEST_CLAMP, **IMPL_OK}, "revised")
SOLO_ASKS = writes(
    {"task.md": TASK_MD_BLOCKING, "mathutils/extra.py": "# written too early\n", **IMPL_OK},
    "asked a question",
)


def at(config: Config, level: str, **settings: Any) -> Config:
    return with_pipeline(config, level=level, **settings)


def logdir(repo: Path, run_id: str = "r1") -> Path:
    return RunLog.for_run(repo, run_id).root


def state_of(repo: Path, run_id: str = "r1") -> RunState:
    return RunLog.for_run(repo, run_id).read_state()


def events(repo: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in (logdir(repo) / "events.jsonl").read_text().splitlines()]


async def start(
    config: Config, repo: Path, scripts: dict[Role, list[Step]], **kw: Any
) -> tuple[RunState, FakeAgentRunner]:
    runner = FakeAgentRunner(scripts)
    orch = Orchestrator(config, runner, repo, "add clamp", "r1", name="r1", **kw)
    return await orch.run(), runner


async def carry_on(
    repo: Path, scripts: dict[Role, list[Step]], config: Config | None = None
) -> tuple[RunState, FakeAgentRunner]:
    runner = FakeAgentRunner(scripts)
    orch = Orchestrator.resume(runner, repo, "r1", config=config)
    return await orch.run(), runner


def only_level_roles(runner: FakeAgentRunner, level: Level) -> None:
    assert {c.role for c in runner.calls} <= set(roles_for(level))


# --------------------------------------------------------------------------- the role


def test_solo_role_tables() -> None:
    assert ROLE_TOOLS[Role.SOLO] == WRITER and ROLE_MODEL[Role.SOLO] == "author"
    from advpipe.runner import load_prompt

    prompt = load_prompt(Role.SOLO)
    # The same BLOCKING form the spec writer uses, so one parser reads both.
    assert "- BLOCKING: <the question>" in prompt
    assert "Revising after answers" in prompt and "Deciding your own questions (Bypass)" in prompt


# --------------------------------------------------------------------------- Basic


async def test_basic_happy_path(config: Config, target_repo: Path) -> None:
    cfg = at(config, "basic")
    state, runner = await start(cfg, target_repo, {Role.SOLO: [SOLO_OK]})

    assert state.status is Status.COMPLETE, (state.notes, state.open_findings)
    assert state.level is Level.BASIC and state.security_scan is False
    assert len(runner.calls) == max_agent_calls(Level.BASIC, cfg.limits)[0] == 1
    assert [c.role for c in runner.calls] == [Role.SOLO]
    only_level_roles(runner, Level.BASIC)
    # task.md, tests and code are one stage and one commit.
    assert list(state.commits) == ["solo"] and state.stage is Stage.DONE
    log = git(target_repo, "log", "--format=%s", "main..advpipe/r1").splitlines()
    assert log == ["advpipe: solo (task.md, tests, code)"]
    files = git(target_repo, "show", "--name-only", "--format=", "advpipe/r1").split()
    assert {"task.md", TEST_FILE, "mathutils/core.py"} <= set(files)
    pass1 = logdir(target_repo) / "stage-solo" / "pass-1"
    assert (pass1 / "author.txt").read_text() == "AUTHOR-REASONING solo"
    assert (pass1 / "task.md").read_text() == TASK_MD
    assert json.loads((pass1 / "gates.json").read_text())[0]["name"] == "test"
    assert "+++ b/mathutils/core.py" in (pass1 / "diff.patch").read_text()
    assert "task.md" not in (pass1 / "diff.patch").read_text()
    assert (logdir(target_repo) / "task.md").read_text() == TASK_MD
    assert (logdir(target_repo) / "final-gates.json").is_file()
    report = (logdir(target_repo) / "report.md").read_text()
    assert "- **Level:** basic (1 of 5): 1 agent · security scan off" in report
    first = runner.calls[0]
    assert "Work item: add clamp" in first.prompt and "Write task.md" in first.prompt
    assert "- test: `" in first.prompt  # the gate commands, so it can run them
    assert [e["kind"] for e in events(target_repo) if e["kind"] == "decision"] == []


async def test_solo_without_task_md_is_a_blocking_guard_finding(
    config: Config, target_repo: Path
) -> None:
    state, _ = await start(
        at(config, "basic"), target_repo, {Role.SOLO: [writes({TEST_FILE: TEST_CLAMP, **IMPL_OK})]}
    )
    d = state.decision
    assert state.status is Status.NEEDS_HUMAN
    assert d is not None and d.kind == "red_checks" and d.stage is Stage.SOLO
    assert [f.criterion for f in d.findings] == ["guard:task-md"]
    assert d.findings[0].severity == "blocking" and "solo" not in state.commits


async def test_basic_red_checks_ask_at_once_without_a_retry(
    config: Config, target_repo: Path
) -> None:
    cfg = at(config, "basic")
    state, runner = await start(cfg, target_repo, {Role.SOLO: [SOLO_WRONG]})

    assert len(runner.calls) == 1  # no automatic fix pass
    d = state.decision
    assert state.status is Status.NEEDS_HUMAN and state.stage is Stage.SOLO
    assert d is not None and d.kind == "red_checks" and d.stage is Stage.SOLO
    assert "The solo agent doesn't retry by itself." in d.explanation
    assert "Another fix pass by the solo agent" in d.explanation
    assert "solo" not in state.commits
    ws = Path(state.worktree)
    assert (ws / "mathutils" / "core.py").read_text().startswith(CORE_HEADER)  # untouched
    assert git(ws, "status", "--porcelain").strip()

    decide(target_repo, "r1", "fix", "clamp must return lo when x < lo")
    state, runner = await carry_on(target_repo, {Role.SOLO: [writes(IMPL_OK, "fixed it")]})
    assert state.status is Status.COMPLETE, (state.notes, state.open_findings)
    (call,) = runner.calls
    assert call.role is Role.SOLO
    assert "Fix pass 1: a person asked for another pass" in call.prompt
    assert "clamp must return lo when x < lo" in call.prompt and "G-test" in call.prompt
    assert state.fix_passes == 1 and list(state.commits) == ["solo"]
    pass2 = logdir(target_repo) / "stage-solo" / "pass-2"
    assert (pass2 / "author.txt").read_text() == "fixed it"
    request = json.loads((pass2 / "request.json").read_text())
    assert request["note"] == "clamp must return lo when x < lo" and request["fix_pass"] == 1


async def test_basic_takeover_hands_over_the_solo_work(config: Config, target_repo: Path) -> None:
    await start(at(config, "basic"), target_repo, {Role.SOLO: [SOLO_WRONG]})
    ended = decide(target_repo, "r1", "takeover")
    assert ended.ended_by == "takeover" and not Path(ended.worktree).exists()
    shown = git(target_repo, "show", "--name-only", "--format=%s", "advpipe/r1")
    assert shown.startswith("advpipe: work in progress, handed over") and "task.md" in shown


# --------------------------------------------------------------------------- questions


async def test_solo_blocking_questions_leave_only_task_md_and_can_be_answered(
    config: Config, target_repo: Path
) -> None:
    cfg = at(config, "basic")
    state, runner = await start(cfg, target_repo, {Role.SOLO: [SOLO_ASKS]})

    assert state.status is Status.NEEDS_HUMAN and state.stage is Stage.SOLO
    (q,) = state.questions
    assert (q.id, q.text, q.asked_in) == ("Q1", QUESTION, 1)
    assert answers.awaiting_answers(state, 3) and state.spec_passes == 1
    ws = Path(state.worktree)
    assert Path(ws, "task.md").read_text() == TASK_MD_BLOCKING
    assert not (ws / "mathutils" / "extra.py").exists()  # everything but task.md reverted
    assert git(ws, "diff", "--stat", "HEAD").strip() == ""
    assert any("mathutils/extra.py" in n and "reverted" in n for n in state.notes)
    assert "solo" not in state.commits and len(runner.calls) == 1
    (ev,) = [e for e in events(target_repo) if e["kind"] == "questions"]
    assert ev["answerable"] is True and "advpipe answer r1" in ev["message"]
    assert "advpipe answer r1" in (logdir(target_repo) / "report.md").read_text()

    record_answers(target_repo, "r1", {"Q1": ANSWER})
    state, runner = await carry_on(target_repo, {Role.SOLO: [SOLO_REVISED]})

    assert state.status is Status.COMPLETE, (state.notes, state.open_findings)
    (call,) = runner.calls
    assert "# Your previous task.md" in call.prompt and TASK_MD_BLOCKING.strip() in call.prompt
    assert f"{q.id}: {QUESTION}\nAnswer: {ANSWER}" in call.prompt
    committed = git(target_repo, "show", "advpipe/r1:task.md")
    assert f"**Answer:** {ANSWER}" in committed and "## Answers" in committed
    assert (logdir(target_repo) / "stage-solo" / "pass-2" / "answers.json").is_file()
    assert (logdir(target_repo) / "stage-solo" / "pass-2" / "task.md").read_text() == committed
    assert list(state.commits) == ["solo"] and state.spec_passes == 2


def test_advpipe_answer_works_at_the_solo_levels(
    config: Config, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(start(at(config, "basic"), target_repo, {Role.SOLO: [SOLO_ASKS]}))
    runners: list[FakeAgentRunner] = []
    monkeypatch.setattr(
        cli_module,
        "SdkAgentRunner",
        lambda: runners.append(FakeAgentRunner({Role.SOLO: [SOLO_REVISED]})) or runners[-1],
    )
    result = cli.invoke(
        app, ["answer", "r1", "--repo", str(target_repo), "--answer", f"Q1={ANSWER}", "-q"]
    )
    assert result.exit_code == 0, result.output
    assert "r1  complete" in result.stdout and "[basic]" in result.stdout
    assert state_of(target_repo).questions[0].answer == ANSWER


async def test_bypass_goes_to_the_solo_agent(config: Config, target_repo: Path) -> None:
    await start(at(config, "basic"), target_repo, {Role.SOLO: [SOLO_ASKS]})
    record_answers(target_repo, "r1", {}, {"Q1"})
    state, runner = await carry_on(target_repo, {Role.SOLO: [bypass_reply("Q1"), SOLO_REVISED]})

    assert state.status is Status.COMPLETE, (state.notes, state.open_findings)
    assert [c.role for c in runner.calls] == [Role.SOLO, Role.SOLO]
    assert "# Questions to decide" in runner.calls[0].prompt
    assert "# Your previous task.md" in runner.calls[1].prompt
    revision = runner.calls[1].prompt
    assert f"Answer (your own best judgement: the person chose Bypass): {AGENT_ANSWER}" in revision
    q = state.questions[0]
    assert q.source == "agent" and q.answer == AGENT_ANSWER
    assert (logdir(target_repo) / "stage-solo" / "pass-2" / "bypass.json").is_file()
    committed = git(target_repo, "show", "advpipe/r1:task.md")
    assert f"**Answer (agent's best judgement):** {AGENT_ANSWER}" in committed


async def test_bypass_up_front_answers_and_goes_on_in_one_run(
    config: Config, target_repo: Path
) -> None:
    cfg = at(config, "basic", bypass_questions=True)
    state, runner = await start(
        cfg, target_repo, {Role.SOLO: [SOLO_ASKS, bypass_reply("Q1"), SOLO_REVISED]}
    )
    assert state.status is Status.COMPLETE, (state.notes, state.open_findings)
    assert [c.role for c in runner.calls] == [Role.SOLO] * 3
    assert state.questions[0].bypassed and state.questions[0].source == "agent"
    assert "mathutils/extra.py" not in git(
        target_repo, "ls-tree", "-r", "--name-only", "advpipe/r1"
    )


async def test_the_cap_asks_begin_work_at_basic_and_begin_goes_on(
    config: Config, target_repo: Path
) -> None:
    cfg = at(config, "basic")
    cfg = cfg.model_copy(update={"limits": cfg.limits.model_copy(update={"max_rounds_spec": 2})})
    await start(cfg, target_repo, {Role.SOLO: [SOLO_ASKS]})
    record_answers(target_repo, "r1", {"Q1": ANSWER})
    asks_again = writes({"task.md": second_question(TASK_MD_REVISED)}, "asked another")
    state, _ = await carry_on(target_repo, {Role.SOLO: [asks_again]}, cfg)

    d = state.decision
    assert state.status is Status.NEEDS_HUMAN and state.spec_passes == 2
    assert d is not None and d.kind == "spec_cap" and d.stage is Stage.SOLO
    assert [f.claim for f in d.findings] == [QUESTION_2]
    assert not answers.awaiting_answers(state, 2)

    decide(target_repo, "r1", "begin")
    state, runner = await carry_on(
        target_repo, {Role.SOLO: [bypass_reply("Q2"), SOLO_REVISED]}, cfg
    )
    assert state.status is Status.COMPLETE, (state.notes, state.open_findings)
    assert state.bypass_questions is True
    assert [c.role for c in runner.calls] == [
        Role.SOLO,
        Role.SOLO,
    ]  # the bypass call, then the work
    assert "# Questions to decide" in runner.calls[0].prompt
    q2 = next(q for q in state.questions if q.id == "Q2")
    assert q2.source == "agent" and q2.answer == AGENT_ANSWER


async def test_stop_at_the_cap_ends_a_solo_run(config: Config, target_repo: Path) -> None:
    cfg = at(config, "basic")
    cfg = cfg.model_copy(update={"limits": cfg.limits.model_copy(update={"max_rounds_spec": 1})})
    state, _ = await start(cfg, target_repo, {Role.SOLO: [SOLO_ASKS]})
    assert state.decision is not None and state.decision.kind == "spec_cap"
    assert decide(target_repo, "r1", "stop").ended_by == "stop"


# --------------------------------------------------------------------------- Reviewer


async def test_reviewer_happy_path_has_one_review_of_the_whole_change(
    config: Config, target_repo: Path
) -> None:
    cfg = at(config, "reviewer")
    state, runner = await start(
        cfg, target_repo, {Role.SOLO: [SOLO_OK], Role.CODE_CRITIC: [verdict_pass()]}
    )
    assert state.status is Status.COMPLETE, (state.notes, state.open_findings)
    assert [c.role for c in runner.calls] == [Role.SOLO, Role.CODE_CRITIC]
    assert len(runner.calls) == max_agent_calls(Level.REVIEWER, cfg.limits)[0] == 2
    only_level_roles(runner, Level.REVIEWER)
    assert list(state.commits) == ["solo", "review"]
    review = runner.calls[1]
    assert review.model == cfg.models.critic
    # Artifacts only: task.md, the diff, the checks; never the solo agent's reply.
    assert f"# task.md\n\n{TASK_MD}" in review.prompt
    assert "This is a single review pass" in review.prompt
    assert "+++ b/mathutils/core.py" in review.prompt and "# Gate output" in review.prompt
    assert f"- {TEST_FILE}" in review.prompt and "AUTHOR-REASONING" not in review.prompt
    assert (logdir(target_repo) / "review" / "single.json").read_text() == verdict_pass()


async def test_reviewer_findings_get_one_pass_by_the_solo_agent_not_the_coder(
    config: Config, target_repo: Path
) -> None:
    cfg = at(config, "reviewer")
    blocking = verdict_fail(finding("F1", claim="clamp never rejects NaN"))
    state, runner = await start(
        cfg,
        target_repo,
        {
            Role.SOLO: [SOLO_OK, writes({}, "added the NaN check")],
            Role.CODE_CRITIC: [blocking],
        },
    )
    assert state.status is Status.COMPLETE, (state.notes, state.open_findings)
    assert [c.role for c in runner.calls] == [Role.SOLO, Role.CODE_CRITIC, Role.SOLO]
    assert not runner.calls_for(Role.CODER) and not runner.calls_for(Role.ARBITER)
    fix = runner.calls[2].prompt
    assert "REV-F1" in fix and "clamp never rejects NaN" in fix
    assert "these findings are still open and must be fixed" in fix
    assert [(r.finding_id, r.decision) for r in state.rulings] == [("REV-F1", "fix")]
    assert "no arbiter at level reviewer" in state.rulings[0].reason
    assert (logdir(target_repo) / "final-pass-solo" / "author.txt").read_text() == (
        "added the NaN check"
    )
    assert state.cost_by_stage["review"] > 0 and "arbiter" not in state.cost_by_stage


async def test_reviewer_red_checks_after_the_review_fix_ask_and_fix_goes_to_solo(
    config: Config, target_repo: Path
) -> None:
    cfg = at(config, "reviewer")
    state, runner = await start(
        cfg,
        target_repo,
        {
            Role.SOLO: [SOLO_OK, writes(IMPL_WRONG, "broke it")],
            Role.CODE_CRITIC: [verdict_fail(finding("F1"))],
        },
    )
    d = state.decision
    assert state.status is Status.NEEDS_HUMAN
    assert d is not None and d.kind == "red_checks" and d.stage is Stage.REVIEW
    assert "solo" in state.commits and "review" not in state.commits
    assert [c.role for c in runner.calls] == [Role.SOLO, Role.CODE_CRITIC, Role.SOLO]

    decide(target_repo, "r1", "fix")
    state, runner = await carry_on(target_repo, {Role.SOLO: [writes(IMPL_OK, "fixed")]})
    assert state.status is Status.COMPLETE and state.fix_passes == 1
    assert [c.role for c in runner.calls] == [Role.SOLO]  # no critic again
    assert (logdir(target_repo) / "fix-pass-1" / "author.txt").read_text() == "fixed"
    assert list(state.commits) == ["solo", "review"]


async def test_reviewer_asks_when_the_solo_pass_is_red_before_any_review(
    config: Config, target_repo: Path
) -> None:
    state, runner = await start(at(config, "reviewer"), target_repo, {Role.SOLO: [SOLO_WRONG]})
    assert state.decision is not None and state.decision.stage is Stage.SOLO
    assert [c.role for c in runner.calls] == [Role.SOLO]  # the reviewer isn't called yet


# --------------------------------------------------------------------------- resume, bounds


async def test_resume_mid_solo_runs_the_solo_stage_again(config: Config, target_repo: Path) -> None:
    cfg = at(config, "basic")
    orch = Orchestrator(
        cfg, FakeAgentRunner({Role.SOLO: [interrupt]}), target_repo, "add clamp", "r1", name="r1"
    )
    with pytest.raises(asyncio.CancelledError):
        await orch.run()
    assert state_of(target_repo).status is Status.RUNNING
    state, runner = await carry_on(target_repo, {Role.SOLO: [SOLO_OK]})
    assert state.status is Status.COMPLETE and len(runner.calls) == 1


async def test_resume_mid_review_skips_the_solo_stage(config: Config, target_repo: Path) -> None:
    cfg = at(config, "reviewer")
    orch = Orchestrator(
        cfg,
        FakeAgentRunner({Role.SOLO: [SOLO_OK], Role.CODE_CRITIC: [interrupt]}),
        target_repo,
        "add clamp",
        "r1",
        name="r1",
    )
    with pytest.raises(asyncio.CancelledError):
        await orch.run()
    assert list(state_of(target_repo).commits) == ["solo"]
    state, runner = await carry_on(target_repo, {Role.CODE_CRITIC: [verdict_pass()]})
    assert state.status is Status.COMPLETE
    assert [c.role for c in runner.calls] == [Role.CODE_CRITIC]


@pytest.mark.parametrize("level", [Level.BASIC, Level.REVIEWER])
async def test_worst_case_agent_calls(config: Config, target_repo: Path, level: Level) -> None:
    cfg = at(config, level.value)
    scripts: dict[Role, list[Step]] = {Role.SOLO: [SOLO_WRONG]}
    if level is Level.REVIEWER:
        # The solo pass is green; the critic's output is garbage twice (one JSON retry), and
        # the review-fix pass leaves the checks red.
        scripts = {
            Role.SOLO: [SOLO_OK, writes(IMPL_WRONG)],
            Role.CODE_CRITIC: ["not json", "still not json"],
        }
    state, runner = await start(cfg, target_repo, scripts)
    assert state.status is Status.NEEDS_HUMAN and decisions.awaiting_decision(state)
    assert len(runner.calls) == max_agent_calls(level, cfg.limits)[1]
    only_level_roles(runner, level)


# --------------------------------------------------------------------------- the scan


async def test_scan_hits_at_basic_ask_and_ignore_judges_the_work_as_it_is(
    config: Config, target_repo: Path
) -> None:
    cfg = scan_cfg(config, SCAN_BAD_TXT, level="basic")
    solo_bad = writes({"task.md": TASK_MD, TEST_FILE: TEST_CLAMP, "bad.txt": "x", **IMPL_OK})
    state, runner = await start(cfg, target_repo, {Role.SOLO: [solo_bad]})
    d = state.decision
    assert d is not None and d.kind == "scan_hits" and d.stage is Stage.SOLO
    assert state.status is Status.NEEDS_HUMAN and len(runner.calls) == 1

    decide(target_repo, "r1", "ignore")
    state, runner = await carry_on(target_repo, {})
    assert state.status is Status.COMPLETE, (state.notes, state.open_findings)
    assert runner.calls == []  # the author isn't called again
    assert any("ignored by you" in f.claim for f in state.minor_findings)
    assert list(state.commits) == ["solo"]


async def test_scan_hits_blocked_at_basic_go_to_the_solo_agent_via_red_checks(
    config: Config, target_repo: Path
) -> None:
    cfg = scan_cfg(config, FAIL_SCAN, level="basic", scan_failures="block")
    state, runner = await start(cfg, target_repo, {Role.SOLO: [SOLO_OK]})
    d = state.decision
    assert d is not None and d.kind == "red_checks"
    assert [f.criterion for f in d.findings] == ["gate:security"] and len(runner.calls) == 1


# --------------------------------------------------------------------------- invariants


@pytest.mark.parametrize("level", ["basic", "reviewer"])
async def test_agent_replies_never_reach_the_review(
    config: Config, target_repo: Path, level: str
) -> None:
    scripts: dict[Role, list[Step]] = {Role.SOLO: [SOLO_OK], Role.CODE_CRITIC: [verdict_pass()]}
    state, runner = await start(at(config, level), target_repo, scripts)
    assert state.status is Status.COMPLETE
    for call in runner.calls_for(Role.CODE_CRITIC):
        assert "AUTHOR-REASONING" not in call.prompt


def test_solo_level_summary_lines(config: Config, target_repo: Path) -> None:
    asyncio.run(start(at(config, "basic"), target_repo, {Role.SOLO: [SOLO_WRONG]}))
    out = cli.invoke(app, ["status", "r1", "--repo", str(target_repo)])
    assert "level:    basic (1 of 5)" in out.stdout
    assert "advpipe decide r1 fix" in out.stdout
    assert TASK_MD_BLOCKING  # (keeps the shared import honest)
