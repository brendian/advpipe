"""The web UI's side of decision points and Bypass (docs/LEVELS_PLAN.md §3.13, milestone L5):
the decision panel for each kind, `POST /runs/<id>/decide`, the confirmation pages for taking
over and stopping, the answer form's Bypass boxes and button, and agent answers on the pages.

Runs stopped at a decision point come from the real orchestrator with fake agents. The actions
are checked by recording the `advpipe` call (the ``cli`` fixture from test_ui_actions): no
process starts.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

import test_ui_actions  # noqa: E402
from fakes import FakeAgentRunner, happy_scripts  # noqa: E402
from test_answers import ANSWER, QUESTION  # noqa: E402
from test_bypass import AGENT_ANSWER, bypassed_resume, second_blocking  # noqa: E402
from test_decisions import (  # noqa: E402
    SCAN_BAD_TXT,
    at_the_cap,
    bad_scripts,
    red_code,
    scan_cfg,
)
from test_solo import SOLO_ASKS, SOLO_OK, SOLO_WRONG, at  # noqa: E402
from test_ui import fake_run, logged_in, make_app  # noqa: E402
from test_ui_actions import Browser, Cli, redirected_to  # noqa: E402
from test_ui_detail import EVIL, no_raw_html, steps_of  # noqa: E402

from advpipe import answers, decisions  # noqa: E402
from advpipe.config import Config  # noqa: E402
from advpipe.decisions import decide  # noqa: E402
from advpipe.models import Decision, Finding, RunState, Stage, Status  # noqa: E402
from advpipe.orchestrator import Orchestrator  # noqa: E402
from advpipe.runlog import RunLog  # noqa: E402
from advpipe.runner import Role  # noqa: E402

cli = test_ui_actions.cli

NOTE = "-keep the signature; <script>alert(1)</script>"


def run_to_decision(config: Config, repo: Path, scripts: dict, cfg: Config | None = None) -> Path:  # type: ignore[type-arg]
    runner = FakeAgentRunner(scripts)
    orch = Orchestrator(cfg or config, runner, repo, "add clamp", "r1", name="r1")
    state = asyncio.run(orch.run())
    assert state.status is Status.NEEDS_HUMAN and state.decision is not None
    return repo.resolve()


def red(config: Config, repo: Path) -> Path:
    return run_to_decision(config, repo, red_code())


def state_of(repo: Path, run_id: str = "r1") -> RunState:
    return RunLog.for_run(repo, run_id).read_state()


def buttons(page: str) -> list[str]:
    """The decision panel's submit buttons, by the choice they send."""
    panel = page[page.index('id="decision"') :]
    panel = panel[: panel.index("</section>")]
    return re.findall(r'<button type="submit" name="choice" value="(\w+)"', panel)


def buttons_of_head(page: str) -> list[str]:
    """Every form the run page's header offers, by its action."""
    head = page[page.index('id="run-head"') :]
    head = (
        head[: head.index("</section>\n</section>")] if "</section>\n</section>" in head else head
    )
    return [a for a in re.findall(r'hx-post="([^"]+)"', head) if not a.endswith("/clean")]


def confirm_links(page: str, run_id: str = "r1") -> list[str]:
    panel = page[page.index('id="decision"') :]
    panel = panel[: panel.index("</section>")]
    return re.findall(rf'href="/runs/{run_id}/decide/(\w+)"', panel)


# --------------------------------------------------------------------------- red_checks


def test_red_checks_panel(config: Config, target_repo: Path, cli: Cli) -> None:
    repo = red(config, target_repo)
    b = Browser(repo)
    page = b.get("/runs/r1").text
    assert "◆ needs a decision" in page and "The checks are still failing" in page
    assert 'id="decision"' in page and "still failing at CODE (test)" in page
    assert "G-test" in page[page.index('id="decision"') :]  # the failing check's output tail
    assert buttons(page) == ["fix"] and confirm_links(page) == ["takeover"]
    assert "Another fix pass" in page and "I&#39;ll take over" in page
    assert 'name="note"' in page and f'maxlength="{decisions.MAX_NOTE_CHARS}"' in page
    assert "Fix pass 1 of 3." in page
    # The timeline: the run can carry on from here, so later steps are "not yet".
    steps = steps_of(page)
    assert steps["CODE"] == steps["ARBITER"] == "stopped here"
    assert steps["REVIEW"] == steps["FINAL CHECKS"] == "not yet"
    # Counts under Needs you, and the terminal route is in the next steps.
    assert 'id="run-r1"' in b.get("/?show=needs_you").text
    assert f"advpipe decide r1 fix | takeover --repo {repo}" in page
    assert cli.calls == []


def test_fix_sends_the_note_as_one_argument(config: Config, target_repo: Path, cli: Cli) -> None:
    repo = red(config, target_repo)
    r = Browser(repo).post("/runs/r1/decide", {"choice": "fix", "note": NOTE})
    assert redirected_to(r) == "/runs/r1?notice=decided"
    assert cli.args() == ["decide", "r1", "fix", "--repo", str(repo), "--detach", f"--note={NOTE}"]
    assert cli.kwargs[0].get("shell", False) is False


def test_fix_without_a_note(config: Config, target_repo: Path, cli: Cli) -> None:
    repo = red(config, target_repo)
    redirected_to(Browser(repo).post("/runs/r1/decide", {"choice": "fix", "note": "  "}))
    assert cli.args() == ["decide", "r1", "fix", "--repo", str(repo), "--detach"]


@pytest.mark.parametrize(
    ("form", "problem"),
    [
        ({"choice": "ignore"}, "isn&#39;t a choice here"),  # another kind's choice
        ({"choice": "rm -rf"}, "isn&#39;t a choice here"),
        ({"choice": ""}, "isn&#39;t a choice here"),
        ({"choice": "takeover", "note": "x", "confirm": "yes"}, "A note goes with the fix"),
        ({"choice": "fix", "note": "x" * 8_001}, "keep it under 8,000"),
    ],
)
def test_decide_refuses_before_any_process(
    config: Config, target_repo: Path, cli: Cli, form: dict[str, str], problem: str
) -> None:
    repo = red(config, target_repo)
    r = Browser(repo).post("/runs/r1/decide", form)
    assert r.status_code == 422 and problem in r.text and "<html" not in r.text
    assert cli.calls == []


def test_fix_is_refused_past_the_cap(config: Config, target_repo: Path, cli: Cli) -> None:
    repo = red(config, target_repo)
    log = RunLog.for_run(repo, "r1")
    state = log.read_state()
    state.fix_passes = 3
    log.write_state(state)
    page = Browser(repo).get("/runs/r1").text
    assert "All 3 fix passes are used" in page and buttons(page) == ["fix"]
    assert re.search(r'value="fix"[^>]*disabled', page)
    r = Browser(repo).post("/runs/r1/decide", {"choice": "fix"})
    assert r.status_code == 422 and "used all 3 fix passes" in r.text
    assert cli.calls == []


def test_takeover_goes_through_a_confirmation_page(
    config: Config, target_repo: Path, cli: Cli
) -> None:
    repo = red(config, target_repo)
    b = Browser(repo)
    page = b.get("/runs/r1/decide/takeover").text
    assert "Take over run" in page and "committed to its branch" in page
    assert state_of(repo).branch in page and state_of(repo).worktree in page
    assert 'name="confirm" value="yes"' in page and 'name="choice" value="takeover"' in page
    # Nothing happens without confirm=yes: only that page's form sends it.
    r = b.post("/runs/r1/decide", {"choice": "takeover"})
    assert r.status_code == 400 and "needs confirming" in r.text
    assert cli.calls == []
    r = b.post("/runs/r1/decide", {"choice": "takeover", "confirm": "yes"})
    assert redirected_to(r) == "/runs/r1?notice=handed_over"
    assert cli.args() == [
        "decide",
        "r1",
        "takeover",
        "--repo",
        str(repo),
    ]  # ends the run: no --detach


def test_the_confirmation_page_is_only_for_ending_choices(
    config: Config, target_repo: Path, cli: Cli
) -> None:
    repo = red(config, target_repo)
    b = Browser(repo)
    assert b.get("/runs/r1/decide/fix").status_code == 409  # a button, not a confirmation
    assert b.get("/runs/r1/decide/stop").status_code == 409  # that's another kind's choice
    assert b.get("/runs/nope/decide/takeover").status_code == 404
    assert cli.calls == []


def test_decide_needs_the_token_and_csrf(config: Config, target_repo: Path, cli: Cli) -> None:
    repo = red(config, target_repo)
    anon = test_ui_actions.TestClient(
        make_app(repo), base_url=test_ui_actions.BASE, follow_redirects=False
    )
    assert anon.post("/runs/r1/decide", data={"choice": "fix"}).status_code == 401
    assert anon.get("/runs/r1/decide/takeover").status_code == 401
    client = logged_in(repo)
    assert client.post("/runs/r1/decide", data={"choice": "fix"}).status_code == 403
    assert cli.calls == []


def test_decide_is_refused_when_the_run_isnt_waiting(
    config: Config, target_repo: Path, cli: Cli
) -> None:
    repo = target_repo.resolve()
    asyncio.run(Orchestrator(config, FakeAgentRunner(happy_scripts()), repo, "x", "r1").run())
    b = Browser(repo)
    r = b.post("/runs/r1/decide", {"choice": "fix"})
    assert r.status_code == 409 and "isn&#39;t waiting for a decision" in r.text
    assert b.post("/runs/missing/decide", {"choice": "fix"}).status_code == 404
    page = b.get("/runs/r1").text
    assert 'id="decision"' not in page and "/decide" not in page
    assert cli.calls == []


def test_the_panel_escapes_everything(target_repo: Path, cli: Cli) -> None:
    d = Decision(
        kind="red_checks",
        stage=Stage.CODE,
        explanation=EVIL,
        findings=[
            Finding(
                id="G-test", severity="blocking", category="correctness", claim=EVIL, evidence=EVIL
            )
        ],
    )
    fake_run(
        target_repo,
        "r-evil",
        status=Status.NEEDS_HUMAN,
        stage=Stage.CODE,
        work_item="x",
        decisions=[d],
    )
    b = Browser(target_repo)
    page = b.get("/runs/r-evil").text
    assert 'id="decision"' in page
    no_raw_html(page)


# --------------------------------------------------------------------------- scan_hits


def scan_hits(config: Config, repo: Path) -> Path:
    cfg = scan_cfg(config, SCAN_BAD_TXT)
    return run_to_decision(config, repo, bad_scripts(), cfg)


def test_scan_hits_panel_and_choices(config: Config, target_repo: Path, cli: Cli) -> None:
    repo = scan_hits(config, target_repo)
    b = Browser(repo)
    page = b.get("/runs/r1").text
    assert "The security scan reported issues" in page and "◆ needs a decision" in page
    assert "may not be real problems" in page and "shell=True at bad.txt:1" in page
    assert buttons(page) == ["ignore", "block"] and confirm_links(page) == []
    assert 'name="note"' not in page[page.index('id="decision"') :]
    assert "Ignore and continue" in page and ">Block</button>" in page
    for choice in ("ignore", "block"):
        cli.calls.clear()
        r = b.post("/runs/r1/decide", {"choice": choice})
        assert redirected_to(r) == "/runs/r1?notice=decided"
        assert cli.args() == ["decide", "r1", choice, "--repo", str(repo), "--detach"]
    cli.calls.clear()
    for form in (
        {"choice": "fix"},
        {"choice": "takeover", "confirm": "yes"},
        {"choice": "ignore", "note": "x"},
    ):
        assert b.post("/runs/r1/decide", form).status_code == 422
    assert b.get("/runs/r1/decide/takeover").status_code == 409
    assert cli.calls == []


def test_a_decided_scan_choice_shows_in_the_timeline(config: Config, target_repo: Path) -> None:
    repo = scan_hits(config, target_repo)
    decide(repo, "r1", "ignore")
    page = Browser(repo).get("/runs/r1").text
    # Decided but not acted on: the run needs a resume, which the page offers.
    assert "■ stopped" in page and "Decision 1: The security scan reported issues" in page
    assert "You chose:</strong> ignore" in page and "(not acted on yet)" in page
    assert "advpipe resume r1" in page


# --------------------------------------------------------------------------- spec_cap


def test_the_cap_asks_begin_work_instead_of_the_form(
    config: Config, target_repo: Path, cli: Cli
) -> None:
    state = asyncio.run(at_the_cap(config, target_repo))
    assert state.decision is not None and state.decision.kind == "spec_cap"
    b = Browser(target_repo.resolve())
    page = b.get("/runs/r1").text
    assert "Begin work?" in page and "◆ needs a decision" in page
    assert "may lo equal hi?" in page[page.index('id="decision"') :]  # the open question
    assert buttons(page) == ["begin"] and confirm_links(page) == ["stop"]
    assert "Save answers and continue" not in page and 'name="answer_Q' not in page
    assert "the agent decides" in page.lower()
    r = b.post("/runs/r1/decide", {"choice": "begin"})
    assert redirected_to(r) == "/runs/r1?notice=decided"
    assert cli.args() == ["decide", "r1", "begin", "--repo", str(target_repo.resolve()), "--detach"]
    # Answering at the cap is refused before any process, naming the choice.
    cli.calls.clear()
    r = b.post("/runs/r1/answer", {"answer_Q2": "no"})
    assert r.status_code == 409 and cli.calls == []


def test_stop_ends_the_run_after_confirming(config: Config, target_repo: Path, cli: Cli) -> None:
    asyncio.run(at_the_cap(config, target_repo))
    repo = target_repo.resolve()
    b = Browser(repo)
    page = b.get("/runs/r1/decide/stop").text
    assert "Stop run" in page and "can't be resumed" in page
    r = b.post("/runs/r1/decide", {"choice": "stop", "confirm": "yes"})
    assert redirected_to(r) == "/runs/r1?notice=ended"
    assert cli.args() == ["decide", "r1", "stop", "--repo", str(repo)]


# --------------------------------------------------------------------------- the solo levels


def test_basic_asks_after_red_checks_and_a_fix_pass_shows(
    config: Config, target_repo: Path, cli: Cli
) -> None:
    cfg = at(config, "basic")
    repo = run_to_decision(config, target_repo, {Role.SOLO: [SOLO_WRONG]}, cfg)
    b = Browser(repo)
    page = b.get("/runs/r1").text
    assert steps_of(page)["SOLO"] == "stopped here" and "Basic (1 of 5)" in page
    assert buttons(page) == ["fix"] and confirm_links(page) == ["takeover"]
    redirected_to(b.post("/runs/r1/decide", {"choice": "fix", "note": NOTE}))
    assert cli.args()[:3] == ["decide", "r1", "fix"]
    # Really do it, with a fix pass that works, and look at the page afterwards.
    decide(repo, "r1", "fix", NOTE)
    runner = FakeAgentRunner({Role.SOLO: [SOLO_OK]})
    state = asyncio.run(Orchestrator.resume(runner, repo, "r1").run())
    assert state.status is Status.COMPLETE
    page = b.get("/runs/r1").text
    assert "Fix pass 1 (you asked)" in page and "Decision 1: The checks are still failing" in page
    assert "You chose:</strong> fix" in page and "Your note:" in page
    no_raw_html(page)
    assert 'id="decision"' not in page


def test_takeover_leaves_a_handed_over_run(config: Config, target_repo: Path, cli: Cli) -> None:
    repo = red(config, target_repo)
    decide(repo, "r1", "takeover")
    b = Browser(repo)
    page = b.get("/runs/r1").text
    assert "→ handed over" in page and 'id="decision"' not in page
    assert state_of(repo).branch in page and "git -C" in page and "switch" in page
    assert "/decide" not in page and buttons_of_head(page) == []
    assert b.get("/?show=needs_you").text.count('id="run-r1"') == 0  # nothing left to decide
    assert "→ handed over" in b.get("/").text
    assert b.post("/runs/r1/decide", {"choice": "fix"}).status_code == 409


def test_stop_leaves_an_ended_run(config: Config, target_repo: Path) -> None:
    asyncio.run(at_the_cap(config, target_repo))
    repo = target_repo.resolve()
    decide(repo, "r1", "stop")
    page = Browser(repo).get("/runs/r1").text
    assert "■ ended" in page and 'id="decision"' not in page
    assert "can&#39;t be resumed" in page


# --------------------------------------------------------------------------- the answer form


def blocked(config: Config, repo: Path, first: str | None = None) -> Path:
    from test_answers import blocked_run

    asyncio.run(blocked_run(config, repo) if first is None else blocked_run(config, repo, first))
    return repo.resolve()


def test_the_answer_form_has_bypass_boxes_and_a_button(
    config: Config, target_repo: Path, cli: Cli
) -> None:
    repo = blocked(config, target_repo, second_blocking())
    page = Browser(repo).get("/runs/r1").text
    for q in ("Q1", "Q2"):
        assert f'<input type="checkbox" name="bypass_{q}" value="yes">' in page
        assert f"Let the agent decide {q}" in page
    assert 'name="bypass_all" value="yes" formnovalidate' in page
    assert "Bypass all and continue" in page and "Save answers and continue" in page
    assert "That covers only these questions" in page
    assert "required" not in page[page.index("answer-form") :].split("</form>")[0]


def test_bypass_all(config: Config, target_repo: Path, cli: Cli) -> None:
    repo = blocked(config, target_repo, second_blocking())
    b = Browser(repo)
    # Whatever is typed is ignored: the button means every question.
    r = b.post("/runs/r1/answer", {"bypass_all": "yes", "answer_Q1": "typed anyway"})
    assert redirected_to(r) == "/runs/r1?notice=bypassed"
    assert cli.args() == [
        "answer", "r1", "--repo", str(repo), "--detach", "--bypass=Q1", "--bypass=Q2"
    ]  # fmt: skip


def test_bypass_some_and_answer_the_rest(config: Config, target_repo: Path, cli: Cli) -> None:
    repo = blocked(config, target_repo, second_blocking())
    b = Browser(repo)
    r = b.post("/runs/r1/answer", {"answer_Q1": "-half-up", "bypass_Q2": "yes"})
    assert redirected_to(r) == "/runs/r1?notice=answered"
    assert cli.args() == [
        "answer", "r1", "--repo", str(repo), "--detach", "--answer=Q1=-half-up", "--bypass=Q2"
    ]  # fmt: skip
    # A ticked box wins over text left in its answer box.
    cli.calls.clear()
    r = b.post("/runs/r1/answer", {"answer_Q1": "x", "bypass_Q1": "yes", "answer_Q2": "y"})
    assert redirected_to(r)
    assert cli.args() == [
        "answer", "r1", "--repo", str(repo), "--detach", "--answer=Q2=y", "--bypass=Q1"
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("form", "problem"),
    [
        ({"answer_Q1": "yes"}, "Q2 still needs an answer"),  # the other is neither
        ({"bypass_Q1": "yes"}, "Q2 still needs an answer"),
        ({"bypass_Q9": "yes"}, "Q9 isn&#39;t one of this run&#39;s questions"),
        ({"answer_Q1": " ", "bypass_Q2": "yes"}, "answer to Q1 is empty"),
        ({"answer_Q1": "x" * 8_001, "bypass_Q2": "yes"}, "keep each answer under 8,000"),
    ],
)
def test_the_answer_form_is_checked_before_any_process(
    config: Config, target_repo: Path, cli: Cli, form: dict[str, str], problem: str
) -> None:
    repo = blocked(config, target_repo, second_blocking())
    r = Browser(repo).post("/runs/r1/answer", form)
    assert r.status_code == 422 and problem in r.text
    assert cli.calls == []


def test_bypass_at_the_solo_levels(config: Config, target_repo: Path, cli: Cli) -> None:
    cfg = at(config, "basic")
    repo = run_to_decision_less_blocked(cfg, target_repo)
    b = Browser(repo)
    page = b.get("/runs/r1").text
    assert (
        "Answer the solo agent&#39;s questions" in page
        or "Answer the solo agent's questions" in page
    )
    assert "The solo agent stopped on" in page and 'name="bypass_all"' in page
    assert steps_of(page)["SOLO"] == "stopped here"
    r = b.post("/runs/r1/answer", {"bypass_all": "yes"})
    assert redirected_to(r).endswith("notice=bypassed")
    assert cli.args() == ["answer", "r1", "--repo", str(repo), "--detach", "--bypass=Q1"]


def run_to_decision_less_blocked(cfg: Config, repo: Path) -> Path:
    runner = FakeAgentRunner({Role.SOLO: [SOLO_ASKS]})
    state = asyncio.run(Orchestrator(cfg, runner, repo, "add clamp", "r1", name="r1").run())
    assert state.status is Status.NEEDS_HUMAN and answers.pending(state)
    return repo.resolve()


# --------------------------------------------------------------------------- agent answers


def test_agent_answers_show_with_a_badge_and_their_reason(
    config: Config, target_repo: Path
) -> None:
    from test_answers import resume

    repo = blocked(config, target_repo)
    answers.record_answers(repo, "r1", {}, {"Q1"})
    page = Browser(repo).get("/runs/r1").text
    assert "you chose Bypass: the agent decides when the run continues" in page
    reply = json.dumps({"answers": [{"id": "Q1", "answer": EVIL, "reasoning": EVIL}]})
    scripts = bypassed_resume("Q1")
    scripts[Role.SPEC_WRITER][0] = reply
    state, _ = asyncio.run(resume(config, repo, scripts))
    assert state.status is Status.COMPLETE
    b = Browser(repo)
    page = b.get("/runs/r1").text
    assert ">agent's best judgement<" in page and "Reason:" in page
    no_raw_html(page)
    context = b.get("/runs/r1/context").text
    assert ">agent's best judgement<" in context
    assert "Answers to the blocking questions" in context
    no_raw_html(context)
    # Spec pass 2's title says whose answers it used.
    assert "Revision 1: with the agent&#39;s best-judgement answers to Q1" in page


def test_person_answers_keep_their_wording(config: Config, target_repo: Path) -> None:
    from test_answers import after_answers, resume

    repo = blocked(config, target_repo)
    answers.record_answers(repo, "r1", {"Q1": ANSWER})
    state, _ = asyncio.run(resume(config, repo, after_answers()))
    assert state.status is Status.COMPLETE
    page = Browser(repo).get("/runs/r1").text
    assert "Revision 1: with your answers to Q1" in page
    assert "Your answer:" in page and "best judgement" not in page
    assert AGENT_ANSWER not in page and QUESTION.replace(">", "&gt;") in page
