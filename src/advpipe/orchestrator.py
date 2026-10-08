"""The deterministic state machine that drives one work item through the pipeline."""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime
from pathlib import Path

from advpipe.budget import Budget, BudgetExceeded
from advpipe.config import Config
from advpipe.gates import gate_findings, run_gates
from advpipe.models import Finding, RunState, Stage, StageResult, Status
from advpipe.runlog import RunLog, render_report
from advpipe.runner import AgentRunner, Role
from advpipe.stages import (
    ALL_GATES,
    Context,
    StageKind,
    adversarial_stage,
    arbiter_stage,
    blocking_open_questions,
    call_agent,
    final_author_pass,
    review_stage,
    spec_prompt,
)
from advpipe.workspace import Workspace

log = logging.getLogger(__name__)


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)


class Orchestrator:
    """INIT → SPEC → TESTS → CODE → REVIEW → [ARBITER] → FINAL_GATES → terminal status.

    Agents are called by this class; they never decide whether a stage runs.
    """

    def __init__(
        self,
        config: Config,
        runner: AgentRunner,
        repo: Path,
        work_item: str,
        run_id: str | None = None,
    ) -> None:
        self.config = config
        self.runner = runner
        self.repo = repo.resolve()
        self.run_id = run_id or new_run_id()
        self.state = RunState(run_id=self.run_id, work_item=work_item, repo=str(self.repo))
        self.runlog = RunLog.for_run(self.repo, self.run_id)
        self.budget = Budget(config.limits.budget_usd_per_task)
        self.ctx: Context | None = None

    async def run(self) -> RunState:
        try:
            await self._run()
        except BudgetExceeded as e:
            self.state.status = Status.BUDGET_EXCEEDED
            self.state.notes.append(str(e))
        except Exception as e:  # noqa: BLE001 - any failure must end in a recorded state
            log.exception("run %s failed", self.run_id)
            self.state.status = Status.ERROR
            self.state.notes.append(f"{type(e).__name__}: {e}")
        finally:
            self.state.cost_usd = self.budget.spent
            self.runlog.write_state(self.state)
            self.runlog.write_text(
                "report.md", render_report(self.state, dict(self.budget.by_stage))
            )
        return self.state

    def _enter(self, stage: Stage) -> None:
        self.state.stage = stage
        self.state.round = 0
        self.runlog.write_state(self.state)

    def _stop(self, status: Status, open_findings: list[Finding] | None = None) -> None:
        self.state.status = status
        if open_findings:
            self.state.open_findings = open_findings

    async def _run(self) -> None:
        self.runlog.write_state(self.state)
        ws = Workspace.create(self.repo, self.run_id)
        self.state.worktree = str(ws.path)
        self.state.branch = ws.branch
        self.state.base_commit = ws.head()
        ctx = self.ctx = Context(
            config=self.config,
            runner=self.runner,
            workspace=ws,
            budget=self.budget,
            runlog=self.runlog,
            state=self.state,
        )

        # SPEC
        self._enter(Stage.SPEC)
        result = await call_agent(ctx, Role.SPEC_WRITER, spec_prompt(self.state.work_item), "spec")
        self.runlog.write_text("spec/author.txt", result.text)
        stray = [f for f in ws.changed_files(self.state.base_commit) if f != "task.md"]
        if stray:
            ws.restore(self.state.base_commit, stray)
            self.state.notes.append(
                "Spec writer edited files other than task.md; reverted: " + ", ".join(stray)
            )
        task_path = ws.path / "task.md"
        if not task_path.is_file():
            self._stop(
                Status.NEEDS_HUMAN, [_finding("S1", "spec", "spec writer did not produce task.md")]
            )
            return
        ctx.task_md = task_path.read_text()
        self.runlog.write_text("task.md", ctx.task_md)
        questions = blocking_open_questions(ctx.task_md)
        if questions:
            self._stop(
                Status.NEEDS_HUMAN,
                [_finding(f"Q{i}", "open-question", q) for i, q in enumerate(questions, 1)],
            )
            return
        ctx.spec_commit = ws.commit("advpipe: spec (task.md)")

        # TESTS
        self._enter(Stage.TESTS)
        if not await self._settle(await adversarial_stage(ctx, "tests"), Role.TEST_AUTHOR, "tests"):
            return
        ctx.tests_commit = ws.commit("advpipe: tests")

        # CODE
        self._enter(Stage.CODE)
        if not await self._settle(await adversarial_stage(ctx, "code"), Role.CODER, "code"):
            return
        ws.commit("advpipe: implementation")

        # REVIEW
        self._enter(Stage.REVIEW)
        if not await self._settle(await review_stage(ctx), Role.CODER, "code"):
            return

        # FINAL_GATES
        self._enter(Stage.FINAL_GATES)
        gates = await run_gates(self.config, ws.path, ALL_GATES)
        self.runlog.write_json("final-gates.json", gates)
        failed = gate_findings(gates)
        if failed:
            self._stop(Status.NEEDS_HUMAN, failed)
            return
        if ws.changed_files(ws.head()):
            ws.commit("advpipe: final")
        self.state.stage = Stage.DONE
        self._stop(Status.COMPLETE)

    async def _settle(self, result: StageResult, author: Role, kind: StageKind) -> bool:
        """Record a stage result; send open findings to the arbiter. False = run stops."""
        assert self.ctx is not None
        self.state.minor_findings += result.minor_findings
        if result.passed:
            return True
        previous = self.state.stage
        self._enter(Stage.ARBITER)
        rulings = await arbiter_stage(self.ctx, result.open_findings, result.name)
        self.state.rulings += rulings
        self.runlog.write_json("arbiter.json", self.state.rulings)
        fix_ids = {r.finding_id for r in rulings if r.decision == "fix"}
        to_fix = [d.finding for d in result.open_findings if d.finding.id in fix_ids]
        if to_fix:
            remaining = await final_author_pass(self.ctx, author, to_fix, kind)
            if remaining:
                self._stop(Status.NEEDS_HUMAN, remaining)
                return False
        self.state.stage = previous
        return True


def _finding(fid: str, criterion: str, claim: str) -> Finding:
    return Finding(id=fid, severity="blocking", category="scope", criterion=criterion, claim=claim)
