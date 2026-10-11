"""Blocking questions from the spec writer, and the person's answers (docs/ANSWERS_PLAN.md).

When the spec writer marks an open question ``BLOCKING:``, the run stops at SPEC with status
``needs_human`` and the question in ``RunState.questions``. A person answers with
`advpipe answer` (or the web UI); the answers are saved in run.json, and resuming the run sends
the spec writer a revision pass with them. The orchestrator then writes the answers into
task.md itself (``with_answers_section``), so later agents see them verbatim.

Everything that asks "is this run waiting on answers?" uses the functions here, so the CLI,
the web UI and the orchestrator always agree.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from advpipe.config import Config
from advpipe.events import emit
from advpipe.models import OpenQuestion, RunState, Stage, Status
from advpipe.runlog import RunLocked, RunLog
from advpipe.workspace import GitError, branch_exists

# Each answer reaches `advpipe answer` as one command-line argument when it comes from the web
# UI: stay well under Linux's 128 KiB per argument and Windows' 32 KiB command line.
MAX_ANSWER_CHARS = 8_000
MAX_TOTAL_ANSWER_CHARS = 24_000

QUESTION_ID_RE = re.compile(r"Q[1-9][0-9]{0,3}")
ANSWERS_HEADING = "## Answers"
_ANSWERS_SECTION_RE = re.compile(r"^##\s*Answers\s*$.*?(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)


class AnswerError(ValueError):
    """Answers that can't be recorded; the message is meant for the person answering."""


def normalise(text: str) -> str:
    """For recognising the same question asked again: lower case, whitespace collapsed."""
    return " ".join(text.lower().split()).rstrip("?. ")


# --------------------------------------------------------------------------- run state


def migrate(state: RunState) -> RunState:
    """Fill ``questions`` for a run stopped by an older advpipe, which kept its blocking
    questions only as open findings (``Q1``... with criterion ``open-question``)."""
    if state.questions or state.status is not Status.NEEDS_HUMAN or state.stage is not Stage.SPEC:
        return state
    old = [f for f in state.open_findings if f.criterion == "open-question"]
    if not old:
        return state
    questions = [OpenQuestion(id=f.id, text=f.claim, asked_in=1) for f in old]
    return state.model_copy(update={"questions": questions, "spec_passes": state.spec_passes or 1})


def pending(state: RunState) -> list[OpenQuestion]:
    """Questions with no answer yet."""
    return [q for q in state.questions if not q.answer]


def unapplied(state: RunState) -> list[OpenQuestion]:
    """Answered, but no spec revision has used the answer yet."""
    return [q for q in state.questions if q.answer and not q.applied]


def answered(state: RunState) -> list[OpenQuestion]:
    return [q for q in state.questions if q.answer]


def _blocked_at_spec(state: RunState, max_passes: int) -> bool:
    """Stopped at SPEC on blocking questions, with spec passes left to answer them."""
    return (
        state.status is Status.NEEDS_HUMAN
        and state.stage is Stage.SPEC
        and "spec" not in state.commits
        and bool(state.questions)
        and state.spec_passes < max_passes
    )


def awaiting_answers(state: RunState, max_passes: int) -> bool:
    """The run is waiting for a person to answer its spec's blocking questions."""
    return _blocked_at_spec(state, max_passes) and bool(pending(state))


def answers_ready(state: RunState, max_passes: int) -> bool:
    """Every question is answered, and resuming will give the answers to the spec writer."""
    return _blocked_at_spec(state, max_passes) and not pending(state) and bool(unapplied(state))


def max_spec_passes(log: RunLog) -> int:
    """The run's spec-pass cap, from the config it ran with."""
    try:
        cfg = log.read_config()
    except (OSError, ValueError):
        cfg = None
    return (cfg or Config()).limits.max_rounds_spec


def answer_command(run_id: str) -> str:
    return f"advpipe answer {run_id}"


# --------------------------------------------------------------------------- recording answers


def check_answers(state: RunState, answers: dict[str, str]) -> dict[str, str]:
    """The answers, stripped, if they answer every question no spec pass has used an answer
    for yet. Raises AnswerError saying what's wrong otherwise."""
    # Questions no spec pass has used an answer for yet: pending ones, and ones answered since
    # the run stopped (answering again before resuming replaces those answers).
    waiting = {q.id for q in state.questions if not q.applied}
    known = {q.id for q in state.questions}
    cleaned: dict[str, str] = {}
    for qid, text in answers.items():
        qid = qid.strip().upper()
        if qid not in known:
            raise AnswerError(
                f"{qid} isn't one of this run's questions ({', '.join(sorted(known))})"
            )
        if qid not in waiting:
            raise AnswerError(f"{qid} is already answered and used by the spec")
        text = text.strip()
        if not text:
            raise AnswerError(f"the answer to {qid} is empty")
        if len(text) > MAX_ANSWER_CHARS:
            raise AnswerError(
                f"the answer to {qid} is {len(text):,} characters; keep each answer under "
                f"{MAX_ANSWER_CHARS:,}"
            )
        cleaned[qid] = text
    missing = sorted(waiting - set(cleaned), key=_qnum)
    if missing:
        raise AnswerError(
            f"answer every question: {', '.join(missing)} still "
            + ("needs an answer" if len(missing) == 1 else "need answers")
        )
    total = sum(len(t) for t in cleaned.values())
    if total > MAX_TOTAL_ANSWER_CHARS:
        raise AnswerError(
            f"the answers are {total:,} characters together; keep them under "
            f"{MAX_TOTAL_ANSWER_CHARS:,}"
        )
    return cleaned


def record_answers(repo: Path, run_id: str, answers: dict[str, str]) -> RunState:
    """Save answers to a run's blocking questions in its run.json. Doesn't resume the run.

    All-or-nothing: every pending question must be answered. Raises AnswerError if the run
    isn't waiting on answers, is being driven by a live process, can't be resumed (its
    branch is gone), or the answers don't fit (see ``check_answers``).
    """
    log = RunLog.for_run(repo.resolve(), run_id)
    if not (log.root / "run.json").is_file():
        raise AnswerError(f"no run {run_id} in {repo}")
    try:
        log.acquire_lock()
    except RunLocked as e:
        raise AnswerError(f"run {run_id} is still being driven ({e}); wait for it to stop") from e
    try:
        state = migrate(log.read_state())
        cap = max_spec_passes(log)
        if not (awaiting_answers(state, cap) or answers_ready(state, cap)):
            raise AnswerError(_not_waiting(state, cap))
        if not _branch_exists(repo, state.branch):
            raise AnswerError(
                f"run {run_id} can't be resumed: its branch {state.branch} no longer exists "
                "(cleaned up?). Start a new run instead."
            )
        cleaned = check_answers(state, answers)
        now = datetime.now(UTC)
        for q in state.questions:
            if q.id in cleaned:
                q.answer, q.answered_at, q.applied = cleaned[q.id], now, False
        log.write_state(state)
        ids = sorted(cleaned, key=_qnum)
        emit(
            log, state, lambda message: None, "answers",
            f"answers recorded for {', '.join(ids)}", ids=ids,
        )  # fmt: skip
        return state
    finally:
        log.release_lock()


def _branch_exists(repo: Path, branch: str) -> bool:
    try:
        return bool(branch) and branch_exists(repo.resolve(), branch)
    except GitError:
        return False


def _not_waiting(state: RunState, cap: int) -> str:
    run = state.run_id
    if (
        state.status is Status.NEEDS_HUMAN
        and state.stage is Stage.SPEC
        and state.questions
        and state.spec_passes >= cap
    ):
        return (
            f"run {run}'s spec still has blocking questions after {state.spec_passes} spec "
            f"pass(es), the limit (limits.max_rounds_spec). Reword the work item and start a "
            "new run."
        )
    return f"run {run} isn't waiting on answers (it's {state.status.value} at {state.stage.value})"


def _qnum(qid: str) -> int:
    return int(qid[1:]) if QUESTION_ID_RE.fullmatch(qid) else 0


# --------------------------------------------------------------------------- answers files


def parse_answers_file(text: str) -> dict[str, str]:
    """Answers from a file: a line starting ``Q<n>:`` begins that answer; the lines after it
    belong to it until the next ``Q<n>:`` line. Lines starting with ``#`` are ignored."""
    answers: dict[str, list[str]] = {}
    current: str | None = None
    start = re.compile(r"^(Q[0-9]+)\s*:\s?(.*)$", re.IGNORECASE)
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = start.match(line)
        if m:
            current = m.group(1).upper()
            if current in answers:
                raise AnswerError(f"{current} is answered twice in the file")
            answers[current] = [m.group(2)]
        elif current is not None:
            answers[current].append(line)
        elif line.strip():
            raise AnswerError(f"text before the first 'Q1:' line: {line.strip()[:60]!r}")
    if not answers:
        raise AnswerError("no answers found: each answer starts with a line like 'Q1: ...'")
    return {qid: "\n".join(lines).strip() for qid, lines in answers.items()}


def answers_template(state: RunState) -> str:
    """An answers file to fill in, for `advpipe answer --template`."""
    lines = [f"# Answers for run {state.run_id}. Lines starting with # are ignored."]
    lines.append("# Write each answer after its 'Qn:'; it may go on over several lines.")
    for q in pending(state):
        lines += [f"# {q.id}: {_one_line(q.text)}", f"{q.id}: "]
    return "\n".join(lines) + "\n"


def _one_line(text: str) -> str:
    return " ".join(text.split())


# --------------------------------------------------------------------------- task.md


def with_answers_section(task_md: str, questions: list[OpenQuestion]) -> str:
    """task.md with its ``## Answers`` section replaced by every answered question, verbatim.

    Written by the orchestrator, not an agent, so the person's words can't be dropped or
    reworded. Answers are indented under their list item, so no line of an answer can start
    a heading or a list item of its own (``stages.blocking_open_questions`` never sees them).
    """
    body = _ANSWERS_SECTION_RE.sub("", task_md).rstrip()
    done = [q for q in questions if q.answer]
    if not done:
        return body + "\n"
    lines = [
        ANSWERS_HEADING,
        "Answers from the person who started this run. They override anything above that "
        "disagrees.",
        "",
    ]
    for q in done:
        lines.append(f"- **{q.id}:** {_one_line(q.text)}")
        answer = q.answer.strip().splitlines() or [""]
        lines.append(f"  **Answer:** {answer[0]}")
        lines += [f"  {line}" if line.strip() else "" for line in answer[1:]]
    return body + "\n\n" + "\n".join(lines).rstrip() + "\n"
