"""Writes .advpipe/runs/<run-id>/... in the target repo."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from advpipe.config import Config
from advpipe.models import Finding, RunState


class RunLocked(RuntimeError):
    pass


class RunLog:
    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def for_run(cls, repo: Path, run_id: str) -> RunLog:
        return cls(runs_dir(repo) / run_id)

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
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            pass  # exists, owned by someone else
        return pid

    def acquire_lock(self) -> None:
        holder = self.lock_holder()
        if holder is not None and holder != os.getpid():
            raise RunLocked(f"run is being driven by pid {holder}")
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock.write_text(str(os.getpid()))

    def release_lock(self) -> None:
        self._lock.unlink(missing_ok=True)

    def write_state(self, state: RunState) -> None:
        state.updated_at = datetime.now(UTC)
        self.write_text("run.json", state.model_dump_json(indent=2))

    def write_text(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

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


def render_report(state: RunState, cost_by_stage: dict[str, float]) -> str:
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
        *([f"- {k}: ${v:.2f}" for k, v in cost_by_stage.items()] or ["- None"]),
        "",
        "## Notes",
        *([f"- {n}" for n in state.notes] or ["- None"]),
    ]
    if state.noise_dropped:
        lines.append(f"- {state.noise_dropped} ungrounded finding(s) dropped as noise")
    return "\n".join(lines) + "\n"
