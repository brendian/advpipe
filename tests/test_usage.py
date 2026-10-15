"""Subscription usage, the login, and pausing at a limit (docs/LEVELS_PLAN.md §3.15 and the
``usage_limit`` decision point of §3.5, milestone L6)."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import claude_agent_sdk as sdk
import pytest
from conftest import git
from fakes import (
    IMPL_OK,
    TASK_MD,
    TASK_MD_BLOCKING,
    TEST_CLAMP,
    FakeAgentRunner,
    Step,
    finding,
    happy_scripts,
    limit_hit,
    rulings,
    said,
    snap,
    verdict_fail,
    verdict_pass,
    writes,
)
from test_answers import TASK_MD_REVISED, after_answers
from test_bypass import AGENT_ANSWER, bypass_reply
from test_decisions import NOTE, fixed_code, red_code
from typer.testing import CliRunner

import advpipe.cli as cli_module
from advpipe import decisions, usage
from advpipe.answers import record_answers
from advpipe.cli import app
from advpipe.config import Config, Limits
from advpipe.control import ControlError, clean_run
from advpipe.decisions import DecisionError, decide
from advpipe.models import RateLimitSnapshot, RunState, Stage, Status
from advpipe.orchestrator import NotResumable, Orchestrator, check_resumable
from advpipe.runlog import RunLog
from advpipe.runner import (
    AgentRequest,
    AgentResult,
    Role,
    SdkAgentRunner,
    UsageLimitReached,
    build_request,
)
from advpipe.stages import CUT_OFF_NOTE

cli = CliRunner()


def logdir(repo: Path) -> Path:
    return RunLog.for_run(repo, "r1").root


def state_of(repo: Path) -> RunState:
    return RunLog.for_run(repo, "r1").read_state()


def events(repo: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in (logdir(repo) / "events.jsonl").read_text().splitlines()]


async def go(
    config: Config,
    repo: Path,
    scripts: dict[Role, list[Step]],
    *,
    auth: str = "unknown",
    progress: Callable[[str], None] | None = None,
    console_warned: bool = False,
) -> tuple[RunState, FakeAgentRunner]:
    runner = FakeAgentRunner(scripts, auth=auth)  # type: ignore[arg-type]
    orch = Orchestrator(
        config,
        runner,
        repo,
        "add clamp",
        "r1",
        name="r1",
        progress=progress,
        console_warned=console_warned,
    )
    return await orch.run(), runner


async def carry_on(
    repo: Path, scripts: dict[Role, list[Step]], config: Config | None = None
) -> tuple[RunState, FakeAgentRunner]:
    runner = FakeAgentRunner(scripts)
    orch = Orchestrator.resume(runner, repo, "r1", config=config)
    return await orch.run(), runner


def writes_said(files: dict[str, str], **kw: Any) -> Step:
    inner = writes(files)
    return lambda req: said(inner(req), **kw)


def partial(path: str = "mathutils/core.py") -> dict[str, str]:
    return {path: "# half-written when the limit hit\n"}


def paused(state: RunState) -> Any:
    d = state.decision
    assert d is not None and d.kind == "usage_limit"
    assert state.status is Status.NEEDS_HUMAN and decisions.awaiting_decision(state)
    return d


# --------------------------------------------------------------------------- the login


def test_login_detection_follows_the_fallbacks_in_order() -> None:
    assert usage.detect_login("oauth", False, {}) == "subscription"
    for source in ("ANTHROPIC_API_KEY", "apiKeyHelper", "/login managed key", "user", "org"):
        assert usage.detect_login(source, False, {}) == "console"
    # apiKeySource first: it beats a rate-limit event and the environment.
    assert usage.detect_login("oauth", False, {"ANTHROPIC_API_KEY": "k"}) == "subscription"
    assert usage.detect_login("user", True, {}) == "console"
    # "none" and "temporary" say nothing about the login: the next fallback decides.
    assert usage.detect_login("none", True, {}) == "subscription"
    assert usage.detect_login("none", False, {"ANTHROPIC_API_KEY": "k"}) == "console"
    assert usage.detect_login(None, True, {"ANTHROPIC_API_KEY": "k"}) == "subscription"
    assert usage.detect_login(None, False, {"ANTHROPIC_API_KEY": "k"}) == "console"


def test_silence_is_unknown_never_a_guess() -> None:
    assert usage.detect_login(None, False, {}) == "unknown"
    assert usage.detect_login("none", False, {}) == "unknown"
    assert usage.detect_login("temporary", False, {}) == "unknown"
    assert usage.detect_login(42, False, {}) == "unknown"
    assert "API-equivalent if you're on a subscription" in usage.cost_label("unknown")
    assert usage.cost_label("console") == "real money"


# --------------------------------------------------------------------------- the SDK stream


def sdk_stream(*messages: Any) -> Callable[..., AsyncIterator[Any]]:
    async def query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        for m in messages:
            yield m

    return query


def init(source: str | None = "oauth") -> Any:
    return sdk.SystemMessage(subtype="init", data={"apiKeySource": source} if source else {})


def limit_event(
    status: str = "allowed",
    window: str = "five_hour",
    utilization: float = 0.62,
    overage: str | None = None,
) -> Any:
    when = int((datetime.now(UTC) + timedelta(hours=1)).timestamp())
    return sdk.RateLimitEvent(
        rate_limit_info=sdk.RateLimitInfo(
            status=status,  # type: ignore[arg-type]
            resets_at=when,
            rate_limit_type=window,  # type: ignore[arg-type]
            utilization=utilization,
            overage_status=overage,  # type: ignore[arg-type]
        ),
        uuid="u",
        session_id="s",
    )


def assistant(text: str = "hello", error: str | None = None) -> Any:
    return sdk.AssistantMessage(
        content=[sdk.TextBlock(text=text)],
        model="m",
        error=error,  # type: ignore[arg-type]
    )


def result(cost: float | None = 0.5, is_error: bool = False, status: int | None = None) -> Any:
    return sdk.ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="s",
        total_cost_usd=cost,
        result="final text",
        api_error_status=status,
    )


def sdk_request(tmp_path: Path) -> AgentRequest:
    return build_request(Role.CODER, "do it", Config(), tmp_path, None)


async def run_sdk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *messages: Any) -> AgentResult:
    monkeypatch.setattr(sdk, "query", sdk_stream(*messages))
    return await SdkAgentRunner().run(sdk_request(tmp_path))


async def test_sdk_runner_reports_login_and_snapshots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    res = await run_sdk(
        monkeypatch,
        tmp_path,
        init("oauth"),
        limit_event("allowed"),
        limit_event("allowed_warning", "seven_day", 0.85),
        assistant(),
        result(0.5),
    )
    assert (res.auth, res.cost_usd, res.text) == ("subscription", 0.5, "final text")
    assert [(s.window, s.status, s.utilization) for s in res.rate_limits] == [
        ("five_hour", "allowed", 0.62),
        ("seven_day", "allowed_warning", 0.85),
    ]
    assert res.rate_limits[0].resets_at is not None


async def test_sdk_runner_console_login_from_the_init_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    res = await run_sdk(monkeypatch, tmp_path, init("ANTHROPIC_API_KEY"), assistant(), result())
    assert res.auth == "console" and res.rate_limits == ()
    res = await run_sdk(monkeypatch, tmp_path, assistant(), result())
    assert res.auth == "unknown"  # no init, no events, no key in the environment


async def test_sdk_runner_a_rejected_window_and_a_failed_call_is_a_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(UsageLimitReached) as caught:
        await run_sdk(
            monkeypatch,
            tmp_path,
            init("oauth"),
            limit_event("rejected", "five_hour", 1.0),
            assistant("limit reached", error="rate_limit"),
            result(0.3, is_error=True),
        )
    e = caught.value
    assert not e.billing and e.cost_usd == 0.3 and e.auth == "subscription"
    assert e.snapshot is not None and e.snapshot.window == "five_hour"
    assert e.snapshot.status == "rejected" and e.rate_limits == (e.snapshot,)


async def test_sdk_runner_the_stream_may_end_without_a_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Not the "no result" error: a limit.
    with pytest.raises(UsageLimitReached) as caught:
        await run_sdk(monkeypatch, tmp_path, init(), limit_event("rejected", "seven_day", 1.0))
    assert caught.value.snapshot is not None and caught.value.snapshot.window == "seven_day"
    assert caught.value.cost_usd == 0.0
    with pytest.raises(UsageLimitReached):
        await run_sdk(monkeypatch, tmp_path, init(), assistant("x", error="rate_limit"))
    # Without a limit signal it stays the old "no result" error result.
    res = await run_sdk(monkeypatch, tmp_path, init(), assistant("partial"))
    assert res.is_error and res.text == "partial"


async def test_sdk_runner_billing_error_is_the_credit_balance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(UsageLimitReached) as caught:
        await run_sdk(
            monkeypatch,
            tmp_path,
            init("ANTHROPIC_API_KEY"),
            assistant("credit balance too low", error="billing_error"),
            result(0.0, is_error=True),
        )
    e = caught.value
    assert e.billing and e.snapshot is None and e.auth == "console"


async def test_sdk_runner_http_429_is_a_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(UsageLimitReached):
        await run_sdk(monkeypatch, tmp_path, init("user"), assistant(), result(0.1, True, 429))


async def test_sdk_runner_a_rejected_event_the_call_survived_is_not_a_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Extra usage was allowed, so the call went through: record it, don't pause.
    res = await run_sdk(
        monkeypatch,
        tmp_path,
        init(),
        limit_event("rejected", "five_hour", 1.0, overage="allowed"),
        assistant(),
        result(0.2),
    )
    assert not res.is_error and res.rate_limits[0].status == "rejected"
    # And a result that isn't an error wins over a stale rejected event.
    res = await run_sdk(
        monkeypatch, tmp_path, init(), limit_event("rejected"), assistant(), result(0.2)
    )
    assert not res.is_error


async def test_a_plain_error_result_is_not_a_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    res = await run_sdk(monkeypatch, tmp_path, init(), assistant(), result(0.1, True, 500))
    assert res.is_error  # unchanged: reported as an agent error, not a pause


# --------------------------------------------------------------------------- recording


async def test_snapshots_land_in_run_json_the_repo_cache_and_events(
    config: Config, target_repo: Path
) -> None:
    scripts = happy_scripts()
    scripts[Role.SPEC_WRITER] = [
        writes_said(
            {"task.md": TASK_MD}, auth="subscription", snapshots=(snap("five_hour", "allowed"),)
        )
    ]
    scripts[Role.CODER] = [
        writes_said(
            IMPL_OK,
            auth="subscription",
            snapshots=(
                snap("five_hour", "allowed_warning", 0.85),
                snap("seven_day", "allowed", 0.3),
            ),
        )
    ]
    state, _ = await go(config, target_repo, scripts)

    assert state.status is Status.COMPLETE and state.auth == "subscription"
    assert state.rate_limits["five_hour"].utilization == 0.85  # the latest per window
    assert state.rate_limits["seven_day"].utilization == 0.3
    cache = usage.load_cache(target_repo)
    assert cache.login == "subscription" and set(cache.windows) == {"five_hour", "seven_day"}
    assert cache.windows["five_hour"].status == "allowed_warning"
    assert (target_repo / ".advpipe" / "usage.json").is_file()
    seen = [e for e in events(target_repo) if e["kind"] == "usage"]
    assert [e["window"] for e in seen] == ["five_hour", "five_hour", "seven_day"]
    assert seen[1]["status"] == "allowed_warning" and seen[1]["utilization"] == 0.85
    # allowed_warning adds a note and the run carries on.
    assert any("pproaching the 5-hour limit" in n for n in state.notes)
    report = (logdir(target_repo) / "report.md").read_text()
    assert "## Login and usage" in report and "**Login:** Claude subscription" in report
    assert "5-hour: 85% used" in report and "API-equivalent estimates" in report


async def test_a_run_that_never_learns_its_login_reads_as_before(
    config: Config, target_repo: Path
) -> None:
    state, _ = await go(config, target_repo, happy_scripts())
    assert state.status is Status.COMPLETE and state.auth == "unknown"
    assert state.rate_limits == {}
    assert not (target_repo / ".advpipe" / "usage.json").exists()
    assert "Login and usage" not in (logdir(target_repo) / "report.md").read_text()
    assert not [e for e in events(target_repo) if e["kind"] == "usage"]


async def test_snapshots_never_change_the_budget_logic(config: Config, target_repo: Path) -> None:
    tight = config.model_copy(update={"limits": Limits(budget_usd_per_task=0.025)})
    runner = FakeAgentRunner(happy_scripts(), cost_per_call=0.01, auth="subscription")
    state = await Orchestrator(tight, runner, target_repo, "add clamp", "r1", name="r1").run()
    assert state.status is Status.BUDGET_EXCEEDED  # the cap holds with a subscription login


def test_stale_snapshots_are_shown_as_unknown() -> None:
    now = datetime.now(UTC)
    fresh = snap("five_hour", "allowed", 0.62, resets_in_s=3600)
    gone = RateLimitSnapshot(
        window="five_hour",
        status="allowed",
        utilization=0.9,
        resets_at=now - timedelta(minutes=5),
        seen_at=now - timedelta(hours=6),
    )
    assert not usage.is_stale(fresh) and usage.is_stale(gone)
    assert usage.describe(fresh).startswith("5-hour: 62% used, resets ")
    assert usage.describe(gone) == "5-hour: reset since; usage unknown"
    assert usage.age(gone) == "6 h ago" and usage.age(fresh) == "just now"
    # A reset window can't make the page warn.
    stale_rejected = gone.model_copy(update={"status": "rejected"})
    assert usage.worst_status({"five_hour": stale_rejected}) == "allowed"
    assert usage.worst_status({"five_hour": snap(status="rejected")}) == "rejected"


def test_the_repo_cache_is_atomic_and_forgiving(tmp_path: Path) -> None:
    assert usage.load_cache(tmp_path).login == "unknown"
    usage.record(tmp_path, "unknown", [])  # nothing to say: nothing written
    assert not (tmp_path / ".advpipe").exists()
    usage.record(tmp_path, "subscription", [snap("five_hour", utilization=0.1)])
    usage.record(tmp_path, "unknown", [snap("seven_day", utilization=0.2)])
    cache = usage.load_cache(tmp_path)
    assert cache.login == "subscription"  # an unknown call doesn't forget the login
    assert set(cache.windows) == {"five_hour", "seven_day"}
    assert not list((tmp_path / ".advpipe").glob(".*tmp"))
    (tmp_path / ".advpipe" / "usage.json").write_text("not json")
    assert usage.load_cache(tmp_path).windows == {}  # unreadable: empty, never an error


# --------------------------------------------------------------------------- pausing


@pytest.mark.parametrize(
    ("role", "scripts_for", "stage"),
    [
        (Role.SPEC_WRITER, lambda s: s.update({Role.SPEC_WRITER: [limit_hit()]}), Stage.SPEC),
        (Role.TEST_CRITIC, lambda s: s.update({Role.TEST_CRITIC: [limit_hit()]}), Stage.TESTS),
        (Role.CODER, lambda s: s.update({Role.CODER: [limit_hit(files=partial())]}), Stage.CODE),
        (Role.CODE_CRITIC, lambda s: s.update({Role.CODE_CRITIC: [limit_hit()]}), Stage.CODE),
    ],
)
async def test_a_limit_pauses_the_run_from_any_stage(
    config: Config,
    target_repo: Path,
    role: Role,
    scripts_for: Callable[[dict[Role, list[Step]]], None],
    stage: Stage,
) -> None:
    scripts = happy_scripts()
    scripts_for(scripts)
    state, _ = await go(config, target_repo, scripts)

    d = paused(state)
    assert d.stage is stage and state.status is not Status.ERROR
    assert "5-hour usage limit was reached" in d.explanation
    assert f"during the {stage.value.lower()} step" in d.explanation
    assert usage.format_reset(d.usage.resets_at) in d.explanation  # when it resets
    assert usage.ADD_USAGE_URL in d.explanation and "Try again" in d.explanation
    assert d.usage is not None and d.usage.status == "rejected" and not d.billing
    # What the interrupted call cost still counts.
    assert state.cost_usd == pytest.approx(
        0.02 + 0.01 * state.calls_by_stage.get("x", 0) + _prior(state)
    )
    assert state.rate_limits["five_hour"].status == "rejected"
    report = (logdir(target_repo) / "report.md").read_text()
    assert "advpipe decide r1 retry | finish" in report and "## Decisions" in report
    (ev,) = [e for e in events(target_repo) if e["kind"] == "decision"]
    assert ev["decision_kind"] == "usage_limit" and ev["choices"] == ["retry", "finish"]
    with pytest.raises(NotResumable, match="waiting for a decision: advpipe decide r1 retry"):
        check_resumable(target_repo, "r1")
    if stage is Stage.CODE and role is Role.CODER:
        wt = Path(state.worktree)
        assert (wt / "mathutils/core.py").read_text() == partial()["mathutils/core.py"]


def _prior(state: RunState) -> float:
    """The cost of the successful calls before the limit (0.01 each); the call that hit the
    limit cost 0.02 and is added on top."""
    return 0.01 * (sum(state.calls_by_stage.values()) - 1)


async def test_a_limit_during_review_leaves_nothing_running(
    config: Config, target_repo: Path
) -> None:
    scripts = happy_scripts()
    scripts[Role.STANDARDS_REVIEWER] = [limit_hit()]
    state, runner = await go(config, target_repo, scripts)
    paused(state)
    assert state.decision is not None and state.decision.stage is Stage.REVIEW
    # Both reviewers ran to the end, and both were paid for.
    assert len(runner.calls_for(Role.STANDARDS_REVIEWER)) == 1
    assert len(runner.calls_for(Role.SECURITY_REVIEWER)) == 1
    assert state.calls_by_stage["review"] == 2
    await asyncio.sleep(0)  # nothing is left pending in the background
    assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]


async def test_a_limit_during_the_arbiter(config: Config, target_repo: Path) -> None:
    scripts = happy_scripts()
    scripts[Role.CODER] = [writes(IMPL_OK), "{}", "{}"]
    scripts[Role.CODE_CRITIC] = [verdict_fail(finding("F1", line=n)) for n in (1, 2, 3)]
    scripts[Role.ARBITER] = [limit_hit()]
    state, _ = await go(config, target_repo, scripts)
    d = paused(state)
    assert d.stage is Stage.ARBITER and "during the arbiter step" in d.explanation
    assert state.rounds_used["code"] == 3


async def test_running_out_of_credits_is_its_own_explanation(
    config: Config, target_repo: Path
) -> None:
    scripts = happy_scripts()
    scripts[Role.CODER] = [limit_hit(billing=True, files=partial())]
    state, _ = await go(config, target_repo, scripts)
    d = paused(state)
    assert d.billing and d.usage is None
    assert "Console credit balance ran out during the code step" in d.explanation
    assert usage.ADD_CREDITS_URL in d.explanation and "5-hour" not in d.explanation
    assert state.auth == "console"


async def test_a_limit_with_no_details_still_pauses(config: Config, target_repo: Path) -> None:
    scripts = happy_scripts()
    scripts[Role.SPEC_WRITER] = [limit_hit(window=None)]
    state, _ = await go(config, target_repo, scripts)
    d = paused(state)
    assert d.usage is None and not d.billing
    assert "no details were given" in d.explanation and usage.ADD_USAGE_URL in d.explanation


# --------------------------------------------------------------------------- retry


async def test_retry_starts_the_step_again_with_its_partial_work_kept(
    config: Config, target_repo: Path
) -> None:
    scripts = happy_scripts()
    scripts[Role.CODER] = [limit_hit(files=partial())]
    state, _ = await go(config, target_repo, scripts)
    paused(state)
    spent = state.cost_usd

    seen: dict[str, Any] = {}

    def coder(req: AgentRequest) -> str:
        seen["partial"] = (req.cwd / "mathutils/core.py").read_text()
        seen["prompt"] = req.prompt
        return writes(IMPL_OK, "carried on")(req)

    decide(target_repo, "r1", "retry")
    assert decisions.decision_ready(state_of(target_repo))
    resumed = happy_scripts()
    resumed.update({Role.CODER: [coder]})
    for gone in (Role.SPEC_WRITER, Role.TEST_AUTHOR, Role.TEST_CRITIC):
        resumed.pop(gone)
    final, _ = await carry_on(target_repo, resumed)

    assert final.status is Status.COMPLETE
    assert seen["partial"] == partial()["mathutils/core.py"]  # kept, not reset
    assert seen["prompt"].startswith(CUT_OFF_NOTE)  # the author is told it was cut off
    assert final.cost_usd > spent  # one budget across the pause
    d = final.decisions[0]
    assert d.choice == "retry" and d.applied
    assert any(e["kind"] == "decision" and e.get("choice") == "retry" for e in events(target_repo))


async def test_a_retry_that_is_still_limited_pauses_again_without_waiting(
    config: Config, target_repo: Path
) -> None:
    scripts = happy_scripts()
    scripts[Role.CODER] = [limit_hit(files=partial())]
    await go(config, target_repo, scripts)
    decide(target_repo, "r1", "retry")
    again, _ = await carry_on(target_repo, {Role.CODER: [limit_hit(files=partial(), cost=0.03)]})
    assert again.status is Status.NEEDS_HUMAN and len(again.decisions) == 2
    assert again.decisions[0].applied and not again.decisions[1].choice
    assert decisions.awaiting_decision(again)
    assert again.cost_usd == pytest.approx(0.0 + 0.01 * 3 + 0.02 + 0.03, abs=0.011)
    # Nothing retried on its own: a plain resume refuses until a person decides.
    with pytest.raises(NotResumable, match="waiting for a decision"):
        check_resumable(target_repo, "r1")


async def test_a_fix_pass_cut_short_by_a_limit_is_still_the_persons_choice(
    config: Config, target_repo: Path
) -> None:
    from test_decisions import start

    blocked = await start(config, target_repo, red_code())
    assert blocked.decision is not None and blocked.decision.kind == "red_checks"
    decide(target_repo, "r1", "fix", NOTE)
    cut, _ = await carry_on(target_repo, {Role.CODER: [limit_hit(files=partial())]})
    assert paused(cut) and cut.fix_passes == 0  # the pass didn't finish, so it isn't counted
    decide(target_repo, "r1", "retry")
    fixes: list[str] = []

    def fixer(req: AgentRequest) -> str:
        fixes.append(req.prompt)
        return writes(IMPL_OK, "fixed")(req)

    done, _ = await carry_on(
        target_repo,
        {
            Role.CODER: [fixer],
            Role.STANDARDS_REVIEWER: [verdict_pass()],
            Role.SECURITY_REVIEWER: [verdict_pass()],
        },
    )
    assert done.status is Status.COMPLETE and done.fix_passes == 1
    (prompt,) = fixes
    assert NOTE in prompt and CUT_OFF_NOTE in prompt  # the person's note, and the cut-off line


async def test_bypass_calls_never_carry_the_cut_off_note(config: Config, target_repo: Path) -> None:
    from test_answers import blocked_scripts

    runner = FakeAgentRunner(blocked_scripts(TASK_MD_BLOCKING))
    state = await Orchestrator(config, runner, target_repo, "add clamp", "r1", name="r1").run()
    assert state.status is Status.NEEDS_HUMAN
    record_answers(target_repo, "r1", {}, {"Q1"})

    cut, _ = await carry_on(target_repo, {Role.SPEC_WRITER: [limit_hit()]})
    assert paused(cut)
    decide(target_repo, "r1", "retry")
    scripts = after_answers(TASK_MD_REVISED)
    scripts[Role.SPEC_WRITER] = [
        bypass_reply("Q1"),
        writes({"task.md": TASK_MD_REVISED}, "revised"),
    ]
    done, runner2 = await carry_on(target_repo, scripts)
    assert done.status is Status.COMPLETE
    bypass_call, revision = runner2.calls_for(Role.SPEC_WRITER)
    assert CUT_OFF_NOTE not in bypass_call.prompt  # it mustn't carry on partial work
    assert CUT_OFF_NOTE in revision.prompt
    task = (logdir(target_repo) / "task.md").read_text()
    assert "agent's best judgement" in task and AGENT_ANSWER in task


# --------------------------------------------------------------------------- finish


async def test_finish_ends_the_run_as_it_is(config: Config, target_repo: Path) -> None:
    scripts = happy_scripts()
    scripts[Role.CODER] = [limit_hit(files=partial())]
    paused_state, _ = await go(config, target_repo, scripts)
    wt = Path(paused_state.worktree)
    commits_before = git(wt, "rev-list", "--count", "HEAD").strip()

    state = decide(target_repo, "r1", "finish")

    assert state.ended_by == "finish" and state.status is Status.NEEDS_HUMAN
    assert "code" not in state.commits  # nothing committed
    assert git(wt, "rev-list", "--count", "HEAD").strip() == commits_before
    assert (wt / "mathutils/core.py").read_text() == partial()["mathutils/core.py"]
    assert git(wt, "status", "--porcelain").strip()  # the work is there, uncommitted
    assert not state.worktree_removed
    with pytest.raises(NotResumable, match="finished as it was"):
        check_resumable(target_repo, "r1")
    with pytest.raises(DecisionError, match="finished as it was"):
        decide(target_repo, "r1", "retry")
    report = (logdir(target_repo) / "report.md").read_text()
    assert "Finished as is at CODE; the uncommitted work is in" in report
    assert any("Finished as is at CODE" in n for n in state.notes)


async def test_clean_refuses_the_finished_runs_dirty_worktree_without_force(
    config: Config, target_repo: Path
) -> None:
    scripts = happy_scripts()
    scripts[Role.CODER] = [limit_hit(files=partial())]
    s, _ = await go(config, target_repo, scripts)
    decide(target_repo, "r1", "finish")
    with pytest.raises(ControlError, match="uncommitted changes"):
        clean_run(target_repo, "r1")
    assert Path(s.worktree).is_dir()
    clean_run(target_repo, "r1", force=True)
    assert not Path(s.worktree).exists()


async def test_finish_after_the_worktree_was_cleaned_by_hand_still_works(
    config: Config, target_repo: Path
) -> None:
    scripts = happy_scripts()
    scripts[Role.SPEC_WRITER] = [limit_hit()]
    s, _ = await go(config, target_repo, scripts)
    assert decide(target_repo, "r1", "finish").ended_by == "finish"
    clean_run(target_repo, "r1")  # nothing uncommitted of value: allowed
    assert not Path(s.worktree).exists()


def test_the_choices_of_each_kind_are_checked(config: Config, target_repo: Path) -> None:
    asyncio.run(go(config, target_repo, {**happy_scripts(), Role.SPEC_WRITER: [limit_hit()]}))
    for wrong in ("fix", "takeover", "ignore", "begin"):
        with pytest.raises(DecisionError, match="isn't a choice here: choose retry or finish"):
            decide(target_repo, "r1", wrong)
    with pytest.raises(DecisionError, match="a note goes with the fix choice only"):
        decide(target_repo, "r1", "retry", "please hurry")


# --------------------------------------------------------------------------- the terminal


def fake_runners(
    monkeypatch: pytest.MonkeyPatch, *runners: FakeAgentRunner
) -> list[FakeAgentRunner]:
    queue = list(runners)
    monkeypatch.setattr(cli_module, "SdkAgentRunner", lambda: queue.pop(0))
    return queue


def run_args(repo: Path) -> list[str]:
    gate = f'test = [{json.dumps(sys.executable)}, "-m", "pytest", "-q", "-p", "no:cacheprovider"]'
    (repo / "pipeline.toml").write_text(f"[gates]\ntypes = []\nlint = []\nsecurity = []\n{gate}\n")
    return ["run", "add clamp", "--repo", str(repo), "--name", "r1", "-q", "--run-id", "r1"]


def limited_scripts() -> dict[Role, list[Step]]:
    scripts = happy_scripts()
    scripts[Role.CODER] = [limit_hit(files=partial())]
    return scripts


def test_a_foreground_run_asks_what_to_do_at_a_limit(
    target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_runners(monkeypatch, FakeAgentRunner(limited_scripts()))
    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: True)
    result = cli.invoke(app, run_args(target_repo), input="later\n")
    assert result.exit_code == 1, result.output
    assert "Your Claude subscription's 5-hour usage limit was reached" in result.stdout
    assert "retry: Try again" in result.stdout and "finish: Or finish as is" in result.stdout
    assert "decide later with: advpipe decide r1 retry | finish" in result.stdout
    assert decisions.awaiting_decision(state_of(target_repo))


def test_a_foreground_run_can_try_again_at_once(
    target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resumed = {
        **happy_scripts(),
        Role.CODER: [writes(IMPL_OK)],
    }
    for gone in (Role.SPEC_WRITER, Role.TEST_AUTHOR, Role.TEST_CRITIC):
        resumed.pop(gone)
    runner2 = FakeAgentRunner(resumed)
    fake_runners(monkeypatch, FakeAgentRunner(limited_scripts()), runner2)
    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: True)
    result = cli.invoke(app, run_args(target_repo), input="retry\n")
    assert result.exit_code == 0, result.output
    assert "r1  needs_human" in result.stdout and "r1  complete" in result.stdout
    assert CUT_OFF_NOTE in runner2.calls_for(Role.CODER)[0].prompt


def test_a_foreground_run_can_finish_as_is(
    target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_runners(monkeypatch, FakeAgentRunner(limited_scripts()))
    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: True)
    result = cli.invoke(app, run_args(target_repo), input="finish\n")
    assert result.exit_code == 0, result.output
    assert "finished as is: run r1 ends here; nothing was committed" in result.stdout
    assert "git -C " in result.stdout and "status" in result.stdout
    assert "advpipe clean r1" in result.stdout
    status = cli.invoke(app, ["status", "r1", "--repo", str(target_repo)]).stdout
    assert "ended:    finished as is: the uncommitted work is in" in status


def test_decide_retry_through_the_cli_and_detach(
    config: Config, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(go(config, target_repo, limited_scripts()))
    bad = cli.invoke(app, ["decide", "r1", "fix", "--repo", str(target_repo)])
    assert bad.exit_code == 2 and "choose retry or finish" in bad.stderr
    started: list[list[str]] = []
    monkeypatch.setattr(cli_module, "start_detached", lambda log, args: started.append(args) or 1)
    result = cli.invoke(app, ["decide", "r1", "retry", "--detach", "--repo", str(target_repo)])
    assert result.exit_code == 0, result.output
    (args,) = started
    assert args[:2] == ["resume", "r1"] and "--detached-child" in args
    assert decisions.decision_ready(state_of(target_repo))


def test_status_and_summary_say_what_is_waiting(config: Config, target_repo: Path) -> None:
    asyncio.run(go(config, target_repo, limited_scripts(), auth="subscription"))
    out = cli.invoke(app, ["status", "r1", "--repo", str(target_repo)]).stdout
    assert "waiting for a decision:" in out and "5-hour usage limit" in out
    assert "advpipe decide r1 retry" in out and "advpipe decide r1 finish" in out
    assert "login:    Claude subscription" in out and "(API-equivalent)" in out
    assert "usage:    5-hour: limit reached" in out or "usage:    5-hour:" in out


# --------------------------------------------------------------------------- the warning


def warn_args(repo: Path, *more: str) -> list[str]:
    return [*run_args(repo), *more]


def test_console_warning_when_the_environment_says_console(
    target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    fake_runners(monkeypatch, FakeAgentRunner(happy_scripts(), auth="console"))
    result = cli.invoke(app, warn_args(target_repo))
    assert result.exit_code == 0, result.output
    assert "uses Anthropic Console credits, which is real money" in result.stderr
    assert "up to $15.00 (the budget; change it with --budget)" in result.stderr
    assert result.stderr.count("real money") == 1  # the run needn't say it again
    assert "r1  complete  $0.07  " in result.stdout  # a Console summary line keeps its format
    assert "API-equivalent" not in result.stdout
    assert state_of(target_repo).auth == "console"
    report = (logdir(target_repo) / "report.md").read_text()
    assert "**Login:** Anthropic Console credits (real money)" in report


def test_console_warning_from_the_cached_login(
    target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage.record(target_repo, "console", [])
    fake_runners(monkeypatch, FakeAgentRunner(happy_scripts(), auth="console"))
    result = cli.invoke(app, warn_args(target_repo, "--detach"))  # parent prints, child runs
    assert "real money" in result.stderr


def test_no_warning_for_a_subscription_or_an_unknown_login(
    target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage.record(target_repo, "subscription", [snap()])
    fake_runners(monkeypatch, FakeAgentRunner(happy_scripts(), auth="subscription"))
    result = cli.invoke(app, warn_args(target_repo))
    assert result.exit_code == 0 and "real money" not in result.stderr
    assert "r1  complete  $0.07 (API-equivalent)" in result.stdout
    assert "usage, last known" not in result.stdout  # nothing reported in this run
    assert not usage.likely_console(target_repo, {})
    assert not usage.likely_console(target_repo.parent, {})  # silence is not Console


def test_one_warning_for_parallel_runs_with_the_total(
    target_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    fake_runners(monkeypatch, FakeAgentRunner({}), FakeAgentRunner({}))
    items = tmp_path / "items.txt"
    items.write_text("add a\nadd b\n")
    result = cli.invoke(
        app,
        ["run", "--from-file", str(items), "--parallel", "2", "--repo", str(target_repo), "-q"],
    )
    assert result.stderr.count("real money") == 1
    assert "these 2 runs" in result.stderr and "$30.00 in total" in result.stderr


async def test_a_console_login_that_only_the_first_call_reveals(
    config: Config, target_repo: Path
) -> None:
    lines: list[str] = []
    state, _ = await go(config, target_repo, happy_scripts(), auth="console", progress=lines.append)
    assert state.auth == "console"
    assert sum("real money" in line for line in lines) == 1  # printed once, when it turned up
    assert any("Anthropic Console credits" in n for n in state.notes)
    (e,) = [e for e in events(target_repo) if e["kind"] == "usage" and e.get("login")]
    assert e["login"] == "console"


async def test_the_warning_is_not_repeated_when_the_cli_already_gave_it(
    config: Config, target_repo: Path
) -> None:
    lines: list[str] = []
    state, _ = await go(
        config,
        target_repo,
        happy_scripts(),
        auth="console",
        progress=lines.append,
        console_warned=True,
    )
    assert not any("real money" in line for line in lines)
    assert any("Anthropic Console credits" in n for n in state.notes)  # still recorded


async def test_a_subscription_run_is_never_warned(config: Config, target_repo: Path) -> None:
    lines: list[str] = []
    state, _ = await go(
        config, target_repo, happy_scripts(), auth="subscription", progress=lines.append
    )
    assert state.auth == "subscription"
    assert not any("real money" in line for line in lines)
    assert not any("Console" in n for n in state.notes)


# --------------------------------------------------------------------------- the CLI


def test_summary_shows_subscription_usage(
    target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripts = happy_scripts()
    scripts[Role.SPEC_WRITER] = [
        writes_said({"task.md": TASK_MD}, auth="subscription", snapshots=(snap(utilization=0.62),))
    ]
    fake_runners(monkeypatch, FakeAgentRunner(scripts, auth="subscription"))
    result = cli.invoke(app, warn_args(target_repo))
    assert "r1  complete  $0.07 (API-equivalent)" in result.stdout
    assert "usage, last known: 5-hour: 62% used, resets " in result.stdout
    status = cli.invoke(app, ["status", "r1", "--repo", str(target_repo)]).stdout
    assert "cost:     $0.07 (API-equivalent)" in status
    assert "usage:    5-hour: 62% used, resets " in status and "(last known)" in status


def test_advpipe_usage_prints_the_cache_with_ages(target_repo: Path) -> None:
    out = cli.invoke(app, ["usage", "--repo", str(target_repo)]).stdout
    assert "login: not known yet" in out and "usage: not known yet" in out
    old = RateLimitSnapshot(
        window="seven_day",
        status="allowed",
        utilization=0.3,
        resets_at=datetime.now(UTC) - timedelta(hours=1),
        seen_at=datetime.now(UTC) - timedelta(days=2),
    )
    usage.record(target_repo, "subscription", [snap("five_hour", utilization=0.62), old])
    out = cli.invoke(app, ["usage", "--repo", str(target_repo)]).stdout
    assert "login: Claude subscription" in out
    assert "5-hour: 62% used, resets " in out and "(seen just now)" in out
    assert "weekly: reset since; usage unknown  (seen 2 d ago)" in out
    usage.record(target_repo, "console", [])
    assert (
        "login: Anthropic Console credits (real money)"
        in cli.invoke(app, ["usage", "--repo", str(target_repo)]).stdout
    )


def test_explanations_name_the_window_and_the_reset() -> None:
    s = snap("seven_day", "rejected", 1.0, resets_in_s=3 * 86400)
    text = usage.limit_explanation("code", s, False)
    assert "weekly usage limit was reached during the code step" in text
    assert f"resets at {usage.format_reset(s.resets_at)}" in text  # type: ignore[arg-type]
    assert usage.retry_warning(s).startswith(
        "If the limit is still in force, the run will pause again."
    )
    assert usage.format_reset(s.resets_at).split()[0] in {  # type: ignore[arg-type]
        "Mon",
        "Tue",
        "Wed",
        "Thu",
        "Fri",
        "Sat",
        "Sun",
    }
    assert usage.retry_warning(None) == "If the limit is still in force, the run will pause again."


async def test_a_full_run_without_limits_is_unchanged(config: Config, target_repo: Path) -> None:
    state, _ = await go(config, target_repo, happy_scripts())
    assert state.status is Status.COMPLETE and not state.decisions
    assert state.ended_by == ""
    assert TEST_CLAMP and rulings and fixed_code  # helpers other test modules rely on
