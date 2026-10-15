"""`advpipe answer`, and what `resume`, `status` and the run summary say about blocking
questions (docs/ANSWERS_PLAN.md, A1)."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest
from fakes import TASK_MD_BLOCKING, FakeAgentRunner, happy_scripts, writes
from test_answers import ANSWER, QUESTION, after_answers, blocked_run
from typer.testing import CliRunner

import advpipe.cli as cli_module
import advpipe.control as control
from advpipe.cli import app
from advpipe.config import Config
from advpipe.models import Status
from advpipe.runlog import RunLog
from advpipe.runner import Role

cli = CliRunner()
FAKE_CHILD = Path(__file__).parent / "advpipe_fake_child.py"


@pytest.fixture
def fake_resume(monkeypatch: pytest.MonkeyPatch) -> list[FakeAgentRunner]:
    """`advpipe resume`/`answer` drive the run with fake agents: a spec revision, then the rest."""
    runners: list[FakeAgentRunner] = []

    def make() -> FakeAgentRunner:
        runners.append(FakeAgentRunner(after_answers()))
        return runners[-1]

    monkeypatch.setattr(cli_module, "SdkAgentRunner", make)
    return runners


def blocked(config: Config, repo: Path) -> None:
    """A run stopped on one blocking question. Set up outside any event loop: the CLI
    commands under test call asyncio.run themselves."""
    asyncio.run(blocked_run(config, repo))


def state(repo: Path):  # type: ignore[no-untyped-def]
    return RunLog.for_run(repo, "r1").read_state()


def answer(repo: Path, *args: str, input: str | None = None):  # type: ignore[no-untyped-def]
    return cli.invoke(app, ["answer", "r1", "--repo", str(repo), *args], input=input)


# --------------------------------------------------------------------------- answering


def test_answer_flag_then_resume(
    config: Config, target_repo: Path, fake_resume: list[FakeAgentRunner]
) -> None:
    blocked(config, target_repo)
    result = answer(target_repo, "--answer", f"q1={ANSWER}", "-q")
    assert result.exit_code == 0, result.output
    assert "answers saved for Q1" in result.stderr
    assert "r1  complete" in result.stdout
    s = state(target_repo)
    assert s.status is Status.COMPLETE and s.questions[0].answer == ANSWER
    (runner,) = fake_resume
    assert f"Answer: {ANSWER}" in runner.calls_for(Role.SPEC_WRITER)[0].prompt


def test_answer_text_may_start_with_a_dash_and_contain_equals(
    config: Config, target_repo: Path, fake_resume: list[FakeAgentRunner]
) -> None:
    blocked(config, target_repo)
    result = answer(target_repo, "--answer=Q1=-1 means lo=hi", "--no-resume")
    assert result.exit_code == 0, result.output
    assert state(target_repo).questions[0].answer == "-1 means lo=hi"


def test_file_and_template_round_trip(
    config: Config, target_repo: Path, tmp_path: Path, fake_resume: list[FakeAgentRunner]
) -> None:
    blocked(config, target_repo)
    template = answer(target_repo, "--template")
    assert template.exit_code == 0
    assert f"# Q1: {QUESTION}\nQ1: \n" in template.stdout
    filled = tmp_path / "answers.md"
    filled.write_text(template.stdout.replace("Q1: \n", f"Q1: {ANSWER}\nand a second line\n"))
    result = answer(target_repo, "--file", str(filled), "-q")
    assert result.exit_code == 0, result.output
    assert state(target_repo).questions[0].answer == f"{ANSWER}\nand a second line"


def test_interactive_prompting(
    config: Config,
    target_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_resume: list[FakeAgentRunner],
) -> None:
    blocked(config, target_repo)
    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: True)
    result = answer(target_repo, "--no-resume", input=f"{ANSWER}\n")
    assert result.exit_code == 0, result.output
    assert QUESTION in result.stderr
    assert state(target_repo).questions[0].answer == ANSWER
    assert "continue with: advpipe resume r1" in result.stderr


def test_no_answers_and_no_terminal_prints_the_questions(
    config: Config, target_repo: Path, fake_resume: list[FakeAgentRunner]
) -> None:
    blocked(config, target_repo)
    result = answer(target_repo)
    assert result.exit_code == 2
    assert f"Q1: {QUESTION}" in result.stderr and "--template" in result.stderr
    assert fake_resume == []


def test_no_resume_then_resume(
    config: Config, target_repo: Path, fake_resume: list[FakeAgentRunner]
) -> None:
    blocked(config, target_repo)
    assert answer(target_repo, "--answer", f"Q1={ANSWER}", "--no-resume").exit_code == 0
    assert state(target_repo).status is Status.NEEDS_HUMAN and fake_resume == []
    result = cli.invoke(app, ["resume", "r1", "--repo", str(target_repo), "-q"])
    assert result.exit_code == 0, result.output
    assert state(target_repo).status is Status.COMPLETE


def test_answer_again_without_new_answers_just_resumes(
    config: Config, target_repo: Path, fake_resume: list[FakeAgentRunner]
) -> None:
    blocked(config, target_repo)
    answer(target_repo, "--answer", f"Q1={ANSWER}", "--no-resume")
    result = answer(target_repo, "-q")
    assert result.exit_code == 0, result.output
    assert state(target_repo).status is Status.COMPLETE


def test_resume_without_answers_points_to_answer(
    config: Config, target_repo: Path, fake_resume: list[FakeAgentRunner]
) -> None:
    blocked(config, target_repo)
    result = cli.invoke(app, ["resume", "r1", "--repo", str(target_repo)])
    assert result.exit_code == 2
    assert "waiting on answers to Q1: advpipe answer r1" in result.stderr
    assert all(not r.calls for r in fake_resume)  # no agent was called


# --------------------------------------------------------------------------- refusals


def test_refusals_exit_2(
    config: Config, target_repo: Path, tmp_path: Path, fake_resume: list[FakeAgentRunner]
) -> None:
    blocked(config, target_repo)
    bad_file = tmp_path / "bad.md"
    bad_file.write_text("Q1: a\nQ1: b\n")
    for args, message in [
        (["--answer", "Q2=x"], "Q2 isn't one of this run's questions"),
        (["--answer", "Q1="], "answer to Q1 is empty"),
        (["--answer", "no equals sign"], "--answer takes ID=TEXT"),
        (["--file", str(bad_file)], "answered twice"),
        (["--file", str(tmp_path / "missing.md")], "can't read"),
    ]:
        result = answer(target_repo, *args)
        assert result.exit_code == 2, args
        assert message in result.stderr, (args, result.stderr)
    assert state(target_repo).questions[0].answer == ""
    nope = cli.invoke(app, ["answer", "nope", "--repo", str(target_repo)])
    assert nope.exit_code == 2 and "no run nope" in nope.stderr
    assert fake_resume == []


def test_answer_refuses_a_run_that_isnt_waiting(config: Config, target_repo: Path) -> None:
    from advpipe.orchestrator import Orchestrator

    orch = Orchestrator(config, FakeAgentRunner(happy_scripts()), target_repo, "x", "r1")
    asyncio.run(orch.run())
    result = answer(target_repo, "--answer", "Q1=x")
    assert result.exit_code == 2 and "isn't waiting on answers" in result.stderr
    assert answer(target_repo, "--template").exit_code == 2


# --------------------------------------------------------------------------- summary and status


def test_run_summary_says_how_to_answer(target_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scripts = happy_scripts()
    scripts[Role.SPEC_WRITER] = [writes({"task.md": TASK_MD_BLOCKING})]
    monkeypatch.setattr(cli_module, "SdkAgentRunner", lambda: FakeAgentRunner(scripts))
    (target_repo / "pipeline.toml").write_text(
        "[gates]\ntest = []\ntypes = []\nlint = []\nsecurity = []\n"
    )
    result = cli.invoke(app, ["run", "add clamp", "--repo", str(target_repo), "-q"])
    assert result.exit_code == 1
    run_id = result.stdout.split()[0]
    assert "needs_human" in result.stdout
    assert f"1 blocking question. Answer them, and the run continues: advpipe answer {run_id}" in (
        result.stdout
    )


def test_status_lists_questions_and_answers(config: Config, target_repo: Path) -> None:
    blocked(config, target_repo)
    out = cli.invoke(app, ["status", "r1", "--repo", str(target_repo)]).output
    assert f"questions:\n  Q1  {QUESTION}\n      (waiting)\nanswer them: advpipe answer r1" in out
    answer(target_repo, "--answer", f"Q1={ANSWER}", "--no-resume")
    out = cli.invoke(app, ["status", "r1", "--repo", str(target_repo)]).output
    assert f"      answer: {ANSWER}" in out and "answer them:" not in out


# --------------------------------------------------------------------------- --detach


def test_answer_detach_resumes_in_the_background(
    config: Config, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked(config, target_repo)
    monkeypatch.setattr(control, "child_command", lambda: [sys.executable, str(FAKE_CHILD)])
    monkeypatch.delenv("ADVPIPE_FAKE_BLOCK", raising=False)
    result = answer(target_repo, "--answer", f"Q1={ANSWER}", "--detach")
    assert result.exit_code == 0, result.output
    assert result.stdout.strip().splitlines()[0] == "r1"
    deadline = time.monotonic() + 90
    while state(target_repo).status is not Status.COMPLETE:
        console = RunLog.for_run(target_repo, "r1").root / "console.log"
        assert time.monotonic() < deadline, console.read_text()
        time.sleep(0.1)
    committed = (RunLog.for_run(target_repo, "r1").root / "task.md").read_text()
    assert f"**Answer:** {ANSWER}" in committed
