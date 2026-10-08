"""Writes .advpipe/runs/<run-id>/... in the target repo."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from advpipe.models import Finding, RunState


class RunLog:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def for_run(cls, repo: Path, run_id: str) -> RunLog:
        return cls(repo / ".advpipe" / "runs" / run_id)

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
        + (f" (worktree `{state.worktree}`)" if state.worktree else ""),
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
