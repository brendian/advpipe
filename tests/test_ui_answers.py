"""The web UI's side of answering blocking spec questions (docs/ANSWERS_PLAN.md, A2).

Runs blocked at the spec come from the real orchestrator with fake agents. The form's action
runs `advpipe answer --detach`: most tests record that call (the ``cli`` fixture from
test_ui_actions), and one runs the real CLI through the fake child, end to end.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

import test_ui_actions  # noqa: E402
from fakes import TASK_MD, FakeAgentRunner, happy_scripts  # noqa: E402
from test_answers import ANSWER, QUESTION, after_answers, blocked_run  # noqa: E402
from test_control import FAKE_CHILD, wait_for  # noqa: E402
from test_ui import anon  # noqa: E402
from test_ui_actions import Browser, Cli, redirected_to  # noqa: E402
from test_ui_detail import EVIL, no_raw_html, steps_of  # noqa: E402

import advpipe.control as control  # noqa: E402
from advpipe.answers import MAX_ANSWER_CHARS, record_answers  # noqa: E402
from advpipe.config import Config  # noqa: E402
from advpipe.models import Status  # noqa: E402
from advpipe.orchestrator import Orchestrator  # noqa: E402
from advpipe.runlog import RunLog  # noqa: E402
from advpipe.ui import actions  # noqa: E402

# test_ui_actions' fixture that records advpipe calls; registered here under the same name.
cli = test_ui_actions.cli

TWO_QUESTIONS = TASK_MD.replace(
    "## Open questions\n- None",
    f"## Open questions\n- BLOCKING: {QUESTION}\n- BLOCKING: may lo equal hi?",
)


def blocked(config: Config, repo: Path, task_md: str | None = None) -> Path:
    if task_md is None:
        asyncio.run(blocked_run(config, repo))
    else:
        asyncio.run(blocked_run(config, repo, task_md))
    return repo.resolve()


def form_fields(page: str) -> list[str]:
    return re.findall(r'<textarea id="answer-(Q\d+)" class="short" name="answer_\1"', page)


# --------------------------------------------------------------------------- pages


def test_list_and_page_show_needs_answers_and_the_form(config: Config, target_repo: Path) -> None:
    repo = blocked(config, target_repo, TWO_QUESTIONS)
    b = Browser(repo)
    listing = b.get("/").text
    assert "? needs answers" in listing
    assert 'id="run-r1"' in b.get("/?show=needs_you").text  # counts as needing you

    page = b.get("/runs/r1").text
    assert "? needs answers" in page and "<h2>Answer the spec's questions</h2>" in page
    assert form_fields(page) == ["Q1", "Q2"]
    assert f"<strong>Q1</strong> {QUESTION.replace('>', '&gt;')}" in page
    assert 'hx-post="/runs/r1/answer"' in page and f'maxlength="{MAX_ANSWER_CHARS}"' in page
    assert "Save answers and continue" in page
    assert "What&#39;s left open" not in page and "What's left open" not in page
    assert f"advpipe answer r1 --repo {repo}" in page  # next steps, for a terminal
    assert "Clean up" in page
    # The timeline: the SPEC pass lists the questions it asked; later steps are still to come.
    assert "Blocking questions it asked" in page and "2 blocking questions" in page
    assert steps_of(page)["TESTS"] == "not yet"


def test_run_being_resumed_shows_as_running(config: Config, target_repo: Path) -> None:
    # Right after `advpipe answer --detach`, run.json still says needs_human, but the
    # background process already holds run.lock: the page must go live, not show the form.
    import os

    repo = blocked(config, target_repo)
    record_answers(repo, "r1", {"Q1": ANSWER})
    RunLog.for_run(repo, "r1").acquire_lock(os.getpid())
    page = Browser(repo).get("/runs/r1").text
    assert "● running" in page and form_fields(page) == []
    assert 'sse-connect="/runs/r1/events' in page


def test_help_page_explains_answering(target_repo: Path) -> None:
    page = Browser(target_repo).get("/help").text
    assert "needs answers" in page and "Answers" in page


def test_answer_form_only_for_runs_waiting_on_answers(config: Config, target_repo: Path) -> None:
    repo = target_repo.resolve()
    asyncio.run(
        Orchestrator(config, FakeAgentRunner(happy_scripts()), repo, "add clamp", "r1").run()
    )
    page = Browser(repo).get("/runs/r1").text
    assert form_fields(page) == [] and "needs answers" not in page


def test_answers_saved_but_not_resumed_prefill_the_form(config: Config, target_repo: Path) -> None:
    repo = blocked(config, target_repo)
    record_answers(repo, "r1", {"Q1": ANSWER})  # e.g. advpipe answer --no-resume
    page = Browser(repo).get("/runs/r1").text
    assert "? needs answers" in page
    assert f'name="answer_Q1" rows="3" maxlength="{MAX_ANSWER_CHARS}">{ANSWER}' in page


# --------------------------------------------------------------------------- posting answers


def test_valid_answers_call_advpipe_answer(config: Config, target_repo: Path, cli: Cli) -> None:
    repo = blocked(config, target_repo, TWO_QUESTIONS)
    tricky = "-1 means: <b>never</b>\nsecond line"
    r = Browser(repo).post(
        "/runs/r1/answer",
        {"answer_Q1": ANSWER, "answer_Q2": tricky, "budget": "999", "answer_x": "ignored"},
    )
    assert redirected_to(r) == "/runs/r1?notice=answered"
    assert cli.args() == [
        "answer", "r1", "--repo", str(repo), "--detach",
        f"--answer=Q1={ANSWER}", f"--answer=Q2={tricky}",
    ]  # fmt: skip
    assert "shell" not in cli.kwargs[0]  # an argument list, never a shell
    page = Browser(repo).get("/runs/r1?notice=answered").text
    assert "Answers saved. The run carries on in the background" in page


def test_bad_answers_are_refused_before_any_process(
    config: Config, target_repo: Path, cli: Cli
) -> None:
    repo = blocked(config, target_repo, TWO_QUESTIONS)
    b = Browser(repo)
    for data, problem in [
        ({"answer_Q1": ANSWER}, "Q2 still needs an answer"),
        ({"answer_Q1": ANSWER, "answer_Q2": "  "}, "answer to Q2 is empty"),
        ({"answer_Q1": ANSWER, "answer_Q2": "x", "answer_Q7": "x"}, "Q7 isn&#39;t one of"),
        ({"answer_Q1": "x" * (MAX_ANSWER_CHARS + 1), "answer_Q2": "x"}, "keep each answer"),
    ]:
        r = b.post("/runs/r1/answer", data)
        assert r.status_code == 422 and problem in r.text, (data, r.text)
    assert cli.calls == []


def test_answer_needs_csrf_and_the_token(config: Config, target_repo: Path, cli: Cli) -> None:
    repo = blocked(config, target_repo)
    data = {"answer_Q1": ANSWER}
    assert anon(repo).post("/runs/r1/answer", data=data).status_code == 401
    b = Browser(repo)
    assert b.client.post("/runs/r1/answer", data=data).status_code == 403  # no CSRF header
    assert b.client.get("/runs/r1/answer").status_code in (404, 405)  # POST only
    assert cli.calls == []


def test_answer_refused_for_runs_not_waiting(config: Config, target_repo: Path, cli: Cli) -> None:
    repo = target_repo.resolve()
    asyncio.run(
        Orchestrator(config, FakeAgentRunner(happy_scripts()), repo, "add clamp", "r1").run()
    )
    b = Browser(repo)
    r = b.post("/runs/r1/answer", {"answer_Q1": "x"})
    assert r.status_code == 409 and "isn&#39;t waiting on answers" in r.text
    assert b.post("/runs/nope/answer", {"answer_Q1": "x"}).status_code == 404
    assert b.post("/runs/..%2f..%2fetc/answer", {"answer_Q1": "x"}).status_code == 404
    assert cli.calls == []


def test_cli_refusal_is_shown(config: Config, target_repo: Path, cli: Cli) -> None:
    import subprocess

    repo = blocked(config, target_repo)
    cli.reply = subprocess.CompletedProcess([], 2, "", "run r1 is still being driven")
    r = Browser(repo).post("/runs/r1/answer", {"answer_Q1": ANSWER})
    assert r.status_code == 422 and "still being driven" in r.text


# --------------------------------------------------------------------------- escaping, timeline


def test_answers_and_questions_are_escaped_everywhere(config: Config, target_repo: Path) -> None:
    evil_question = TASK_MD.replace(
        "## Open questions\n- None", f"## Open questions\n- BLOCKING: {EVIL}"
    )
    repo = blocked(config, target_repo, evil_question)
    b = Browser(repo)
    no_raw_html(b.get("/runs/r1").text)  # the question, in the form and the timeline
    record_answers(repo, "r1", {"Q1": EVIL})
    asyncio.run(Orchestrator.resume(FakeAgentRunner(after_answers()), repo, "r1").run())
    for url in ("/runs/r1", "/runs/r1/body", "/runs/r1/context", "/"):
        page = b.get(url).text
        no_raw_html(page)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in b.get("/runs/r1").text


def test_timeline_shows_each_spec_pass(config: Config, target_repo: Path) -> None:
    repo = blocked(config, target_repo)
    record_answers(repo, "r1", {"Q1": ANSWER})
    state = asyncio.run(Orchestrator.resume(FakeAgentRunner(after_answers()), repo, "r1").run())
    assert state.status is Status.COMPLETE
    b = Browser(repo)
    page = b.get("/runs/r1").text
    assert "<strong>Spec writer</strong>" in page
    assert "<strong>Revision 1: with your answers to Q1</strong>" in page
    assert f'Your answer:</span> <span class="answer-text">{ANSWER}</span>' in page
    assert "task.md</code> as this pass left it" in page
    assert steps_of(page)["SPEC"] == "done"
    # The spec & context tab highlights the Answers section.
    context = b.get("/runs/r1/context").text
    assert 'class="task-ans"' in context and ANSWER in context


def test_answer_args() -> None:
    assert actions.answer_args(Path("/r"), "r1", {"Q1": "-x", "Q2": "a=b"}) == [
        "answer", "r1", "--repo", "/r", "--detach", "--answer=Q1=-x", "--answer=Q2=a=b",
    ]  # fmt: skip


# --------------------------------------------------------------------------- end to end


def test_answer_from_the_page_end_to_end(
    config: Config, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real `advpipe answer --detach`, through the fake child: the run completes."""
    repo = blocked(config, target_repo)
    monkeypatch.setattr(control, "child_command", lambda: [sys.executable, str(FAKE_CHILD)])
    monkeypatch.delenv("ADVPIPE_FAKE_BLOCK", raising=False)
    b = Browser(repo)
    r = b.post("/runs/r1/answer", {"answer_Q1": ANSWER})
    assert redirected_to(r) == "/runs/r1?notice=answered"
    log = RunLog.for_run(repo, "r1")
    wait_for(lambda: log.read_state().status is Status.COMPLETE, timeout=90)
    state = log.read_state()
    assert state.questions[0].answer == ANSWER and state.spec_passes == 2
    page = b.get("/runs/r1").text
    assert "✔ complete" in page and "Revision 1: with your answers to Q1" in page
