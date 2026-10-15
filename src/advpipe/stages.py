"""Stage implementations: adversarial author/critic loops, parallel review, arbiter."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import BaseModel, ValidationError

from advpipe import usage
from advpipe.answers import MAX_ANSWER_CHARS, MAX_REASONING_CHARS
from advpipe.budget import Budget
from advpipe.config import Config
from advpipe.decisions import DecisionNeeded
from advpipe.events import EventKind, emit, gate_statuses
from advpipe.gates import (
    GateResult,
    format_gates,
    gate_findings,
    gates_line,
    red_gate_findings,
    run_gate,
    run_gates,
    tail,
)
from advpipe.levels import scan_on
from advpipe.models import (
    ArbiterRuling,
    AuthorResponse,
    BypassAnswers,
    Dispute,
    Finding,
    Level,
    LoginKind,
    OpenQuestion,
    RateLimitSnapshot,
    Ruling,
    RunState,
    StageResult,
    Verdict,
)
from advpipe.runlog import RunLog
from advpipe.runner import AgentResult, AgentRunner, Role, UsageLimitReached, build_request
from advpipe.workspace import Workspace

log = logging.getLogger(__name__)

StageKind = Literal["tests", "code", "solo"]
ALL_GATES = ["test", "types", "lint"]
SCAN_OFF = "turned off for this run"
M = TypeVar("M", bound=BaseModel)


def run_gate_names(state: RunState) -> list[str]:
    """The checks run for authors and at the final gates. Below full, the security scan runs
    with them when it's on; at full the review stage runs it, for the security reviewer."""
    if state.level is not Level.FULL and scan_on(state):
        return [*ALL_GATES, "security"]
    return list(ALL_GATES)


def _no_progress(message: str) -> None:
    pass


@dataclass
class Context:
    config: Config
    runner: AgentRunner
    workspace: Workspace
    budget: Budget
    runlog: RunLog
    state: RunState
    task_md: str = ""
    last_gates: list[GateResult] = field(default_factory=list)
    # A person just decided something about the work in the worktree (the scan policy): the
    # next author step judges that work as it is, instead of calling its author first.
    judge_existing: bool = False
    # A person pressed *Try again* after a usage limit cut a step off: the next author call says
    # so, and carries on from the partial work in the worktree.
    cut_off: bool = False
    # The CLI already printed the Console-credits warning, so don't print it again.
    console_warned: bool = False
    progress: Callable[[str], None] = _no_progress

    def emit(self, kind: EventKind, message: str, *, echo: bool = True, **data: object) -> None:
        """Record a progress event in events.jsonl and print ``message`` live (CLI)."""
        emit(self.runlog, self.state, self.progress, kind, message, echo=echo, **data)

    @property
    def spec_commit(self) -> str:
        return self.state.commits.get("spec", "")

    @property
    def tests_commit(self) -> str:
        return self.state.commits.get("tests", "")

    def save(self) -> None:
        self.state.cost_usd = self.budget.spent
        self.state.cost_by_stage = dict(self.budget.by_stage)
        self.state.calls_by_stage = dict(self.budget.calls_by_stage)
        self.runlog.write_state(self.state)


# --------------------------------------------------------------------------- agent calls


CUT_OFF_ROLES = frozenset({Role.SPEC_WRITER, Role.TEST_AUTHOR, Role.CODER, Role.SOLO})
CUT_OFF_NOTE = (
    "A previous attempt at this step was cut off by a usage limit; the worktree has its partial "
    "changes; carry on from them."
)


def _note_usage(
    ctx: Context,
    auth: LoginKind,
    snapshots: tuple[RateLimitSnapshot, ...] | list[RateLimitSnapshot],
) -> None:
    """Record what a call revealed about the login and the usage windows: in run.json, in the
    repo's usage cache, as ``usage`` events, and as notes for a Console login or a window near
    its limit. Nothing here changes the budget logic."""
    state = ctx.state
    if auth == "console" and state.auth != "console":
        warning = usage.console_warning(ctx.config.limits.budget_usd_per_task)
        plain = warning.removeprefix("Warning: ")
        state.notes.append(plain[0].upper() + plain[1:])
        ctx.emit("usage", warning, echo=not ctx.console_warned, login="console")
        ctx.console_warned = True
    if auth != "unknown":
        state.auth = auth
    for snap in snapshots:
        usage.merge_snapshots(state.rate_limits, [snap])
        label = usage.window_label(snap.window)
        text = usage.describe(snap)
        if snap.status == "allowed_warning":
            text = f"approaching the {label} limit ({text.split(': ', 1)[1]})"
            state.notes.append(text[0].upper() + text[1:])
        ctx.emit(
            "usage",
            text,
            window=snap.window,
            status=snap.status,
            utilization=snap.utilization,
            resets_at=snap.resets_at.isoformat() if snap.resets_at else None,
        )
    usage.record(ctx.workspace.repo, auth, snapshots)


async def call_agent(ctx: Context, role: Role, prompt: str, stage: str) -> AgentResult:
    if ctx.cut_off and role in CUT_OFF_ROLES:
        prompt, ctx.cut_off = f"{CUT_OFF_NOTE}\n\n{prompt}", False
    request = build_request(role, prompt, ctx.config, ctx.workspace.path, ctx.budget.remaining)
    ctx.emit("agent_start", f"{role.value} working ({request.model})...", role=role.value)
    started = time.monotonic()
    try:
        result = await ctx.runner.run(request)
    except UsageLimitReached as limit:
        # A limit, not a bug: what the call cost still counts, then the run pauses (the
        # orchestrator asks the person what to do).
        try:
            _note_usage(ctx, limit.auth, limit.rate_limits)
            ctx.budget.add(limit.cost_usd, stage)
        finally:
            ctx.save()
            ctx.emit(
                "agent_done",
                f"{role.value} stopped by a usage limit after "
                f"{time.monotonic() - started:.0f}s, ${limit.cost_usd:.2f} "
                f"(run total ${ctx.budget.spent:.2f})",
                role=role.value,
                cost_usd=limit.cost_usd,
                total_usd=ctx.budget.spent,
                is_error=True,
            )
        raise
    _note_usage(ctx, result.auth, result.rate_limits)
    try:
        ctx.budget.add(result.cost_usd, stage)
    finally:
        ctx.save()
        error = " [agent reported an error]" if result.is_error else ""
        ctx.emit(
            "agent_done",
            f"{role.value} done in {time.monotonic() - started:.0f}s, ${result.cost_usd:.2f}"
            f" (run total ${ctx.budget.spent:.2f}){error}",
            role=role.value,
            cost_usd=result.cost_usd,
            total_usd=ctx.budget.spent,
            is_error=result.is_error,
        )
    return result


def _emit_verdict(ctx: Context, role: Role, verdict: Verdict) -> None:
    blocking, minor = len(verdict.blocking), len(verdict.minor)
    ctx.emit(
        "verdict",
        f"{role.value}: {verdict.verdict} ({blocking} blocking, {minor} minor)",
        role=role.value,
        verdict=verdict.verdict,
        blocking=blocking,
        minor=minor,
    )


def extract_json(text: str) -> str:
    """Pull a JSON object out of agent output that may be wrapped in prose or code fences."""
    stripped = text.strip()
    if stripped.startswith("{"):
        return stripped
    fences = re.findall(r"```(?:json)?\s*\n(.*?)```", stripped, re.DOTALL)
    if fences:
        return str(fences[-1]).strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        return stripped[start : end + 1]
    return stripped


def parse_model(text: str, model: type[M]) -> M:
    """Parse agent output into ``model``. Raises ValueError with a readable message."""
    try:
        return model.model_validate_json(extract_json(text))
    except ValidationError as e:
        raise ValueError(str(e)) from e


async def call_json_agent(
    ctx: Context, role: Role, prompt: str, stage: str, model: type[M]
) -> tuple[M | None, str]:
    """Run an agent expecting JSON. Retry once with the parse error; return None on 2nd failure."""
    result = await call_agent(ctx, role, prompt, stage)
    try:
        return parse_model(result.text, model), result.text
    except ValueError as first_error:
        retry = (
            f"{prompt}\n\n---\nYour previous reply could not be parsed:\n{first_error}\n\n"
            f"Your previous reply was:\n{result.text[-2000:]}\n\n"
            f"Reply again with JSON only, matching the {model.__name__} schema."
        )
        second = await call_agent(ctx, role, retry, stage)
        try:
            return parse_model(second.text, model), second.text
        except ValueError:
            return None, second.text


async def call_critic(ctx: Context, role: Role, prompt: str, stage: str) -> tuple[Verdict, str]:
    verdict, raw = await call_json_agent(ctx, role, prompt, stage, Verdict)
    if verdict is None:
        verdict = Verdict(
            verdict="FAIL",
            findings=[
                Finding(
                    id="P1",
                    severity="blocking",
                    category="correctness",
                    criterion=f"{role.value}:output",
                    claim="critic output unparseable",
                    evidence=tail(raw),
                )
            ],
        )
    ctx.state.noise_dropped += len(verdict.noise)
    return verdict, raw


def parse_author_response(text: str) -> AuthorResponse:
    """Authors' replies are parsed leniently: unparseable means nothing was disputed."""
    try:
        return parse_model(text, AuthorResponse)
    except ValueError:
        return AuthorResponse()


# --------------------------------------------------------------------------- prompts


def _fence(text: str, lang: str = "") -> str:
    return f"```{lang}\n{text.rstrip()}\n```"


def _gate_commands(config: Config, names: list[str]) -> str:
    lines = []
    for name in names:
        cmd: list[str] = getattr(config.gates, name)
        if cmd:
            lines.append(f"- {name}: `{' '.join(cmd)}`")
    return "\n".join(lines) or "- (none configured)"


def spec_prompt(work_item: str) -> str:
    return (
        f"Work item: {work_item}\n\n"
        "The target repository is your current working directory. "
        "Write task.md at the repository root."
    )


def _questions_and_answers(questions: list[OpenQuestion]) -> str:
    return "\n\n".join(
        f"{q.id}: {q.text}\nAnswer"
        + (" (your own best judgement: the person chose Bypass)" if q.source == "agent" else "")
        + f": {q.answer}"
        for q in questions
        if q.answer
    )


def spec_revision_prompt(
    work_item: str, previous_task_md: str, questions: list[OpenQuestion]
) -> str:
    """The spec writer's next pass, after a person answered its blocking questions."""
    qa = _questions_and_answers(questions)
    previous = previous_task_md.strip() or "(missing)"
    return (
        f"Work item: {work_item}\n\n"
        "The target repository is your current working directory.\n\n"
        "You wrote the task.md below, and the run stopped on its BLOCKING open questions. "
        "The person who started the run has now answered them.\n\n"
        f"# Your previous task.md\n\n{_fence(previous, 'markdown')}\n\n"
        f"# Questions and answers\n\n{qa}\n\n"
        "Revise the spec: write the complete task.md again at the repository root, following "
        'the section "Revising after answers" in your instructions.'
    )


def _solo_tail(ctx: Context) -> str:
    paths = ", ".join(ctx.config.paths.tests)
    return (
        f"# Gate commands (run from repo root)\n\n"
        f"{_gate_commands(ctx.config, run_gate_names(ctx.state))}\n\nTest paths: {paths}."
    )


def solo_prompt(ctx: Context) -> str:
    """The solo agent's first pass: task.md first, then tests and code (or BLOCKING questions
    and nothing else)."""
    return (
        f"Work item: {ctx.state.work_item}\n\n"
        "The target repository is your current working directory. Write task.md at the "
        "repository root first, then do the work, as your instructions say.\n\n"
        f"{_solo_tail(ctx)}"
    )


def solo_revision_prompt(ctx: Context, previous_task_md: str, questions: list[OpenQuestion]) -> str:
    """The solo agent's next pass, after its blocking questions were answered (by a person, or
    by the agent itself under Bypass)."""
    previous = previous_task_md.strip() or "(missing)"
    return (
        f"Work item: {ctx.state.work_item}\n\n"
        "The target repository is your current working directory.\n\n"
        "You wrote the task.md below, and the run stopped on its BLOCKING open questions. They "
        "have now been answered.\n\n"
        f"# Your previous task.md\n\n{_fence(previous, 'markdown')}\n\n"
        f"# Questions and answers\n\n{_questions_and_answers(questions)}\n\n"
        'Write the complete task.md again at the repository root, following the section "Revising '
        'after answers" in your instructions, and then do the work.\n\n'
        f"{_solo_tail(ctx)}"
    )


def bypass_prompt(work_item: str, task_md: str, questions: list[OpenQuestion]) -> str:
    """Ask the agent that wrote task.md to answer its own blocking questions, because the
    person chose Bypass. Reply is JSON; the agent edits nothing."""
    listed = "\n".join(f"{q.id}: {q.text}" for q in questions)
    return (
        f"Work item: {work_item}\n\n"
        "The target repository is your current working directory.\n\n"
        "You wrote the task.md below and marked the questions listed after it BLOCKING. The "
        "person who started the run has chosen to let you decide them yourself.\n\n"
        f"# Your task.md\n\n{_fence(task_md.strip() or '(missing)', 'markdown')}\n\n"
        f"# Questions to decide\n\n{listed}\n\n"
        "Answer every question with your best judgement: read the code if it helps, pick the "
        "answer a careful engineer would pick, and give a one-paragraph reason. Do not edit "
        "or create any file. Reply with JSON only, in this shape:\n\n"
        + _fence(
            '{"answers": [{"id": "Q1", "answer": "...", "reasoning": "..."}]}',
            "json",
        )
    )


FALLBACK_ANSWER = (
    "No answer given; use your best judgement and record the choice under Assumptions."
)
FALLBACK_REASON = "The agent gave no usable answer when asked, so the run went on without one."


async def bypass_questions(
    ctx: Context,
    role: Role,
    questions: list[OpenQuestion],
    previous_task_md: str,
    stage: str,
    rel_path: str,
) -> None:
    """The person chose Bypass for ``questions``: ask ``role`` (the agent that asked them) for
    its own answers, once, and record them with ``source="agent"``.

    One JSON call, retried once on bad JSON (``call_json_agent``). Whatever files the call
    changed are put back. A question the agent leaves unanswered gets a stand-in answer, so
    the run can't stall. The reply is saved to ``rel_path`` in the run directory and is never
    shown to later agents: they see only the Answers section of task.md.
    """
    if not questions:
        return
    state, ws = ctx.state, ctx.workspace
    task_path = ws.path / "task.md"
    before = task_path.read_text() if task_path.is_file() else None
    ids = [q.id for q in questions]
    ctx.emit("round", f"the agent decides {', '.join(ids)} itself (Bypass)")
    prompt = bypass_prompt(state.work_item, previous_task_md, questions)
    cut_off, ctx.cut_off = ctx.cut_off, False  # this call mustn't carry on partial work
    try:
        parsed, raw = await call_json_agent(ctx, role, prompt, stage, BypassAnswers)
    finally:
        ctx.cut_off = cut_off
    stray = [f for f in ws.changed_files(state.base_commit) if f != "task.md"]
    if stray:
        ws.restore(state.base_commit, stray)
        state.notes.append(
            f"{role.value} edited files while answering its own questions; reverted: "
            + ", ".join(stray)
        )
    if before is None:
        task_path.unlink(missing_ok=True)
    else:
        task_path.write_text(before)
    given = {a.id.strip().upper(): a for a in parsed.answers} if parsed else {}
    now = datetime.now(UTC)
    fallback: list[str] = []
    for q in questions:
        a = given.get(q.id)
        text = a.answer.strip() if a else ""
        if text:
            q.answer = text[:MAX_ANSWER_CHARS]
            q.reasoning = a.reasoning.strip()[:MAX_REASONING_CHARS] if a else ""
        else:
            q.answer, q.reasoning = FALLBACK_ANSWER, FALLBACK_REASON
            fallback.append(q.id)
        q.source, q.answered_at, q.applied = "agent", now, False
    if fallback:
        state.notes.append(
            f"{role.value} gave no usable answer to {', '.join(fallback)} under Bypass; "
            "a stand-in answer was recorded."
        )
    ctx.runlog.write_json(
        rel_path,
        {
            "asked": ids,
            "answers": [
                {
                    "id": q.id,
                    "answer": q.answer,
                    "reasoning": q.reasoning,
                    "stand_in": q.id in fallback,
                }
                for q in questions
            ],
            "reply": raw,
        },
    )
    ctx.save()
    ctx.emit(
        "answers",
        f"{role.value} answered {', '.join(ids)} with its best judgement (Bypass)",
        ids=ids,
        agent=True,
    )


def author_initial_prompt(ctx: Context, kind: StageKind) -> str:
    paths = ", ".join(ctx.config.paths.tests)
    parts = [
        f"# task.md\n\n{ctx.task_md}",
        f"# Gate commands (run from repo root)\n\n"
        f"{_gate_commands(ctx.config, run_gate_names(ctx.state))}",
    ]
    if kind == "tests":
        parts.append(f"Write the tests now. Test paths: {paths}. This is round 1.")
    else:
        test_files = ctx.workspace.changed_files(ctx.spec_commit, ctx.config.paths.tests)
        listed = "\n".join(f"- {f}" for f in test_files) or "- (none)"
        parts.append(
            f"# Tests written for this task\n\n{listed}\n\n"
            f"Implement the change now. Do not modify anything under: {paths}. This is round 1."
        )
    return "\n\n".join(parts)


def author_findings_prompt(findings: list[Finding], gates: list[GateResult], note: str) -> str:
    verdict = {"verdict": "FAIL", "findings": [f.model_dump(mode="json") for f in findings]}
    verdict_json = _fence(json.dumps(verdict, indent=2), "json")
    return (
        f"# {note}\n\nBlocking findings to address:\n\n{verdict_json}"
        f"\n\n# Latest gate output\n\n{format_gates(gates)}\n\n"
        "Fix or dispute each finding (failed gates cannot be disputed), re-run the gates, "
        "then reply with AuthorResponse JSON only."
    )


# task.md is the contract, not part of the change: keep it out of diffs that critics judge.
REVIEW_PATHS = [".", ":(exclude)task.md"]


def critic_prompt(
    ctx: Context,
    kind: StageKind,
    diff_label: str,
    diff: str,
    gates: list[GateResult],
    *,
    single: bool = False,
) -> str:
    """What a critic judges: task.md, the diff, gate output and, for code, which tests the run
    wrote. ``single``: the one review of the whole change at levels without critics."""
    context = ""
    if single:
        tests = ctx.workspace.changed_files(ctx.state.base_commit, ctx.config.paths.tests)
        listed = "\n".join(f"- {t}" for t in tests) or "- (none)"
        context = (
            "\n\n# Review\n\nThis is a single review pass: judge the whole change once; there "
            "won't be another round.\n\n# Test files changed in this run (part of the diff "
            f"below)\n\n{listed}"
        )
    elif kind == "code":
        tests = ctx.workspace.changed_files(ctx.spec_commit, ctx.config.paths.tests)
        listed = "\n".join(f"- {t}" for t in tests) or "- (none)"
        context = (
            "\n\n# Tests written in the previous stage (not part of this diff; the coder may "
            f"not modify them)\n\n{listed}"
        )
    return (
        f"# task.md\n\n{ctx.task_md}{context}\n\n# {diff_label}\n\n"
        f"{_fence(diff or '(empty diff)', 'diff')}"
        f"\n\n# Gate output\n\n{format_gates(gates)}\n\nReply with Verdict JSON only."
    )


# --------------------------------------------------------------------------- guards & gates


def test_file_guard(ctx: Context, base: str) -> list[Finding]:
    """Coder may not modify tests: revert any change under the test paths and flag it."""
    changed = ctx.workspace.changed_files(base, ctx.config.paths.tests)
    if not changed:
        return []
    ctx.workspace.restore(base, changed)
    return [
        Finding(
            id="T-guard",
            severity="blocking",
            category="scope",
            file=changed[0],
            line=1,
            criterion="guard:tests",
            claim="coder modified test files; the changes were reverted",
            evidence="Reverted: " + ", ".join(changed),
        )
    ]


def scan_hit_count(output: str) -> int | None:
    """How many issues a scanner's output says it found, when it says (semgrep: "2 findings")."""
    m = re.search(r"(\d+)\s+(?:code\s+)?findings?\b", output, re.IGNORECASE)
    return int(m.group(1)) if m else None


def checked_findings(ctx: Context, results: list[GateResult]) -> list[Finding]:
    """Blocking findings for failed gates (ground truth). The security scan below full goes
    through the run's scan policy first (docs/LEVELS_PLAN.md §3.7): ``block`` makes its hits
    blocking like a failing test, ``ignore`` records them as minor findings marked as ignored
    by the person, and ``ask`` raises DecisionNeeded, so the run stops and asks."""
    hits = [r for r in results if r.name == "security" and not r.passed]
    findings = gate_findings([r for r in results if r not in hits])
    if not hits:
        return findings
    hit_findings = gate_findings(hits)
    policy = ctx.state.scan_policy
    if policy == "block":
        return findings + hit_findings
    if policy == "ask":
        count = scan_hit_count(hits[0].output)
        found = f"{count} issue(s)" if count is not None else "issues"
        raise DecisionNeeded(
            "scan_hits",
            hit_findings,
            f"The security scan reported {found}. No security reviewer checks them at this "
            "level, so some may not be real problems.",
        )
    ignored = hit_findings[0].model_copy(
        update={
            "id": "G-security-ignored",
            "severity": "minor",
            "category": "security",
            "claim": "Security scan reported issues; ignored by you",
        }
    )
    minors = [f for f in ctx.state.minor_findings if f.id != ignored.id]
    ctx.state.minor_findings = [*minors, ignored]
    return findings


async def stage_gates(ctx: Context, kind: StageKind) -> tuple[list[GateResult], list[Finding]]:
    if kind == "tests":
        (result,) = await run_gates(ctx.config, ctx.workspace.path, ["test"])
        new_tests = ctx.workspace.changed_files(ctx.spec_commit, ctx.config.paths.tests)
        return [result], red_gate_findings(result, new_tests)
    results = await run_gates(ctx.config, ctx.workspace.path, run_gate_names(ctx.state))
    note_scanner_skipped(ctx, results)
    return results, checked_findings(ctx, results)


def note_scanner_skipped(ctx: Context, results: list[GateResult]) -> None:
    """Say once in the report when the security scan was meant to run but couldn't (below
    full; at full the review stage says so)."""
    for r in results:
        if r.name == "security" and r.skipped:
            note = f"Security scanner not run ({r.skip_reason})."
            if note not in ctx.state.notes:
                ctx.state.notes.append(note)


def _is_gate(f: Finding) -> bool:
    return bool(f.criterion and f.criterion.startswith(("gate:", "guard:")))


def _prefix(findings: list[Finding], prefix: str) -> list[Finding]:
    return [f.model_copy(update={"id": f"{prefix}{f.id}"}) for f in findings]


def _in_paths(file: str, paths: list[str]) -> bool:
    return any(file == p.rstrip("/") or file.startswith(p.rstrip("/") + "/") for p in paths)


def _read_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


async def test_fix_pass(
    ctx: Context, defects: list[Finding], gates: list[GateResult], tests_base: str, rdir: str
) -> str:
    """Send tests the code critic says are defective back to the test author, once.

    The test author may only change files under the test paths; anything else it touches is
    put back. Changed tests are committed on their own, and that commit becomes the new
    baseline for the coder's test-file guard. Returns the new baseline commit.
    """
    ws, paths = ctx.workspace, ctx.config.paths.tests
    head = ws.head()
    before = {
        f: _read_bytes(ws.path / f) for f in ws.changed_files(head) if not _in_paths(f, paths)
    }
    defects_json = json.dumps([f.model_dump(mode="json") for f in defects], indent=2)
    prompt = (
        f"# task.md\n\n{ctx.task_md}\n\n# Test fix pass\n\n"
        "The implementation was judged against your tests, and the code critic reports that "
        "these failing tests are themselves defective (the test is wrong, not the code):\n\n"
        f"{_fence(defects_json, 'json')}\n\n# Latest gate output\n\n{format_gates(gates)}\n\n"
        f"Fix only these defects, in files under {', '.join(paths)}. Keep every test checking "
        "the behaviour task.md requires: don't delete or skip tests, and don't weaken "
        "assertions just to make them pass. Don't edit implementation files; changes outside "
        "the test paths are reverted. Re-run the tests, then reply with a short summary."
    )
    ctx.emit(
        "test_fix",
        f"code-critic reports {len(defects)} defective test(s): one fix pass by test-author",
    )
    result = await call_agent(ctx, Role.TEST_AUTHOR, prompt, "stage-code")
    ctx.runlog.write_text(f"{rdir}/test-fix.txt", result.text)

    reverted = []
    for f in sorted(set(before) | {f for f in ws.changed_files(head) if not _in_paths(f, paths)}):
        target = ws.path / f
        if f not in before:
            ws.restore(head, [f])
            reverted.append(f)
        elif _read_bytes(target) != before[f]:
            original = before[f]
            if original is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(original)
            reverted.append(f)
    if reverted:
        note = "Test author edited non-test files during its fix pass; reverted: "
        ctx.state.notes.append(note + ", ".join(reverted))
        ctx.emit("guard", f"test-author edited non-test files; reverted: {', '.join(reverted)}")

    changed = ws.changed_files(tests_base, paths)
    if not changed:
        ctx.emit("test_fix", "test-author changed no tests")
        return tests_base
    commit = ws.commit_paths(f"advpipe: test fix ({rdir.replace('/', ' ')})", changed)
    ctx.state.commits["tests"] = commit  # the coder's test guard now protects the fixed tests
    ctx.state.notes.append("Test author fixed defective tests: " + ", ".join(changed))
    ctx.emit("test_fix", f"test fix committed: {', '.join(changed)}", files=changed)
    return commit


# --------------------------------------------------------------------------- stages


async def adversarial_stage(ctx: Context, kind: StageKind, *, critic: bool = True) -> StageResult:
    """Author ⇄ critic loop with a round cap. Returns findings the arbiter must settle.

    ``critic=False`` (the Tests level): each round runs the author, the guard and the gates,
    and no critic, so only gate and guard findings go back to the author.
    """
    limits = ctx.config.limits
    if kind == "tests":
        author, critic_role, cap, label = (
            Role.TEST_AUTHOR,
            Role.TEST_CRITIC,
            limits.max_rounds_tests,
            "Test diff",
        )
    else:
        author, critic_role, cap, label = (
            Role.CODER,
            Role.CODE_CRITIC,
            limits.max_rounds_code,
            "Implementation diff",
        )
    stage = f"stage-{kind}"
    base = ctx.workspace.head()

    to_author: list[Finding] = []
    for_arbiter: list[Dispute] = []
    minors: list[Finding] = []
    rounds = 0
    tests_base = base  # what the coder's test-file guard protects; moves after a test fix
    test_fix_used = False
    for rnd in range(1, cap + 1):
        rounds = rnd
        ctx.state.round = rnd
        ctx.save()
        ctx.emit("round", f"round {rnd}/{cap}", cap=cap)
        rdir = f"{stage}/round-{rnd}"

        if rnd == 1 and ctx.judge_existing:
            # The work is in the worktree already, and a person has decided how to treat the
            # scan's hits: judge it as it is.
            ctx.judge_existing = False
            result = AgentResult(text="", cost_usd=0.0)
        else:
            if rnd == 1:
                prompt = author_initial_prompt(ctx, kind)
            else:
                prompt = author_findings_prompt(to_author, ctx.last_gates, f"Round {rnd}")
            result = await call_agent(ctx, author, prompt, stage)
            ctx.runlog.write_text(f"{rdir}/author.txt", result.text)

        # Disputes from this round are judged by this round's critic.
        pending: list[Dispute] = []
        if rnd > 1:
            disputed = parse_author_response(result.text).disputed()
            pending = [
                Dispute(finding=f, response=disputed[f.id])
                for f in to_author
                if f.id in disputed and not _is_gate(f)
            ]

        guard = test_file_guard(ctx, tests_base) if kind == "code" else []
        if guard:
            ctx.emit("guard", f"test-file guard: coder edited tests ({guard[0].evidence})")
        gates, gate_f = await stage_gates(ctx, kind)
        ctx.last_gates = gates
        expect = " (new tests should fail before implementation)" if kind == "tests" else ""
        ctx.emit("gates", f"gates: {gates_line(gates)}{expect}", gates=gate_statuses(gates))
        for f in gate_f:
            ctx.emit("gates", f"  blocking: {f.claim}")

        diff = ctx.workspace.diff(base, REVIEW_PATHS)
        ctx.runlog.write_text(f"{rdir}/diff.patch", diff)  # exactly what the critic judges
        if critic:
            prompt = critic_prompt(ctx, kind, label, diff, gates)
            verdict, raw = await call_critic(ctx, critic_role, prompt, stage)
            _emit_verdict(ctx, critic_role, verdict)
            ctx.runlog.write_text(f"{rdir}/critic.json", raw)
        else:
            verdict = Verdict(verdict="PASS")  # no critic at this level: the gates decide

        # A failing test the critic says is itself wrong goes back to the test author, once per
        # stage. The rerun gates are the ground truth: the claim alone never clears a failure.
        defects = [f for f in verdict.findings if f.category == "test-defect"]
        test_failed = any(g.name == "test" and not g.passed for g in gates)
        if kind == "code" and defects and test_failed and not test_fix_used:
            test_fix_used = True
            tests_base = await test_fix_pass(ctx, defects, gates, tests_base, rdir)
            gates, gate_f = await stage_gates(ctx, kind)
            ctx.last_gates = gates
            ctx.emit(
                "gates", f"gates after test fix: {gates_line(gates)}", gates=gate_statuses(gates)
            )
        ctx.runlog.write_json(f"{rdir}/gates.json", gates)

        # Test-defect claims are recorded, never sent to the coder (it can't edit tests).
        critic_findings = [
            f.model_copy(update={"severity": "minor"}) if f.category == "test-defect" else f
            for f in verdict.findings
        ]
        found = _prefix(guard + gate_f + critic_findings, f"{kind[0].upper()}{rnd}-")
        minors += [f for f in found if not f.blocking]
        blocking = [f for f in found if f.blocking]

        # A disputed finding that the critic raises again goes straight to the arbiter.
        reflagged_keys = {f.match_key() for f in blocking if not _is_gate(f)} - {None}
        for d in pending:
            if d.finding.match_key() in reflagged_keys:
                for_arbiter.append(d)
        arbiter_keys = {d.finding.match_key() for d in for_arbiter}
        to_author = [f for f in blocking if _is_gate(f) or f.match_key() not in arbiter_keys]

        if not to_author:
            break

    ctx.state.rounds_used[kind] = rounds
    if for_arbiter or to_author:
        reason = "round cap reached" if to_author else "disputed findings"
        count = len(for_arbiter) + len(to_author)
        where = "go to the arbiter" if critic else "are still open"
        ctx.emit("stage", f"{reason}: {count} open finding(s) {where}", passed=False)
    else:
        ctx.emit("stage", f"{kind} stage passed in {rounds} round(s)", passed=True)
    open_findings = for_arbiter + [Dispute(finding=f) for f in to_author]
    return StageResult(
        name=kind,
        passed=not open_findings,
        rounds=rounds,
        open_findings=open_findings,
        minor_findings=minors,
    )


async def review_stage(ctx: Context) -> StageResult:
    """Standards and security reviewers, in parallel, one pass each."""
    command = ctx.config.gates.security
    if scan_on(ctx.state):
        scanner = await run_gate(
            "security", command, ctx.workspace.path, ctx.config.limits.gate_timeout_s
        )
    else:
        scanner = GateResult(name="security", command=command, skipped=True, skip_reason=SCAN_OFF)
    ctx.emit("gates", f"security scanner: {gates_line([scanner])}", gates=gate_statuses([scanner]))
    if scanner.skipped:
        ctx.state.notes.append(
            f"Security scanner not run ({scanner.skip_reason}); "
            "security review used the diff alone."
        )
    ctx.runlog.write_json("review/scanner.json", scanner)

    diff = ctx.workspace.diff(ctx.state.base_commit, REVIEW_PATHS)
    ctx.runlog.write_text("review/diff.patch", diff)  # exactly what both reviewers judge
    gates_text = format_gates(ctx.last_gates)
    standards_doc = ctx.workspace.path / ctx.config.paths.standards_doc
    doc = standards_doc.read_text() if standards_doc.is_file() else "(no standards doc found)"
    base_prompt = (
        f"# task.md\n\n{ctx.task_md}\n\n# Full diff\n\n{_fence(diff, 'diff')}"
        f"\n\n# Gate output\n\n{gates_text}"
    )
    std_prompt = (
        f"{base_prompt}\n\n# Standards doc ({ctx.config.paths.standards_doc})\n\n{doc}\n\n"
        "Reply with Verdict JSON only."
    )
    sec_prompt = (
        f"{base_prompt}\n\n# Security scanner output\n\n{scanner.summary()}\n\n"
        "Reply with Verdict JSON only."
    )

    # Both reviewers run to the end before anything is raised, so a usage limit in one never
    # leaves the other running in the background (its cost still counts).
    both = await asyncio.gather(
        call_critic(ctx, Role.STANDARDS_REVIEWER, std_prompt, "review"),
        call_critic(ctx, Role.SECURITY_REVIEWER, sec_prompt, "review"),
        return_exceptions=True,
    )
    for outcome in sorted(both, key=lambda o: not isinstance(o, UsageLimitReached)):
        if isinstance(outcome, BaseException):
            raise outcome
    (std, std_raw), (sec, sec_raw) = both  # type: ignore[misc]  # no exceptions left
    _emit_verdict(ctx, Role.STANDARDS_REVIEWER, std)
    _emit_verdict(ctx, Role.SECURITY_REVIEWER, sec)
    ctx.runlog.write_text("review/standards.json", std_raw)
    ctx.runlog.write_text("review/security.json", sec_raw)

    found = _prefix(std.findings, "STD-") + _prefix(sec.findings, "SEC-")
    blocking = [Dispute(finding=f) for f in found if f.blocking]
    ctx.state.rounds_used["review"] = 1
    return StageResult(
        name="review",
        passed=not blocking,
        rounds=1,
        open_findings=blocking,
        minor_findings=[f for f in found if not f.blocking],
    )


async def single_review_stage(ctx: Context) -> StageResult:
    """One code-critic pass over the whole change, no back-and-forth (levels without critics).

    A test-defect finding is an ordinary finding here: there's no test-fix pass to send it to.
    """
    diff = ctx.workspace.diff(ctx.state.base_commit, REVIEW_PATHS)
    ctx.runlog.write_text("review/diff.patch", diff)  # exactly what the critic judges
    prompt = critic_prompt(ctx, "code", "Full diff", diff, ctx.last_gates, single=True)
    verdict, raw = await call_critic(ctx, Role.CODE_CRITIC, prompt, "review")
    _emit_verdict(ctx, Role.CODE_CRITIC, verdict)
    ctx.runlog.write_text("review/single.json", raw)

    found = _prefix(verdict.findings, "REV-")
    blocking = [Dispute(finding=f) for f in found if f.blocking]
    ctx.state.rounds_used["review"] = 1
    return StageResult(
        name="review",
        passed=not blocking,
        rounds=1,
        open_findings=blocking,
        minor_findings=[f for f in found if not f.blocking],
    )


def _arbiter_prompt(ctx: Context, disputes: list[Dispute]) -> str:
    items = [
        {
            "finding": d.finding.model_dump(mode="json"),
            "author_response": d.response.model_dump(mode="json") if d.response else None,
        }
        for d in disputes
    ]
    diff = ctx.workspace.diff(ctx.state.base_commit, REVIEW_PATHS)
    return (
        f"# task.md\n\n{ctx.task_md}\n\n# Current diff\n\n{_fence(diff, 'diff')}"
        f"\n\n# Latest gate output\n\n{format_gates(ctx.last_gates)}"
        f"\n\n# Open findings\n\n{_fence(json.dumps(items, indent=2), 'json')}"
        "\n\nRule on each finding. Reply with JSON only."
    )


async def arbiter_stage(ctx: Context, disputes: list[Dispute], label: str) -> list[Ruling]:
    """Rule fix/dismiss on each open finding. Failed gates are always `fix`, without debate."""
    rulings = [
        Ruling(finding_id=d.finding.id, decision="fix", reason="failed gate is ground truth")
        for d in disputes
        if _is_gate(d.finding)
    ]
    to_rule = [d for d in disputes if not _is_gate(d.finding)]
    if to_rule:
        parsed, raw = await call_json_agent(
            ctx, Role.ARBITER, _arbiter_prompt(ctx, to_rule), "arbiter", ArbiterRuling
        )
        ctx.runlog.write_text(f"arbiter-{label}.json", raw)
        by_id = {r.finding_id: r for r in (parsed.rulings if parsed else [])}
        if parsed is None:
            ctx.state.notes.append(f"Arbiter output unparseable ({label}); all findings ruled fix.")
        for d in to_rule:
            rulings.append(
                by_id.get(d.finding.id)
                or Ruling(
                    finding_id=d.finding.id,
                    decision="fix",
                    reason="no ruling given; defaulting to fix",
                )
            )
    for r in rulings:
        ctx.emit(
            "ruling",
            f"  {r.finding_id}: {r.decision} ({r.reason})",
            finding_id=r.finding_id,
            decision=r.decision,
        )
    return rulings


ARBITER_NOTE = "Final pass: the arbiter ruled these must be fixed"
NO_ARBITER_NOTE = "Final pass: these findings are still open and must be fixed"


async def final_author_pass(
    ctx: Context,
    author: Role,
    findings: list[Finding],
    kind: StageKind,
    *,
    note: str = ARBITER_NOTE,
    stage: str = "arbiter",
    extra: str = "",
    rdir: str = "",
) -> list[Finding]:
    """One last author pass on `fix` rulings; no critic. Returns findings still failing.
    ``stage`` is what the call is charged to; ``extra`` is added to the prompt (a person's
    note) and ``rdir`` is the run-directory folder for the pass's files."""
    rdir = rdir or f"final-pass-{kind}"
    prompt = author_findings_prompt(findings, ctx.last_gates, note) + extra
    result = await call_agent(ctx, author, prompt, stage)
    ctx.runlog.write_text(f"{rdir}/author.txt", result.text)
    guard = test_file_guard(ctx, ctx.tests_commit) if kind == "code" else []
    gates, gate_f = await stage_gates(ctx, kind)
    ctx.last_gates = gates
    ctx.emit("gates", f"gates after final pass: {gates_line(gates)}", gates=gate_statuses(gates))
    ctx.runlog.write_json(f"{rdir}/gates.json", gates)
    return guard + gate_f


def fix_pass_note(number: int) -> str:
    return f"Fix pass {number}: a person asked for another pass at the failing checks"


def fix_pass_extra(note: str) -> str:
    """What a person's note adds to an author's fix-pass prompt, verbatim."""
    if not note.strip():
        return ""
    return f"\n\nA person asked for this pass and left you this note:\n\n{_fence(note.strip())}"


def blocking_open_questions(task_md: str) -> list[str]:
    """Open questions the spec writer marked `BLOCKING:`."""
    match = re.search(
        r"^##\s*Open questions\s*$(.*?)(?=^##\s|\Z)",
        task_md,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    if not match:
        return []
    pattern = re.compile(r"^\s*[-*]\s*(?:\*\*)?BLOCKING(?::\*\*|\*\*:|:)\s*(.+)$", re.IGNORECASE)
    return [
        m.group(1).strip() for line in match.group(1).splitlines() if (m := pattern.match(line))
    ]
