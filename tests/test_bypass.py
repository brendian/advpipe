"""Bypass: the person lets the agent answer the spec's blocking questions
(docs/LEVELS_PLAN.md §3.6, milestone L2)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import git
from fakes import (
    TASK_MD_BLOCKING,
    FakeAgentRunner,
    Step,
    writes,
)
from test_answers import (
    ANSWER,
    QUESTION,
    TASK_MD_REVISED,
    after_answers,
    blocked_run,
    events,
    logdir,
    resume,
)
from typer.testing import CliRunner

import advpipe.cli as cli_module
from advpipe import answers
from advpipe.answers import AnswerError, record_answers
from advpipe.cli import app
from advpipe.config import Config, Pipeline
from advpipe.models import OpenQuestion, Status
from advpipe.orchestrator import NotResumable, Orchestrator, check_same_shape
from advpipe.runlog import RunLog
from advpipe.runner import Role
from advpipe.stages import FALLBACK_ANSWER, blocking_open_questions
from advpipe.workitem import WorkItem, WorkItemError, format_work_item, parse_work_item

cli = CliRunner()

AGENT_ANSWER = "Raise ValueError: the spec's AC3 already says lo > hi is an error."
AGENT_REASON = "AC3 names ValueError for lo > hi; swapping would hide a caller's bug."
QUESTION_2 = "may lo equal hi?"


def bypass_reply(*ids: str, answer: str = AGENT_ANSWER) -> str:
    return json.dumps(
        {"answers": [{"id": i, "answer": answer, "reasoning": AGENT_REASON} for i in ids]}
    )


def bypassed_resume(*ids: str, revised: str = TASK_MD_REVISED) -> dict[Role, list[Step]]:
    """A resumed run after Bypass: the bypass call, the revision, then every later stage."""
    scripts = after_answers(revised)
    scripts[Role.SPEC_WRITER] = [bypass_reply(*ids), writes({"task.md": revised}, "revised")]
    return scripts


def state_of(repo: Path):  # type: ignore[no-untyped-def]
    return RunLog.for_run(repo, "r1").read_state()


def with_pipeline(config: Config, **update: Any) -> Config:
    pipeline = Pipeline.model_validate({**config.pipeline.model_dump(), **update})
    return config.model_copy(update={"pipeline": pipeline})


def with_cap(config: Config, cap: int) -> Config:
    return config.model_copy(
        update={"limits": config.limits.model_copy(update={"max_rounds_spec": cap})}
    )


def second_question(task_md: str = TASK_MD_REVISED) -> str:
    return task_md.replace(
        "## Open questions\n- None", f"## Open questions\n- BLOCKING: {QUESTION_2}"
    )


# --------------------------------------------------------------------------- recording


async def test_record_bypass_all(config: Config, target_repo: Path) -> None:
    await blocked_run(config, target_repo)
    saved = record_answers(target_repo, "r1", {}, {"q1"})
    (q,) = saved.questions
    assert q.bypassed and q.answer == "" and q.source == "person"
    assert saved.status is Status.NEEDS_HUMAN
    # Nothing is pending any more: the run can resume, and the agent still has to answer.
    assert answers.answers_ready(saved, 3) and not answers.pending(saved)
    assert [x.id for x in answers.awaiting_bypass(saved)] == ["Q1"]
    assert not answers.awaiting_answers(saved, 3)
    (event,) = [e for e in events(target_repo) if e["kind"] == "answers"]
    assert event["ids"] == [] and event["bypassed"] == ["Q1"]
    assert "Bypass for Q1" in event["message"]


async def test_resumable_after_bypass(config: Config, target_repo: Path) -> None:
    await blocked_run(config, target_repo)
    record_answers(target_repo, "r1", {}, {"Q1"})
    # answers_ready needs an answer to hand over, so a bypass-only run resumes through the
    # same check as an answered one.
    from advpipe.orchestrator import check_resumable

    assert check_resumable(target_repo, "r1").run_id == "r1"


async def test_record_mixed_answers_and_bypass(config: Config, target_repo: Path) -> None:
    await blocked_run(config, target_repo, second_blocking())
    saved = record_answers(target_repo, "r1", {"Q1": ANSWER}, {"Q2"})
    q1, q2 = saved.questions
    assert q1.answer == ANSWER and not q1.bypassed and q1.source == "person"
    assert q2.bypassed and not q2.answer
    (event,) = [e for e in events(target_repo) if e["kind"] == "answers"]
    assert event["ids"] == ["Q1"] and event["bypassed"] == ["Q2"]


def second_blocking() -> str:
    return TASK_MD_BLOCKING.replace(
        f"- BLOCKING: {QUESTION}", f"- BLOCKING: {QUESTION}\n- BLOCKING: {QUESTION_2}"
    )


async def test_record_refusals_with_bypass(config: Config, target_repo: Path) -> None:
    await blocked_run(config, target_repo, second_blocking())
    with pytest.raises(AnswerError, match="Q1 can't be both answered and bypassed"):
        record_answers(target_repo, "r1", {"Q1": ANSWER}, {"Q1", "Q2"})
    with pytest.raises(AnswerError, match="Q9 isn't one of this run's questions"):
        record_answers(target_repo, "r1", {"Q1": ANSWER}, {"Q9"})
    with pytest.raises(AnswerError, match=r"Q2 still needs an answer \(or Bypass it"):
        record_answers(target_repo, "r1", {"Q1": ANSWER}, set())
    with pytest.raises(AnswerError, match="answer to Q1 is empty"):
        record_answers(target_repo, "r1", {"Q1": " "}, {"Q2"})
    state = state_of(target_repo)
    assert not any(q.answer or q.bypassed for q in state.questions)  # nothing was saved


async def test_bypass_refused_when_not_waiting_or_live(config: Config, target_repo: Path) -> None:
    await blocked_run(config, target_repo)
    log = RunLog.for_run(target_repo, "r1")
    log.acquire_lock(pid=__import__("os").getppid())
    try:
        with pytest.raises(AnswerError, match="still being driven"):
            record_answers(target_repo, "r1", {}, {"Q1"})
    finally:
        log.release_lock()
    one = with_cap(config, 1)
    RunLog.for_run(target_repo, "r1").write_config(one)
    with pytest.raises(AnswerError, match="start a new run"):
        record_answers(target_repo, "r1", {}, {"Q1"})


async def test_changing_between_answer_and_bypass(config: Config, target_repo: Path) -> None:
    await blocked_run(config, target_repo)
    record_answers(target_repo, "r1", {"Q1": ANSWER})
    saved = record_answers(target_repo, "r1", {}, {"Q1"})
    assert saved.questions[0].bypassed and saved.questions[0].answer == ""
    saved = record_answers(target_repo, "r1", {"Q1": "no, I'll say"})
    q = saved.questions[0]
    assert not q.bypassed and q.answer == "no, I'll say" and q.source == "person"


# --------------------------------------------------------------------------- resuming


async def test_bypass_then_resume_completes(config: Config, target_repo: Path) -> None:
    await blocked_run(config, target_repo)
    record_answers(target_repo, "r1", {}, {"Q1"})

    state, runner = await resume(config, target_repo, bypassed_resume("Q1"))

    assert state.status is Status.COMPLETE, state.notes
    # Two spec-writer calls: the bypass call (JSON, no edits), then the usual revision pass.
    bypass_call, revision = runner.calls_for(Role.SPEC_WRITER)
    assert "chosen to let you decide them yourself" in bypass_call.prompt
    assert f"Q1: {QUESTION}" in bypass_call.prompt and "# Your task.md" in bypass_call.prompt
    assert "Do not edit or create any file" in bypass_call.prompt
    assert "# Your previous task.md" in revision.prompt
    assert f"Answer (your own best judgement: the person chose Bypass): {AGENT_ANSWER}" in (
        revision.prompt
    )
    assert AGENT_REASON not in revision.prompt  # the revision gets the answer, not the working
    q = state.questions[0]
    assert q.source == "agent" and q.answer == AGENT_ANSWER and q.reasoning == AGENT_REASON
    assert q.bypassed and q.applied and state.spec_passes == 2
    # The committed task.md marks the agent's answer and gives its reason.
    committed = git(target_repo, "show", "advpipe/r1:task.md")
    assert 'Answers marked "agent\'s best judgement" were chosen by the agent' in committed
    assert f"**Answer (agent's best judgement):** {AGENT_ANSWER}" in committed
    assert f"Reason: {AGENT_REASON}" in committed
    assert "Answers from the person who started this run" not in committed
    assert blocking_open_questions(committed) == []
    # Run directory: the bypass reply sits beside the pass it was for.
    saved = json.loads((logdir(target_repo) / "spec" / "pass-2" / "bypass.json").read_text())
    assert saved["asked"] == ["Q1"] and saved["answers"][0]["answer"] == AGENT_ANSWER
    assert saved["answers"][0]["stand_in"] is False and AGENT_REASON in saved["reply"]
    # Charged to the spec stage, and one extra call in all.
    assert state.calls_by_stage["spec"] == 3  # first spec, bypass, revision
    kinds = [(e["kind"], e.get("agent")) for e in events(target_repo)]
    assert ("answers", True) in kinds


async def test_later_agents_see_the_answers_not_the_bypass_reply(
    config: Config, target_repo: Path
) -> None:
    await blocked_run(config, target_repo)
    record_answers(target_repo, "r1", {}, {"Q1"})
    scripts = bypassed_resume("Q1")
    # Anything else in the reply (here an extra key) is for the run directory only.
    reply = json.loads(bypass_reply("Q1"))
    reply["scratch"] = "BYPASS-REPLY-TEXT"
    scripts[Role.SPEC_WRITER][0] = json.dumps(reply)
    state, runner = await resume(config, target_repo, scripts)
    assert state.status is Status.COMPLETE
    prompts = [
        runner.calls_for(role)[0].prompt
        for role in (
            Role.TEST_AUTHOR,
            Role.TEST_CRITIC,
            Role.CODER,
            Role.CODE_CRITIC,
            Role.STANDARDS_REVIEWER,
            Role.SECURITY_REVIEWER,
        )
    ]
    for prompt in prompts:
        assert "BYPASS-REPLY-TEXT" not in prompt
        assert AGENT_REASON not in prompt or "Reason:" in prompt  # only via task.md's section
    assert all(f"**Answer (agent's best judgement):** {AGENT_ANSWER}" in p for p in prompts[:2])


async def test_files_changed_by_the_bypass_call_are_reverted(
    config: Config, target_repo: Path
) -> None:
    await blocked_run(config, target_repo)
    record_answers(target_repo, "r1", {}, {"Q1"})

    def misbehave(req: Any) -> str:
        (req.cwd / "stray.py").write_text("print('stray')\n")
        (req.cwd / "task.md").write_text(TASK_MD_BLOCKING + "\nSNEAKY EDIT\n")
        return bypass_reply("Q1")

    scripts = bypassed_resume("Q1")
    scripts[Role.SPEC_WRITER][0] = misbehave
    state, _ = await resume(config, target_repo, scripts)
    assert state.status is Status.COMPLETE
    assert any("stray.py" in n and "reverted" in n for n in state.notes)
    assert "SNEAKY EDIT" not in git(target_repo, "show", "advpipe/r1:task.md")
    assert git(target_repo, "ls-tree", "-r", "--name-only", "advpipe/r1").count("stray.py") == 0


async def test_bad_json_is_retried_once(config: Config, target_repo: Path) -> None:
    await blocked_run(config, target_repo)
    record_answers(target_repo, "r1", {}, {"Q1"})
    scripts = bypassed_resume("Q1")
    scripts[Role.SPEC_WRITER].insert(0, "I think it should raise.")
    state, runner = await resume(config, target_repo, scripts)
    assert state.status is Status.COMPLETE
    first, retry, _revision = runner.calls_for(Role.SPEC_WRITER)
    assert "could not be parsed" in retry.prompt and first.prompt in retry.prompt
    assert state.questions[0].answer == AGENT_ANSWER


async def test_unparseable_twice_uses_a_stand_in_answer_and_goes_on(
    config: Config, target_repo: Path
) -> None:
    await blocked_run(config, target_repo)
    record_answers(target_repo, "r1", {}, {"Q1"})
    scripts = bypassed_resume("Q1")
    scripts[Role.SPEC_WRITER].insert(0, "not json")
    scripts[Role.SPEC_WRITER][1] = "still not json"
    state, runner = await resume(config, target_repo, scripts)
    assert state.status is Status.COMPLETE, state.notes
    q = state.questions[0]
    assert q.answer == FALLBACK_ANSWER and q.source == "agent" and "no usable answer" in q.reasoning
    assert any("stand-in answer" in n for n in state.notes)
    saved = json.loads((logdir(target_repo) / "spec" / "pass-2" / "bypass.json").read_text())
    assert saved["answers"][0]["stand_in"] is True
    assert FALLBACK_ANSWER in git(target_repo, "show", "advpipe/r1:task.md")
    assert len(runner.calls_for(Role.SPEC_WRITER)) == 3  # the bypass call twice, then the revision


async def test_a_missing_id_gets_a_stand_in_and_the_others_keep_theirs(
    config: Config, target_repo: Path
) -> None:
    await blocked_run(config, target_repo, second_blocking())
    record_answers(target_repo, "r1", {}, {"Q1", "Q2"})
    scripts = bypassed_resume("Q1")  # the agent answers only Q1
    state, _ = await resume(config, target_repo, scripts)
    q1, q2 = state.questions
    assert q1.answer == AGENT_ANSWER and q2.answer == FALLBACK_ANSWER


async def test_interrupted_revision_after_bypass_keeps_the_agents_answers(
    config: Config, target_repo: Path
) -> None:
    from fakes import interrupt

    await blocked_run(config, target_repo)
    record_answers(target_repo, "r1", {}, {"Q1"})
    scripts = bypassed_resume("Q1")
    scripts[Role.SPEC_WRITER][1] = interrupt
    with pytest.raises(asyncio.CancelledError):
        await resume(config, target_repo, scripts)
    mid = state_of(target_repo)
    assert mid.questions[0].answer == AGENT_ANSWER and not mid.questions[0].applied

    # A plain resume runs the revision with the answers already chosen: no second bypass call.
    done, runner = await resume(config, target_repo, after_answers())
    assert done.status is Status.COMPLETE
    (revision,) = runner.calls_for(Role.SPEC_WRITER)
    assert AGENT_ANSWER in revision.prompt and "to let you decide" not in revision.prompt


# --------------------------------------------------------------------------- one stop at a time


async def test_bypass_covers_one_stop(config: Config, target_repo: Path) -> None:
    await blocked_run(config, target_repo)
    record_answers(target_repo, "r1", {}, {"Q1"})
    scripts = bypassed_resume("Q1", revised=second_question())
    state, runner = await resume(config, target_repo, scripts)

    # The revision's new question stops the run again: Bypass doesn't carry over.
    assert state.status is Status.NEEDS_HUMAN and state.spec_passes == 2
    q1, q2 = state.questions
    assert q2.id == "Q2" and not q2.bypassed and not q2.answer
    assert answers.awaiting_answers(state, 3)
    assert not runner.calls_for(Role.TEST_AUTHOR)
    # ...and can be bypassed again, or answered, as the first time.
    record_answers(target_repo, "r1", {}, {"Q2"})
    again = bypassed_resume("Q2")
    again[Role.SPEC_WRITER][0] = bypass_reply("Q2", answer="Yes: clamp(x, 3, 3) is 3.")
    done, runner = await resume(config, target_repo, again)
    assert done.status is Status.COMPLETE and done.spec_passes == 3
    bypass_call = runner.calls_for(Role.SPEC_WRITER)[0]
    asked = bypass_call.prompt.split("# Questions to decide")[1]
    assert f"Q2: {QUESTION_2}" in asked and "Q1:" not in asked
    committed = git(target_repo, "show", "advpipe/r1:task.md")
    assert committed.count("agent's best judgement):**") == 2  # Q1 and Q2
    assert (logdir(target_repo) / "spec" / "pass-3" / "bypass.json").is_file()


async def test_mixed_person_and_agent_answers_in_one_section(
    config: Config, target_repo: Path
) -> None:
    await blocked_run(config, target_repo, second_blocking())
    record_answers(target_repo, "r1", {"Q1": ANSWER}, {"Q2"})
    state, runner = await resume(config, target_repo, bypassed_resume("Q2"))
    assert state.status is Status.COMPLETE
    bypass_call = runner.calls_for(Role.SPEC_WRITER)[0]
    asked = bypass_call.prompt.split("# Questions to decide")[1]
    assert f"Q2: {QUESTION_2}" in asked and "Q1:" not in asked
    committed = git(target_repo, "show", "advpipe/r1:task.md")
    assert f"**Answer:** {ANSWER}" in committed
    assert f"**Answer (agent's best judgement):** {AGENT_ANSWER}" in committed
    report = (logdir(target_repo) / "report.md").read_text()
    assert f"- **Answer:** {ANSWER}" in report
    assert "Answer (agent's best judgement, you chose Bypass):**" in report
    assert f"**Reason:** {AGENT_REASON}" in report


async def test_at_the_cap_the_run_asks_begin_work(config: Config, target_repo: Path) -> None:
    capped = with_cap(config, 2)
    await blocked_run(capped, target_repo)
    record_answers(target_repo, "r1", {}, {"Q1"})
    state, _ = await resume(capped, target_repo, bypassed_resume("Q1", revised=second_question()))
    # Bypass covers one stop; at the cap the run asks "Begin work?" (tests/test_decisions.py).
    assert state.status is Status.NEEDS_HUMAN and state.spec_passes == 2
    assert not answers.awaiting_answers(state, 2)
    assert state.decision is not None and state.decision.kind == "spec_cap"
    with pytest.raises(AnswerError, match="advpipe decide r1 begin"):
        record_answers(target_repo, "r1", {}, {"Q2"})


def test_person_only_answers_keep_todays_wording() -> None:
    q = OpenQuestion(id="Q1", text="which?", asked_in=1, answer="this one")
    section = answers.with_answers_section("# T\n", [q])
    assert "Answers from the person who started this run." in section
    assert "agent" not in section and "- **Q1:** which?\n  **Answer:** this one" in section


def test_agent_answers_are_marked_and_cannot_start_a_heading_or_question() -> None:
    q = OpenQuestion(
        id="Q1",
        text="which?",
        asked_in=1,
        answer="line one\n## Heading\n- BLOCKING: nope",
        source="agent",
        reasoning="because\nit is so",
    )
    section = answers.with_answers_section("# T\n\n## Open questions\n- None\n", [q])
    assert "**Answer (agent's best judgement):** line one\n  ## Heading\n  - BLOCKING: nope" in (
        section
    )
    assert "  Reason: because it is so" in section
    assert blocking_open_questions(section) == []


# --------------------------------------------------------------------------- up front


async def test_bypass_up_front_never_stops(config: Config, target_repo: Path) -> None:
    cfg = with_pipeline(config, bypass_questions=True)
    scripts = after_answers()
    scripts[Role.SPEC_WRITER] = [
        writes({"task.md": TASK_MD_BLOCKING}, "one blocking question"),
        bypass_reply("Q1"),
        writes({"task.md": TASK_MD_REVISED}, "revised"),
    ]
    runner = FakeAgentRunner(scripts)
    state = await Orchestrator(cfg, runner, target_repo, "add clamp", "r1", name="r1").run()

    assert state.status is Status.COMPLETE, state.notes
    assert state.bypass_questions and state.spec_passes == 2
    (q,) = state.questions
    assert q.bypassed and q.source == "agent" and q.applied
    assert len(runner.calls_for(Role.SPEC_WRITER)) == 3
    questions_event = next(e for e in events(target_repo) if e["kind"] == "questions")
    assert questions_event["bypassed"] is True and questions_event["answerable"] is False
    report = (logdir(target_repo) / "report.md").read_text()
    assert "bypassed up front" in report and "you chose Bypass up front" in report
    assert f"**Answer (agent's best judgement):** {AGENT_ANSWER}" in git(
        target_repo, "show", "advpipe/r1:task.md"
    )
    # Resuming keeps it, and a config that says otherwise is refused.
    with pytest.raises(NotResumable, match="bypassed up front; a resumed run keeps that"):
        check_same_shape(state, with_pipeline(config, bypass_questions=False))
    check_same_shape(state, with_pipeline(config, bypass_questions=True))


async def test_bypass_up_front_at_the_cap_goes_on_without_another_revision(
    config: Config, target_repo: Path
) -> None:
    cfg = with_cap(with_pipeline(config, bypass_questions=True), 2)
    scripts = after_answers()
    scripts[Role.SPEC_WRITER] = [
        writes({"task.md": TASK_MD_BLOCKING}),
        bypass_reply("Q1"),
        writes({"task.md": second_question()}, "still has a question"),
        bypass_reply("Q2", answer="Yes."),
    ]
    runner = FakeAgentRunner(scripts)
    state = await Orchestrator(cfg, runner, target_repo, "add clamp", "r1", name="r1").run()

    assert state.status is Status.COMPLETE, state.notes
    assert state.spec_passes == 2 and len(runner.calls_for(Role.SPEC_WRITER)) == 4
    assert [q.applied for q in state.questions] == [True, True]
    assert any("the agent decided them and the run went on" in n for n in state.notes)
    committed = git(target_repo, "show", "advpipe/r1:task.md")
    assert f"**Q2:** {QUESTION_2}" in committed and "**Answer (agent's best judgement):** Yes." in (
        committed
    )
    (test_call,) = runner.calls_for(Role.TEST_AUTHOR)
    assert "**Answer (agent's best judgement):** Yes." in test_call.prompt
    assert (logdir(target_repo) / "spec" / "pass-2" / "bypass.json").is_file()


async def test_bypass_up_front_with_a_cap_of_one(config: Config, target_repo: Path) -> None:
    cfg = with_cap(with_pipeline(config, bypass_questions=True), 1)
    scripts = after_answers()
    scripts[Role.SPEC_WRITER] = [writes({"task.md": TASK_MD_BLOCKING}), bypass_reply("Q1")]
    state = await Orchestrator(
        cfg, FakeAgentRunner(scripts), target_repo, "add clamp", "r1", name="r1"
    ).run()
    assert state.status is Status.COMPLETE and state.spec_passes == 1


async def test_without_bypass_a_blocked_run_is_unchanged(config: Config, target_repo: Path) -> None:
    state = await blocked_run(config, target_repo)
    assert not state.bypass_questions and not state.questions[0].bypassed
    assert "Bypass" not in " ".join(e["message"] for e in events(target_repo))
    report = (logdir(target_repo) / "report.md").read_text()
    assert "Bypass" not in report and "bypassed" not in report


# --------------------------------------------------------------------------- front matter, config


def test_front_matter_bypass_questions() -> None:
    item = parse_work_item("---\nbypass_questions: yes\n---\nDo it\n")
    assert item.bypass_questions == "yes"
    assert format_work_item(item) == "---\nbypass_questions: yes\n---\nDo it\n"
    assert parse_work_item(format_work_item(item)) == item
    assert WorkItem(body="x").bypass_questions is None
    with pytest.raises(WorkItemError, match=r"f.md:2: 'bypass_questions': expected yes or no"):
        parse_work_item("---\nbypass_questions: maybe\n---\nDo it\n", "f.md")


def test_config_bypass_questions_defaults_off() -> None:
    assert Config().pipeline.bypass_questions is False
    cfg = Config.model_validate({"pipeline": {"bypass_questions": True}})
    assert cfg.pipeline.bypass_questions is True


@pytest.fixture
def run_script(monkeypatch: pytest.MonkeyPatch) -> list[FakeAgentRunner]:
    runners: list[FakeAgentRunner] = []
    scripts = after_answers()
    scripts[Role.SPEC_WRITER] = [
        writes({"task.md": TASK_MD_BLOCKING}),
        bypass_reply("Q1"),
        writes({"task.md": TASK_MD_REVISED}),
    ]

    def make() -> FakeAgentRunner:
        runners.append(FakeAgentRunner({r: list(s) for r, s in scripts.items()}))
        return runners[-1]

    monkeypatch.setattr(cli_module, "SdkAgentRunner", make)
    return runners


def write_gates_off(repo: Path, extra: str = "") -> None:
    """A pipeline.toml whose only check is pytest (the tests stage needs a test gate)."""
    test = json.dumps([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"])
    (repo / "pipeline.toml").write_text(
        f"[gates]\ntest = {test}\ntypes = []\nlint = []\nsecurity = []\n" + extra
    )


def test_run_bypass_questions_flag(target_repo: Path, run_script: list[FakeAgentRunner]) -> None:
    write_gates_off(target_repo)
    result = cli.invoke(
        app, ["run", "add clamp", "--repo", str(target_repo), "--bypass-questions", "-q"]
    )
    assert result.exit_code == 0, result.output
    run_id = result.stdout.split()[0]
    state = RunLog.for_run(target_repo, run_id).read_state()
    assert state.bypass_questions and state.status is Status.COMPLETE
    saved = RunLog.for_run(target_repo, run_id).read_config()
    assert saved is not None and saved.pipeline.bypass_questions is True


def test_run_bypass_precedence(target_repo: Path, run_script: list[FakeAgentRunner]) -> None:
    write_gates_off(target_repo, "[pipeline]\nbypass_questions = true\n")
    item = target_repo / "item.md"
    item.write_text("---\nbypass_questions: no\n---\nadd clamp\n")
    # Front matter beats the config: Bypass is off, so the run stops at the question.
    result = cli.invoke(app, ["run", "--item", str(item), "--repo", str(target_repo), "-q"])
    assert result.exit_code == 1 and "needs_human" in result.stdout
    # The command line beats front matter.
    result = cli.invoke(
        app,
        ["run", "--item", str(item), "--repo", str(target_repo), "--bypass-questions", "-q"],
    )
    assert result.exit_code == 0, result.output
    # Config alone turns it on.
    result = cli.invoke(app, ["run", "add clamp too", "--repo", str(target_repo), "-q"])
    assert result.exit_code == 0, result.output
    # ...and --no-bypass-questions turns it off again.
    result = cli.invoke(
        app, ["run", "add clamp three", "--repo", str(target_repo), "--no-bypass-questions", "-q"]
    )
    assert result.exit_code == 1 and "needs_human" in result.stdout


def test_run_front_matter_with_bad_value_is_refused(target_repo: Path) -> None:
    item = target_repo / "item.md"
    item.write_text("---\nbypass_questions: sure\n---\nadd clamp\n")
    result = cli.invoke(app, ["run", "--item", str(item), "--repo", str(target_repo)])
    assert result.exit_code == 2 and "'bypass_questions': expected yes or no" in result.output


# --------------------------------------------------------------------------- advpipe answer


@pytest.fixture
def fake_resume(monkeypatch: pytest.MonkeyPatch) -> list[FakeAgentRunner]:
    runners: list[FakeAgentRunner] = []

    def make() -> FakeAgentRunner:
        runners.append(FakeAgentRunner(bypassed_resume("Q1", "Q2")))
        return runners[-1]

    monkeypatch.setattr(cli_module, "SdkAgentRunner", make)
    return runners


def blocked_two(config: Config, repo: Path) -> None:
    asyncio.run(blocked_run(config, repo, second_blocking()))


def answer(repo: Path, *args: str, input: str | None = None):  # type: ignore[no-untyped-def]
    return cli.invoke(app, ["answer", "r1", "--repo", str(repo), *args], input=input)


def test_answer_bypass_all(
    config: Config, target_repo: Path, fake_resume: list[FakeAgentRunner]
) -> None:
    blocked_two(config, target_repo)
    result = answer(target_repo, "--bypass", "-q")
    assert result.exit_code == 0, result.output
    assert "Bypass: the agent decides Q1, Q2" in result.stderr
    state = state_of(target_repo)
    assert state.status is Status.COMPLETE
    assert [q.source for q in state.questions] == ["agent", "agent"]


def test_answer_bare_bypass_works_before_the_run_id_and_with_other_options(
    config: Config, target_repo: Path, fake_resume: list[FakeAgentRunner]
) -> None:
    blocked_two(config, target_repo)
    result = cli.invoke(
        app, ["answer", "--bypass", "r1", "--repo", str(target_repo), "--no-resume"]
    )
    assert result.exit_code == 0, result.output
    assert all(q.bypassed for q in state_of(target_repo).questions)


def test_answer_bypass_only_the_named_questions(
    config: Config, target_repo: Path, fake_resume: list[FakeAgentRunner]
) -> None:
    blocked_two(config, target_repo)
    result = answer(target_repo, "--answer", f"Q1={ANSWER}", "--bypass", "q2", "--no-resume")
    assert result.exit_code == 0, result.output
    q1, q2 = state_of(target_repo).questions
    assert q1.answer == ANSWER and not q1.bypassed and q2.bypassed and not q2.answer
    assert fake_resume == []


def test_answer_bypass_all_leaves_answered_questions_alone(
    config: Config, target_repo: Path, fake_resume: list[FakeAgentRunner]
) -> None:
    blocked_two(config, target_repo)
    result = answer(target_repo, "--answer", f"Q1={ANSWER}", "--bypass", "--no-resume")
    assert result.exit_code == 0, result.output
    q1, q2 = state_of(target_repo).questions
    assert q1.answer == ANSWER and not q1.bypassed and q2.bypassed


def test_answer_bypass_repeatable_and_comma_separated(
    config: Config, target_repo: Path, fake_resume: list[FakeAgentRunner]
) -> None:
    blocked_two(config, target_repo)
    assert answer(target_repo, "--bypass", "Q1", "--bypass", "Q2", "--no-resume").exit_code == 0
    assert all(q.bypassed for q in state_of(target_repo).questions)
    blocked_again = answer(target_repo, "--bypass", "Q1,Q2", "--no-resume")
    assert blocked_again.exit_code == 0, blocked_again.output


def test_answer_bypass_refusals(
    config: Config, target_repo: Path, fake_resume: list[FakeAgentRunner]
) -> None:
    blocked_two(config, target_repo)
    for args, message in [
        (["--answer", f"Q1={ANSWER}", "--bypass", "Q1"], "Q1 can't be both answered and bypassed"),
        (["--bypass", "Q7"], "Q7 isn't one of this run's questions"),
        (["--bypass", "Q1"], "Q2 still needs an answer"),
    ]:
        result = answer(target_repo, *args)
        assert result.exit_code == 2, args
        assert message in result.stderr, (args, result.stderr)
    assert not any(q.bypassed for q in state_of(target_repo).questions)
    assert fake_resume == []


def test_answers_file_bypass_line(
    config: Config, target_repo: Path, tmp_path: Path, fake_resume: list[FakeAgentRunner]
) -> None:
    blocked_two(config, target_repo)
    template = answer(target_repo, "--template").stdout
    assert "BYPASS instead of an answer" in template
    filled = tmp_path / "answers.md"
    filled.write_text(f"Q1: {ANSWER}\nQ2: BYPASS\n")
    result = answer(target_repo, "--file", str(filled), "--no-resume")
    assert result.exit_code == 0, result.output
    q1, q2 = state_of(target_repo).questions
    assert q1.answer == ANSWER and q2.bypassed
    # Only the exact word counts: anything else is an answer.
    assert answers.split_bypass({"Q1": "BYPASS", "Q2": "bypass it", "Q3": "x BYPASS"}) == (
        {"Q2": "bypass it", "Q3": "x BYPASS"},
        {"Q1"},
    )


def test_interactive_empty_answer_offers_bypass(
    config: Config,
    target_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_resume: list[FakeAgentRunner],
) -> None:
    blocked_two(config, target_repo)
    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: True)
    # Q1: empty, declined, then answered. Q2: empty, accepted.
    result = answer(target_repo, "--no-resume", input=f"\nn\n{ANSWER}\n\ny\n")
    assert result.exit_code == 0, result.output
    assert "Bypass Q1?" in result.output and "Bypass Q2?" in result.output
    q1, q2 = state_of(target_repo).questions
    assert q1.answer == ANSWER and not q1.bypassed and q2.bypassed


def test_status_shows_bypassed_and_agent_answers(
    config: Config, target_repo: Path, fake_resume: list[FakeAgentRunner]
) -> None:
    blocked_two(config, target_repo)
    answer(target_repo, "--bypass", "--no-resume")
    out = cli.invoke(app, ["status", "r1", "--repo", str(target_repo)]).output
    assert "(Bypass: the agent decides when the run resumes)" in out and "(waiting)" not in out
    answer(target_repo, "-q")
    out = cli.invoke(app, ["status", "r1", "--repo", str(target_repo)]).output
    assert "answer (agent's best judgement):" in out


def test_resume_after_bypass_only_says_nothing_is_waiting(
    config: Config, target_repo: Path, fake_resume: list[FakeAgentRunner]
) -> None:
    blocked_two(config, target_repo)
    answer(target_repo, "--bypass", "--no-resume")
    result = cli.invoke(app, ["resume", "r1", "--repo", str(target_repo), "-q"])
    assert result.exit_code == 0, result.output
    assert state_of(target_repo).status is Status.COMPLETE
