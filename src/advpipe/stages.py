"""Stage implementations: adversarial author/critic loops, parallel review, arbiter."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, TypeVar

from pydantic import BaseModel, ValidationError

from advpipe.budget import Budget
from advpipe.config import Config
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
from advpipe.models import (
    ArbiterRuling,
    AuthorResponse,
    Dispute,
    Finding,
    Ruling,
    RunState,
    StageResult,
    Verdict,
)
from advpipe.runlog import RunLog
from advpipe.runner import AgentResult, AgentRunner, Role, build_request
from advpipe.workspace import Workspace

log = logging.getLogger(__name__)

StageKind = Literal["tests", "code"]
ALL_GATES = ["test", "types", "lint"]
M = TypeVar("M", bound=BaseModel)


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
    progress: Callable[[str], None] = _no_progress

    def emit(self, message: str) -> None:
        """Report human-readable progress (printed live by the CLI)."""
        self.progress(message)

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


async def call_agent(ctx: Context, role: Role, prompt: str, stage: str) -> AgentResult:
    request = build_request(role, prompt, ctx.config, ctx.workspace.path, ctx.budget.remaining)
    ctx.emit(f"{role.value} working ({request.model})...")
    started = time.monotonic()
    result = await ctx.runner.run(request)
    try:
        ctx.budget.add(result.cost_usd, stage)
    finally:
        ctx.save()
        error = " [agent reported an error]" if result.is_error else ""
        ctx.emit(
            f"{role.value} done in {time.monotonic() - started:.0f}s, ${result.cost_usd:.2f}"
            f" (run total ${ctx.budget.spent:.2f}){error}"
        )
    return result


def _verdict_line(role: Role, verdict: Verdict) -> str:
    return (
        f"{role.value}: {verdict.verdict} "
        f"({len(verdict.blocking)} blocking, {len(verdict.minor)} minor)"
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


def _gate_commands(config: Config) -> str:
    lines = []
    for name in ALL_GATES:
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


def author_initial_prompt(ctx: Context, kind: StageKind) -> str:
    paths = ", ".join(ctx.config.paths.tests)
    parts = [
        f"# task.md\n\n{ctx.task_md}",
        f"# Gate commands (run from repo root)\n\n{_gate_commands(ctx.config)}",
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
    ctx: Context, kind: StageKind, diff_label: str, diff: str, gates: list[GateResult]
) -> str:
    context = ""
    if kind == "code":
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


async def stage_gates(ctx: Context, kind: StageKind) -> tuple[list[GateResult], list[Finding]]:
    if kind == "tests":
        (result,) = await run_gates(ctx.config, ctx.workspace.path, ["test"])
        new_tests = ctx.workspace.changed_files(ctx.spec_commit, ctx.config.paths.tests)
        return [result], red_gate_findings(result, new_tests)
    results = await run_gates(ctx.config, ctx.workspace.path, ALL_GATES)
    return results, gate_findings(results)


def _is_gate(f: Finding) -> bool:
    return bool(f.criterion and f.criterion.startswith(("gate:", "guard:")))


def _prefix(findings: list[Finding], prefix: str) -> list[Finding]:
    return [f.model_copy(update={"id": f"{prefix}{f.id}"}) for f in findings]


# --------------------------------------------------------------------------- stages


async def adversarial_stage(ctx: Context, kind: StageKind) -> StageResult:
    """Author ⇄ critic loop with a round cap. Returns findings the arbiter must settle."""
    limits = ctx.config.limits
    if kind == "tests":
        author, critic, cap, label = (
            Role.TEST_AUTHOR,
            Role.TEST_CRITIC,
            limits.max_rounds_tests,
            "Test diff",
        )
    else:
        author, critic, cap, label = (
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
    for rnd in range(1, cap + 1):
        rounds = rnd
        ctx.state.round = rnd
        ctx.save()
        ctx.emit(f"round {rnd}/{cap}")
        rdir = f"{stage}/round-{rnd}"

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

        guard = test_file_guard(ctx, base) if kind == "code" else []
        if guard:
            ctx.emit(f"test-file guard: coder edited tests ({guard[0].evidence})")
        gates, gate_f = await stage_gates(ctx, kind)
        ctx.last_gates = gates
        expect = " (new tests should fail before implementation)" if kind == "tests" else ""
        ctx.emit(f"gates: {gates_line(gates)}{expect}")
        for f in gate_f:
            ctx.emit(f"  blocking: {f.claim}")

        diff = ctx.workspace.diff(base, REVIEW_PATHS)
        prompt = critic_prompt(ctx, kind, label, diff, gates)
        verdict, raw = await call_critic(ctx, critic, prompt, stage)
        ctx.emit(_verdict_line(critic, verdict))
        ctx.runlog.write_text(f"{rdir}/critic.json", raw)
        ctx.runlog.write_json(f"{rdir}/gates.json", gates)

        found = _prefix(guard + gate_f + verdict.findings, f"{kind[0].upper()}{rnd}-")
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
        ctx.emit(f"{reason}: {len(for_arbiter) + len(to_author)} open finding(s) go to the arbiter")
    else:
        ctx.emit(f"{kind} stage passed in {rounds} round(s)")
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
    scanner = await run_gate(
        "security", ctx.config.gates.security, ctx.workspace.path, ctx.config.limits.gate_timeout_s
    )
    ctx.emit(f"security scanner: {gates_line([scanner])}")
    if scanner.skipped:
        ctx.state.notes.append(
            f"Security scanner not run ({scanner.skip_reason}); "
            "security review used the diff alone."
        )
    ctx.runlog.write_json("review/scanner.json", scanner)

    diff = ctx.workspace.diff(ctx.state.base_commit, REVIEW_PATHS)
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

    (std, std_raw), (sec, sec_raw) = await asyncio.gather(
        call_critic(ctx, Role.STANDARDS_REVIEWER, std_prompt, "review"),
        call_critic(ctx, Role.SECURITY_REVIEWER, sec_prompt, "review"),
    )
    ctx.emit(_verdict_line(Role.STANDARDS_REVIEWER, std))
    ctx.emit(_verdict_line(Role.SECURITY_REVIEWER, sec))
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
        ctx.emit(f"  {r.finding_id}: {r.decision} ({r.reason})")
    return rulings


async def final_author_pass(
    ctx: Context, author: Role, findings: list[Finding], kind: StageKind
) -> list[Finding]:
    """One last author pass on `fix` rulings; no critic. Returns findings still failing."""
    prompt = author_findings_prompt(
        findings, ctx.last_gates, "Final pass: the arbiter ruled these must be fixed"
    )
    result = await call_agent(ctx, author, prompt, "arbiter")
    ctx.runlog.write_text(f"final-pass-{kind}/author.txt", result.text)
    guard = test_file_guard(ctx, ctx.tests_commit) if kind == "code" else []
    gates, gate_f = await stage_gates(ctx, kind)
    ctx.last_gates = gates
    ctx.emit(f"gates after final pass: {gates_line(gates)}")
    ctx.runlog.write_json(f"final-pass-{kind}/gates.json", gates)
    return guard + gate_f


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
