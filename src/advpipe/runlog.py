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
from advpipe.models import Finding, RunState

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


def render_report(state: RunState) -> str:
    elapsed = (state.updated_at - state.started_at).total_seconds()
    lines = [
        f"# advpipe run {state.run_id}",
        "",
        f"- **Status:** {state.status.value}",
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
        "## Notes",
        *([f"- {n}" for n in state.notes] or ["- None"]),
    ]
    if state.noise_dropped:
        lines.append(f"- {state.noise_dropped} ungrounded finding(s) dropped as noise")
    return "\n".join(lines) + "\n"
