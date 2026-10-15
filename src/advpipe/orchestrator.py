"""The deterministic state machine that drives one work item through the pipeline."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from advpipe import answers
from advpipe.budget import Budget, BudgetExceeded
from advpipe.config import Config
from advpipe.events import EventKind, emit
from advpipe.gates import gate_findings, run_gates
from advpipe.levels import SHAPES, check_available, level_tag, scan_on
from advpipe.models import (
    RESUMABLE,
    Finding,
    Level,
    OpenQuestion,
    Ruling,
    RunState,
    Stage,
    StageResult,
    Status,
)
from advpipe.runlog import RunLog, is_this_process, render_report
from advpipe.runner import AgentRunner, Role
from advpipe.stages import (
    ARBITER_NOTE,
    NO_ARBITER_NOTE,
    Context,
    StageKind,
    adversarial_stage,
    arbiter_stage,
    blocking_open_questions,
    call_agent,
    final_author_pass,
    note_scanner_skipped,
    review_stage,
    run_gate_names,
    single_review_stage,
    spec_prompt,
    spec_revision_prompt,
)
from advpipe.workspace import GitError, Workspace, branch_exists

log = logging.getLogger(__name__)


class NotResumable(RuntimeError):
    pass


def check_resumable(repo: Path, run_id: str, *, locked_for_me: bool = False) -> RunState:
    """The run's state, if it can be resumed now. Raises NotResumable saying why not.

    ``locked_for_me``: a run.lock naming this process is expected, because `advpipe resume
    --detach` wrote this (child) process's pid there before starting it.
    """
    runlog = RunLog.for_run(repo.resolve(), run_id)
    if not (runlog.root / "run.json").is_file():
        raise NotResumable(f"no run {run_id} in {repo}")
    state = answers.migrate(runlog.read_state())
    if state.status is Status.NEEDS_HUMAN:
        _check_answered(repo, runlog, state)
    elif state.status not in RESUMABLE:
        raise NotResumable(f"run {run_id} is {state.status.value}; nothing to resume")
    holder = runlog.lock_holder()
    if holder is not None and not (locked_for_me and is_this_process(holder)):
        raise NotResumable(f"run {run_id} is still being driven by pid {holder}")
    return state


def _check_answered(repo: Path, runlog: RunLog, state: RunState) -> None:
    """A needs_human run can be resumed only when it stopped on blocking spec questions and
    every one of them has been answered (docs/ANSWERS_PLAN.md). Raises NotResumable if not."""
    run_id, cap = state.run_id, answers.max_spec_passes(runlog)
    if answers.awaiting_answers(state, cap):
        ids = ", ".join(q.id for q in answers.pending(state))
        raise NotResumable(
            f"run {run_id} is waiting on answers to {ids}: {answers.answer_command(run_id)}"
        )
    if not answers.answers_ready(state, cap):
        raise NotResumable(f"run {run_id} is {state.status.value}; nothing to resume")
    try:
        exists = branch_exists(repo.resolve(), state.branch)
    except GitError:
        exists = False
    if not exists:
        raise NotResumable(
            f"run {run_id} can't be resumed: its branch {state.branch} no longer exists"
        )


def check_same_shape(state: RunState, config: Config) -> None:
    """A resumed run keeps its level and security-scan setting. Raises NotResumable if
    ``config`` explicitly sets either to something else."""
    pipeline = config.pipeline
    asked = pipeline.model_fields_set
    if "level" in asked and pipeline.level is not state.level:
        raise NotResumable(
            f"run {state.run_id} is at level {state.level.value}; a resumed run keeps its "
            "level. Start a new run to change it"
        )
    scan = pipeline.security_scan
    if "security_scan" in asked and scan is not None and scan != scan_on(state):
        now = "on" if scan_on(state) else "off"
        raise NotResumable(
            f"run {state.run_id} has the security scan {now}; a resumed run keeps that. "
            "Start a new run to change it"
        )


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
        work_item_file: str = "",
    ) -> None:
        if state is None and not work_item.strip():
            raise ValueError("work_item must not be empty")
        self.config = config
        self.runner = runner
        self.repo = repo.resolve()
        self.keep_worktree = keep_worktree
        self.state = state or self._new_state(run_id, work_item, work_item_file)
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

    def _new_state(self, run_id: str | None, work_item: str, work_item_file: str) -> RunState:
        """A new run's state. Its level and scan setting come from the effective config
        (command line, front matter and pipeline.toml already merged) and never change."""
        pipeline = self.config.pipeline
        check_available(pipeline.level)
        state = RunState(
            run_id=run_id or new_run_id(),
            work_item=work_item,
            work_item_file=work_item_file,
            repo=str(self.repo),
            level=pipeline.level,
            security_scan=pipeline.security_scan,
            scan_policy=pipeline.scan_failures,
        )
        state.security_scan = scan_on(state)  # the level's default, when not set
        if state.scan_policy != "block" and state.level is not Level.FULL and state.security_scan:
            # Asking (and ignoring hits) comes with decision points (LEVELS_PLAN.md L3).
            state.notes.append(
                f"Security scan policy {state.scan_policy!r} isn't available yet; scan "
                "failures block the run, like a failing test."
            )
            state.scan_policy = "block"
        return state

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
        locked_for_me: bool = False,
    ) -> Orchestrator:
        """Reload an interrupted run from run.json. Uses the run's saved config by default."""
        state = check_resumable(repo, run_id, locked_for_me=locked_for_me)
        if config is not None:
            check_same_shape(state, config)
        runlog = RunLog.for_run(repo.resolve(), run_id)
        cfg = config or runlog.read_config() or Config()
        # config.json says what the run runs under, so it mustn't contradict the run's own
        # level and scan setting (e.g. a run whose config.json predates levels).
        pipeline = cfg.pipeline
        scan = pipeline.security_scan
        if scan is None:
            scan = SHAPES[pipeline.level].security_scan
        if pipeline.level is not state.level or scan != scan_on(state):
            pipeline = pipeline.model_copy(
                update={"level": state.level, "security_scan": scan_on(state)}
            )
            cfg = cfg.model_copy(update={"pipeline": pipeline})
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
            self._emit("status", str(e), status=self.state.status.value)
        except asyncio.CancelledError:
            # Interrupted (Ctrl-C or `advpipe cancel`): status stays "running", so it's resumable.
            self.state.notes.append(f"Interrupted during {self.state.stage.value}")
            self._emit(
                "status",
                f"interrupted; resume with: advpipe resume {self.run_id}",
                status=self.state.status.value,
            )
            raise
        except Exception as e:  # noqa: BLE001 - any failure must end in a recorded state
            log.exception("run %s failed", self.run_id)
            self.state.status = Status.ERROR
            self.state.notes.append(f"{type(e).__name__}: {e}")
            self._emit("error", f"error: {type(e).__name__}: {e}")
        finally:
            self.state.cost_usd = self.budget.spent
            self.state.cost_by_stage = dict(self.budget.by_stage)
            self.state.calls_by_stage = dict(self.budget.calls_by_stage)
            self.runlog.write_state(self.state)
            waiting = answers.awaiting_answers(self.state, self.config.limits.max_rounds_spec)
            self.runlog.write_text(
                "report.md", render_report(self.state, waiting_on_answers=waiting)
            )
            # The last event of every process driving the run; not printed (the CLI prints
            # its own summary).
            self._emit(
                "status",
                f"run ended: {self.state.status.value}",
                echo=False,
                status=self.state.status.value,
                final=True,
                total_usd=self.budget.spent,
            )
            self.runlog.release_lock()
        return self.state

    def _emit(self, kind: EventKind, message: str, *, echo: bool = True, **data: object) -> None:
        emit(self.runlog, self.state, self.progress, kind, message, echo=echo, **data)

    def _enter(self, stage: Stage) -> None:
        self.state.stage = stage
        self.state.round = 0
        self._emit("stage", f"== {stage.value}")
        self.runlog.write_state(self.state)

    def _stop(self, status: Status, open_findings: list[Finding] | None = None) -> None:
        self.state.status = status
        if open_findings:
            self.state.open_findings = open_findings

    def _open_workspace(self) -> Workspace:
        if self.state.branch:
            self.state.notes.append(f"Resumed at stage {self.state.stage.value}")
            self._emit(
                "run_start",
                f"resuming run {self.run_id} at stage {self.state.stage.value}",
                level=self.state.level.value,
                security_scan=scan_on(self.state),
            )
            self.state.status = Status.RUNNING
            self.state.open_findings = []
            # What it runs under from now on, e.g. after `resume --budget`, so the UI and a
            # later resume see the new budget.
            self.runlog.write_config(self.config)
            return Workspace.reopen(self.repo, Path(self.state.worktree), self.state.branch)
        self.runlog.write_config(self.config)
        # Readable branch: from --name if given, else from the work item's first line.
        ws = Workspace.create(self.repo, self.run_id, self.name or self.state.work_item)
        self.state.worktree = str(ws.path)
        self.state.branch = ws.branch
        self.state.base_commit = ws.head()
        tag = level_tag(self.state)
        at = f" at level {tag}" if tag else ""
        self._emit(
            "run_start",
            f"run {self.run_id} started{at}; branch {ws.branch}",
            branch=ws.branch,
            level=self.state.level.value,
            security_scan=scan_on(self.state),
        )
        self._emit("run_start", f"worktree: {ws.path}", worktree=str(ws.path))
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
        shape = SHAPES[self.state.level]

        if "spec" in done:
            ctx.task_md = (ws.path / "task.md").read_text()
        elif not await self._spec(ctx):
            return

        if "tests" not in done:
            self._enter(Stage.TESTS)
            result = await adversarial_stage(ctx, "tests", critic=shape.critics)
            if not await self._settle(result, Role.TEST_AUTHOR, "tests", arbiter=shape.arbiter):
                return
            done["tests"] = ws.commit("advpipe: tests")

        if "code" not in done:
            self._enter(Stage.CODE)
            result = await adversarial_stage(ctx, "code", critic=shape.critics)
            if not await self._settle(result, Role.CODER, "code", arbiter=shape.arbiter):
                return
            done["code"] = ws.commit("advpipe: implementation")

        if shape.review != "none" and "review" not in done:
            self._enter(Stage.REVIEW)
            if not ctx.last_gates:  # resumed: reviewers still need current gate output
                ctx.last_gates = await run_gates(self.config, ws.path, run_gate_names(self.state))
            if shape.review == "full":
                result = await review_stage(ctx)
            else:
                result = await single_review_stage(ctx)
            if not await self._settle(result, Role.CODER, "code", arbiter=shape.arbiter):
                return
            changed = ws.changed_files(ws.head())
            done["review"] = ws.commit("advpipe: review fixes") if changed else ws.head()

        self._enter(Stage.FINAL_GATES)
        gates = await run_gates(self.config, ws.path, run_gate_names(self.state))
        note_scanner_skipped(ctx, gates)
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
        """One spec-writer pass: the first spec, or, once a person has answered the blocking
        questions of the last pass, a revision with the answers. False = the run stops."""
        ws, state = ctx.workspace, self.state
        cap = self.config.limits.max_rounds_spec
        self._enter(Stage.SPEC)
        given = answers.unapplied(state)
        number = state.spec_passes + 1
        if given:
            # The worktree's task.md was wiped when the run was reopened (Workspace.reopen):
            # the previous spec is the copy in the run directory.
            previous = self.runlog.root / "task.md"
            previous_md = previous.read_text() if previous.is_file() else ""
            given_ids = ", ".join(q.id for q in given)
            state.round = number
            self._emit(
                "round",
                f"spec pass {number}/{cap}: revising task.md with the answers to {given_ids}",
                cap=cap,
            )
            self.runlog.write_json(f"spec/pass-{number}/answers.json", answers.answered(state))
            prompt = spec_revision_prompt(state.work_item, previous_md, answers.answered(state))
        else:
            prompt = spec_prompt(state.work_item)
        result = await call_agent(ctx, Role.SPEC_WRITER, prompt, "spec")
        state.spec_passes = number
        for q in given:
            q.applied = True
        reply = "spec/author.txt" if number == 1 else f"spec/pass-{number}/author.txt"
        self.runlog.write_text(reply, result.text)
        stray = [f for f in ws.changed_files(state.base_commit) if f != "task.md"]
        if stray:
            ws.restore(state.base_commit, stray)
            state.notes.append(
                "Spec writer edited files other than task.md; reverted: " + ", ".join(stray)
            )
        task_path = ws.path / "task.md"
        if not task_path.is_file():
            self._stop(
                Status.NEEDS_HUMAN, [_finding("S1", "spec", "spec writer did not produce task.md")]
            )
            return False
        task_md = task_path.read_text()
        if answers.answered(state):
            # The person's answers, verbatim, written by code: later agents see them as part
            # of the contract, and no agent can drop or reword them.
            task_md = answers.with_answers_section(task_md, state.questions)
            task_path.write_text(task_md)
        ctx.task_md = task_md
        self.runlog.write_text("task.md", task_md)
        self.runlog.write_text(f"spec/pass-{number}/task.md", task_md)
        new = self._new_questions(blocking_open_questions(task_md), number)
        if new:
            findings = [_finding(q.id, "open-question", q.text) for q in new]
            self._stop(Status.NEEDS_HUMAN, findings)
            ids = [q.id for q in new]
            if number >= cap:
                note = (
                    f"The spec still has blocking questions after {number} spec pass(es), the "
                    "limit (limits.max_rounds_spec). Reword the work item and start a new run."
                )
                state.notes.append(note)
                self._emit("questions", note, ids=ids, answerable=False)
            else:
                self._emit(
                    "questions",
                    f"spec has {len(new)} blocking question(s); answer them with: "
                    f"{answers.answer_command(self.run_id)}",
                    ids=ids,
                    answerable=True,
                )
            return False
        state.commits["spec"] = ws.commit("advpipe: spec (task.md)")
        return True

    def _new_questions(self, asked: list[str], number: int) -> list[OpenQuestion]:
        """Record this pass's blocking questions with fresh ids (numbering continues across
        passes). A question that was already answered isn't asked again: its answer stands."""
        state = self.state
        done = {answers.normalise(q.text): q for q in answers.answered(state)}
        new: list[OpenQuestion] = []
        for text in asked:
            again = done.get(answers.normalise(text))
            if again is not None:
                state.notes.append(f"Spec writer asked {again.id} again; its answer stands.")
                continue
            q = OpenQuestion(id=f"Q{len(state.questions) + 1}", text=text, asked_in=number)
            state.questions.append(q)
            new.append(q)
        return new

    async def _settle(
        self, result: StageResult, author: Role, kind: StageKind, *, arbiter: bool = True
    ) -> bool:
        """Record a stage result; send open findings to the arbiter, then one final author
        pass on those ruled fix. Without an arbiter (``arbiter=False``) every open finding is
        ruled fix, with the reason recorded. False = run stops."""
        assert self.ctx is not None
        self.state.minor_findings += result.minor_findings
        if result.passed:
            return True
        previous = self.state.stage
        if arbiter:
            self._enter(Stage.ARBITER)
            rulings = await arbiter_stage(self.ctx, result.open_findings, result.name)
            self.state.rulings += rulings
            self.runlog.write_json("arbiter.json", self.state.rulings)
            note, charged = ARBITER_NOTE, "arbiter"
        else:
            reason = f"no arbiter at level {self.state.level.value}"
            rulings = [
                Ruling(finding_id=d.finding.id, decision="fix", reason=reason)
                for d in result.open_findings
            ]
            for r in rulings:
                self._emit(
                    "ruling",
                    f"  {r.finding_id}: fix ({reason})",
                    finding_id=r.finding_id,
                    decision=r.decision,
                )
            self.state.rulings += rulings
            # Charged to the stage being settled: there's no arbiter step at this level.
            note, charged = NO_ARBITER_NOTE, _BUDGET_STAGE[result.name]
        fix_ids = {r.finding_id for r in rulings if r.decision == "fix"}
        to_fix = [d.finding for d in result.open_findings if d.finding.id in fix_ids]
        if to_fix:
            remaining = await final_author_pass(
                self.ctx, author, to_fix, kind, note=note, stage=charged
            )
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


# Budget keys of the stages _settle can settle (StageResult.name → Budget stage).
_BUDGET_STAGE = {"tests": "stage-tests", "code": "stage-code", "review": "review"}


def _finding(fid: str, criterion: str, claim: str) -> Finding:
    return Finding(id=fid, severity="blocking", category="scope", criterion=criterion, claim=claim)
