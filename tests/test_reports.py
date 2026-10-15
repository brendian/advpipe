"""The report, `advpipe status` and the Claude Code skill (docs/LEVELS_PLAN.md §3.12 and §3.14,
milestone L4): level, scan and decision text for every level and outcome."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fakes import happy_scripts
from test_decisions import (
    SCAN_BAD_TXT,
    at_the_cap,
    bad_scripts,
    carry_on,
    fixed_code,
    logdir,
    red_code,
    scan_cfg,
    start,
    state_of,
)
from typer.testing import CliRunner

from advpipe.cli import app
from advpipe.config import Config
from advpipe.decisions import decide
from advpipe.levels import LEVEL_ORDER, SHAPES, dropped_safeguards, level_number, roles_for
from advpipe.models import Decision, Level, OpenQuestion, RunState, Stage, Status
from advpipe.runlog import render_report
from advpipe.runner import Role

cli = CliRunner()
ROOT = Path(__file__).resolve().parent.parent
SKILL = (ROOT / ".claude" / "skills" / "adversarial-build" / "SKILL.md").read_text()


def report_of(repo: Path) -> str:
    return (logdir(repo) / "report.md").read_text()


def status_of(repo: Path) -> str:
    return cli.invoke(app, ["status", "r1", "--repo", str(repo)]).stdout


def section(report: str, heading: str) -> str:
    """The text of one ``## heading`` section."""
    head = report.split(f"## {heading}\n", 1)[1]
    return head.split("\n## ", 1)[0]


# --------------------------------------------------------------------------- level lines


@pytest.mark.parametrize("level", LEVEL_ORDER)
@pytest.mark.parametrize("scan", [True, False])
def test_report_names_the_level_scan_and_what_is_not_done(level: Level, scan: bool) -> None:
    state = RunState(run_id="r", work_item="w", repo="/x", level=level, security_scan=scan)
    report = render_report(state)
    if level is Level.FULL and scan:
        assert "- **Level:** full\n" in report and "Not done at this level" not in report
        return
    agents = len(roles_for(level))
    assert f"({level_number(level)} of 5): {agents} agent" in report
    assert f"security scan {'on' if scan else 'off'}" in report
    assert "Not done at this level" in report
    for line in dropped_safeguards(level, scan):
        assert f"  - {line}" in report


def test_every_level_has_a_distinct_dropped_list_except_full() -> None:
    lists = {level: dropped_safeguards(level, SHAPES[level].security_scan) for level in LEVEL_ORDER}
    assert lists[Level.FULL] == []
    assert all(lists[level] for level in LEVEL_ORDER if level is not Level.FULL)


# --------------------------------------------------------------------------- decisions


async def test_report_and_status_for_a_waiting_decision(config: Config, target_repo: Path) -> None:
    await start(config, target_repo, red_code())
    decisions_text = section(report_of(target_repo), "Decisions")
    assert "**1. red_checks** at CODE" in decisions_text
    assert "(waiting for you: advpipe decide r1 fix | takeover)" in decisions_text
    assert "You chose" not in decisions_text
    out = status_of(target_repo)
    assert "waiting for a decision:" in out and "advpipe decide r1 takeover" in out
    assert "decisions:" not in out  # the open one is already shown above, in full


async def test_report_after_a_fix_with_a_note_and_a_second_stop(
    config: Config, target_repo: Path
) -> None:
    await start(config, target_repo, red_code())
    decide(target_repo, "r1", "fix", note="keep the public\nsignature")
    assert "**You chose:** fix (not acted on yet)" in report_of(target_repo)
    state, _ = await carry_on(target_repo, fixed_code())
    assert state.status is Status.COMPLETE
    d = section(report_of(target_repo), "Decisions")
    assert "**You chose:** fix" in d and "not acted on" not in d
    assert "**Your note:** keep the public\n    signature" in d
    assert "Fix passes you asked for: 1" in d
    out = status_of(target_repo)
    assert "1. red_checks at CODE: you chose fix (note: keep the public)" in out
    assert "1 fix pass(es) you asked for" in out


async def test_report_after_a_takeover(config: Config, target_repo: Path) -> None:
    await start(config, target_repo, red_code())
    decide(target_repo, "r1", "takeover")
    d = section(report_of(target_repo), "Decisions")
    assert "**You chose:** takeover" in d
    assert "Handed over to you at CODE; the branch has the work as it was." in d
    assert "waiting for you" not in d
    assert "handed over to you: git switch advpipe/r1" in status_of(target_repo)


async def test_report_for_scan_hits_ignored_and_blocked(config: Config, target_repo: Path) -> None:
    cfg = scan_cfg(config, SCAN_BAD_TXT)
    await start(cfg, target_repo, bad_scripts())
    d = section(report_of(target_repo), "Decisions")
    assert "**1. scan_hits** at CODE" in d and "advpipe decide r1 ignore | block" in d
    assert "Scan hits" not in report_of(target_repo)  # the policy is still "ask"

    decide(target_repo, "r1", "ignore")
    state, _ = await carry_on(target_repo, {Role.CODE_CRITIC: happy_scripts()[Role.CODE_CRITIC]})
    assert state.status is Status.COMPLETE
    report = report_of(target_repo)
    assert "**You chose:** ignore" in section(report, "Decisions")
    assert "Security scan reported issues; ignored by you" in section(
        report, "Minor findings (not fixed)"
    )
    assert "- **Scan hits:** security scan hits ignored" in report and "your choice" in report
    out = status_of(target_repo)
    assert "scan:     security scan hits ignored" in out and "your choice" in out
    assert "ignored:  1 security scan finding(s), ignored by you" in out


async def test_scan_policy_chosen_up_front_is_said_so(config: Config, target_repo: Path) -> None:
    cfg = scan_cfg(config, SCAN_BAD_TXT, scan_failures="ignore")
    scripts = bad_scripts()
    state = await start(cfg, target_repo, scripts)
    assert state.status is Status.COMPLETE and not state.decisions
    assert "treated" not in report_of(target_repo)
    assert "ignored (recorded as minor findings" in report_of(target_repo)
    assert "chosen up front" in report_of(target_repo)
    assert "decisions:" not in status_of(target_repo)  # nothing was decided


async def test_report_for_begin_work_and_stop(config: Config, target_repo: Path) -> None:
    await at_the_cap(config, target_repo)
    d = section(report_of(target_repo), "Decisions")
    assert "**1. spec_cap** at SPEC" in d and "Begin work anyway?" in d
    assert "advpipe decide r1 begin | stop" in d
    decide(target_repo, "r1", "stop")
    d = section(report_of(target_repo), "Decisions")
    assert "**You chose:** stop" in d
    assert "Stopped at your choice at SPEC; nothing was committed." in d


async def test_decision_files_are_written_as_asked_and_as_decided(
    config: Config, target_repo: Path
) -> None:
    await start(config, target_repo, red_code())
    path = logdir(target_repo) / "decisions" / "1.json"
    asked = json.loads(path.read_text())
    assert asked["kind"] == "red_checks" and asked["choice"] == "" and asked["findings"]
    decide(target_repo, "r1", "fix", note="try again")
    decided = json.loads(path.read_text())
    assert decided["choice"] == "fix" and decided["note"] == "try again"
    assert decided["decided_at"] is not None
    assert not (logdir(target_repo) / "decisions" / "2.json").exists()
    await carry_on(target_repo, red_code(cap=1))
    assert len(state_of(target_repo).decisions) == 2
    assert json.loads((logdir(target_repo) / "decisions" / "2.json").read_text())["choice"] == ""


# --------------------------------------------------------------------------- answers


def test_agent_answers_are_marked_in_the_report() -> None:
    state = RunState(
        run_id="r",
        work_item="w",
        repo="/x",
        spec_passes=2,
        questions=[
            OpenQuestion(id="Q1", text="Round how?", asked_in=1, answer="half-up"),
            OpenQuestion(
                id="Q2",
                text="After 90 days?",
                asked_in=1,
                answer="No.",
                source="agent",
                reasoning="RefundPolicy caps it",
                bypassed=True,
            ),
        ],
    )
    q = section(render_report(state), "Questions and answers")
    assert "**Answer:** half-up" in q
    assert "**Answer (agent's best judgement, you chose Bypass):** No." in q
    assert "**Reason:** RefundPolicy caps it" in q


def test_bypass_up_front_is_in_the_report() -> None:
    state = RunState(run_id="r", work_item="w", repo="/x", bypass_questions=True)
    assert "bypassed up front" in render_report(state)


def test_a_run_without_decisions_has_no_decisions_section() -> None:
    state = RunState(run_id="r", work_item="w", repo="/x")
    assert "## Decisions" not in render_report(state)
    state.decisions.append(
        Decision(kind="red_checks", stage=Stage.CODE, explanation="Checks failed.")
    )
    assert "## Decisions" in render_report(state)


# --------------------------------------------------------------------------- the skill


def test_skill_covers_levels_bypass_scan_decisions_and_solo() -> None:
    for level in LEVEL_ORDER:
        assert f"`{level.value}`" in SKILL
    for role in roles_for(Level.FULL) + roles_for(Level.BASIC):
        assert role.value in SKILL
    for phrase in (
        "solo",
        "bypass",
        "Bypass up front",
        "Begin work?",
        "security scan",
        "Decision points",
        "take over",
        "fix pass",
        "ignore or block",
        "agent's best judgement",
        "single review pass",
        "never pick or change the level",
        "At most **3** fix passes",
    ):
        assert phrase.lower() in SKILL.lower(), phrase


def test_skill_summary_asks_for_the_new_facts() -> None:
    summary = SKILL.split("## Stop and summarize")[1]
    for phrase in ("level", "security scan", "bypassed", "decision"):
        assert phrase in summary.lower()
