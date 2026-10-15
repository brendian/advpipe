"""What the run detail page shows, read from one run directory (.advpipe/runs/<id>/).

Everything comes from files the pipeline already writes (see docs/ARCHITECTURE.md,
"Persistence"); nothing here changes them. A file that's missing or can't be parsed is shown as
such rather than failing the page: a live run writes them one at a time.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from markupsafe import Markup
from pydantic import ValidationError

from advpipe import answers, decisions, usage
from advpipe.config import Config
from advpipe.control import CONSOLE_LOG
from advpipe.gates import GateResult
from advpipe.levels import (
    LEVEL_INFO,
    SHAPES,
    dropped_safeguards,
    level_number,
    roles_for,
    scan_on,
    steps_for,
)
from advpipe.models import (
    RESUMABLE,
    ArbiterRuling,
    Decision,
    Level,
    OpenQuestion,
    Ruling,
    RunState,
    Stage,
    Status,
    Verdict,
)
from advpipe.runlog import RunLog
from advpipe.stages import parse_model
from advpipe.ui import usage_view
from advpipe.ui.actions import VALID, suggested_budget
from advpipe.ui.render import markdown
from advpipe.ui.runs import (
    STAGE_LABELS,
    STATUSES,
    StatusInfo,
    display_status,
    stage_label,
    title_of,
)
from advpipe.ui.usage_view import UsageView

# A diff longer than this is cut in the page; the full file stays in the run directory.
MAX_DIFF_LINES = 3000

# The timeline of a full run, in pipeline order. The arbiter runs only when findings are left
# open, right after the stage that left them; it's shown as one step between REVIEW and FINAL
# CHECKS. A run's own timeline is ``steps_for(state.level)``.
TIMELINE = steps_for(Level.FULL)
# Mainline stage -> the key it records in RunState.commits when it finishes.
COMMIT_KEYS = {
    Stage.SPEC: "spec",
    Stage.SOLO: "solo",
    Stage.TESTS: "tests",
    Stage.CODE: "code",
    Stage.REVIEW: "review",
}
# The final author pass of each kind (final-pass-<kind>/): who wrote it, and the step it
# belongs to at a level without an arbiter.
FINAL_PASS = {
    "tests": ("test author", Stage.TESTS),
    "code": ("coder", Stage.CODE),
    "solo": ("solo agent", Stage.SOLO),
}

# Timeline step states: (label, explanation).
STEP_STATES: dict[str, tuple[str, str]] = {
    "done": ("done", "Finished."),
    "current": ("in progress", "An agent or check is working on this step right now."),
    "stopped": ("stopped here", "The run stopped during this step. The report says why."),
    "pending": ("not yet", "Not started yet."),
    "not_reached": ("not reached", "The run ended before this step."),
    "not_needed": ("not needed", "No findings were left open, so the arbiter wasn't called."),
    "if_needed": ("only if needed", "Runs only if findings are left open after a stage."),
}


@dataclass(frozen=True)
class DiffLine:
    kind: str  # "meta", "hunk", "add", "del" or "ctx": a CSS class
    text: str


@dataclass(frozen=True)
class DiffView:
    path: str  # relative to the run directory
    lines: list[DiffLine]
    total: int  # lines in the file; more than len(lines) when cut

    @property
    def truncated(self) -> bool:
        return self.total > len(self.lines)

    @property
    def stat(self) -> str:
        added = sum(line.kind == "add" for line in self.lines)
        removed = sum(line.kind == "del" for line in self.lines)
        return f"+{added} −{removed}"


@dataclass(frozen=True)
class CriticView:
    role: str  # "code critic"
    verdict: Verdict | None  # None: output missing or unparseable (raw is shown instead)
    raw: str
    path: str

    @property
    def dropped(self) -> int:
        """Findings thrown away as noise because they cited no file+line or criterion."""
        return len(self.verdict.noise) if self.verdict else 0


@dataclass(frozen=True)
class GatesView:
    path: str
    results: list[GateResult]
    readable: bool = True

    @property
    def passed(self) -> bool:
        return self.readable and all(g.passed for g in self.results)

    @property
    def line(self) -> str:
        """'test fail · types pass · lint skipped'."""
        if not self.readable:
            return "couldn't read the results"
        parts = []
        for g in self.results:
            parts.append(f"{g.name} {'skipped' if g.skipped else 'pass' if g.passed else 'fail'}")
        return " · ".join(parts) or "no checks"


@dataclass(frozen=True)
class RoundView:
    """One panel in the timeline: an author/critic round, the review, a final pass, ..."""

    title: str
    author_role: str = ""
    author: Markup | None = None  # the author's reply, rendered
    diff: DiffView | None = None
    gates: list[tuple[str, GatesView]] = field(default_factory=list)  # (label, results)
    critics: list[CriticView] = field(default_factory=list)
    test_fix: Markup | None = None  # the test author's reply in a test-defect fix pass
    expect_red: bool = False  # tests stage: the new tests are supposed to fail
    task_md: Markup | None = None  # SPEC: task.md as this pass left it
    asked: list[OpenQuestion] = field(default_factory=list)  # SPEC: blocking questions it asked
    notice: str = ""  # a plain line the panel opens with ("No critic at this level.")
    decision: Decision | None = None  # a decision point you chose at (shown as its own panel)

    @property
    def outcome(self) -> str:
        """A short result for the panel's summary line."""
        if self.critics:
            if any(c.verdict is None for c in self.critics):
                return "critic output unreadable"
            return " · ".join(f"{c.role}: {c.verdict.verdict}" for c in self.critics if c.verdict)
        if self.gates:
            return "checks: " + ("pass" if all(g.passed for _, g in self.gates) else "fail")
        if self.decision is not None:
            return f"you chose {self.decision.choice}"
        if self.asked:
            n = len(self.asked)
            return f"{n} blocking question{'s' if n != 1 else ''}"
        return ""


@dataclass(frozen=True)
class StepView:
    stage: Stage
    label: str
    state: str  # a STEP_STATES key
    summary: str  # "2 of 3 rounds", "" if nothing to say
    rounds: list[RoundView] = field(default_factory=list)
    task_md: Markup | None = None  # SPEC, SOLO: the task.md the agent wrote
    rulings: list[Ruling] = field(default_factory=list)  # ARBITER (or the step that has none)
    rulings_note: str = ""  # the rulings were made by code: there's no arbiter at this level

    @property
    def state_label(self) -> str:
        return STEP_STATES[self.state][0]

    @property
    def state_help(self) -> str:
        return STEP_STATES[self.state][1]

    @property
    def has_content(self) -> bool:
        return bool(self.rounds or self.task_md or self.rulings)


@dataclass(frozen=True)
class NextStep:
    label: str
    command: str


@dataclass(frozen=True)
class ChoiceView:
    value: str  # what `advpipe decide` takes
    label: str  # the button
    help: str  # one line on what it does
    confirm: bool = False  # ends the run: asks on its own page first
    primary: bool = False


# What each choice of each decision kind is called and does, in plain words.
CHOICE_VIEWS: dict[str, ChoiceView] = {
    "fix": ChoiceView(
        "fix",
        "Another fix pass",
        "One more pass by the author with the failing output (and your note, if you write "
        "one). Then the checks run again.",
        primary=True,
    ),
    "takeover": ChoiceView(
        "takeover",
        "I'll take over",
        "Commits the work as it is to the branch, removes the working copy and ends the run, "
        "so you can check the branch out and finish it by hand.",
        confirm=True,
    ),
    "ignore": ChoiceView(
        "ignore",
        "Ignore and continue",
        "Records the hits as minor findings marked as ignored by you, and carries on. Later "
        "hits in this run aren't asked about.",
        primary=True,
    ),
    "block": ChoiceView(
        "block",
        "Block",
        "Treats the hits like a failing test: the author is asked to fix them and the scan "
        "runs again. Later hits in this run are blocking too.",
    ),
    "begin": ChoiceView(
        "begin",
        "Begin work",
        "The agent decides the questions that are still open, and any question that comes up "
        "later in this run, and carries on.",
        primary=True,
    ),
    "retry": ChoiceView(
        "retry",
        "Try again",
        "Starts the step that was cut off again, with its partial work kept. Add usage or "
        "credits first, or wait for the reset. If the limit is still in force, the run "
        "pauses again.",
        primary=True,
    ),
    "finish": ChoiceView(
        "finish",
        "Finish as is",
        "Ends the run here. Nothing is committed: the work so far stays in the working copy, "
        "uncommitted, for you to do with as you please.",
        confirm=True,
    ),
    "stop": ChoiceView(
        "stop",
        "Stop here",
        "Ends the run. Nothing is written to the branch, and it can't be resumed.",
        confirm=True,
    ),
}

DECISION_TITLES = {
    "red_checks": "The checks are still failing",
    "scan_hits": "The security scan reported issues",
    "usage_limit": "Paused: a usage limit was reached",
    "spec_cap": "Begin work?",
}


@dataclass(frozen=True)
class DecisionView:
    """The decision panel in the run page's header: what's asked, and the buttons."""

    decision: Decision
    choices: list[ChoiceView]
    pending: list[OpenQuestion]  # spec_cap: the questions still open
    fix_pass: int  # red_checks: the pass a "fix" would be (1-based)
    max_fix_passes: int
    retry_warning: str = ""  # usage_limit: what Try again does if the limit still holds
    add_url: str = ""  # usage_limit: where to add usage or credits (a fixed constant)
    add_label: str = ""

    @property
    def title(self) -> str:
        return DECISION_TITLES[self.decision.kind]

    @property
    def can_fix(self) -> bool:
        return self.fix_pass <= self.max_fix_passes


@dataclass(frozen=True)
class RunDetail:
    run_id: str
    state: RunState
    status: str  # a STATUSES key (adds "stopped")
    title: str
    stage: str  # "CODE r2/3"
    budget_usd: float | None
    timeline: list[StepView]
    report: Markup | None
    next_steps: list[NextStep]
    decision_view: DecisionView | None = None
    usage: UsageView = field(default_factory=lambda: UsageView("unknown"))
    max_decision_note_chars: int = decisions.MAX_NOTE_CHARS

    @property
    def level_label(self) -> str:
        """'Tests (3 of 5)'."""
        level = self.state.level
        return f"{LEVEL_INFO[level].label} ({level_number(level)} of 5)"

    @property
    def scan_text(self) -> str:
        return "on" if scan_on(self.state) else "off"

    @property
    def dropped(self) -> list[str]:
        """What this run's level doesn't do, in plain words (empty for full with the scan on)."""
        return dropped_safeguards(self.state.level, scan_on(self.state))

    @property
    def agents(self) -> int:
        return len(roles_for(self.state.level))

    @property
    def asker(self) -> str:
        """Who asks the blocking questions: the one agent that writes task.md."""
        return "solo agent" if SHAPES[self.state.level].solo else "spec writer"

    @property
    def info(self) -> StatusInfo:
        return STATUSES[self.status]

    @property
    def live(self) -> bool:
        return self.status == "running"

    @property
    def actions(self) -> frozenset[str]:
        """The buttons to show: cancel, resume, clean (see ui.actions.VALID)."""
        return VALID.get(self.status, frozenset())

    @property
    def questions(self) -> list[OpenQuestion]:
        """The blocking questions the answer form asks (with any answers already saved)."""
        if self.status != "needs_answers":
            return []
        return answers.to_answer(answers.migrate(self.state))

    @property
    def max_answer_chars(self) -> int:
        return answers.MAX_ANSWER_CHARS

    @property
    def suggested_budget(self) -> float:
        return suggested_budget(self.state.cost_usd, self.budget_usd)

    @property
    def elapsed(self) -> str:
        seconds = max(0, int((self.state.updated_at - self.state.started_at).total_seconds()))
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m {seconds:02d}s"


# --------------------------------------------------------------------------- file readers


def read_text(path: Path) -> str | None:
    """The file's text, or None if it doesn't exist (yet)."""
    try:
        return path.read_text(errors="replace")
    except (FileNotFoundError, IsADirectoryError):
        return None


def _rel(log: RunLog, path: Path) -> str:
    return path.relative_to(log.root).as_posix()


def _diff(log: RunLog, path: Path) -> DiffView | None:
    text = read_text(path)
    if text is None:
        return None
    all_lines = text.splitlines()
    lines = []
    for line in all_lines[:MAX_DIFF_LINES]:
        if line.startswith(("diff --git", "index ", "+++", "---", "new file", "deleted file")):
            kind = "meta"
        elif line.startswith("@@"):
            kind = "hunk"
        elif line.startswith("+"):
            kind = "add"
        elif line.startswith("-"):
            kind = "del"
        else:
            kind = "ctx"
        lines.append(DiffLine(kind, line))
    return DiffView(_rel(log, path), lines, len(all_lines))


def _gates(log: RunLog, path: Path) -> GatesView | None:
    """A gates.json (a list of results) or scanner.json (one result)."""
    text = read_text(path)
    if text is None:
        return None
    try:
        data = json.loads(text)
        items = data if isinstance(data, list) else [data]
        return GatesView(_rel(log, path), [GateResult.model_validate(g) for g in items])
    except (ValueError, ValidationError):
        return GatesView(_rel(log, path), [], readable=False)


def _critic(log: RunLog, path: Path, role: str) -> CriticView | None:
    raw = read_text(path)
    if raw is None:
        return None
    try:
        verdict: Verdict | None = parse_model(raw, Verdict)
    except ValueError:
        verdict = None
    return CriticView(role, verdict, raw, _rel(log, path))


def _reply(path: Path) -> Markup | None:
    text = read_text(path)
    return markdown(text) if text is not None else None


def _revision_title(number: int, earlier: list[OpenQuestion]) -> str:
    """'Revision 1: with your answers to Q1, Q2', or the agent's, when you chose Bypass."""
    mine = ", ".join(q.id for q in earlier if q.answer and q.source != "agent")
    agent = ", ".join(q.id for q in earlier if q.answer and q.source == "agent")
    parts = [
        p
        for p in (
            f"your answers to {mine}" if mine else "",
            f"the agent's best-judgement answers to {agent}" if agent else "",
        )
        if p
    ]
    return f"Revision {number}" + (": with " + " and ".join(parts) if parts else "")


def _spec_rounds(log: RunLog, state: RunState) -> list[RoundView]:
    """One panel per spec-writer pass: the first spec, then each revision after answers."""
    found = (
        re.fullmatch(r"pass-(\d+)", p.name)
        for p in (log.root / "spec").glob("pass-*")
        if p.is_dir()
    )
    numbers = sorted({1, *(int(m.group(1)) for m in found if m)})
    rounds = []
    for n in numbers:
        pdir = log.root / "spec" / f"pass-{n}"
        reply = _reply(log.root / "spec" / "author.txt" if n == 1 else pdir / "author.txt")
        task_text = read_text(pdir / "task.md")
        asked = [q for q in state.questions if q.asked_in == n]
        if reply is None and task_text is None and not asked:
            continue
        title = "Spec writer"
        if n > 1:
            title = _revision_title(n - 1, [q for q in state.questions if q.asked_in < n])
        rounds.append(
            RoundView(
                title=title,
                author_role="spec writer",
                author=reply,
                task_md=markdown(task_text) if task_text is not None and n > 1 else None,
                asked=asked,
            )
        )
    return rounds


def _round_numbers(stage_dir: Path) -> list[int]:
    if not stage_dir.is_dir():
        return []
    found = (re.fullmatch(r"round-(\d+)", p.name) for p in stage_dir.iterdir() if p.is_dir())
    return sorted(int(m.group(1)) for m in found if m)


def _solo_rounds(log: RunLog, state: RunState, live: bool) -> list[RoundView]:
    """One panel per pass of the solo agent (levels basic and reviewer): the first pass, each
    revision after answers, and each fix pass you asked for."""
    root = log.root / "stage-solo"
    if not root.is_dir():
        return []
    found = (re.fullmatch(r"pass-(\d+)", p.name) for p in root.iterdir() if p.is_dir())
    rounds = []
    spec_pass = 0  # passes that wrote task.md, as state.spec_passes counts them
    for n in sorted(int(m.group(1)) for m in found if m):
        pdir = root / f"pass-{n}"
        request = read_text(pdir / "request.json")
        title = f"Pass {n}"
        asked: list[OpenQuestion] = []
        if request is not None:
            try:
                number = int(json.loads(request).get("fix_pass", 0))
            except (ValueError, AttributeError):
                number = 0
            title = f"Fix pass {number} (you asked)" if number else "Fix pass (you asked)"
        else:
            spec_pass += 1
            asked = [q for q in state.questions if q.asked_in == spec_pass]
            if (pdir / "answers.json").is_file():
                earlier = [q for q in state.questions if q.asked_in < spec_pass]
                title = _revision_title(spec_pass - 1, earlier)
        gates = _gates(log, pdir / "gates.json")
        task_text = read_text(pdir / "task.md")
        rounds.append(
            RoundView(
                title=title,
                author_role="solo agent",
                author=_reply(pdir / "author.txt"),
                diff=_diff(log, pdir / "diff.patch"),
                gates=[("Checks", gates)] if gates else [],
                task_md=markdown(task_text) if task_text is not None else None,
                asked=asked,
            )
        )
    return rounds


# --------------------------------------------------------------------------- timeline steps


def _loop_rounds(log: RunLog, kind: str, level: Level) -> list[RoundView]:
    """The author/critic rounds of the tests or code stage."""
    author, critic = ("test author", "test critic") if kind == "tests" else ("coder", "code critic")
    rounds = []
    no_critic = "" if SHAPES[level].critics else "No critic at this level: the checks decide."
    for n in _round_numbers(log.root / f"stage-{kind}"):
        rdir = log.root / f"stage-{kind}" / f"round-{n}"
        gates = _gates(log, rdir / "gates.json")
        critic_view = _critic(log, rdir / "critic.json", critic)
        rounds.append(
            RoundView(
                title=f"Round {n}",
                author_role=author,
                author=_reply(rdir / "author.txt"),
                diff=_diff(log, rdir / "diff.patch"),
                gates=[("Checks", gates)] if gates else [],
                critics=[critic_view] if critic_view else [],
                test_fix=_reply(rdir / "test-fix.txt"),
                expect_red=kind == "tests",
                notice=no_critic if critic_view is None else "",
            )
        )
    return rounds


def _review_rounds(log: RunLog) -> list[RoundView]:
    rdir = log.root / "review"
    if not rdir.is_dir():
        return []
    single = _critic(log, rdir / "single.json", "code critic (one review pass)")
    if single is not None:
        return [
            RoundView(
                title="Single review",
                diff=_diff(log, rdir / "diff.patch"),
                critics=[single],
                notice="One code-critic pass over the whole change; there's no second round.",
            )
        ]
    scanner = _gates(log, rdir / "scanner.json")
    critics = [
        c
        for c in (
            _critic(log, rdir / "standards.json", "standards reviewer"),
            _critic(log, rdir / "security.json", "security reviewer"),
        )
        if c
    ]
    return [
        RoundView(
            title="Review",
            diff=_diff(log, rdir / "diff.patch"),
            gates=[("Security scanner", scanner)] if scanner else [],
            critics=critics,
        )
    ]


def _unreadable_arbiter_rounds(log: RunLog) -> list[RoundView]:
    """Raw arbiter replies that couldn't be parsed."""
    rounds = []
    for label in ("tests", "code", "review"):
        raw = read_text(log.root / f"arbiter-{label}.json")
        if raw is not None:
            try:
                parse_model(raw, ArbiterRuling)
            except ValueError:
                rounds.append(
                    RoundView(
                        title=f"Arbiter reply after {label} (unreadable)",
                        author_role="arbiter",
                        author=markdown(f"```\n{raw}\n```"),
                    )
                )
    return rounds


def _final_pass_rounds(log: RunLog) -> dict[str, RoundView]:
    """The final author pass after a step's open findings were ruled on, by kind."""
    rounds = {}
    for kind, (author, _) in FINAL_PASS.items():
        rdir = log.root / f"final-pass-{kind}"
        if rdir.is_dir():
            gates = _gates(log, rdir / "gates.json")
            rounds[kind] = RoundView(
                title=f"Final fix pass ({kind})",
                author_role=author,
                author=_reply(rdir / "author.txt"),
                gates=[("Checks after the fix", gates)] if gates else [],
            )
    return rounds


def _fix_pass_rounds(log: RunLog, state: RunState) -> dict[Stage, list[RoundView]]:
    """The fix passes you asked for (``fix-pass-N/``, outside the solo stage), by the step
    they were for: the stage of the N-th ``fix`` decision."""
    asked = [d for d in state.decisions if d.choice == "fix"]
    rounds: dict[Stage, list[RoundView]] = {}
    for n in range(1, len(asked) + 1):
        rdir = log.root / f"fix-pass-{n}"
        if not rdir.is_dir():
            continue
        gates = _gates(log, rdir / "gates.json")
        stage = asked[n - 1].stage
        author = "test author" if stage is Stage.TESTS else "coder"
        rounds.setdefault(stage, []).append(
            RoundView(
                title=f"Fix pass {n} (you asked)",
                author_role=author,
                author=_reply(rdir / "author.txt"),
                gates=[("Checks after the fix", gates)] if gates else [],
            )
        )
    return rounds


def _decision_rounds(state: RunState, steps: list[Stage]) -> dict[Stage, list[RoundView]]:
    """The decisions you made, each a panel in the step where the run stopped. The open one
    isn't here: it's in the page's header."""
    rounds: dict[Stage, list[RoundView]] = {}
    for n, d in enumerate(state.decisions, 1):
        if not d.choice:
            continue
        stage = d.stage if d.stage in steps else Stage.FINAL_GATES
        rounds.setdefault(stage, []).append(
            RoundView(title=f"Decision {n}: {DECISION_TITLES[d.kind]}", decision=d)
        )
    return rounds


def _active_stage(state: RunState) -> Stage:
    """The mainline stage the run is in. During ARBITER, that's the stage it was called from:
    the first one of the level's own steps without a recorded commit."""
    if state.stage is not Stage.ARBITER:
        return state.stage
    mainline = [s for s in steps_for(state.level) if s in COMMIT_KEYS]
    return next(
        (s for s in mainline if COMMIT_KEYS[s] not in state.commits),
        mainline[-1] if mainline else Stage.REVIEW,
    )


def _step_state(
    stage: Stage, state: RunState, live: bool, arbiter_used: bool, can_continue: bool
) -> str:
    """``can_continue``: the run can still go on (resumable, or waiting on answers), so the
    steps after where it stopped are "not yet" rather than "not reached"."""
    here = "current" if live else "stopped"
    later = "pending" if can_continue else "not_reached"
    if stage is Stage.ARBITER:
        if state.stage is Stage.ARBITER:
            return here
        if arbiter_used:
            return "done"
        if state.status is Status.COMPLETE or state.stage is Stage.FINAL_GATES:
            return "not_needed"
        return "if_needed" if can_continue else "not_reached"
    if stage is Stage.FINAL_GATES:
        if state.status is Status.COMPLETE:
            return "done"
        return here if state.stage is Stage.FINAL_GATES else later
    if COMMIT_KEYS[stage] in state.commits:
        return "done"
    if _active_stage(state) is stage:
        return here
    return later


def _summary(stage: Stage, state: RunState, cfg: Config | None, rounds: list[RoundView]) -> str:
    if stage in (Stage.TESTS, Stage.CODE):
        n = sum(r.title.startswith("Round ") for r in rounds)
        if not n:
            return ""
        cap = None
        if cfg is not None:
            cap = (
                cfg.limits.max_rounds_tests if stage is Stage.TESTS else cfg.limits.max_rounds_code
            )
        return f"{n} of {cap} rounds" if cap else f"{n} round{'s' if n != 1 else ''}"
    if stage is Stage.ARBITER and state.rulings:
        fixes = sum(r.decision == "fix" for r in state.rulings)
        dismissed = len(state.rulings) - fixes
        return f"{len(state.rulings)} ruling(s): {fixes} fix, {dismissed} dismiss"
    if stage is Stage.SOLO:
        n = sum(r.title.startswith(("Pass", "Revision")) for r in rounds)
        return f"{n} pass{'es' if n != 1 else ''}" if n else ""
    if rounds and rounds[0].outcome:
        return rounds[0].outcome
    return ""


def build_timeline(log: RunLog, state: RunState, cfg: Config | None, live: bool) -> list[StepView]:
    """The steps of the run's level (``steps_for``), each with its rounds and state."""
    level_steps = steps_for(state.level)
    unreadable = _unreadable_arbiter_rounds(log)
    final_passes = _final_pass_rounds(log)
    arbiter_used = bool(state.rulings or unreadable) or any(
        (log.root / f"arbiter-{label}.json").is_file() for label in ("tests", "code", "review")
    )
    has_arbiter = Stage.ARBITER in level_steps
    current = answers.migrate(state)
    cap = (cfg or Config()).limits.max_rounds_spec
    can_continue = (
        state.status in RESUMABLE
        or answers.awaiting_answers(current, cap)
        or answers.answers_ready(current, cap)
        or decisions.awaiting_decision(state)
        or decisions.decision_ready(state)
    )
    fix_rounds = _fix_pass_rounds(log, state)
    decided = _decision_rounds(state, level_steps)
    # Without an arbiter, each final fix pass sits in the step it was for, and the rulings code
    # made go with the last of them.
    ruled_in = next(
        (FINAL_PASS[k][1] for k in ("code", "tests", "solo") if k in final_passes), None
    )
    steps = []
    for stage in level_steps:
        rounds: list[RoundView] = []
        task_md = None
        if stage is Stage.SPEC:
            rounds = _spec_rounds(log, current)
            task_text = read_text(log.root / "task.md")
            task_md = markdown(task_text) if task_text is not None else None
        elif stage is Stage.SOLO:
            rounds = _solo_rounds(log, current, live)
            task_text = read_text(log.root / "task.md")
            task_md = markdown(task_text) if task_text is not None else None
        elif stage is Stage.TESTS:
            rounds = _loop_rounds(log, "tests", state.level)
        elif stage is Stage.CODE:
            rounds = _loop_rounds(log, "code", state.level)
        elif stage is Stage.REVIEW:
            rounds = _review_rounds(log)
        elif stage is Stage.ARBITER:
            rounds = [*unreadable, *final_passes.values()]
        elif stage is Stage.FINAL_GATES:
            gates = _gates(log, log.root / "final-gates.json")
            rounds = [RoundView(title="Final checks", gates=[("Checks", gates)])] if gates else []
        if not has_arbiter:
            rounds += [v for k, v in final_passes.items() if FINAL_PASS[k][1] is stage]
        rounds += fix_rounds.get(stage, [])
        rounds += decided.get(stage, [])
        rulings: list[Ruling] = []
        rulings_note = ""
        if stage is Stage.ARBITER:
            rulings = list(state.rulings)
        elif not has_arbiter and stage is ruled_in:
            rulings = list(state.rulings)
            rulings_note = (
                f"No arbiter at the {state.level.value} level: code ruled every open finding fix."
            )
        steps.append(
            StepView(
                stage=stage,
                label=STAGE_LABELS.get(stage, stage.value),
                state=_step_state(stage, state, live, arbiter_used, can_continue),
                summary=_summary(stage, state, cfg, rounds),
                rounds=rounds,
                task_md=task_md,
                rulings=rulings,
                rulings_note=rulings_note,
            )
        )
    return steps


# --------------------------------------------------------------------------- next steps


def next_steps(state: RunState, status: str, repo: Path, budget: float | None) -> list[NextStep]:
    """Commands for what to do next, ready to paste into a terminal. Every value is quoted:
    run.json is a file on disk, not something to trust in a shell."""
    q = shlex.quote
    r, run_id = q(str(repo)), q(state.run_id)
    git = f"git -C {r}"
    steps: list[NextStep] = []
    branch = q(state.branch) if state.branch else ""
    base = q(state.base_commit[:12]) if state.base_commit else ""
    worktree = state.worktree if state.worktree and not state.worktree_removed else ""
    read_change = (
        NextStep(
            "Read the change, one commit per stage", f"{git} log -p --reverse {base}..{branch}"
        )
        if branch and base
        else None
    )
    clean = NextStep(
        "Or throw it away (removes its branch and working copy)",
        f"advpipe clean {run_id} --repo {r}",
    )

    if status == "running":
        steps.append(NextStep("Follow it in a terminal", f"advpipe status {run_id} --repo {r}"))
        steps.append(NextStep("Stop it (resumable later)", f"advpipe cancel {run_id} --repo {r}"))
    elif status == "complete":
        if read_change:
            steps.append(read_change)
            steps.append(
                NextStep("List the files it changed", f"{git} diff --stat {base}...{branch}")
            )
        if branch:
            steps.append(
                NextStep("Merge it, from the branch you want it on", f"{git} merge {branch}")
            )
            steps.append(NextStep("Then delete the branch", f"{git} branch -d {branch}"))
        if worktree:
            steps.append(
                NextStep("Remove the working copy it kept", f"{git} worktree remove {q(worktree)}")
            )
    elif status == "needs_answers":
        steps.append(
            NextStep(
                "Answer the spec's questions in a terminal (or in the form above)",
                f"advpipe answer {run_id} --repo {r}",
            )
        )
        steps.append(clean)
    elif status == "finished":
        if worktree:
            steps.append(
                NextStep("Look at the work, uncommitted, in its working copy", f"cd {q(worktree)}")
            )
            steps.append(NextStep("See what changed", f"git -C {q(worktree)} status"))
        steps.append(clean)
    elif status in ("needs_decision", "paused"):
        d = state.decision
        choices = decisions.choices_text(d.kind) if d else "CHOICE"
        steps.append(
            NextStep(
                "Choose in a terminal (or with the buttons above)",
                f"advpipe decide {run_id} {choices} --repo {r}",
            )
        )
        if read_change:
            steps.append(NextStep("See what it did so far", read_change.command))
        if worktree:
            steps.append(NextStep("Look at the work in its working copy", f"cd {q(worktree)}"))
        steps.append(clean)
    elif status == "handed_over":
        if branch:
            steps.append(
                NextStep("Check the branch out and finish it by hand", f"{git} switch {branch}")
            )
        if read_change:
            steps.append(NextStep("Read what it did", read_change.command))
        steps.append(clean)
    elif status == "ended":
        steps.append(clean)
    elif status == "needs_human":
        if read_change:
            steps.append(NextStep("See what it did so far", read_change.command))
        if worktree:
            steps.append(NextStep("Finish it by hand in its working copy", f"cd {q(worktree)}"))
        steps.append(clean)
    elif status == "budget_exceeded":
        more = f"{suggested_budget(state.cost_usd, budget):.0f}"
        steps.append(
            NextStep(
                "Continue with a bigger budget",
                f"advpipe resume {run_id} --repo {r} --budget {more}",
            )
        )
        steps.append(clean)
    else:  # stopped, error
        steps.append(NextStep("Continue where it stopped", f"advpipe resume {run_id} --repo {r}"))
        steps.append(clean)
    return steps


# --------------------------------------------------------------------------- the open decision


def decision_view(state: RunState, log: RunLog) -> DecisionView | None:
    """The panel for the decision the run is waiting at, or None if it isn't at one."""
    d = state.decision
    if d is None or not decisions.awaiting_decision(state):
        return None
    cap = decisions.max_fix_passes(log)
    extra: dict[str, str] = {}
    if d.kind == "usage_limit":
        extra = {
            "retry_warning": usage.retry_warning(d.usage),
            "add_url": usage.ADD_CREDITS_URL if d.billing else usage.ADD_USAGE_URL,
            "add_label": "Add credits in the Console" if d.billing else "Add extra usage",
        }
    return DecisionView(
        decision=d,
        choices=[CHOICE_VIEWS[c] for c in decisions.CHOICES[d.kind]],
        pending=answers.pending(state) if d.kind == "spec_cap" else [],
        fix_pass=state.fix_passes + 1,
        max_fix_passes=cap,
        **extra,
    )


# --------------------------------------------------------------------------- the page


def load_detail(repo: Path, run_id: str) -> RunDetail | None:
    """The run detail for ``run_id``, or None if there is no such run.

    Raises ValueError for an id that isn't a valid run id, and OSError/ValueError if run.json
    exists but can't be read.
    """
    log = RunLog.for_run(repo, run_id)
    if not (log.root / "run.json").is_file():
        return None
    state = log.read_state()
    try:
        cfg = log.read_config()
    except (OSError, ValueError):
        cfg = None
    status = display_status(state, log)
    budget = cfg.limits.budget_usd_per_task if cfg else None
    report = read_text(log.root / "report.md")
    view = decision_view(state, log) if status in ("needs_decision", "paused") else None
    return RunDetail(
        run_id=state.run_id,
        state=state,
        status=status,
        title=title_of(state.work_item, width=120),
        stage=stage_label(state, cfg),
        budget_usd=budget,
        timeline=build_timeline(log, state, cfg, status == "running"),
        report=markdown(report) if report is not None else None,
        next_steps=next_steps(state, status, repo, budget),
        decision_view=view,
        usage=usage_view.for_run(repo, state),
    )


# --------------------------------------------------------------------------- a run starting up

CONSOLE_TAIL_LINES = 60


@dataclass(frozen=True)
class Starting:
    """A run directory without run.json yet: a background run that's starting up, or one
    that failed before it got going (its console.log says why)."""

    run_id: str
    alive: bool
    console: str  # the end of console.log


def load_starting(repo: Path, run_id: str) -> Starting | None:
    """The starting-up view of ``run_id``, or None if it has a run.json (or doesn't exist)."""
    log = RunLog.for_run(repo, run_id)
    if not log.root.is_dir() or (log.root / "run.json").is_file():
        return None
    console = read_text(log.root / CONSOLE_LOG) or ""
    tail = "\n".join(console.splitlines()[-CONSOLE_TAIL_LINES:])
    return Starting(run_id, log.lock_holder() is not None, tail)
