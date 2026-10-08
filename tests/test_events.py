from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, get_args

import pytest
from fakes import (
    IMPL_OK,
    FakeAgentRunner,
    happy_scripts,
    interrupt,
    verdict_pass,
    writes,
)

from advpipe.config import Config
from advpipe.events import EventKind
from advpipe.orchestrator import Orchestrator
from advpipe.runlog import RunLog
from advpipe.runner import AgentRequest, Role


def read_events(repo: Path, run_id: str) -> list[dict[str, Any]]:
    path = RunLog.for_run(repo, run_id).root / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


async def test_events_match_printed_progress(config: Config, target_repo: Path) -> None:
    lines: list[str] = []
    orch = Orchestrator(
        config, FakeAgentRunner(happy_scripts()), target_repo, "w", "t1", progress=lines.append
    )
    await orch.run()
    events = read_events(target_repo, "t1")

    # One emit() feeds both: every printed line is an event, in the same order, and the only
    # extra event is the final status (which the terminal never showed).
    *echoed, last = events
    assert [e["message"] for e in echoed] == lines
    assert last == {**last, "kind": "status", "status": "complete", "final": True}
    assert not any(e.get("final") for e in echoed)

    kinds = set(get_args(EventKind))
    for e in events:
        assert {"ts", "kind", "stage", "round", "message"} <= set(e)
        assert e["kind"] in kinds
        assert e["ts"].endswith("Z")
    timestamps = [e["ts"] for e in events]
    assert timestamps == sorted(timestamps)

    assert events[0]["kind"] == "run_start" and events[0]["branch"] == "advpipe/w"
    stages = [e["stage"] for e in events if e["kind"] == "stage" and e["message"].startswith("==")]
    assert stages == ["SPEC", "TESTS", "CODE", "REVIEW", "FINAL_GATES"]
    done = [e for e in events if e["kind"] == "agent_done"]
    assert [e["role"] for e in done][:2] == ["spec-writer", "test-author"]
    assert done[-1]["total_usd"] == pytest.approx(0.07)
    assert all(e["cost_usd"] == pytest.approx(0.01) for e in done)
    (coder_done,) = [e for e in done if e["role"] == "coder"]
    assert (coder_done["stage"], coder_done["round"]) == ("CODE", 1)
    code_gates = [e for e in events if e["kind"] == "gates" and e["stage"] == "CODE"]
    assert code_gates[0]["gates"] == {"test": "pass", "types": "skipped", "lint": "skipped"}
    verdicts = [e for e in events if e["kind"] == "verdict"]
    assert {v["role"] for v in verdicts} == {
        "test-critic",
        "code-critic",
        "standards-reviewer",
        "security-reviewer",
    }
    assert all(v["verdict"] == "PASS" and v["blocking"] == 0 for v in verdicts)


async def test_events_are_on_disk_before_the_agent_returns(
    config: Config, target_repo: Path
) -> None:
    seen: list[str] = []

    def coder(req: AgentRequest) -> str:
        seen.extend(e["message"] for e in read_events(target_repo, "t1"))
        return writes(IMPL_OK)(req)

    scripts = happy_scripts()
    scripts[Role.CODER] = [coder]
    await Orchestrator(config, FakeAgentRunner(scripts), target_repo, "w", "t1").run()
    assert seen[-1] == "coder working (claude-opus-5-5)..."
    assert "== CODE" in seen


async def test_interrupt_and_resume_append_to_one_log(config: Config, target_repo: Path) -> None:
    scripts = happy_scripts()
    scripts[Role.CODE_CRITIC] = [interrupt]
    with pytest.raises(asyncio.CancelledError):
        await Orchestrator(config, FakeAgentRunner(scripts), target_repo, "w", "t1").run()
    events = read_events(target_repo, "t1")
    assert events[-2]["kind"] == "status" and "interrupted" in events[-2]["message"]
    assert events[-1] == {**events[-1], "status": "running", "final": True}

    rest = FakeAgentRunner(
        {
            Role.CODER: [writes(IMPL_OK)],
            Role.CODE_CRITIC: [verdict_pass()],
            Role.STANDARDS_REVIEWER: [verdict_pass()],
            Role.SECURITY_REVIEWER: [verdict_pass()],
        }
    )
    await Orchestrator.resume(rest, target_repo, "t1").run()
    after = read_events(target_repo, "t1")
    assert after[: len(events)] == events
    assert after[len(events)]["message"] == "resuming run t1 at stage CODE"
    assert after[-1] == {**after[-1], "status": "complete", "final": True}


async def test_error_and_budget_events(config: Config, target_repo: Path) -> None:
    await Orchestrator(config, FakeAgentRunner(happy_scripts(), 2.0), target_repo, "w", "b1").run()
    budget = read_events(target_repo, "b1")
    assert budget[-2]["kind"] == "status" and budget[-2]["status"] == "budget_exceeded"

    await Orchestrator(config, FakeAgentRunner({}), target_repo, "w", "e1").run()
    error = read_events(target_repo, "e1")
    assert error[-2]["kind"] == "error" and "AssertionError" in error[-2]["message"]
    assert error[-1]["status"] == "error"


async def test_diff_patch_saved_per_round(config: Config, target_repo: Path) -> None:
    from fakes import IMPL_WRONG, finding, verdict_fail

    scripts = happy_scripts()
    scripts[Role.CODER] = [writes(IMPL_WRONG), writes(IMPL_OK, "{}")]
    scripts[Role.CODE_CRITIC] = [verdict_fail(finding("F1")), verdict_pass()]
    runner = FakeAgentRunner(scripts)
    orch = Orchestrator(config, runner, target_repo, "w", "t1")
    await orch.run()
    root = orch.runlog.root

    tests_patch = (root / "stage-tests/round-1/diff.patch").read_text()
    assert "tests/test_clamp.py" in tests_patch and "task.md" not in tests_patch
    code1 = (root / "stage-code/round-1/diff.patch").read_text()
    code2 = (root / "stage-code/round-2/diff.patch").read_text()
    assert "return x\n" in code1 and "max(lo, min(x, hi))" in code2
    review = (root / "review/diff.patch").read_text()
    assert "tests/test_clamp.py" in review and "def clamp" in review

    # Each file is exactly the diff in that critic's prompt.
    critic_prompts = [c.prompt for c in runner.calls_for(Role.CODE_CRITIC)]
    assert code1 in critic_prompts[0] and code2 in critic_prompts[1]
    (test_critic,) = runner.calls_for(Role.TEST_CRITIC)
    assert tests_patch in test_critic.prompt
    for role in (Role.STANDARDS_REVIEWER, Role.SECURITY_REVIEWER):
        (call,) = runner.calls_for(role)
        assert review in call.prompt
