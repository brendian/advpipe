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
from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path

from advpipe.config import Config
from advpipe.events import emit
from advpipe.levels import SHAPES
from advpipe.models import OpenQuestion, RunState, Stage, Status
from advpipe.runlog import RunLocked, RunLog
from advpipe.workspace import GitError, branch_exists

# Each answer reaches `advpipe answer` as one command-line argument when it comes from the web
# UI: stay well under Linux's 128 KiB per argument and Windows' 32 KiB command line.
MAX_ANSWER_CHARS = 8_000
MAX_TOTAL_ANSWER_CHARS = 24_000

QUESTION_ID_RE = re.compile(r"Q[1-9][0-9]{0,3}")
ANSWERS_HEADING = "## Answers"
# In an answers file, `Q2: BYPASS` (exactly, upper case) means "let the agent decide Q2".
BYPASS_WORD = "BYPASS"
MAX_REASONING_CHARS = 2_000
_ANSWERS_SECTION_RE = re.compile(r"^##\s*Answers\s*$.*?(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)


class AnswerError(ValueError):
    """Answers that can't be recorded; the message is meant for the person answering."""


def normalise(text: str) -> str:
    """For recognising the same question asked again: lower case, whitespace collapsed."""
    return " ".join(text.lower().split()).rstrip("?. ")


# --------------------------------------------------------------------------- run state


def spec_stage(state: RunState) -> tuple[Stage, str]:
    """The stage that writes task.md, and its commit key: SPEC and ``spec`` normally, SOLO and
    ``solo`` at the levels where one agent does it all (basic and reviewer)."""
    if SHAPES[state.level].solo:
        return Stage.SOLO, "solo"
    return Stage.SPEC, "spec"


def migrate(state: RunState) -> RunState:
    """Fill ``questions`` for a run stopped by an older advpipe, which kept its blocking
    questions only as open findings (``Q1``... with criterion ``open-question``)."""
    if (
        state.questions
        or state.status is not Status.NEEDS_HUMAN
        or state.stage is not spec_stage(state)[0]
    ):
        return state
    old = [f for f in state.open_findings if f.criterion == "open-question"]
    if not old:
        return state
    questions = [OpenQuestion(id=f.id, text=f.claim, asked_in=1) for f in old]
    return state.model_copy(update={"questions": questions, "spec_passes": state.spec_passes or 1})


def pending(state: RunState) -> list[OpenQuestion]:
    """Questions nobody has dealt with yet: no answer, and no Bypass chosen."""
    return [q for q in state.questions if not q.answer and not q.bypassed]


def unapplied(state: RunState) -> list[OpenQuestion]:
    """Answered, but no spec revision has used the answer yet."""
    return [q for q in state.questions if q.answer and not q.applied]


def awaiting_bypass(state: RunState) -> list[OpenQuestion]:
    """Questions the person bypassed that the agent hasn't answered yet (the orchestrator
    asks the agent, once, when the run resumes)."""
    return [q for q in state.questions if q.bypassed and not q.answer]


def to_answer(state: RunState) -> list[OpenQuestion]:
    """The questions an answer is wanted for now: those no spec pass has used an answer for
    (pending ones, and ones answered since the run stopped, whose answers can be replaced)."""
    return [q for q in state.questions if not q.applied]


def answered(state: RunState) -> list[OpenQuestion]:
    return [q for q in state.questions if q.answer]


def _blocked_at_spec(state: RunState, max_passes: int) -> bool:
    """Stopped at SPEC (or SOLO) on blocking questions, with spec passes left to answer them."""
    stage, key = spec_stage(state)
    return (
        state.status is Status.NEEDS_HUMAN
        and state.stage is stage
        and key not in state.commits
        and bool(state.questions)
        and state.spec_passes < max_passes
    )


def awaiting_answers(state: RunState, max_passes: int) -> bool:
    """The run is waiting for a person to answer its spec's blocking questions."""
    return _blocked_at_spec(state, max_passes) and bool(pending(state))


def answers_ready(state: RunState, max_passes: int) -> bool:
    """Every question is answered or bypassed, and resuming will give the answers (the
    agent's, for bypassed ones) to the spec writer."""
    return (
        _blocked_at_spec(state, max_passes)
        and not pending(state)
        and bool(unapplied(state) or awaiting_bypass(state))
    )


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


def normalise_ids(ids: Collection[str]) -> set[str]:
    return {qid.strip().upper() for qid in ids}


def check_answers(
    state: RunState, answers: dict[str, str], bypass: Collection[str] = ()
) -> dict[str, str]:
    """The answers, stripped, if every question no spec pass has used an answer for yet is
    either answered or bypassed (``bypass``: ids the agent decides). Raises AnswerError saying
    what's wrong otherwise."""
    # Questions no spec pass has used an answer for yet: pending ones, and ones answered since
    # the run stopped (answering again before resuming replaces those answers).
    waiting = {q.id for q in to_answer(state)}
    known = {q.id for q in state.questions}
    skipped = normalise_ids(bypass)
    for qid in sorted(skipped, key=_qnum):
        if qid not in known:
            raise AnswerError(
                f"{qid} isn't one of this run's questions ({', '.join(sorted(known))})"
            )
        if qid not in waiting:
            raise AnswerError(f"{qid} is already answered and used by the spec")
    cleaned: dict[str, str] = {}
    for qid, text in answers.items():
        qid = qid.strip().upper()
        if qid not in known:
            raise AnswerError(
                f"{qid} isn't one of this run's questions ({', '.join(sorted(known))})"
            )
        if qid not in waiting:
            raise AnswerError(f"{qid} is already answered and used by the spec")
        if qid in skipped:
            raise AnswerError(f"{qid} can't be both answered and bypassed; choose one")
        text = text.strip()
        if not text:
            raise AnswerError(f"the answer to {qid} is empty")
        if len(text) > MAX_ANSWER_CHARS:
            raise AnswerError(
                f"the answer to {qid} is {len(text):,} characters; keep each answer under "
                f"{MAX_ANSWER_CHARS:,}"
            )
        cleaned[qid] = text
    missing = sorted(waiting - set(cleaned) - skipped, key=_qnum)
    if missing:
        raise AnswerError(
            f"answer every question: {', '.join(missing)} still "
            + ("needs an answer" if len(missing) == 1 else "need answers")
            + " (or Bypass it, to let the agent decide)"
        )
    total = sum(len(t) for t in cleaned.values())
    if total > MAX_TOTAL_ANSWER_CHARS:
        raise AnswerError(
            f"the answers are {total:,} characters together; keep them under "
            f"{MAX_TOTAL_ANSWER_CHARS:,}"
        )
    return cleaned


def record_answers(
    repo: Path, run_id: str, answers: dict[str, str], bypass: Collection[str] = ()
) -> RunState:
    """Save answers to a run's blocking questions in its run.json. Doesn't resume the run.

    All-or-nothing: every pending question must be answered, or bypassed (``bypass``: the
    person lets the agent decide; it answers when the run resumes). Raises AnswerError if the run
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
            raise AnswerError(not_waiting_message(state, cap))
        if not _branch_exists(repo, state.branch):
            raise AnswerError(
                f"run {run_id} can't be resumed: its branch {state.branch} no longer exists "
                "(cleaned up?). Start a new run instead."
            )
        cleaned = check_answers(state, answers, bypass)
        skipped = normalise_ids(bypass)
        now = datetime.now(UTC)
        for q in state.questions:
            if q.id in cleaned:
                q.answer, q.answered_at, q.applied = cleaned[q.id], now, False
                q.source, q.reasoning, q.bypassed = "person", "", False
            elif q.id in skipped:
                # The agent answers when the run resumes (see stages.bypass_questions).
                q.answer, q.answered_at, q.applied = "", None, False
                q.source, q.reasoning, q.bypassed = "person", "", True
        log.write_state(state)
        ids = sorted(cleaned, key=_qnum)
        bypassed = sorted(skipped, key=_qnum)
        said = f"answers recorded for {', '.join(ids)}" if ids else "answers recorded"
        if bypassed:
            said += f"{';' if ids else ':'} Bypass for {', '.join(bypassed)}"
        if bypassed:
            emit(log, state, lambda m: None, "answers", said, ids=ids, bypassed=bypassed)
        else:
            emit(log, state, lambda m: None, "answers", said, ids=ids)
        return state
    finally:
        log.release_lock()


def _branch_exists(repo: Path, branch: str) -> bool:
    try:
        return bool(branch) and branch_exists(repo.resolve(), branch)
    except GitError:
        return False


def not_waiting_message(state: RunState, cap: int) -> str:
    """Why a run can't take answers now, for the person trying."""
    run = state.run_id
    stage, _ = spec_stage(state)
    d = state.decision
    if d is not None and d.kind == "spec_cap" and not d.choice:
        return (
            f"run {run}'s spec still has blocking questions after {state.spec_passes} spec "
            f"pass(es), the limit (limits.max_rounds_spec), so they can't be answered one more "
            f"time. Begin work anyway, and the agent decides them (and any later question), or "
            f"stop: advpipe decide {run} begin | stop"
        )
    if (
        state.status is Status.NEEDS_HUMAN
        and state.stage is stage
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


def split_bypass(given: dict[str, str]) -> tuple[dict[str, str], set[str]]:
    """Answers from a file, split into real answers and the ids whose whole answer is the word
    ``BYPASS`` (let the agent decide)."""
    answers = {qid: text for qid, text in given.items() if text != BYPASS_WORD}
    return answers, set(given) - set(answers)


def answers_template(state: RunState) -> str:
    """An answers file to fill in, for `advpipe answer --template`."""
    lines = [f"# Answers for run {state.run_id}. Lines starting with # are ignored."]
    lines.append("# Write each answer after its 'Qn:'; it may go on over several lines.")
    lines.append(
        f"# Write {BYPASS_WORD} instead of an answer to let the agent decide that question."
    )
    for q in to_answer(state):
        lines += [f"# {q.id}: {_one_line(q.text)}", f"{q.id}: {q.answer}".rstrip() + " "]
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
    by_agent = any(q.source == "agent" for q in done)
    if by_agent:
        # The person chose Bypass for some questions: say so, so later agents and the human
        # reviewer can tell the agent's decisions from the person's words.
        intro = (
            "Answers to the blocking questions. They override anything above that disagrees. "
            'Answers marked "agent\'s best judgement" were chosen by the agent because the '
            "person chose Bypass."
        )
    else:
        intro = (
            "Answers from the person who started this run. They override anything above that "
            "disagrees."
        )
    lines = [ANSWERS_HEADING, intro, ""]
    for q in done:
        lines.append(f"- **{q.id}:** {_one_line(q.text)}")
        answer = q.answer.strip().splitlines() or [""]
        label = "Answer (agent's best judgement)" if q.source == "agent" else "Answer"
        lines.append(f"  **{label}:** {answer[0]}")
        lines += [f"  {line}" if line.strip() else "" for line in answer[1:]]
        if q.source == "agent" and q.reasoning.strip():
            lines.append(f"  Reason: {_one_line(q.reasoning)}")
    return body + "\n\n" + "\n".join(lines).rstrip() + "\n"
