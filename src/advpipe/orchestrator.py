"""The deterministic state machine that drives one work item through the pipeline."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from advpipe import answers, decisions, usage
from advpipe.budget import Budget, BudgetExceeded
from advpipe.config import Config
from advpipe.decisions import DecisionNeeded, take
from advpipe.events import EventKind, emit, gate_statuses
from advpipe.gates import gates_line, run_gates
from advpipe.levels import SHAPES, level_tag, scan_on
from advpipe.models import (
    RESUMABLE,
    Decision,
    DecisionKind,
    Finding,
    OpenQuestion,
    RateLimitSnapshot,
    Ruling,
    RunState,
    Stage,
    StageResult,
    Status,
)
from advpipe.runlog import RunLog, is_this_process, render_report
from advpipe.runner import AgentRunner, Role, UsageLimitReached
from advpipe.stages import (
    ARBITER_NOTE,
    NO_ARBITER_NOTE,
    REVIEW_PATHS,
    Context,
    StageKind,
    adversarial_stage,
    arbiter_stage,
    author_findings_prompt,
    blocking_open_questions,
    bypass_questions,
    call_agent,
    checked_findings,
    final_author_pass,
    fix_pass_extra,
    fix_pass_note,
    note_scanner_skipped,
    review_stage,
    run_gate_names,
    single_review_stage,
    solo_prompt,
    solo_revision_prompt,
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
        if state.ended_by:
            raise NotResumable(decisions.ended_message(state))
        if decisions.awaiting_decision(state):
            raise NotResumable(decisions.waiting_message(state))
        if decisions.decision_ready(state):
            _check_branch(repo, state)
        else:
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
    _check_branch(repo, state)


def _check_branch(repo: Path, state: RunState) -> None:
    try:
        exists = branch_exists(repo.resolve(), state.branch)
    except GitError:
        exists = False
    if not exists:
        raise NotResumable(
            f"run {state.run_id} can't be resumed: its branch {state.branch} no longer exists"
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
    if "bypass_questions" in asked and pipeline.bypass_questions != state.bypass_questions:
        now = "bypassed up front" if state.bypass_questions else "asked as usual"
        raise NotResumable(
            f"run {state.run_id} has its blocking questions {now}; a resumed run keeps that. "
            "Start a new run to change it"
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
        console_warned: bool = False,
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
        self.console_warned = console_warned

    def _new_state(self, run_id: str | None, work_item: str, work_item_file: str) -> RunState:
        """A new run's state. Its level and scan setting come from the effective config
        (command line, front matter and pipeline.toml already merged) and never change."""
        pipeline = self.config.pipeline
        state = RunState(
            run_id=run_id or new_run_id(),
            work_item=work_item,
            work_item_file=work_item_file,
            repo=str(self.repo),
            level=pipeline.level,
            security_scan=pipeline.security_scan,
            scan_policy=pipeline.scan_failures,
            bypass_questions=pipeline.bypass_questions,
        )
        state.security_scan = scan_on(state)  # the level's default, when not set
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
        console_warned: bool = False,
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
        if (
            pipeline.level is not state.level
            or scan != scan_on(state)
            or pipeline.bypass_questions != state.bypass_questions
        ):
            pipeline = pipeline.model_copy(
                update={
                    "level": state.level,
                    "security_scan": scan_on(state),
                    "bypass_questions": state.bypass_questions,
                }
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
            console_warned=console_warned,
        )

    async def run(self) -> RunState:
        self.runlog.acquire_lock()
        try:
            await self._run()
        except DecisionNeeded as d:
            self._ask(d.kind, d.findings, d.explanation)
        except UsageLimitReached as limit:
            self._ask_usage(limit)
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

    def _ask(
        self,
        kind: DecisionKind,
        findings: list[Finding],
        explanation: str,
        *,
        stage: Stage | None = None,
        usage_snapshot: RateLimitSnapshot | None = None,
        billing: bool = False,
    ) -> None:
        """Stop at a decision point (docs/LEVELS_PLAN.md §3.5): record what's being asked, leave
        the worktree exactly as it is, and wait for a person. Nothing here picks a choice."""
        state = self.state
        asked = Decision(
            kind=kind,
            stage=stage or state.stage,
            explanation=explanation,
            findings=findings,
            usage=usage_snapshot,
            billing=billing,
        )
        state.decisions.append(asked)
        state.status = Status.NEEDS_HUMAN
        state.open_findings = findings
        command = decisions.decide_command(self.run_id, kind)
        self._emit(
            "decision",
            f"{explanation} Decide with: {command}",
            decision_kind=kind,
            choice="asked",
            decision_stage=asked.stage.value,
            choices=list(decisions.CHOICES[kind]),
        )

    def _ask_usage(self, limit: UsageLimitReached) -> None:
        """A usage limit or the Console credit balance ran out during an agent call: pause the run
        at a ``usage_limit`` decision point, with the worktree as it is. Never a failure, never
        an automatic retry (docs/LEVELS_PLAN.md §3.15)."""
        stage = self.state.stage
        where = stage.value.lower().replace("_", " ")
        self._ask(
            "usage_limit",
            [],
            usage.limit_explanation(where, limit.snapshot, limit.billing),
            usage_snapshot=limit.snapshot,
            billing=limit.billing,
        )

    def _apply_retry(self, ctx: Context) -> None:
        """A person pressed *Try again* after a usage limit: the interrupted step starts again
        with its partial work kept, and its author is told it was cut off. No waiting and no
        retry loop of our own: if the limit is still in force, the run pauses again."""
        d = take(self.state, "usage_limit", "retry")
        if d is None:
            return
        d.applied = True
        ctx.cut_off = True
        self._emit(
            "decision",
            f"trying again after the usage limit: {d.stage.value} starts again with its partial "
            "work kept",
            decision_kind="usage_limit",
            choice="retry",
            decision_stage=d.stage.value,
        )

    def _open_workspace(self) -> Workspace:
        if self.state.branch:
            self.state.notes.append(f"Resumed at stage {self.state.stage.value}")
            self._emit(
                "run_start",
                f"resuming run {self.run_id} at stage {self.state.stage.value}",
                level=self.state.level.value,
                security_scan=scan_on(self.state),
            )
            # A person has just decided what to do about the work in the worktree: keep it.
            keep = decisions.pending_choice(self.state)
            self.state.status = Status.RUNNING
            self.state.open_findings = []
            # What it runs under from now on, e.g. after `resume --budget`, so the UI and a
            # later resume see the new budget.
            self.runlog.write_config(self.config)
            worktree = Path(self.state.worktree)
            if keep and not worktree.is_dir():
                self.state.notes.append(
                    "The worktree was gone when the run resumed; it was recreated from the "
                    "branch, so the uncommitted work from before your decision is lost."
                )
            return Workspace.reopen(self.repo, worktree, self.state.branch, keep_changes=keep)
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
            console_warned=self.console_warned,
        )
        done = self.state.commits
        shape = SHAPES[self.state.level]
        author = Role.SOLO if shape.solo else Role.CODER
        kind: StageKind = "solo" if shape.solo else "code"
        self._apply_retry(ctx)
        self._apply_scan_choice(ctx)

        if shape.solo:
            if "solo" not in done:
                self._enter(Stage.SOLO)
                if not await self._solo(ctx):
                    return
                done["solo"] = ws.commit("advpipe: solo (task.md, tests, code)")
            else:
                ctx.task_md = (ws.path / "task.md").read_text()
        else:
            if "spec" in done:
                ctx.task_md = (ws.path / "task.md").read_text()
            elif not await self._spec(ctx):
                return

            if "tests" not in done:
                self._enter(Stage.TESTS)
                if not await self._stage(ctx, Role.TEST_AUTHOR, "tests"):
                    return
                done["tests"] = ws.commit("advpipe: tests")

            if "code" not in done:
                self._enter(Stage.CODE)
                if not await self._stage(ctx, Role.CODER, "code"):
                    return
                done["code"] = ws.commit("advpipe: implementation")

        if shape.review != "none" and "review" not in done:
            self._enter(Stage.REVIEW)
            fix = take(self.state, "red_checks", "fix", Stage.REVIEW)
            if fix is not None:
                if not await self._person_fix(ctx, fix, author, kind, "review"):
                    return
            else:
                if not ctx.last_gates:  # resumed: reviewers still need current gate output
                    ctx.last_gates = await run_gates(
                        self.config, ws.path, run_gate_names(self.state)
                    )
                if shape.review == "full":
                    result = await review_stage(ctx)
                else:
                    result = await single_review_stage(ctx)
                if not await self._settle(result, author, kind, arbiter=shape.arbiter):
                    return
            changed = ws.changed_files(ws.head())
            done["review"] = ws.commit("advpipe: review fixes") if changed else ws.head()

        self._enter(Stage.FINAL_GATES)
        fix = take(self.state, "red_checks", "fix", Stage.FINAL_GATES)
        if fix is not None and not await self._person_fix(ctx, fix, author, kind, "fix"):
            return
        gates = await run_gates(self.config, ws.path, run_gate_names(self.state))
        note_scanner_skipped(ctx, gates)
        self.runlog.write_json("final-gates.json", gates)
        failed = checked_findings(ctx, gates)
        if failed:
            ctx.last_gates = gates
            self._ask_red(failed, "the final checks", self._fixer(author))
            return
        self.state.stage = Stage.DONE
        self._stop(Status.COMPLETE)
        if not self.keep_worktree:
            ws.remove()
            self.state.worktree_removed = True

    def _apply_scan_choice(self, ctx: Context) -> None:
        """A person decided what a failing security scan means: set the policy from here on,
        and let the next author step judge the work in the worktree as it is."""
        d = take(self.state, "scan_hits", "ignore") or take(self.state, "scan_hits", "block")
        if d is None:
            return
        self.state.scan_policy = "ignore" if d.choice == "ignore" else "block"
        d.applied = True
        ctx.judge_existing = True
        self._emit(
            "decision",
            f"security scan hits: {d.choice}; the scan keeps running and its hits are "
            + ("recorded as minor findings" if d.choice == "ignore" else "blocking findings"),
            decision_kind="scan_hits",
            choice=d.choice,
            decision_stage=d.stage.value,
        )

    def _fixer(self, author: Role) -> str:
        return "solo agent" if author is Role.SOLO else author.value

    def _ask_red(
        self,
        findings: list[Finding],
        where: str,
        fixer: str,
        tried: str = "",
        *,
        stage: Stage | None = None,
    ) -> None:
        """Stop at a ``red_checks`` decision point: the checks are red and nothing automatic is
        left to try."""
        names = sorted(
            {
                f.criterion.split(":", 1)[1]
                for f in findings
                if f.criterion and f.criterion.startswith("gate:")
            }
        )
        failing = f" ({', '.join(names)})" if names else ""
        used = self.state.fix_passes
        cap = self.config.limits.max_fix_passes
        explanation = (
            f"The checks are still failing at {where}{failing}. "
            + (f"{tried} " if tried else "")
            + f"Another fix pass by the {fixer}, with the failing output and a note from you if "
            f"you like (fix pass {used + 1} of {cap}); or take over: the work so far is "
            f"committed to the branch {self.state.branch} and the worktree removed, so you can "
            "finish it by hand."
        )
        self._ask("red_checks", findings, explanation, stage=stage)

    async def _stage(self, ctx: Context, author: Role, kind: StageKind) -> bool:
        """The tests or code stage: its author ⇄ critic rounds, then `_settle`. After a person
        asked for a fix pass at this stage, that pass instead."""
        shape = SHAPES[self.state.level]
        stage = Stage.TESTS if kind == "tests" else Stage.CODE
        fix = take(self.state, "red_checks", "fix", stage)
        if fix is not None:
            return await self._person_fix(ctx, fix, author, kind, _BUDGET_STAGE[kind])
        result = await adversarial_stage(ctx, kind, critic=shape.critics)
        return await self._settle(result, author, kind, arbiter=shape.arbiter)

    async def _person_fix(
        self, ctx: Context, d: Decision, author: Role, kind: StageKind, charged: str
    ) -> bool:
        """One more author pass because a person asked for it (docs/LEVELS_PLAN.md §3.5): the
        failing output and the person's note verbatim, no critic round afterwards. The gates
        decide: green carries on, red asks again. False = the run stops."""
        state = self.state
        number = state.fix_passes + 1
        cap = self.config.limits.max_fix_passes
        self._emit("round", f"fix pass {number}/{cap}: you asked for another pass", cap=cap)
        remaining = await final_author_pass(
            ctx,
            author,
            d.findings,
            kind,
            note=fix_pass_note(number),
            stage=charged,
            extra=fix_pass_extra(d.note),
            rdir=f"fix-pass-{number}",
        )
        # Counted once the pass has run, so one an interrupt cut short runs again on resume.
        state.fix_passes, d.applied = number, True
        if remaining:
            self._ask_red(
                remaining, f"{state.stage.value}, after your fix pass", self._fixer(author)
            )
            return False
        return True

    async def _solo(self, ctx: Context) -> bool:
        """The solo agent's stage (levels basic and reviewer): task.md, tests and code in one
        pass, or the blocking questions and nothing else. One agent call per entry and no
        automatic retries: red checks stop and ask. False = the run stops."""
        ws, state = ctx.workspace, self.state
        cap = self.config.limits.max_rounds_spec
        task_path = ws.path / "task.md"
        if task_path.is_file():
            ctx.task_md = task_path.read_text()
        fix = take(state, "red_checks", "fix", Stage.SOLO)
        if fix is not None:
            return await self._solo_fix(ctx, fix)
        begin = take(state, "spec_cap", "begin", Stage.SOLO)
        if begin is not None:
            self._begin(begin)
        if ctx.judge_existing:
            ctx.judge_existing = False
            return await self._solo_checks(ctx, self._solo_dir(newest=True))
        while True:
            number = state.spec_passes + 1
            await self._bypass(ctx, number)
            given = answers.unapplied(state)
            pdir = self._solo_dir()
            previous = self.runlog.root / "task.md"
            if given:
                previous_md = previous.read_text() if previous.is_file() else ""
                given_ids = ", ".join(q.id for q in given)
                state.round = number
                self._emit(
                    "round",
                    f"solo pass {number}/{cap}: with the answers to {given_ids}",
                    cap=cap,
                )
                self.runlog.write_json(f"{pdir}/answers.json", answers.answered(state))
                prompt = solo_revision_prompt(ctx, previous_md, answers.answered(state))
            else:
                prompt = solo_prompt(ctx)
            result = await call_agent(ctx, Role.SOLO, prompt, "solo")
            state.spec_passes = number
            for q in given:
                q.applied = True
            self.runlog.write_text(f"{pdir}/author.txt", result.text)
            if not task_path.is_file():
                return await self._solo_checks(ctx, pdir)
            task_md = task_path.read_text()
            if answers.answered(state):
                task_md = answers.with_answers_section(task_md, state.questions)
                task_path.write_text(task_md)
            self._set_task_md(ctx, task_md, pdir)
            outcome = await self._question_stop(ctx, task_md, number, pdir, solo=True)
            if outcome == "stop":
                self._revert_all_but_task_md(ctx, "solo agent", "while it had blocking questions")
                return False
            if outcome == "again":
                self._revert_all_but_task_md(ctx, "solo agent", "while it had blocking questions")
                continue
            if outcome == "go":
                task_md = answers.with_answers_section(task_md, state.questions)
                task_path.write_text(task_md)
                self._set_task_md(ctx, task_md, pdir)
            return await self._solo_checks(ctx, pdir)

    def _solo_dir(self, *, newest: bool = False) -> str:
        """``stage-solo/pass-N`` for the next solo pass (or, ``newest``, the last one)."""
        root = self.runlog.root / "stage-solo"
        count = len(list(root.glob("pass-*"))) if root.is_dir() else 0
        return f"stage-solo/pass-{max(count, 1) if newest else count + 1}"

    def _set_task_md(self, ctx: Context, task_md: str, pdir: str) -> None:
        ctx.task_md = task_md
        self.runlog.write_text("task.md", task_md)
        self.runlog.write_text(f"{pdir}/task.md", task_md)

    def _revert_all_but_task_md(self, ctx: Context, who: str, why: str) -> None:
        """An agent that stopped on blocking questions mustn't leave code behind."""
        ws, state = ctx.workspace, self.state
        stray = [f for f in ws.changed_files(state.base_commit) if f != "task.md"]
        if stray:
            ws.restore(state.base_commit, stray)
            state.notes.append(f"The {who} edited files {why}; reverted: " + ", ".join(stray))

    async def _solo_fix(self, ctx: Context, d: Decision) -> bool:
        """A person-requested pass by the solo agent, then the checks."""
        state, ws = self.state, ctx.workspace
        number = state.fix_passes + 1
        cap = self.config.limits.max_fix_passes
        pdir = self._solo_dir()
        self._emit("round", f"fix pass {number}/{cap}: you asked for another pass", cap=cap)
        self.runlog.write_json(
            f"{pdir}/request.json",
            {"fix_pass": number, "note": d.note, "findings": [f.model_dump() for f in d.findings]},
        )
        prompt = author_findings_prompt(d.findings, ctx.last_gates, fix_pass_note(number))
        result = await call_agent(ctx, Role.SOLO, prompt + fix_pass_extra(d.note), "solo")
        state.fix_passes, d.applied = number, True
        self.runlog.write_text(f"{pdir}/author.txt", result.text)
        task_path = ws.path / "task.md"
        if task_path.is_file():
            task_md = task_path.read_text()
            if answers.answered(state):
                task_md = answers.with_answers_section(task_md, state.questions)
                task_path.write_text(task_md)
            self._set_task_md(ctx, task_md, pdir)
        return await self._solo_checks(ctx, pdir)

    async def _solo_checks(self, ctx: Context, pdir: str) -> bool:
        """The solo stage's checks: task.md exists, then the gates. Red stops and asks."""
        ws, state = ctx.workspace, self.state
        findings: list[Finding] = []
        if not (ws.path / "task.md").is_file():
            findings.append(
                Finding(
                    id="S1",
                    severity="blocking",
                    category="scope",
                    criterion="guard:task-md",
                    claim="the solo agent did not produce task.md",
                )
            )
        gates = await run_gates(self.config, ws.path, run_gate_names(state))
        note_scanner_skipped(ctx, gates)
        ctx.last_gates = gates
        self.runlog.write_json(f"{pdir}/gates.json", gates)
        self.runlog.write_text(f"{pdir}/diff.patch", ws.diff(state.base_commit, REVIEW_PATHS))
        self._emit("gates", f"gates: {gates_line(gates)}", gates=gate_statuses(gates))
        findings += checked_findings(ctx, gates)
        if findings:
            self._ask_red(
                findings,
                "the solo agent's pass",
                "solo agent",
                "The solo agent doesn't retry by itself.",
            )
            return False
        return True

    async def _spec(self, ctx: Context) -> bool:
        """The spec writer's passes: the first spec, then a revision each time a person (or,
        under Bypass, the agent) has answered the blocking questions of the last pass. False =
        the run stops."""
        ws, state = ctx.workspace, self.state
        cap = self.config.limits.max_rounds_spec
        self._enter(Stage.SPEC)
        begin = take(state, "spec_cap", "begin", Stage.SPEC)
        if begin is not None:
            return await self._begin_spec(ctx, begin)
        while True:
            number = state.spec_passes + 1
            # The person chose Bypass for some questions: the spec writer answers them itself,
            # once, before the revision that uses the answers.
            await self._bypass(ctx, number)
            given = answers.unapplied(state)
            if given:
                # The worktree's task.md was wiped when the run was reopened
                # (Workspace.reopen): the previous spec is the copy in the run directory.
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
                    Status.NEEDS_HUMAN,
                    [_finding("S1", "spec", "spec writer did not produce task.md")],
                )
                return False
            task_md = task_path.read_text()
            if answers.answered(state):
                # The answers, verbatim, written by code: later agents see them as part of the
                # contract, and no agent can drop or reword them.
                task_md = answers.with_answers_section(task_md, state.questions)
                task_path.write_text(task_md)
            pdir = f"spec/pass-{number}"
            self._set_task_md(ctx, task_md, pdir)
            outcome = await self._question_stop(ctx, task_md, number, pdir, solo=False)
            if outcome == "stop":
                return False
            if outcome == "again":
                continue
            if outcome == "go":
                task_md = answers.with_answers_section(task_md, state.questions)
                task_path.write_text(task_md)
                self._set_task_md(ctx, task_md, pdir)
            state.commits["spec"] = ws.commit("advpipe: spec (task.md)")
            return True

    async def _question_stop(
        self, ctx: Context, task_md: str, number: int, pdir: str, *, solo: bool
    ) -> Literal["none", "again", "stop", "go"]:
        """What the blocking questions of the pass just finished mean for the run (the spec
        writer's, or the solo agent's):

        - ``none``: there are none, carry on;
        - ``stop``: the run stopped (waiting for answers, or at the *Begin work?* decision);
        - ``again``: Bypass is on, so the agent answers them and runs another pass;
        - ``go``: Bypass is on and this was the last pass: the agent has answered them, and
          the answers go into task.md without another revision.
        """
        state = self.state
        cap = self.config.limits.max_rounds_spec
        new = self._new_questions(blocking_open_questions(task_md), number)
        if not new:
            return "none"
        ids = [q.id for q in new]
        if state.bypass_questions:
            # Bypass up front (or *Begin work*): nobody is asked. Each pass is still capped; at
            # the cap the agent's answers go in without another revision and the run begins
            # work. The solo agent first gets one more pass, because the pass that asked the
            # questions did none of the work.
            for q in new:
                q.bypassed = True
            self._emit(
                "questions",
                f"{'solo agent' if solo else 'spec'} has {len(new)} blocking question(s); "
                "Bypass is on, so the agent decides them",
                ids=ids,
                answerable=False,
                bypassed=True,
            )
            if number < cap or (solo and number == cap):
                return "again"
            await self._bypass(ctx, number, rel=f"{pdir}/bypass.json")
            for q in new:
                q.applied = True
            state.notes.append(
                f"The spec still had blocking questions after {number} spec pass(es), the "
                "limit (limits.max_rounds_spec); the agent decided them and the run went on "
                "(Bypass)."
            )
            return "go"
        findings = [_finding(q.id, "open-question", q.text) for q in new]
        self._stop(Status.NEEDS_HUMAN, findings)
        if number >= cap:
            # The pass at the cap still has blocking questions: don't ask them one more time.
            # *Begin work?* lets the agent decide them, and every later question.
            n = len(new)
            explanation = (
                f"The spec still has {n} blocking question{'s' if n != 1 else ''} after {number} "
                f"pass{'es' if number != 1 else ''}. Begin work anyway? The agent will decide "
                "these, and any question that comes up later in this run, itself."
            )
            self._emit(
                "questions",
                f"{explanation} Decide with: {decisions.decide_command(self.run_id, 'spec_cap')}",
                ids=ids,
                answerable=False,
            )
            self._ask("spec_cap", findings, explanation)
        else:
            self._emit(
                "questions",
                f"{'solo agent' if solo else 'spec'} has {len(new)} blocking question(s); "
                f"answer them with: {answers.answer_command(self.run_id)}",
                ids=ids,
                answerable=True,
            )
        return "stop"

    def _begin(self, d: Decision) -> None:
        """*Begin work*: from now on the agent decides every question, including those still
        waiting, so nothing stops the run for questions again."""
        state = self.state
        state.bypass_questions = True
        for q in answers.pending(state):
            q.bypassed = True
        d.applied = True
        self._emit(
            "decision",
            "you chose to begin work: the agent decides the open questions, and any later one",
            decision_kind="spec_cap",
            choice="begin",
            decision_stage=d.stage.value,
        )

    async def _begin_spec(self, ctx: Context, d: Decision) -> bool:
        """*Begin work* at the spec stage: the spec writer answers the questions the last pass
        left open, the answers go into task.md without another revision, and the spec is
        committed."""
        ws, state = ctx.workspace, self.state
        self._begin(d)
        number = state.spec_passes
        await self._bypass(ctx, number, rel=f"spec/pass-{number}/bypass-begin.json")
        for q in state.questions:
            if q.answer:
                q.applied = True
        latest = self.runlog.root / "task.md"
        task_md = answers.with_answers_section(latest.read_text(), state.questions)
        (ws.path / "task.md").write_text(task_md)
        self._set_task_md(ctx, task_md, f"spec/pass-{number}")
        state.notes.append(
            f"The spec still had blocking questions after {number} spec pass(es), the limit "
            "(limits.max_rounds_spec); you chose to begin work, and the agent decided them."
        )
        state.commits["spec"] = ws.commit("advpipe: spec (task.md)")
        return True

    async def _bypass(self, ctx: Context, number: int, rel: str | None = None) -> None:
        """Have the agent that wrote task.md (the spec writer, or the solo agent) answer the
        questions a person bypassed (docs/LEVELS_PLAN.md §3.6). Does nothing when there are
        none."""
        waiting = answers.awaiting_bypass(self.state)
        if not waiting:
            return
        solo = SHAPES[self.state.level].solo
        role, stage = (Role.SOLO, "solo") if solo else (Role.SPEC_WRITER, "spec")
        if rel is None:
            where = self._solo_dir() if solo else f"spec/pass-{number}"
            rel = f"{where}/bypass.json"
        previous = self.runlog.root / "task.md"
        previous_md = previous.read_text() if previous.is_file() else ""
        await bypass_questions(ctx, role, waiting, previous_md, stage, rel)

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
        ruled fix, with the reason recorded. Checks still red after that stop at a ``red_checks``
        decision point. False = run stops."""
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
                # The run stays "in" the arbiter step, as before; the decision remembers the
                # stage that was being settled, which is where a fix pass goes.
                self._ask_red(
                    remaining,
                    previous.value,
                    self._fixer(author),
                    "The step's rounds, the rulings and a final fix pass didn't clear them.",
                    stage=previous,
                )
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
_BUDGET_STAGE = {
    "tests": "stage-tests",
    "code": "stage-code",
    "solo": "solo",
    "review": "review",
}


def _finding(fid: str, criterion: str, claim: str) -> Finding:
    return Finding(id=fid, severity="blocking", category="scope", criterion=criterion, claim=claim)
