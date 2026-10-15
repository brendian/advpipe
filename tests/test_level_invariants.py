"""The invariants of docs/LEVELS_PLAN.md §4 that hold at every level, one test each, run at all
five levels."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import git
from fakes import FakeAgentRunner, Step, happy_scripts, verdict_pass
from test_decisions import red_code
from test_solo import SOLO_OK, SOLO_WRONG, at, start

from advpipe import decisions
from advpipe.config import Config
from advpipe.decisions import DecisionError, decide
from advpipe.levels import LEVEL_ORDER, SHAPES, max_agent_calls, roles_for
from advpipe.models import Level, Status
from advpipe.orchestrator import NotResumable, Orchestrator, check_resumable
from advpipe.runner import Role

ALL = [pytest.param(level, id=level.value) for level in LEVEL_ORDER]


def keep(scripts: dict[Role, list[Step]], level: Level) -> dict[Role, list[Step]]:
    return {role: steps for role, steps in scripts.items() if role in roles_for(level)}


def happy(level: Level) -> dict[Role, list[Step]]:
    if SHAPES[level].solo:
        return keep({Role.SOLO: [SOLO_OK], Role.CODE_CRITIC: [verdict_pass()]}, level)
    return keep(happy_scripts(), level)


def red(level: Level) -> dict[Role, list[Step]]:
    if SHAPES[level].solo:
        return keep({Role.SOLO: [SOLO_WRONG]}, level)
    return keep(red_code(), level)


@pytest.mark.parametrize("level", ALL)
async def test_happy_path_makes_exactly_the_planned_calls_with_the_levels_roles(
    config: Config, target_repo: Path, level: Level
) -> None:
    cfg = at(config, level.value)
    state, runner = await start(cfg, target_repo, happy(level))
    assert state.status is Status.COMPLETE, (state.notes, state.open_findings)
    assert len(runner.calls) == max_agent_calls(level, cfg.limits)[0]
    assert {c.role for c in runner.calls} <= set(roles_for(level))
    assert state.level is level and state.decisions == []


@pytest.mark.parametrize("level", ALL)
async def test_red_checks_stop_and_ask_within_the_worst_case_and_leave_the_worktree(
    config: Config, target_repo: Path, level: Level
) -> None:
    cfg = at(config, level.value)
    state, runner = await start(cfg, target_repo, red(level))

    # Never complete on red checks; the run waits for a person.
    assert state.status is Status.NEEDS_HUMAN and decisions.awaiting_decision(state)
    d = state.decision
    assert d is not None and d.kind == "red_checks" and not d.choice
    assert len(runner.calls) <= max_agent_calls(level, cfg.limits)[1]
    assert {c.role for c in runner.calls} <= set(roles_for(level))
    # The stage that was red isn't committed, and its work is still in the worktree.
    assert "solo" not in state.commits and "code" not in state.commits
    assert git(Path(state.worktree), "status", "--porcelain").strip()
    # Nothing resumes it, and nothing decides for the person.
    with pytest.raises(NotResumable, match="waiting for a decision"):
        check_resumable(target_repo, "r1")
    with pytest.raises(NotResumable, match="waiting for a decision"):
        Orchestrator.resume(FakeAgentRunner({}), target_repo, "r1")
    assert state.fix_passes == 0 and state.ended_by == ""


@pytest.mark.parametrize("level", ALL)
async def test_fix_passes_never_exceed_the_cap_at_any_level(
    config: Config, target_repo: Path, level: Level
) -> None:
    cfg = at(config, level.value)
    cfg = cfg.model_copy(update={"limits": cfg.limits.model_copy(update={"max_fix_passes": 1})})
    await start(cfg, target_repo, red(level))
    author = Role.SOLO if SHAPES[level].solo else Role.CODER
    decide(target_repo, "r1", "fix")
    runner = FakeAgentRunner({author: ["still red"]})
    state = await Orchestrator.resume(runner, target_repo, "r1").run()
    assert state.status is Status.NEEDS_HUMAN and state.fix_passes == 1
    assert [c.role for c in runner.calls] == [author]  # one pass by the stage's author, no critic
    with pytest.raises(DecisionError, match="fix passes"):
        decide(target_repo, "r1", "fix")


@pytest.mark.parametrize("level", [lv for lv in LEVEL_ORDER if SHAPES[lv].review != "none"])
async def test_reviewers_get_artifacts_not_reasoning(
    config: Config, target_repo: Path, level: Level
) -> None:
    state, runner = await start(at(config, level.value), target_repo, happy(level))
    assert state.status is Status.COMPLETE
    judges = [
        c
        for c in runner.calls
        if c.role in (Role.CODE_CRITIC, Role.STANDARDS_REVIEWER, Role.SECURITY_REVIEWER)
    ]
    assert judges
    for call in judges:
        assert "# task.md" in call.prompt and "Gate output" in call.prompt
        assert "diff" in call.prompt.lower() and "AUTHOR-REASONING" not in call.prompt


@pytest.mark.parametrize("level", [lv for lv in LEVEL_ORDER if not SHAPES[lv].solo])
async def test_the_test_author_sees_task_md_and_no_implementation(
    config: Config, target_repo: Path, level: Level
) -> None:
    state, runner = await start(at(config, level.value), target_repo, happy(level))
    assert state.status is Status.COMPLETE
    prompt = runner.calls_for(Role.TEST_AUTHOR)[0].prompt
    assert "# task.md" in prompt and "def clamp" not in prompt
    assert "AUTHOR-REASONING" not in prompt
    # The tests were written before any code.
    order = [c.role for c in runner.calls]
    assert order.index(Role.TEST_AUTHOR) < order.index(Role.CODER)


@pytest.mark.parametrize("level", ALL)
async def test_only_a_person_can_decide(config: Config, target_repo: Path, level: Level) -> None:
    """No agent output sets a decision: a scripted reply that says 'takeover' changes nothing."""
    scripts = red(level)
    for role in scripts:
        scripts[role] = [*scripts[role], *["decide: takeover"] * 4]
    state, _ = await start(at(config, level.value), target_repo, scripts)
    assert state.decision is not None and not state.decision.choice and not state.ended_by
