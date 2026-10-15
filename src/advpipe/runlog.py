"""Writes .advpipe/runs/<run-id>/... in the target repo."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from advpipe.config import Config
from advpipe.levels import LEVEL_ORDER, dropped_safeguards, level_number, roles_for, scan_on
from advpipe.models import DECISION_CHOICES, Finding, Level, RunState, Status
from advpipe.usage import LOGIN_LABELS, describe_all

EVENTS_FILE = "events.jsonl"
# Run ids are path components: letters, digits, '.', '_' and '-', not starting with '.' or '-'.
RUN_ID_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,99}")


class RunLocked(RuntimeError):
    pass


def pid_alive(pid: int) -> bool:
    """Whether process ``pid`` is running."""
    if sys.platform == "win32":
        # os.kill(pid, 0) means "send Ctrl+C" on Windows (and fails with WinError 87), so
        # ask the process itself.
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        process_query_limited_information, still_active, error_access_denied = 0x1000, 259, 5
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return ctypes.get_last_error() == error_access_denied  # exists, someone else's
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # exists, owned by someone else
    return True


def is_this_process(pid: int) -> bool:
    """Whether ``pid`` names this process, as written to run.lock by `start_detached`.

    On Windows a venv's python.exe (and a console-script .exe) is a launcher that runs the real
    interpreter as its child, so the pid `start_detached` saw is our parent's.
    """
    return pid == os.getpid() or (sys.platform == "win32" and pid == os.getppid())


def check_run_id(run_id: str) -> str:
    """Return ``run_id`` if it's safe to use as a directory name; raise ValueError otherwise."""
    if not RUN_ID_RE.fullmatch(run_id) or ".." in run_id:
        raise ValueError(f"invalid run id: {run_id!r}")
    return run_id


class RunLog:
    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def for_run(cls, repo: Path, run_id: str) -> RunLog:
        return cls(runs_dir(repo) / check_run_id(run_id))

    def read_state(self) -> RunState:
        return RunState.model_validate_json((self.root / "run.json").read_text())

    def write_config(self, config: Config) -> None:
        self.write_text("config.json", config.model_dump_json(indent=2))

    def read_config(self) -> Config | None:
        path = self.root / "config.json"
        return Config.model_validate_json(path.read_text()) if path.is_file() else None

    # A run.lock holding the driving process's pid stops two processes driving one run.
    @property
    def _lock(self) -> Path:
        return self.root / "run.lock"

    def lock_holder(self) -> int | None:
        """PID of a live process driving this run, if any."""
        try:
            pid = int(self._lock.read_text().strip())
        except (FileNotFoundError, ValueError):
            return None
        return pid if pid_alive(pid) else None

    def acquire_lock(self, pid: int | None = None) -> None:
        """Mark the run as driven by ``pid`` (default: this process)."""
        holder = self.lock_holder()
        if holder is not None and not (holder == pid if pid else is_this_process(holder)):
            raise RunLocked(f"run is being driven by pid {holder}")
        pid = pid or os.getpid()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock.write_text(str(pid))

    def release_lock(self) -> None:
        self._lock.unlink(missing_ok=True)

    def write_state(self, state: RunState) -> None:
        state.updated_at = datetime.now(UTC)
        self.write_text("run.json", state.model_dump_json(indent=2))
        for n, d in enumerate(state.decisions, 1):
            # Each decision as asked and as decided (docs/LEVELS_PLAN.md §3.12).
            self.write_text(f"decisions/{n}.json", d.model_dump_json(indent=2))

    def write_text(self, rel: str, text: str) -> None:
        """Write via a temporary file and rename, so a reader (`advpipe status`, the web UI)
        polling a live run never sees a half-written run.json."""
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(text)
        os.replace(tmp, path)

    def append_event(self, event: dict[str, object]) -> None:
        """Append one JSON line to events.jsonl. Closing the file flushes it at once."""
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / EVENTS_FILE).open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def write_json(
        self, rel: str, obj: BaseModel | Sequence[BaseModel] | dict[str, object]
    ) -> None:
        if isinstance(obj, BaseModel):
            data: object = obj.model_dump(mode="json")
        elif isinstance(obj, Sequence):
            data = [o.model_dump(mode="json") for o in obj]
        else:
            data = obj
        self.write_text(rel, json.dumps(data, indent=2))


def runs_dir(repo: Path) -> Path:
    return repo / ".advpipe" / "runs"


def list_runs(repo: Path) -> list[RunState]:
    """All runs recorded in ``repo``, oldest first."""
    root = runs_dir(repo)
    if not root.is_dir():
        return []
    states = [RunLog(p).read_state() for p in sorted(root.iterdir()) if (p / "run.json").is_file()]
    return sorted(states, key=lambda s: s.started_at)


def _finding_lines(findings: list[Finding]) -> list[str]:
    if not findings:
        return ["- None"]
    out = []
    for f in findings:
        where = f"{f.file}:{f.line}" if f.file and f.line is not None else (f.file or "")
        ref = ", ".join(x for x in (where, f.criterion or "") if x)
        out.append(f"- **{f.id}** [{f.severity}/{f.category}] ({ref}) {f.claim}")
        if f.evidence:
            ev = f.evidence.strip().replace("\n", "\n    ")
            out.append(f"  - evidence:\n    ```\n    {ev}\n    ```")
    return out


def _cost_table(state: RunState) -> list[str]:
    if not state.cost_by_stage:
        return ["- None"]
    total = sum(state.cost_by_stage.values())
    rows = ["| Stage | Agent calls | Cost | Share |", "|---|---:|---:|---:|"]
    for stage, cost in state.cost_by_stage.items():
        share = f"{cost / total:.0%}" if total else "-"
        rows.append(f"| {stage} | {state.calls_by_stage.get(stage, 0)} | ${cost:.2f} | {share} |")
    calls = sum(state.calls_by_stage.values())
    rows.append(f"| **total** | **{calls}** | **${total:.2f}** | |")
    return rows


def _questions(state: RunState, waiting: bool) -> list[str]:
    if not state.questions:
        return []
    lines = ["## Questions and answers"]
    for q in state.questions:
        lines.append(f"- **{q.id}** (spec pass {q.asked_in}): {q.text}")
        if q.answer and q.source == "agent":
            why = "you chose Bypass up front" if state.bypass_questions else "you chose Bypass"
            answer = q.answer.strip().replace("\n", "\n    ")
            lines.append(f"  - **Answer (agent's best judgement, {why}):** {answer}")
            if q.reasoning.strip():
                lines.append(f"  - **Reason:** {' '.join(q.reasoning.split())}")
        elif q.answer:
            answer = q.answer.strip().replace("\n", "\n    ")
            lines.append(f"  - **Answer:** {answer}")
        elif q.bypassed:
            lines.append("  - (Bypass: the agent decides when the run resumes)")
        else:
            lines.append("  - (not answered" + (" yet)" if waiting else ")"))
    return [*lines, ""]


def scan_policy_line(state: RunState) -> str:
    """What happens to security scan hits below full, once that isn't "ask any more"; empty when
    there's nothing to say. Says whether the person chose up front or at a decision point."""
    if state.level is Level.FULL or not scan_on(state) or state.scan_policy == "ask":
        return ""
    asked = any(d.kind == "scan_hits" and d.choice for d in state.decisions)
    how = "your choice" if asked else "chosen up front"
    what = (
        'ignored (recorded as minor findings, marked "ignored by you")'
        if state.scan_policy == "ignore"
        else "treated like failing checks"
    )
    return f"security scan hits {what}, {how}"


def _decisions(state: RunState) -> list[str]:
    """Every decision point, in order: what was asked, and what the person chose."""
    if not state.decisions:
        return []
    lines = ["## Decisions"]
    for n, d in enumerate(state.decisions, 1):
        lines.append(f"- **{n}. {d.kind}** at {d.stage.value}: {' '.join(d.explanation.split())}")
        if d.findings:
            lines.append(f"  - {len(d.findings)} finding(s): {', '.join(f.id for f in d.findings)}")
        if d.choice:
            lines.append(
                f"  - **You chose:** {d.choice}" + ("" if d.applied else " (not acted on yet)")
            )
            if d.note.strip():
                note = d.note.strip().replace("\n", "\n    ")
                lines.append(f"  - **Your note:** {note}")
        else:
            choices = " | ".join(DECISION_CHOICES[d.kind])
            lines.append(f"  - (waiting for you: advpipe decide {state.run_id} {choices})")
    last = state.decisions[-1]
    if state.ended_by == "takeover":
        lines.append(
            f"- Handed over to you at {last.stage.value}; the branch has the work as it was."
        )
    elif state.ended_by == "stop":
        lines.append(f"- Stopped at your choice at {last.stage.value}; nothing was committed.")
    elif state.ended_by == "finish":
        where = f" in `{state.worktree}`" if state.worktree else ""
        lines.append(
            f"- Finished as is at {last.stage.value}; the uncommitted work is{where or ' there'}."
        )
    if state.fix_passes:
        lines.append(f"- Fix passes you asked for: {state.fix_passes}")
    return [*lines, ""]


def _login(state: RunState) -> list[str]:
    """Which login the agents ran under and the last known usage; empty when nothing was
    reported, so a run that never learned its login reads as it always did."""
    if state.auth == "unknown" and not state.rate_limits:
        return []
    lines = ["## Login and usage", f"- **Login:** {LOGIN_LABELS[state.auth]}"]
    if state.rate_limits:
        lines.append(
            "- **Usage, last known:** " + "; ".join(describe_all(state.rate_limits)) + ". Claude "
            "reports usage only when it changes, so this may be out of date."
        )
    if state.auth == "subscription":
        lines.append(
            "- The dollar figures are API-equivalent estimates: a subscription login isn't "
            "billed per call. The budget still caps them."
        )
    elif state.auth == "console":
        lines.append(
            "- Every call is billed to your Console credits, so the dollar figures are real."
        )
    return [*lines, ""]


def _level(state: RunState) -> list[str]:
    """The run's level and what it leaves out. Just the name for full with the scan on."""
    scan = scan_on(state)
    up_front = (
        ["- **Blocking questions:** bypassed up front; the agent answers them itself"]
        if state.bypass_questions
        else []
    )
    if line := scan_policy_line(state):
        up_front.append(f"- **Scan hits:** {line}")
    if state.level is Level.FULL and scan:
        return [f"- **Level:** {state.level.value}", *up_front]
    agents = len(roles_for(state.level))
    lines = [
        f"- **Level:** {state.level.value} ({level_number(state.level)} of {len(LEVEL_ORDER)}): "
        f"{agents} agent{'s' if agents != 1 else ''} · security scan {'on' if scan else 'off'}",
        "- **Not done at this level:**",
    ]
    lines += [f"  - {line}" for line in dropped_safeguards(state.level, scan)]
    return lines + up_front


def render_report(state: RunState, *, waiting_on_answers: bool = False) -> str:
    """report.md. ``waiting_on_answers``: the run stopped on blocking questions a person can
    answer (answers.awaiting_answers), so the report says how."""
    elapsed = (state.updated_at - state.started_at).total_seconds()
    notes = list(state.notes)
    if waiting_on_answers:
        notes.append(
            "The spec has blocking questions. Answer them, and the run continues: "
            f"advpipe answer {state.run_id}"
        )
    d = state.decision
    if d is not None and state.status is Status.NEEDS_HUMAN and not d.choice and not state.ended_by:
        choices = " | ".join(DECISION_CHOICES[d.kind])
        notes.append(f"{d.explanation} Decide with: advpipe decide {state.run_id} {choices}")
    lines = [
        f"# advpipe run {state.run_id}",
        "",
        f"- **Status:** {state.status.value}",
        *_level(state),
        f"- **Work item:** {state.work_item}",
        f"- **Branch:** `{state.branch}`"
        + (
            " (worktree removed)"
            if state.worktree_removed
            else (f" (worktree `{state.worktree}`)" if state.worktree else "")
        ),
        f"- **Stopped at stage:** {state.stage.value}",
        f"- **Cost:** ${state.cost_usd:.2f}",
        f"- **Time:** {elapsed:.0f}s",
        "",
        "## Rounds used",
        *([f"- {k}: {v}" for k, v in state.rounds_used.items()] or ["- None"]),
        "",
        "## Open blocking findings",
        *_finding_lines(state.open_findings),
        "",
        "## Minor findings (not fixed)",
        *_finding_lines(state.minor_findings),
        "",
        "## Arbiter rulings",
        *([f"- {r.finding_id}: **{r.decision}**: {r.reason}" for r in state.rulings] or ["- None"]),
        "",
        "## Cost by stage",
        *_cost_table(state),
        "",
        *_login(state),
        *_questions(state, waiting_on_answers),
        *_decisions(state),
        "## Notes",
        *([f"- {n}" for n in notes] or ["- None"]),
    ]
    if state.noise_dropped:
        lines.append(f"- {state.noise_dropped} ungrounded finding(s) dropped as noise")
    return "\n".join(lines) + "\n"
