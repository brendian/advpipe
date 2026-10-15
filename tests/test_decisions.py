"""Decision points: the run stops and asks a person (docs/LEVELS_PLAN.md §3.5, milestone L3):
failing checks the pipeline couldn't fix (``red_checks``), security scan hits below Full
(``scan_hits``) and the spec's last allowed pass still having blocking questions
(``spec_cap``)."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import git
from fakes import (
    CLAMP_WRONG,
    CORE_HEADER,
    IMPL_OK,
    IMPL_WRONG,
    TASK_MD,
    TEST_CLAMP,
    TRIVIAL_TEST,
    FakeAgentRunner,
    Step,
    finding,
    happy_scripts,
    interrupt,
    verdict_fail,
    verdict_pass,
    writes,
)
from test_bypass import AGENT_ANSWER, QUESTION_2, bypass_reply, second_question
from test_levels import FAIL_SCAN, PASS_SCAN, with_pipeline, with_scan
from typer.testing import CliRunner

import advpipe.cli as cli_module
from advpipe import answers, decisions
from advpipe.answers import AnswerError, record_answers
from advpipe.cli import app
from advpipe.config import Config
from advpipe.control import ControlError, clean_run
from advpipe.decisions import DecisionError, decide
from advpipe.models import RunState, Stage, Status
from advpipe.orchestrator import NotResumable, Orchestrator, check_resumable
from advpipe.runlog import RunLog
from advpipe.runner import Role

cli = CliRunner()
NOTE = "Please keep the public signature; don't touch mathutils/__init__.py!"


def logdir(repo: Path, run_id: str = "r1") -> Path:
    return RunLog.for_run(repo, run_id).root


def state_of(repo: Path, run_id: str = "r1") -> RunState:
    return RunLog.for_run(repo, run_id).read_state()


def events(repo: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in (logdir(repo) / "events.jsonl").read_text().splitlines()]


async def start(config: Config, repo: Path, scripts: dict[Role, list[Step]]) -> RunState:
    orch = Orchestrator(config, FakeAgentRunner(scripts), repo, "add clamp", "r1", name="r1")
    return await orch.run()


async def carry_on(
    repo: Path, scripts: dict[Role, list[Step]], config: Config | None = None
) -> tuple[RunState, FakeAgentRunner]:
    runner = FakeAgentRunner(scripts)
    orch = Orchestrator.resume(runner, repo, "r1", config=config)
    return await orch.run(), runner


def worktree_core(repo: Path) -> str:
    return (Path(state_of(repo).worktree) / "mathutils" / "core.py").read_text()


def red_code(cap: int = 3) -> dict[Role, list[Step]]:
    """The coder never gets it right and the critic never notices: the checks stay red through
    every round, and then through the final pass."""
    scripts = happy_scripts()
    scripts[Role.CODER] = [writes(IMPL_WRONG)] + ["tried"] * cap
    scripts[Role.CODE_CRITIC] = [verdict_pass()] * cap
    return scripts


def fixed_code() -> dict[Role, list[Step]]:
    return {
        Role.CODER: [writes(IMPL_OK, "fixed")],
        Role.STANDARDS_REVIEWER: [verdict_pass()],
        Role.SECURITY_REVIEWER: [verdict_pass()],
    }


# --------------------------------------------------------------------------- red_checks


async def test_red_checks_asks_only_after_the_rounds_and_the_final_pass(
    config: Config, target_repo: Path
) -> None:
    runner = FakeAgentRunner(red_code())
    orch = Orchestrator(config, runner, target_repo, "add clamp", "r1", name="r1")
    state = await orch.run()

    assert state.status is Status.NEEDS_HUMAN
    d = state.decision
    assert d is not None and d.kind == "red_checks" and d.stage is Stage.CODE
    assert decisions.awaiting_decision(state) and not decisions.decision_ready(state)
    # The whole of CODE ran first: 3 rounds, then the arbiter step's final pass by the coder.
    assert [c.role for c in runner.calls].count(Role.CODER) == 4
    assert state.rounds_used["code"] == 3 and state.stage is Stage.ARBITER
    assert [f.criterion for f in d.findings] == ["gate:test"]
    assert "still failing at CODE (test)" in d.explanation and "fix pass 1 of 3" in d.explanation
    assert [f.id for f in state.open_findings] == [f.id for f in d.findings]
    # The worktree is left exactly as it was: the wrong code, uncommitted.
    assert worktree_core(target_repo) == CORE_HEADER + CLAMP_WRONG
    assert git(Path(state.worktree), "status", "--porcelain").strip()
    assert "code" not in state.commits
    (ev,) = [e for e in events(target_repo) if e["kind"] == "decision"]
    assert ev["choice"] == "asked" and ev["decision_kind"] == "red_checks"
    assert ev["choices"] == ["fix", "takeover"]
    assert "advpipe decide r1 fix | takeover" in ev["message"]
    report = (logdir(target_repo) / "report.md").read_text()
    assert "advpipe decide r1 fix | takeover" in report


async def test_waiting_run_is_not_resumable_and_names_decide(
    config: Config, target_repo: Path
) -> None:
    await start(config, target_repo, red_code())
    with pytest.raises(NotResumable, match="waiting for a decision: advpipe decide r1 fix"):
        check_resumable(target_repo, "r1")
    result = cli.invoke(app, ["resume", "r1", "--repo", str(target_repo)])
    assert result.exit_code == 2 and "advpipe decide r1 fix | takeover" in result.output


async def test_fix_with_a_note_runs_one_pass_by_the_coder(
    config: Config, target_repo: Path
) -> None:
    await start(config, target_repo, red_code())
    saved = decide(target_repo, "r1", "fix", NOTE)
    assert saved.status is Status.NEEDS_HUMAN and decisions.decision_ready(saved)
    assert saved.decision is not None and saved.decision.note == NOTE
    assert worktree_core(target_repo) == CORE_HEADER + CLAMP_WRONG  # deciding changes nothing

    state, runner = await carry_on(target_repo, fixed_code())

    assert state.status is Status.COMPLETE, (state.notes, state.open_findings)
    # One coder pass, no critic, and the stage's commit and the rest of the run as usual.
    assert [c.role for c in runner.calls] == [
        Role.CODER,
        Role.STANDARDS_REVIEWER,
        Role.SECURITY_REVIEWER,
    ]
    prompt = runner.calls[0].prompt
    assert "Fix pass 1: a person asked for another pass" in prompt
    assert f"A person asked for this pass and left you this note:\n\n```\n{NOTE}\n```" in prompt
    assert "G-test" in prompt  # the failing checks, from the decision
    assert state.fix_passes == 1 and state.decision is not None and state.decision.applied
    assert list(state.commits) == ["spec", "tests", "code", "review"]
    assert (logdir(target_repo) / "fix-pass-1" / "author.txt").read_text() == "fixed"
    assert (logdir(target_repo) / "fix-pass-1" / "gates.json").is_file()
    assert state.cost_by_stage["stage-code"] > 0 and state.calls_by_stage["stage-code"] == 7
    kinds = [(e["kind"], e.get("choice")) for e in events(target_repo) if e["kind"] == "decision"]
    assert kinds == [("decision", "asked"), ("decision", "fix")]


async def test_fix_without_a_note(config: Config, target_repo: Path) -> None:
    await start(config, target_repo, red_code())
    decide(target_repo, "r1", "fix")
    state, runner = await carry_on(target_repo, fixed_code())
    assert state.status is Status.COMPLETE
    assert "left you this note" not in runner.calls[0].prompt


async def test_fix_pass_that_is_still_red_asks_again_until_the_cap(
    config: Config, target_repo: Path
) -> None:
    capped = config.model_copy(
        update={"limits": config.limits.model_copy(update={"max_fix_passes": 2})}
    )
    await start(capped, target_repo, red_code())
    for n in (1, 2):
        decide(target_repo, "r1", "fix")
        state, _ = await carry_on(target_repo, {Role.CODER: ["still wrong"]})
        assert state.status is Status.NEEDS_HUMAN and state.fix_passes == n
        assert len(state.decisions) == n + 1 and decisions.awaiting_decision(state)
    with pytest.raises(DecisionError, match="all 2 fix passes.*take over"):
        decide(target_repo, "r1", "fix")
    # Taking over is still possible.
    assert decide(target_repo, "r1", "takeover").ended_by == "takeover"


async def test_fix_at_the_tests_stage_goes_to_the_test_author(
    config: Config, target_repo: Path
) -> None:
    scripts = happy_scripts()
    # Tests that pass before any implementation exist never show a red gate.
    scripts[Role.TEST_AUTHOR] = [writes({"tests/test_clamp.py": TRIVIAL_TEST})] + ["again"] * 2
    scripts[Role.TEST_CRITIC] = [verdict_pass()] * 2
    state = await start(config, target_repo, scripts)
    d = state.decision
    assert d is not None and d.kind == "red_checks" and d.stage is Stage.TESTS
    assert [f.id for f in d.findings] == ["G-tests-green"]

    decide(target_repo, "r1", "fix", "test the lo > hi case")
    state, runner = await carry_on(
        target_repo,
        {
            Role.TEST_AUTHOR: [writes({"tests/test_clamp.py": TEST_CLAMP})],
            Role.CODER: [writes(IMPL_OK)],
            Role.CODE_CRITIC: [verdict_pass()],
            Role.TEST_CRITIC: [],
            Role.STANDARDS_REVIEWER: [verdict_pass()],
            Role.SECURITY_REVIEWER: [verdict_pass()],
        },
    )
    assert state.status is Status.COMPLETE, state.open_findings
    assert runner.calls[0].role is Role.TEST_AUTHOR  # not the coder
    assert "test the lo > hi case" in runner.calls[0].prompt
    assert not runner.calls_for(Role.TEST_CRITIC)  # no critic round after a requested pass


async def test_red_final_gates_ask_too(config: Config, target_repo: Path, tmp_path: Path) -> None:
    """The final gates are red although every stage passed: ask, then a fix pass by the coder."""
    counter = tmp_path / "calls"
    flaky = (
        "import pathlib, sys\n"
        "p = pathlib.Path(sys.argv[1])\n"
        "n = int(p.read_text() or 0) + 1 if p.exists() else 1\n"
        "p.write_text(str(n))\n"
        "sys.exit(1 if n == 2 else 0)\n"  # the 2nd run of the types gate: the final gates
    )
    cfg = config.model_copy(
        update={
            "gates": config.gates.model_copy(
                update={"types": [sys.executable, "-c", flaky, str(counter)]}
            )
        }
    )
    cfg = with_pipeline(cfg, level="adversarial")
    state = await start(cfg, target_repo, happy_scripts())
    d = state.decision
    assert d is not None and d.kind == "red_checks" and d.stage is Stage.FINAL_GATES
    assert [f.criterion for f in d.findings] == ["gate:types"]
    assert list(state.commits) == ["spec", "tests", "code"]

    decide(target_repo, "r1", "fix", "types are flaky, look again")
    state, runner = await carry_on(target_repo, {Role.CODER: ["looked again"]}, cfg)
    assert state.status is Status.COMPLETE, state.open_findings
    assert [c.role for c in runner.calls] == [Role.CODER]
    assert state.stage is Stage.DONE and state.fix_passes == 1


async def test_interrupted_fix_pass_runs_again_on_a_plain_resume(
    config: Config, target_repo: Path
) -> None:
    await start(config, target_repo, red_code())
    decide(target_repo, "r1", "fix", NOTE)
    orch = Orchestrator.resume(FakeAgentRunner({Role.CODER: [interrupt]}), target_repo, "r1")
    with pytest.raises(asyncio.CancelledError):
        await orch.run()
    after = state_of(target_repo)
    assert after.status is Status.RUNNING and after.fix_passes == 0
    assert after.decision is not None and not after.decision.applied
    # The work in the worktree is kept, and the pass runs again, note and all.
    assert worktree_core(target_repo) == CORE_HEADER + CLAMP_WRONG
    state, runner = await carry_on(target_repo, fixed_code())
    assert state.status is Status.COMPLETE and state.fix_passes == 1
    assert NOTE in runner.calls[0].prompt


# --------------------------------------------------------------------------- takeover


async def test_takeover_commits_removes_the_worktree_and_ends_the_run(
    config: Config, target_repo: Path
) -> None:
    state = await start(config, target_repo, red_code())
    worktree = Path(state.worktree)
    assert worktree.is_dir()

    ended = decide(target_repo, "r1", "takeover")

    assert ended.ended_by == "takeover" and ended.status is Status.NEEDS_HUMAN
    assert ended.worktree_removed and not worktree.exists()
    subjects = git(target_repo, "log", "--format=%s", "main..advpipe/r1").splitlines()
    assert subjects[0] == "advpipe: work in progress, handed over (checks failing)"
    assert "advpipe: tests" in subjects
    assert git(target_repo, "show", "advpipe/r1:mathutils/core.py") == CORE_HEADER + CLAMP_WRONG
    assert any("Handed over to you at CODE" in n for n in ended.notes)
    # The run is over: nothing decides, resumes or answers it any more.
    with pytest.raises(DecisionError, match="handed over to you"):
        decide(target_repo, "r1", "fix")
    with pytest.raises(NotResumable, match="handed over to you; nothing to resume"):
        check_resumable(target_repo, "r1")
    # `clean` keeps its usual rules: the branch has commits no other branch has.
    with pytest.raises(ControlError, match="no other branch has"):
        clean_run(target_repo, "r1")
    assert "deleted branch advpipe/r1" in clean_run(target_repo, "r1", force=True)


async def test_takeover_with_nothing_uncommitted(config: Config, target_repo: Path) -> None:
    """A final-gates stop can come with a clean worktree: no empty handover commit."""
    await start(config, target_repo, red_code())
    ws = Path(state_of(target_repo).worktree)
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "-m", "by hand")
    decide(target_repo, "r1", "takeover")
    subjects = git(target_repo, "log", "--format=%s", "main..advpipe/r1").splitlines()
    assert subjects[0] == "by hand"
    assert "handed over" not in " ".join(subjects)
    assert any("nothing was uncommitted" in n for n in state_of(target_repo).notes)


# --------------------------------------------------------------------------- refusals


async def test_decide_refusals(config: Config, target_repo: Path) -> None:
    with pytest.raises(DecisionError, match="no run nope"):
        decide(target_repo, "nope", "fix")
    state = await start(config, target_repo, happy_scripts())
    assert state.status is Status.COMPLETE
    with pytest.raises(DecisionError, match="isn't waiting for a decision"):
        decide(target_repo, "r1", "fix")

    await start_second(config, target_repo)
    with pytest.raises(DecisionError, match="isn't a choice here: choose fix or takeover"):
        decide(target_repo, "r2", "ignore")
    with pytest.raises(DecisionError, match="note goes with the fix choice only"):
        decide(target_repo, "r2", "takeover", "hand it over")
    with pytest.raises(DecisionError, match="characters; keep it under 8,000"):
        decide(target_repo, "r2", "fix", "x" * 8_001)
    # A live process holds the lock.
    log = RunLog.for_run(target_repo, "r2")
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        log.acquire_lock(other.pid)
        with pytest.raises(DecisionError, match="still being driven"):
            decide(target_repo, "r2", "fix")
    finally:
        other.kill()
        other.wait()
        log.release_lock()
    # The branch is gone.
    git(target_repo, "worktree", "remove", "--force", str(state_of(target_repo, "r2").worktree))
    git(target_repo, "branch", "-D", "advpipe/r2")
    with pytest.raises(DecisionError, match="branch advpipe/r2 no longer exists"):
        decide(target_repo, "r2", "fix")
    assert state_of(target_repo, "r2").decision is not None
    assert not state_of(target_repo, "r2").decision.choice  # nothing was recorded


async def start_second(config: Config, repo: Path) -> None:
    orch = Orchestrator(
        config, FakeAgentRunner(red_code()), repo, "add clamp again", "r2", name="r2"
    )
    assert (await orch.run()).status is Status.NEEDS_HUMAN


async def test_a_decision_is_recorded_once(config: Config, target_repo: Path) -> None:
    await start(config, target_repo, red_code())
    decide(target_repo, "r1", "fix")
    with pytest.raises(DecisionError, match="isn't waiting for a decision"):
        decide(target_repo, "r1", "takeover")


# --------------------------------------------------------------------------- scan_hits

# A scan that fails while bad.txt exists in the worktree.
SCAN_BAD_TXT = [
    sys.executable,
    "-c",
    "import pathlib, sys\n"
    "bad = pathlib.Path('bad.txt').exists()\n"
    "print('rule: shell=True at bad.txt:1\\n2 findings' if bad else 'clean')\n"
    "sys.exit(1 if bad else 0)",
]


def scan_cfg(config: Config, command: list[str], **pipeline: Any) -> Config:
    settings = {"level": "adversarial", "security_scan": True, **pipeline}
    return with_pipeline(with_scan(config, command), **settings)


def bad_scripts(fix_round: bool = False) -> dict[Role, list[Step]]:
    scripts = happy_scripts()
    scripts[Role.CODER] = [writes({**IMPL_OK, "bad.txt": "x"})]
    if fix_round:
        scripts[Role.CODER].append(lambda req: (req.cwd / "bad.txt").unlink() or "removed bad.txt")
        scripts[Role.CODE_CRITIC] = [verdict_pass()] * 2
    return scripts


async def test_scan_hits_ask_the_first_time_below_full(config: Config, target_repo: Path) -> None:
    cfg = scan_cfg(config, SCAN_BAD_TXT)
    runner = FakeAgentRunner(bad_scripts())
    orch = Orchestrator(cfg, runner, target_repo, "add clamp", "r1", name="r1")
    state = await orch.run()

    assert state.status is Status.NEEDS_HUMAN and state.scan_policy == "ask"
    d = state.decision
    assert d is not None and d.kind == "scan_hits" and d.stage is Stage.CODE
    assert "reported 2 issue(s)" in d.explanation and "may not be real problems" in d.explanation
    assert [f.criterion for f in d.findings] == ["gate:security"]
    assert "shell=True at bad.txt:1" in d.findings[0].evidence
    assert [c.role for c in runner.calls] == [
        Role.SPEC_WRITER,
        Role.TEST_AUTHOR,
        Role.TEST_CRITIC,
        Role.CODER,
    ]
    assert (Path(state.worktree) / "bad.txt").exists()  # untouched while it waits
    (ev,) = [e for e in events(target_repo) if e["kind"] == "decision"]
    assert ev["choices"] == ["ignore", "block"]


async def test_scan_hits_ignore_records_minor_findings_and_never_asks_again(
    config: Config, target_repo: Path
) -> None:
    cfg = scan_cfg(config, SCAN_BAD_TXT)
    await start(cfg, target_repo, bad_scripts())
    decide(target_repo, "r1", "ignore")
    state, runner = await carry_on(target_repo, {Role.CODE_CRITIC: [verdict_pass()]})

    assert state.status is Status.COMPLETE, (state.notes, state.open_findings)
    assert state.scan_policy == "ignore"
    # The author isn't called again: its work is judged as it is.
    assert [c.role for c in runner.calls] == [Role.CODE_CRITIC]
    (minor,) = [f for f in state.minor_findings if f.criterion == "gate:security"]
    assert minor.severity == "minor" and "ignored by you" in minor.claim
    assert "shell=True" in minor.evidence
    assert len(state.decisions) == 1  # the final gates hit the scan again: not asked again
    assert "ignored by you" in (logdir(target_repo) / "report.md").read_text()


async def test_scan_hits_block_sends_them_to_the_author_and_the_run_can_complete(
    config: Config, target_repo: Path
) -> None:
    cfg = scan_cfg(config, SCAN_BAD_TXT)
    await start(cfg, target_repo, bad_scripts())
    decide(target_repo, "r1", "block")
    scripts: dict[Role, list[Step]] = {
        Role.CODE_CRITIC: [verdict_pass()] * 2,
        Role.CODER: [lambda req: (req.cwd / "bad.txt").unlink() or "removed bad.txt"],
    }
    state, runner = await carry_on(target_repo, scripts)

    assert state.status is Status.COMPLETE, (state.notes, state.open_findings)
    assert state.scan_policy == "block"
    (coder,) = runner.calls_for(Role.CODER)
    assert "G-security" in coder.prompt and "shell=True at bad.txt:1" in coder.prompt
    assert not any(f.criterion == "gate:security" for f in state.minor_findings)
    assert len(state.decisions) == 1


async def test_scan_hits_block_unresolved_ends_at_red_checks(
    config: Config, target_repo: Path
) -> None:
    cfg = scan_cfg(config, SCAN_BAD_TXT)
    await start(cfg, target_repo, bad_scripts())
    decide(target_repo, "r1", "block")
    scripts: dict[Role, list[Step]] = {
        Role.CODE_CRITIC: [verdict_pass()] * 3,
        Role.CODER: ["no idea"] * 3,
    }
    state, _ = await carry_on(target_repo, scripts)
    d = state.decision
    assert state.status is Status.NEEDS_HUMAN
    assert (
        d is not None
        and d.kind == "red_checks"
        and [f.criterion for f in d.findings] == ["gate:security"]
    )
    assert [x.kind for x in state.decisions] == ["scan_hits", "red_checks"]


@pytest.mark.parametrize("policy", ["ignore", "block"])
async def test_up_front_policy_never_asks(config: Config, target_repo: Path, policy: str) -> None:
    cfg = scan_cfg(config, SCAN_BAD_TXT, scan_failures=policy)
    scripts = bad_scripts(fix_round=policy == "block")
    if policy == "block":
        scripts[Role.CODE_CRITIC] = [verdict_pass()] * 2
    state = await start(cfg, target_repo, scripts)
    assert state.status is Status.COMPLETE, (state.notes, state.open_findings)
    assert state.decisions == [] and state.scan_policy == policy


async def test_full_never_asks_about_scan_hits(config: Config, target_repo: Path) -> None:
    cfg = with_scan(config, FAIL_SCAN)  # level full, the scan on, policy ask
    state = await start(cfg, target_repo, happy_scripts())
    assert state.status is Status.COMPLETE and state.decisions == []


async def test_a_scan_that_passes_never_asks(config: Config, target_repo: Path) -> None:
    state = await start(scan_cfg(config, PASS_SCAN), target_repo, happy_scripts())
    assert state.status is Status.COMPLETE and state.decisions == []


# --------------------------------------------------------------------------- spec_cap


def capped(config: Config, cap: int = 2) -> Config:
    return config.model_copy(
        update={"limits": config.limits.model_copy(update={"max_rounds_spec": cap})}
    )


async def at_the_cap(config: Config, repo: Path) -> RunState:
    """Two spec passes, the second still with a blocking question (the first was answered)."""
    from test_answers import ANSWER, TASK_MD_REVISED, after_answers, blocked_run

    cfg = capped(config)
    await blocked_run(cfg, repo)
    record_answers(repo, "r1", {"Q1": ANSWER})
    scripts = after_answers(second_question(TASK_MD_REVISED))
    state, _ = await carry_on(repo, scripts, cfg)
    assert state.status is Status.NEEDS_HUMAN
    return state


async def test_the_cap_asks_begin_work(config: Config, target_repo: Path) -> None:
    state = await at_the_cap(config, target_repo)
    d = state.decision
    assert d is not None and d.kind == "spec_cap" and d.stage is Stage.SPEC
    assert "still has 1 blocking question after 2 passes. Begin work anyway?" in d.explanation
    assert [(f.id, f.claim) for f in d.findings] == [("Q2", QUESTION_2)]
    assert state.spec_passes == 2 and "spec" not in state.commits
    assert not answers.awaiting_answers(state, 2)
    with pytest.raises(AnswerError, match="advpipe decide r1 begin"):
        record_answers(target_repo, "r1", {"Q2": "no"})


async def test_begin_has_the_agent_decide_everything_and_goes_on(
    config: Config, target_repo: Path
) -> None:
    from test_answers import after_answers

    await at_the_cap(config, target_repo)
    decide(target_repo, "r1", "begin")
    scripts = after_answers()
    scripts[Role.SPEC_WRITER] = [bypass_reply("Q2")]
    state, runner = await carry_on(target_repo, scripts, capped(config))

    assert state.status is Status.COMPLETE, (state.notes, state.open_findings)
    assert state.bypass_questions is True
    # One bypass call, and no further revision pass.
    (call,) = runner.calls_for(Role.SPEC_WRITER)
    assert f"Q2: {QUESTION_2}" in call.prompt and "# Questions to decide" in call.prompt
    assert state.spec_passes == 2
    q2 = next(q for q in state.questions if q.id == "Q2")
    assert q2.source == "agent" and q2.answer == AGENT_ANSWER and q2.applied
    committed = git(target_repo, "show", "advpipe/r1:task.md")
    assert f"**Answer (agent's best judgement):** {AGENT_ANSWER}" in committed
    assert (logdir(target_repo) / "spec" / "pass-2" / "bypass-begin.json").is_file()
    assert any("you chose to begin work" in n for n in state.notes)


async def test_stop_ends_the_run_and_it_is_not_resumable(config: Config, target_repo: Path) -> None:
    await at_the_cap(config, target_repo)
    ended = decide(target_repo, "r1", "stop")
    assert ended.ended_by == "stop" and "spec" not in ended.commits
    with pytest.raises(NotResumable, match="stopped at your choice"):
        check_resumable(target_repo, "r1")
    assert (logdir(target_repo) / "task.md").is_file()  # the last task.md stays for reading


# --------------------------------------------------------------------------- the CLI


def blocked_cli(config: Config, repo: Path) -> None:
    asyncio.run(start(config, repo, red_code()))


@pytest.fixture
def fake_fix(monkeypatch: pytest.MonkeyPatch) -> list[FakeAgentRunner]:
    runners: list[FakeAgentRunner] = []

    def make() -> FakeAgentRunner:
        runners.append(FakeAgentRunner(fixed_code()))
        return runners[-1]

    monkeypatch.setattr(cli_module, "SdkAgentRunner", make)
    return runners


def test_decide_cli_fix_then_resume(
    config: Config, target_repo: Path, fake_fix: list[FakeAgentRunner]
) -> None:
    blocked_cli(config, target_repo)
    result = cli.invoke(
        app, ["decide", "r1", "fix", "--note", NOTE, "--repo", str(target_repo), "-q"]
    )
    assert result.exit_code == 0, result.output
    assert "you chose fix" in result.stderr and "r1  complete" in result.stdout
    assert NOTE in fake_fix[0].calls[0].prompt


def test_decide_cli_takeover(config: Config, target_repo: Path) -> None:
    blocked_cli(config, target_repo)
    result = cli.invoke(app, ["decide", "r1", "takeover", "--repo", str(target_repo)])
    assert result.exit_code == 0, result.output
    assert "handed over: branch advpipe/r1 has the work as it was" in result.stdout
    assert "git switch advpipe/r1" in result.stdout and "the checks: " in result.stdout
    assert cli.invoke(app, ["decide", "r1", "fix", "--repo", str(target_repo)]).exit_code == 2
    assert cli.invoke(app, ["resume", "r1", "--repo", str(target_repo)]).exit_code == 2


def test_decide_cli_refusals_exit_2(config: Config, target_repo: Path) -> None:
    blocked_cli(config, target_repo)
    base = ["decide", "r1", "--repo", str(target_repo)]
    bad = cli.invoke(app, [base[0], base[1], "begin", *base[2:]])
    assert bad.exit_code == 2 and "isn't a choice here" in bad.stderr
    note = cli.invoke(app, [*base[:2], "takeover", *base[2:], "--note", "x"])
    assert note.exit_code == 2 and "fix choice only" in note.stderr
    assert cli.invoke(app, ["decide", "zz", "fix", "--repo", str(target_repo)]).exit_code == 2


def test_decide_without_a_choice_and_no_terminal_prints_the_choices(
    config: Config, target_repo: Path
) -> None:
    blocked_cli(config, target_repo)
    result = cli.invoke(app, ["decide", "r1", "--repo", str(target_repo)])
    assert result.exit_code == 2
    assert (
        "advpipe decide r1 fix" in result.stderr and "advpipe decide r1 takeover" in result.stderr
    )
    assert "--note" in result.stderr


def test_decide_without_a_choice_asks_in_a_terminal(
    config: Config,
    target_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_fix: list[FakeAgentRunner],
) -> None:
    blocked_cli(config, target_repo)
    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: True)
    result = cli.invoke(
        app, ["decide", "r1", "--repo", str(target_repo), "-q"], input="fix\nmind the types\n"
    )
    assert result.exit_code == 0, result.output
    assert "The checks are still failing at CODE" in result.output
    assert "mind the types" in fake_fix[0].calls[0].prompt
    assert state_of(target_repo).status is Status.COMPLETE


def test_a_foreground_run_asks_at_once_and_can_decide_later(
    config: Config, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripts = red_code()
    monkeypatch.setattr(cli_module, "SdkAgentRunner", lambda: FakeAgentRunner(scripts))
    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: True)
    (target_repo / "pipeline.toml").write_text(
        "[gates]\ntypes = []\nlint = []\nsecurity = []\n"
        f'test = [{json.dumps(sys.executable)}, "-m", "pytest", "-q", "-p", "no:cacheprovider"]\n'
    )
    args = ["run", "add clamp", "--repo", str(target_repo), "--name", "r1", "-q", "--run-id", "r1"]
    result = cli.invoke(app, args, input="later\n")
    assert result.exit_code == 1, result.output
    assert "The checks are still failing at CODE" in result.stdout
    assert "decide later with: advpipe decide r1 fix | takeover" in result.stdout
    assert "fix: Another fix pass" in result.stdout
    state = state_of(target_repo)
    assert state.status is Status.NEEDS_HUMAN and decisions.awaiting_decision(state)


def test_summary_and_status_show_the_decision(config: Config, target_repo: Path) -> None:
    blocked_cli(config, target_repo)
    out = cli.invoke(app, ["status", "r1", "--repo", str(target_repo)]).stdout
    assert "waiting for a decision:" in out
    assert "advpipe decide r1 fix" in out and "advpipe decide r1 takeover" in out
    decide(target_repo, "r1", "takeover")
    out = cli.invoke(app, ["status", "r1", "--repo", str(target_repo)]).stdout
    assert "ended:    handed over to you: git switch advpipe/r1" in out


def test_decide_detach_resumes_in_the_background(
    config: Config, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked_cli(config, target_repo)
    started: list[list[str]] = []
    monkeypatch.setattr(cli_module, "start_detached", lambda log, args: started.append(args) or 1)
    result = cli.invoke(app, ["decide", "r1", "fix", "--detach", "--repo", str(target_repo)])
    assert result.exit_code == 0, result.output
    (args,) = started
    assert args[:2] == ["resume", "r1"] and "--detached-child" in args
    assert decisions.decision_ready(state_of(target_repo))


def test_finding_helpers_stay_importable() -> None:
    # fakes' finding/verdict_fail are used by sibling tests; keep this module's imports honest.
    assert verdict_fail(finding())
    assert TASK_MD
    assert PASS_SCAN
    assert bypass_reply("Q1")


def test_a_foreground_run_asks_at_once_and_carries_on_with_the_choice(
    config: Config, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixer = FakeAgentRunner(fixed_code())
    runners = [FakeAgentRunner(red_code()), fixer]
    monkeypatch.setattr(cli_module, "SdkAgentRunner", lambda: runners.pop(0))
    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: True)
    gate = f'test = [{json.dumps(sys.executable)}, "-m", "pytest", "-q", "-p", "no:cacheprovider"]'
    (target_repo / "pipeline.toml").write_text(
        f"[gates]\ntypes = []\nlint = []\nsecurity = []\n{gate}\n"
    )
    args = ["run", "add clamp", "--repo", str(target_repo), "--name", "r1", "-q", "--run-id", "r1"]
    result = cli.invoke(app, args, input="fix\nkeep it simple\n")
    assert result.exit_code == 0, result.output
    assert "r1  needs_human" in result.stdout and "r1  complete" in result.stdout
    assert "keep it simple" in fixer.calls[0].prompt
    assert state_of(target_repo).status is Status.COMPLETE
