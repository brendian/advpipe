from __future__ import annotations

from pathlib import Path

import pytest
from fakes import FakeAgentRunner, verdict_pass

from advpipe.budget import Budget, BudgetExceeded
from advpipe.config import Config
from advpipe.models import RunState, Verdict
from advpipe.runlog import RunLog
from advpipe.runner import Role
from advpipe.stages import (
    Context,
    blocking_open_questions,
    call_critic,
    extract_json,
    parse_author_response,
    parse_model,
)
from advpipe.workspace import Workspace


def make_ctx(config: Config, repo: Path, runner: FakeAgentRunner) -> Context:
    ws = Workspace.create(repo, "s1")
    return Context(
        config=config,
        runner=runner,
        workspace=ws,
        budget=Budget(config.limits.budget_usd_per_task),
        runlog=RunLog(repo / ".advpipe" / "runs" / "s1"),
        state=RunState(run_id="s1", work_item="w", repo=str(repo)),
    )


@pytest.mark.parametrize(
    "text",
    [
        '{"verdict": "PASS", "findings": []}',
        'Here you go:\n```json\n{"verdict": "PASS", "findings": []}\n```\n',
        'Sure. {"verdict": "PASS", "findings": []} Done.',
    ],
)
def test_extract_json_variants(text: str) -> None:
    assert parse_model(text, Verdict).verdict == "PASS"


def test_extract_json_passthrough_when_no_object() -> None:
    assert extract_json("no json here") == "no json here"


def test_parse_model_raises_value_error() -> None:
    with pytest.raises(ValueError):
        parse_model('{"verdict": "MAYBE"}', Verdict)


def test_parse_author_response_lenient() -> None:
    assert parse_author_response("not json").responses == []


async def test_critic_retry_then_success(config: Config, target_repo: Path) -> None:
    runner = FakeAgentRunner({Role.CODE_CRITIC: ["garbage", verdict_pass()]})
    ctx = make_ctx(config, target_repo, runner)
    verdict, _ = await call_critic(ctx, Role.CODE_CRITIC, "judge this", "stage-code")
    assert verdict.verdict == "PASS"
    assert len(runner.calls) == 2
    assert "garbage" in runner.calls[1].prompt


async def test_critic_unparseable_twice_is_fail(config: Config, target_repo: Path) -> None:
    runner = FakeAgentRunner({Role.CODE_CRITIC: ["garbage", "more garbage"]})
    ctx = make_ctx(config, target_repo, runner)
    verdict, raw = await call_critic(ctx, Role.CODE_CRITIC, "judge this", "stage-code")
    assert verdict.verdict == "FAIL"
    (f,) = verdict.blocking
    assert f.claim == "critic output unparseable"
    assert raw == "more garbage"


async def test_budget_checked_after_each_call(config: Config, target_repo: Path) -> None:
    runner = FakeAgentRunner({Role.CODE_CRITIC: ["garbage", verdict_pass()]}, cost_per_call=3.0)
    ctx = make_ctx(config, target_repo, runner)
    with pytest.raises(BudgetExceeded):
        await call_critic(ctx, Role.CODE_CRITIC, "judge", "stage-code")
    assert ctx.state.cost_usd == 6.0
    assert ctx.budget.by_stage["stage-code"] == 6.0


def test_agent_requests_carry_role_settings(config: Config, target_repo: Path) -> None:
    from advpipe.runner import build_request

    critic = build_request(Role.CODE_CRITIC, "p", config, target_repo, 1.5)
    assert critic.model == "claude-sonnet-5-5"
    assert critic.tools == ["Read", "Glob", "Grep"]
    assert critic.max_budget_usd == 1.5
    assert "code critic" in critic.system_prompt
    coder = build_request(Role.CODER, "p", config, target_repo, None)
    assert coder.model == "claude-opus-5-5"
    assert "Edit" in coder.tools and "Bash" in coder.tools


def test_blocking_open_questions() -> None:
    md = """# Task: x

## Open questions
- BLOCKING: which API version?
- **BLOCKING:** who owns the data?
- Chose default: raise on empty (not blocking: just noting)

## Other
- BLOCKING: not in the section
"""
    assert blocking_open_questions(md) == ["which API version?", "who owns the data?"]
    assert blocking_open_questions("## Open questions\n- None\n") == []
    assert blocking_open_questions("no section") == []
