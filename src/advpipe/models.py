"""Data contracts: findings, verdicts, author responses, rulings, run state."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

log = logging.getLogger(__name__)

Severity = Literal["blocking", "minor"]
Category = Literal["correctness", "coverage", "standards", "security", "scope"]


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


class Stage(StrEnum):
    INIT = "INIT"
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


class RunState(BaseModel):
    run_id: str
    work_item: str
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
    notes: list[str] = Field(default_factory=list)
    noise_dropped: int = 0
