"""Data contracts: findings, verdicts, author responses, rulings, run state."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

log = logging.getLogger(__name__)

Severity = Literal["blocking", "minor"]
# "test-defect": a failing test is itself wrong (not the implementation). Code stage only.
Category = Literal["correctness", "coverage", "standards", "security", "scope", "test-defect"]


class Finding(BaseModel):
    id: str
    severity: Severity
    category: Category
    file: str | None = None
    line: int | None = None
    criterion: str | None = None
    claim: str
    evidence: str = ""
    suggested_check: str = ""

    @property
    def is_grounded(self) -> bool:
        """A finding must point at file + line, or at an acceptance criterion."""
        return (bool(self.file) and self.line is not None) or bool(self.criterion)

    @property
    def blocking(self) -> bool:
        return self.severity == "blocking"

    def match_key(self) -> tuple[str, str] | None:
        """Key used to recognise the same finding raised again in a later round."""
        if not self.file and not self.criterion:
            return None
        return (self.file or "", self.criterion or "")


class Verdict(BaseModel):
    verdict: Literal["PASS", "FAIL"]
    findings: list[Finding] = Field(default_factory=list)
    noise: list[Finding] = Field(default_factory=list, exclude=True)

    @model_validator(mode="after")
    def _enforce_rules(self) -> Verdict:
        grounded = [f for f in self.findings if f.is_grounded]
        dropped = [f for f in self.findings if not f.is_grounded]
        for f in dropped:
            log.info("dropping ungrounded finding as noise: %s", f.claim)
        self.findings = grounded
        self.noise = [*self.noise, *dropped]
        # PASS implies no blocking findings: a PASS with blocking findings counts as FAIL.
        if self.verdict == "PASS" and self.blocking:
            self.verdict = "FAIL"
        return self

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.blocking]

    @property
    def minor(self) -> list[Finding]:
        return [f for f in self.findings if not f.blocking]


class FindingResponse(BaseModel):
    finding_id: str
    action: Literal["fixed", "disputed"]
    note: str = ""
    reason: str = ""


class AuthorResponse(BaseModel):
    responses: list[FindingResponse] = Field(default_factory=list)

    def disputed(self) -> dict[str, FindingResponse]:
        return {r.finding_id: r for r in self.responses if r.action == "disputed"}


class Ruling(BaseModel):
    finding_id: str
    decision: Literal["dismiss", "fix"]
    reason: str = ""


class ArbiterRuling(BaseModel):
    rulings: list[Ruling] = Field(default_factory=list)


class Level(StrEnum):
    """How much checking a run gets, from one agent (basic) to the whole pipeline (full).
    What each level runs is in levels.py (``SHAPES``); a person picks it, never an agent."""

    BASIC = "basic"
    REVIEWER = "reviewer"
    TESTS = "tests"
    ADVERSARIAL = "adversarial"
    FULL = "full"


ScanPolicy = Literal["ask", "ignore", "block"]


class Stage(StrEnum):
    INIT = "INIT"
    SOLO = "SOLO"  # levels basic and reviewer: one agent writes task.md, tests and code
    SPEC = "SPEC"
    TESTS = "TESTS"
    CODE = "CODE"
    REVIEW = "REVIEW"
    ARBITER = "ARBITER"
    FINAL_GATES = "FINAL_GATES"
    DONE = "DONE"


class Status(StrEnum):
    RUNNING = "running"
    COMPLETE = "complete"
    NEEDS_HUMAN = "needs_human"
    BUDGET_EXCEEDED = "budget_exceeded"
    ERROR = "error"


class Dispute(BaseModel):
    """A blocking finding headed for the arbiter, with the author's side if any."""

    finding: Finding
    response: FindingResponse | None = None


class StageResult(BaseModel):
    name: str
    passed: bool
    rounds: int
    open_findings: list[Dispute] = Field(default_factory=list)
    minor_findings: list[Finding] = Field(default_factory=list)


def _now() -> datetime:
    return datetime.now(UTC)


class OpenQuestion(BaseModel):
    """A question the spec writer marked BLOCKING, and the person's answer to it."""

    id: str  # "Q1", "Q2", ...: unique within the run, never reused
    text: str  # as the spec writer wrote it, without the BLOCKING: prefix
    asked_in: int  # the spec pass that asked it (1 = the first spec)
    answer: str = ""  # empty until answered
    answered_at: datetime | None = None
    applied: bool = False  # a spec revision has been given this answer
    # Bypass (docs/LEVELS_PLAN.md §3.6): the person chose to let the agent decide. The answer
    # then comes from the agent, with its one-paragraph reason, and `source` says so.
    source: Literal["person", "agent"] = "person"
    reasoning: str = ""
    bypassed: bool = False  # the person chose Bypass; the agent's answer may not exist yet


class BypassAnswer(BaseModel):
    id: str
    answer: str = ""
    reasoning: str = ""


class BypassAnswers(BaseModel):
    """An agent's reply when a person chose Bypass: its own answer to each question."""

    answers: list[BypassAnswer] = Field(default_factory=list)


DecisionKind = Literal["red_checks", "scan_hits", "usage_limit", "spec_cap"]


# What a person can choose at each kind of decision point (see decisions.py).
DECISION_CHOICES: dict[DecisionKind, tuple[str, ...]] = {
    "red_checks": ("fix", "takeover"),
    "scan_hits": ("ignore", "block"),
    "usage_limit": ("retry", "finish"),
    "spec_cap": ("begin", "stop"),
}


LoginKind = Literal["console", "subscription", "unknown"]


class RateLimitSnapshot(BaseModel):
    """What Claude last reported about one usage window (a subscription's 5-hour or weekly
    limit). Only these fields are kept, never the raw event (docs/LEVELS_PLAN.md §3.15)."""

    window: str  # five_hour, seven_day, seven_day_opus, seven_day_sonnet, overage
    status: Literal["allowed", "allowed_warning", "rejected"]
    utilization: float | None = None  # 0.0 to 1.0
    resets_at: datetime | None = None
    seen_at: datetime = Field(default_factory=_now)


class Decision(BaseModel):
    """A stop where the run waits for a person to pick one of two choices (docs/LEVELS_PLAN.md
    §3.5). The run stays ``needs_human``; nothing picks a choice but a person."""

    kind: DecisionKind
    stage: Stage  # where the run stopped
    asked_at: datetime = Field(default_factory=_now)
    explanation: str  # plain words, shown to the person as is
    findings: list[Finding] = Field(default_factory=list)  # failing checks, scanner hits
    usage: RateLimitSnapshot | None = None  # usage_limit: the subscription window that ran out
    billing: bool = False  # usage_limit: the Console credit balance ran out
    choice: str = ""  # set by the person
    note: str = ""  # red_checks + fix: the person's note, verbatim
    decided_at: datetime | None = None
    applied: bool = False  # the choice has been acted on


class RunState(BaseModel):
    run_id: str
    work_item: str
    # The work-item file the run was started from (`advpipe run --item`), as an absolute path.
    work_item_file: str = ""
    repo: str
    worktree: str = ""
    branch: str = ""
    base_commit: str = ""
    status: Status = Status.RUNNING
    stage: Stage = Stage.INIT
    round: int = 0
    cost_usd: float = 0.0
    started_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    open_findings: list[Finding] = Field(default_factory=list)
    minor_findings: list[Finding] = Field(default_factory=list)
    rulings: list[Ruling] = Field(default_factory=list)
    rounds_used: dict[str, int] = Field(default_factory=dict)
    # Commit recorded at the end of each completed stage ("spec", "tests", "code", "review").
    # A resumed run skips every stage listed here.
    commits: dict[str, str] = Field(default_factory=dict)
    cost_by_stage: dict[str, float] = Field(default_factory=dict)
    calls_by_stage: dict[str, int] = Field(default_factory=dict)
    worktree_removed: bool = False
    notes: list[str] = Field(default_factory=list)
    noise_dropped: int = 0
    # Every BLOCKING question the spec writer asked, over all passes, with the answers given
    # (see answers.py), and how many spec-writer passes have finished (1 after the first spec).
    questions: list[OpenQuestion] = Field(default_factory=list)
    spec_passes: int = 0
    # Set when the run is created and never changed, resume included (docs/LEVELS_PLAN.md).
    # Runs recorded before levels existed load as full with the security scan on.
    level: Level = Level.FULL
    security_scan: bool | None = None  # None: an old run, so the level's default
    # What a failing security scan below full means: "block" makes its hits blocking findings.
    scan_policy: ScanPolicy = "ask"
    # Bypass up front: blocking questions never stop the run; the agent answers them (L2).
    bypass_questions: bool = False
    # Every decision point the run stopped at, in order (the last one is the open or latest).
    decisions: list[Decision] = Field(default_factory=list)
    fix_passes: int = 0  # extra fix passes a person asked for
    # How a run ended at a decision point: the person took over, or chose to stop.
    ended_by: Literal["", "takeover", "stop", "finish"] = ""
    # Which login the agents ran under, as far as Claude reported it (docs/LEVELS_PLAN.md §3.15),
    # and the latest usage snapshot per window. Old runs load as "unknown" with no snapshots.
    auth: LoginKind = "unknown"
    rate_limits: dict[str, RateLimitSnapshot] = Field(default_factory=dict)

    @property
    def decision(self) -> Decision | None:
        """The open decision point, or the latest one."""
        return self.decisions[-1] if self.decisions else None


RESUMABLE = frozenset({Status.RUNNING, Status.ERROR, Status.BUDGET_EXCEEDED})
