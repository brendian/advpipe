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

from advpipe.config import Config
from advpipe.gates import GateResult
from advpipe.models import RESUMABLE, ArbiterRuling, Ruling, RunState, Stage, Status, Verdict
from advpipe.runlog import RunLog
from advpipe.stages import parse_model
from advpipe.ui.render import markdown
from advpipe.ui.runs import (
    STAGE_LABELS,
    STATUSES,
    StatusInfo,
    display_status,
    stage_label,
    title_of,
)

# A diff longer than this is cut in the page; the full file stays in the run directory.
MAX_DIFF_LINES = 3000

# The timeline, in pipeline order. The arbiter runs only when findings are left open, right
# after the stage that left them; it's shown as one step between REVIEW and FINAL CHECKS.
TIMELINE = [Stage.SPEC, Stage.TESTS, Stage.CODE, Stage.REVIEW, Stage.ARBITER, Stage.FINAL_GATES]
# Mainline stage -> the key it records in RunState.commits when it finishes.
COMMIT_KEYS = {Stage.SPEC: "spec", Stage.TESTS: "tests", Stage.CODE: "code", Stage.REVIEW: "review"}

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

    @property
    def outcome(self) -> str:
        """A short result for the panel's summary line."""
        if self.critics:
            if any(c.verdict is None for c in self.critics):
                return "critic output unreadable"
            return " · ".join(f"{c.role}: {c.verdict.verdict}" for c in self.critics if c.verdict)
        if self.gates:
            return "checks: " + ("pass" if all(g.passed for _, g in self.gates) else "fail")
        return ""


@dataclass(frozen=True)
class StepView:
    stage: Stage
    label: str
    state: str  # a STEP_STATES key
    summary: str  # "2 of 3 rounds", "" if nothing to say
    rounds: list[RoundView] = field(default_factory=list)
    task_md: Markup | None = None  # SPEC: the spec writer's task.md
    rulings: list[Ruling] = field(default_factory=list)  # ARBITER

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

    @property
    def info(self) -> StatusInfo:
        return STATUSES[self.status]

    @property
    def live(self) -> bool:
        return self.status == "running"

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


def _round_numbers(stage_dir: Path) -> list[int]:
    if not stage_dir.is_dir():
        return []
    found = (re.fullmatch(r"round-(\d+)", p.name) for p in stage_dir.iterdir() if p.is_dir())
    return sorted(int(m.group(1)) for m in found if m)


# --------------------------------------------------------------------------- timeline steps


def _loop_rounds(log: RunLog, kind: str) -> list[RoundView]:
    """The author/critic rounds of the tests or code stage."""
    author, critic = ("test author", "test critic") if kind == "tests" else ("coder", "code critic")
    rounds = []
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
            )
        )
    return rounds


def _review_rounds(log: RunLog) -> list[RoundView]:
    rdir = log.root / "review"
    if not rdir.is_dir():
        return []
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


def _arbiter_rounds(log: RunLog) -> list[RoundView]:
    """Raw arbiter replies that couldn't be parsed, and the final author passes."""
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
    for kind, author in (("tests", "test author"), ("code", "coder")):
        rdir = log.root / f"final-pass-{kind}"
        if rdir.is_dir():
            gates = _gates(log, rdir / "gates.json")
            rounds.append(
                RoundView(
                    title=f"Final fix pass ({kind})",
                    author_role=author,
                    author=_reply(rdir / "author.txt"),
                    gates=[("Checks after the fix", gates)] if gates else [],
                )
            )
    return rounds


def _active_stage(state: RunState) -> Stage:
    """The mainline stage the run is in. During ARBITER, that's the stage it was called from:
    the first one without a recorded commit."""
    if state.stage is not Stage.ARBITER:
        return state.stage
    return next((s for s, key in COMMIT_KEYS.items() if key not in state.commits), Stage.REVIEW)


def _step_state(stage: Stage, state: RunState, live: bool, arbiter_used: bool) -> str:
    here = "current" if live else "stopped"
    later = "pending" if state.status in RESUMABLE else "not_reached"
    if stage is Stage.ARBITER:
        if state.stage is Stage.ARBITER:
            return here
        if arbiter_used:
            return "done"
        if state.status is Status.COMPLETE or state.stage is Stage.FINAL_GATES:
            return "not_needed"
        return "if_needed" if state.status in RESUMABLE else "not_reached"
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
        n = len(rounds)
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
    if rounds and rounds[0].outcome:
        return rounds[0].outcome
    return ""


def build_timeline(log: RunLog, state: RunState, cfg: Config | None, live: bool) -> list[StepView]:
    arbiter_rounds = _arbiter_rounds(log)
    arbiter_used = bool(state.rulings or arbiter_rounds) or any(
        (log.root / f"arbiter-{label}.json").is_file() for label in ("tests", "code", "review")
    )
    steps = []
    for stage in TIMELINE:
        rounds: list[RoundView] = []
        task_md = None
        if stage is Stage.SPEC:
            reply = _reply(log.root / "spec" / "author.txt")
            if reply is not None:
                rounds = [RoundView(title="Spec writer", author_role="spec writer", author=reply)]
            task_text = read_text(log.root / "task.md")
            task_md = markdown(task_text) if task_text is not None else None
        elif stage is Stage.TESTS:
            rounds = _loop_rounds(log, "tests")
        elif stage is Stage.CODE:
            rounds = _loop_rounds(log, "code")
        elif stage is Stage.REVIEW:
            rounds = _review_rounds(log)
        elif stage is Stage.ARBITER:
            rounds = arbiter_rounds
        elif stage is Stage.FINAL_GATES:
            gates = _gates(log, log.root / "final-gates.json")
            rounds = [RoundView(title="Final checks", gates=[("Checks", gates)])] if gates else []
        steps.append(
            StepView(
                stage=stage,
                label=STAGE_LABELS.get(stage, stage.value),
                state=_step_state(stage, state, live, arbiter_used),
                summary=_summary(stage, state, cfg, rounds),
                rounds=rounds,
                task_md=task_md,
                rulings=list(state.rulings) if stage is Stage.ARBITER else [],
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
    elif status == "needs_human":
        if read_change:
            steps.append(NextStep("See what it did so far", read_change.command))
        if worktree:
            steps.append(NextStep("Finish it by hand in its working copy", f"cd {q(worktree)}"))
        steps.append(clean)
    elif status == "budget_exceeded":
        more = f"{max(budget * 2, state.cost_usd + 5):.0f}" if budget else "30"
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
    )
