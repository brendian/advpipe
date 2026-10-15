"""Decision points: where a run stops and asks a person to choose (docs/LEVELS_PLAN.md §3.5).

A decision point has status ``needs_human``, like every other stop, and one of four kinds:

- ``red_checks``: the checks are red and the pipeline has nothing automatic left to try.
  Choices: ``fix`` (one more pass by the stage's author, with an optional note) or ``takeover``.
- ``scan_hits``: the security scan failed below level full, and the scan policy is ``ask``.
  Choices: ``ignore`` (blow past them) or ``block`` (treat them like a failing test).
- ``usage_limit``: a subscription usage limit or the Console credit balance ran out mid-run.
  Choices: ``retry`` (the interrupted stage starts again, partial work kept) or ``finish``
  (end the run as it is: nothing committed, the worktree kept with its changes).
- ``spec_cap``: the spec's last allowed pass still has blocking questions. Choices: ``begin``
  (the agent decides them, and every later question) or ``stop``.

Only a person picks a choice (`advpipe decide`, or the web UI later); no code path does.
``takeover`` and ``stop`` end the run at once. The other choices are applied when the run
resumes, with the worktree kept as it was (``Workspace.reopen(keep_changes=True)``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from advpipe.config import Config
from advpipe.events import emit
from advpipe.models import (
    DECISION_CHOICES,
    Decision,
    DecisionKind,
    Finding,
    RunState,
    Stage,
    Status,
)
from advpipe.runlog import RunLocked, RunLog, render_report
from advpipe.workspace import GitError, Workspace, branch_exists

CHOICES = DECISION_CHOICES
CHOICE_LABELS = {
    "fix": "Another fix pass (add --note to guide it)",
    "takeover": "Or take over (commits the work, removes the worktree)",
    "ignore": "Ignore the hits and carry on",
    "block": "Or treat the hits like failing checks",
    "retry": "Try again (after adding usage or credits, or the reset)",
    "finish": "Or finish as is (the work stays in the worktree, uncommitted)",
    "begin": "Begin work: the agent decides the questions",
    "stop": "Or stop here",
}
# Choices that end the run on the spot; the rest are applied when the run resumes.
ENDING = frozenset({"takeover", "stop", "finish"})
MAX_NOTE_CHARS = 8_000  # as for answers: the UI passes it as one command-line argument
HANDED_OVER_COMMIT = "advpipe: work in progress, handed over (checks failing)"


class DecisionError(ValueError):
    """A decision that can't be recorded; the message is meant for the person deciding."""


class DecisionNeeded(Exception):  # noqa: N818 - a signal, not an error
    """Raised from deep inside a stage when a person has to decide (the scan policy is ``ask``
    and the scan failed). ``Orchestrator.run`` turns it into a decision point."""

    def __init__(self, kind: DecisionKind, findings: list[Finding], explanation: str) -> None:
        super().__init__(explanation)
        self.kind = kind
        self.findings = findings
        self.explanation = explanation


# --------------------------------------------------------------------------- run state


def awaiting_decision(state: RunState) -> bool:
    """The run stopped at a decision point and nobody has chosen yet."""
    d = state.decision
    return (
        state.status is Status.NEEDS_HUMAN and d is not None and not d.choice and not state.ended_by
    )


def current(state: RunState) -> Decision | None:
    """The decision that is in force: the latest, except that a *Try again* already acted on
    doesn't hide the choice made before it. A fix pass a usage limit cut short is still the
    person's choice; retrying the limit carries it on."""
    for d in reversed(state.decisions):
        if d.kind == "usage_limit" and d.applied:
            continue
        return d
    return None


def pending_choice(state: RunState) -> bool:
    """A continuing choice (fix, ignore, block, begin, retry) has been made and not acted on
    yet. It survives an interrupted run: a plain `advpipe resume` acts on it."""
    d = current(state)
    return (
        d is not None
        and bool(d.choice)
        and d.choice not in ENDING
        and not d.applied
        and not state.ended_by
    )


def decision_ready(state: RunState) -> bool:
    """The run waits at a decision point whose choice has been made, so resuming applies it."""
    return state.status is Status.NEEDS_HUMAN and pending_choice(state)


def take(state: RunState, kind: DecisionKind, choice: str, *stages: Stage) -> Decision | None:
    """The decided, not yet applied decision of ``kind`` with ``choice`` (at one of ``stages``,
    if given), or None."""
    d = current(state)
    if d is None or d.kind != kind or d.choice != choice or d.applied:
        return None
    if stages and d.stage not in stages:
        return None
    return d


def choices_text(kind: DecisionKind) -> str:
    return " | ".join(CHOICES[kind])


def decide_command(run_id: str, kind: DecisionKind | None = None) -> str:
    return f"advpipe decide {run_id} " + (choices_text(kind) if kind else "CHOICE")


def waiting_message(state: RunState) -> str:
    """Why a run can't resume yet, naming the command, for a run waiting on a decision."""
    d = state.decision
    assert d is not None
    return f"run {state.run_id} is waiting for a decision: {decide_command(state.run_id, d.kind)}"


def ended_message(state: RunState) -> str:
    how = {
        "takeover": "handed over to you",
        "stop": "stopped at your choice",
        "finish": "finished as it was, at your choice",
    }[state.ended_by]
    return f"run {state.run_id} ended: {how}; nothing to resume"


def max_fix_passes(log: RunLog) -> int:
    """The run's cap on person-requested fix passes, from the config it ran with."""
    try:
        cfg = log.read_config()
    except (OSError, ValueError):
        cfg = None
    return (cfg or Config()).limits.max_fix_passes


def not_waiting_message(state: RunState) -> str:
    if state.ended_by:
        return ended_message(state)
    return (
        f"run {state.run_id} isn't waiting for a decision "
        f"(it's {state.status.value} at {state.stage.value})"
    )


# --------------------------------------------------------------------------- deciding


def decide(repo: Path, run_id: str, choice: str, note: str = "") -> RunState:
    """Record a person's choice at a run's decision point.

    ``takeover`` and ``stop`` act at once (commit the work and remove the worktree, or just
    end the run). The other choices leave the run ``needs_human`` until something resumes it.
    Raises DecisionError if the run isn't waiting for a decision, the choice isn't one of that
    kind's, a live process drives the run, the branch is gone, the note doesn't fit, or ``fix``
    is past ``limits.max_fix_passes``.
    """
    repo = repo.resolve()
    log = RunLog.for_run(repo, run_id)
    if not (log.root / "run.json").is_file():
        raise DecisionError(f"no run {run_id} in {repo}")
    try:
        log.acquire_lock()
    except RunLocked as e:
        raise DecisionError(f"run {run_id} is still being driven ({e}); wait for it to stop") from e
    try:
        state = log.read_state()
        d = state.decision
        if not awaiting_decision(state) or d is None:
            raise DecisionError(not_waiting_message(state))
        choice = choice.strip().lower()
        if choice not in CHOICES[d.kind]:
            raise DecisionError(
                f"{choice!r} isn't a choice here: choose {' or '.join(CHOICES[d.kind])} "
                f"({decide_command(run_id, d.kind)})"
            )
        if note and choice != "fix":
            raise DecisionError("a note goes with the fix choice only")
        if len(note) > MAX_NOTE_CHARS:
            raise DecisionError(
                f"the note is {len(note):,} characters; keep it under {MAX_NOTE_CHARS:,}"
            )
        if choice == "fix" and state.fix_passes >= max_fix_passes(log):
            raise DecisionError(
                f"run {run_id} has used all {state.fix_passes} fix passes it's allowed "
                "(limits.max_fix_passes): take over, or start a new run"
            )
        if choice not in ("stop", "finish") and not _branch_exists(repo, state.branch):
            raise DecisionError(
                f"run {run_id} can't go on: its branch {state.branch} no longer exists "
                "(cleaned up?). Start a new run instead."
            )
        d.choice, d.note, d.decided_at = choice, note.strip() if note else "", datetime.now(UTC)
        if choice == "takeover":
            _take_over(repo, state)
        elif choice == "stop":
            state.ended_by = "stop"
            state.notes.append(f"Stopped at your choice at {d.stage.value}.")
        elif choice == "finish":
            state.ended_by = "finish"
            where = f" The uncommitted work is in {state.worktree}." if state.worktree else ""
            state.notes.append(f"Finished as is at {d.stage.value}.{where}")
        log.write_state(state)
        emit(
            log,
            state,
            lambda m: None,
            "decision",
            f"you chose {choice} ({d.kind})",
            decision_kind=d.kind,
            choice=choice,
            decision_stage=d.stage.value,
        )
        log.write_text("report.md", render_report(state))
        return state
    finally:
        log.release_lock()


def _take_over(repo: Path, state: RunState) -> None:
    """Commit the work as it is to the run's branch, remove the worktree (the branch stays)
    and end the run, so the person can check the branch out."""
    path = Path(state.worktree)
    if path.is_dir():
        ws = Workspace(repo, path, state.branch)
        committed = False
        if ws.has_changes():
            ws.commit(HANDED_OVER_COMMIT)
            committed = True
        ws.remove()
        state.worktree_removed = True
        what = "committed the work as it was and removed the worktree"
        if not committed:
            what = "removed the worktree (nothing was uncommitted)"
    else:
        what = "the worktree was already gone"
    state.ended_by = "takeover"
    state.notes.append(
        f"Handed over to you at {state.decisions[-1].stage.value}: {what}. "
        f"The branch {state.branch} has the work as it was."
    )


def _branch_exists(repo: Path, branch: str) -> bool:
    try:
        return bool(branch) and branch_exists(repo, branch)
    except GitError:
        return False
