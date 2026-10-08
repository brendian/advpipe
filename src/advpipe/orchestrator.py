"""The deterministic state machine that drives one work item through the pipeline."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from advpipe.budget import Budget, BudgetExceeded
from advpipe.config import Config
from advpipe.gates import gate_findings, run_gates
from advpipe.models import RESUMABLE, Finding, RunState, Stage, StageResult, Status
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


class NotResumable(RuntimeError):
    pass


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)


class Orchestrator:
    """INIT → SPEC → TESTS → CODE → REVIEW → [ARBITER] → FINAL_GATES → terminal status.

    Agents are called by this class; they never decide whether a stage runs. Each completed
    stage is committed and recorded in run.json, so an interrupted run resumes at the first
    stage that didn't finish.
    """

    def __init__(
        self,
        config: Config,
        runner: AgentRunner,
        repo: Path,
        work_item: str,
        run_id: str | None = None,
        *,
        keep_worktree: bool = False,
        state: RunState | None = None,
        progress: Callable[[str], None] | None = None,
        name: str | None = None,
    ) -> None:
        if state is None and not work_item.strip():
            raise ValueError("work_item must not be empty")
        self.config = config
        self.runner = runner
        self.repo = repo.resolve()
        self.keep_worktree = keep_worktree
        self.state = state or RunState(
            run_id=run_id or new_run_id(), work_item=work_item, repo=str(self.repo)
        )
        self.run_id = self.state.run_id
        self.runlog = RunLog.for_run(self.repo, self.run_id)
        self.budget = Budget(
            config.limits.budget_usd_per_task,
            self.state.cost_usd,
            self.state.cost_by_stage,
            self.state.calls_by_stage,
        )
        self.ctx: Context | None = None
        self.progress: Callable[[str], None] = progress or (lambda message: None)
        self.name = name

    @classmethod
    def resume(
        cls,
        runner: AgentRunner,
        repo: Path,
        run_id: str,
        *,
        config: Config | None = None,
        budget_usd: float | None = None,
        keep_worktree: bool = False,
        progress: Callable[[str], None] | None = None,
    ) -> Orchestrator:
        """Reload an interrupted run from run.json. Uses the run's saved config by default."""
        runlog = RunLog.for_run(repo.resolve(), run_id)
        if not (runlog.root / "run.json").is_file():
            raise NotResumable(f"no run {run_id} in {repo}")
        state = runlog.read_state()
        if state.status not in RESUMABLE:
            raise NotResumable(f"run {run_id} is {state.status.value}; nothing to resume")
        holder = runlog.lock_holder()
        if holder is not None:
            raise NotResumable(f"run {run_id} is still being driven by pid {holder}")
        cfg = config or runlog.read_config() or Config()
        if budget_usd is not None:
            limits = cfg.limits.model_copy(update={"budget_usd_per_task": budget_usd})
            cfg = cfg.model_copy(update={"limits": limits})
        return cls(
            cfg,
            runner,
            repo,
            state.work_item,
            keep_worktree=keep_worktree,
            state=state,
            progress=progress,
        )

    async def run(self) -> RunState:
        self.runlog.acquire_lock()
        try:
            await self._run()
        except BudgetExceeded as e:
            self.state.status = Status.BUDGET_EXCEEDED
            self.state.notes.append(str(e))
            self.progress(str(e))
        except asyncio.CancelledError:
            # Interrupted (e.g. Ctrl-C): status stays "running" so the run can be resumed.
            self.state.notes.append(f"Interrupted during {self.state.stage.value}")
            self.progress(f"interrupted; resume with: advpipe resume {self.run_id}")
            raise
        except Exception as e:  # noqa: BLE001 - any failure must end in a recorded state
            log.exception("run %s failed", self.run_id)
            self.state.status = Status.ERROR
            self.state.notes.append(f"{type(e).__name__}: {e}")
            self.progress(f"error: {type(e).__name__}: {e}")
        finally:
            self.state.cost_usd = self.budget.spent
            self.state.cost_by_stage = dict(self.budget.by_stage)
            self.state.calls_by_stage = dict(self.budget.calls_by_stage)
            self.runlog.write_state(self.state)
            self.runlog.write_text("report.md", render_report(self.state))
            self.runlog.release_lock()
        return self.state

    def _enter(self, stage: Stage) -> None:
        self.progress(f"== {stage.value}")
        self.state.stage = stage
        self.state.round = 0
        self.runlog.write_state(self.state)

    def _stop(self, status: Status, open_findings: list[Finding] | None = None) -> None:
        self.state.status = status
        if open_findings:
            self.state.open_findings = open_findings

    def _open_workspace(self) -> Workspace:
        if self.state.branch:
            self.state.notes.append(f"Resumed at stage {self.state.stage.value}")
            self.progress(f"resuming run {self.run_id} at stage {self.state.stage.value}")
            self.state.status = Status.RUNNING
            self.state.open_findings = []
            return Workspace.reopen(self.repo, Path(self.state.worktree), self.state.branch)
        self.runlog.write_config(self.config)
        # Readable branch: from --name if given, else from the work item's first line.
        ws = Workspace.create(self.repo, self.run_id, self.name or self.state.work_item)
        self.state.worktree = str(ws.path)
        self.state.branch = ws.branch
        self.state.base_commit = ws.head()
        self.progress(f"run {self.run_id} started; branch {ws.branch}")
        self.progress(f"worktree: {ws.path}")
        return ws

    async def _run(self) -> None:
        self.runlog.write_state(self.state)
        ws = self._open_workspace()
        ctx = self.ctx = Context(
            config=self.config,
            runner=self.runner,
            workspace=ws,
            budget=self.budget,
            runlog=self.runlog,
            state=self.state,
            progress=self.progress,
        )
        done = self.state.commits

        if "spec" in done:
            ctx.task_md = (ws.path / "task.md").read_text()
        elif not await self._spec(ctx):
            return

        if "tests" not in done:
            self._enter(Stage.TESTS)
            if not await self._settle(
                await adversarial_stage(ctx, "tests"), Role.TEST_AUTHOR, "tests"
            ):
                return
            done["tests"] = ws.commit("advpipe: tests")

        if "code" not in done:
            self._enter(Stage.CODE)
            if not await self._settle(await adversarial_stage(ctx, "code"), Role.CODER, "code"):
                return
            done["code"] = ws.commit("advpipe: implementation")

        if "review" not in done:
            self._enter(Stage.REVIEW)
            if not ctx.last_gates:  # resumed: reviewers still need current gate output
                ctx.last_gates = await run_gates(self.config, ws.path, ALL_GATES)
            if not await self._settle(await review_stage(ctx), Role.CODER, "code"):
                return
            changed = ws.changed_files(ws.head())
            done["review"] = ws.commit("advpipe: review fixes") if changed else ws.head()

        self._enter(Stage.FINAL_GATES)
        gates = await run_gates(self.config, ws.path, ALL_GATES)
        self.runlog.write_json("final-gates.json", gates)
        failed = gate_findings(gates)
        if failed:
            self._stop(Status.NEEDS_HUMAN, failed)
            return
        self.state.stage = Stage.DONE
        self._stop(Status.COMPLETE)
        if not self.keep_worktree:
            ws.remove()
            self.state.worktree_removed = True

    async def _spec(self, ctx: Context) -> bool:
        ws = ctx.workspace
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
            return False
        ctx.task_md = task_path.read_text()
        self.runlog.write_text("task.md", ctx.task_md)
        questions = blocking_open_questions(ctx.task_md)
        if questions:
            self._stop(
                Status.NEEDS_HUMAN,
                [_finding(f"Q{i}", "open-question", q) for i, q in enumerate(questions, 1)],
            )
            return False
        self.state.commits["spec"] = ws.commit("advpipe: spec (task.md)")
        return True

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


async def run_many(
    items: list[str], make: Callable[[str], Orchestrator], parallel: int
) -> list[RunState]:
    """Run independent work items concurrently, each in its own worktree; results in order."""
    semaphore = asyncio.Semaphore(max(parallel, 1))

    async def one(item: str) -> RunState:
        async with semaphore:
            return await make(item).run()

    return list(await asyncio.gather(*(one(item) for item in items)))


def _finding(fid: str, criterion: str, claim: str) -> Finding:
    return Finding(id=fid, severity="blocking", category="scope", criterion=criterion, claim=claim)
