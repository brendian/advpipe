"""What the runs list shows, read from .advpipe/runs/ (files are the source of truth)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from advpipe.config import Config
from advpipe.models import RunState, Stage, Status
from advpipe.runlog import RUN_ID_RE, RunLog, runs_dir


@dataclass(frozen=True)
class StatusInfo:
    label: str
    icon: str
    help: str


# Keyed by RunState.status, plus "stopped": status "running" with no live process.
STATUSES: dict[str, StatusInfo] = {
    "running": StatusInfo("running", "●", "An advpipe process is working on this run right now."),
    "stopped": StatusInfo(
        "stopped",
        "■",
        "Interrupted or cancelled part-way through. Continue it with: advpipe resume <run id>",
    ),
    "complete": StatusInfo(
        "complete",
        "✔",
        "Finished with all checks passing. The branch is ready for you to review and merge.",
    ),
    "needs_human": StatusInfo(
        "needs you",
        "⚠",
        "Stopped for a person to decide: open findings, failing checks, or a blocking question "
        "in task.md. The report says which.",
    ),
    "budget_exceeded": StatusInfo(
        "over budget",
        "$",
        "Hit its dollar limit. Continue with a bigger limit: advpipe resume <run id> --budget N",
    ),
    "error": StatusInfo(
        "error",
        "✖",
        "Stopped by an unexpected error (see the report). It can be resumed: advpipe resume",
    ),
}

FILTERS: dict[str, tuple[str, frozenset[str]]] = {
    "all": ("All", frozenset(STATUSES)),
    "running": ("Running", frozenset({"running"})),
    "needs_you": (
        "Needs you",
        frozenset({"stopped", "needs_human", "budget_exceeded", "error"}),
    ),
    "complete": ("Complete", frozenset({"complete"})),
}

# Plain words in the UI: "checks", not "gates".
STAGE_LABELS = {Stage.FINAL_GATES: "FINAL CHECKS"}


@dataclass(frozen=True)
class RunRow:
    run_id: str
    status: str  # a STATUSES key
    title: str
    branch: str
    stage: str
    cost_usd: float
    budget_usd: float | None
    started_at: datetime
    age: str
    work_item_file: str = ""  # absolute path, for runs started with --item

    @property
    def info(self) -> StatusInfo:
        return STATUSES[self.status]


@dataclass(frozen=True)
class RunList:
    rows: list[RunRow]  # newest first
    unreadable: list[str]  # run ids whose run.json couldn't be read

    def counts(self) -> dict[str, int]:
        return {
            key: sum(r.status in statuses for r in self.rows)
            for key, (_, statuses) in FILTERS.items()
        }

    def only(self, show: str) -> list[RunRow]:
        statuses = FILTERS[show][1]
        return [r for r in self.rows if r.status in statuses]


def title_of(work_item: str, width: int = 80) -> str:
    """The work item's first non-empty line, without a leading markdown heading mark."""
    first = next((line.strip() for line in work_item.splitlines() if line.strip()), "")
    first = first.lstrip("#").strip() or "(empty work item)"
    return first if len(first) <= width else first[: width - 1] + "…"


def age(then: datetime, now: datetime) -> str:
    seconds = max(0, int((now - then).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    days = seconds // 86400
    return f"{days} day{'s' if days != 1 else ''} ago"


def stage_label(state: RunState, cfg: Config | None) -> str:
    """'CODE r2/3': the stage, plus the round and its cap during the author/critic loops."""
    label = STAGE_LABELS.get(state.stage, state.stage.value)
    if state.stage in (Stage.TESTS, Stage.CODE) and state.round:
        label += f" r{state.round}"
        if cfg is not None:
            limits = cfg.limits
            cap = limits.max_rounds_tests if state.stage is Stage.TESTS else limits.max_rounds_code
            label += f"/{cap}"
    return label


def _config(log: RunLog) -> Config | None:
    try:
        return log.read_config()
    except (OSError, ValueError):
        return None


def display_status(state: RunState, log: RunLog) -> str:
    if state.status is Status.RUNNING:
        return "running" if log.lock_holder() is not None else "stopped"
    return state.status.value


def load_runs(repo: Path, now: datetime | None = None) -> RunList:
    """Every run under ``repo``, newest first. A run.json that can't be read (being written by
    an older advpipe, or damaged) is reported in ``unreadable`` instead of breaking the list."""
    now = now or datetime.now(UTC)
    root = runs_dir(repo)
    rows: list[RunRow] = []
    unreadable: list[str] = []
    if not root.is_dir():
        return RunList(rows, unreadable)
    for path in sorted(root.iterdir()):
        if not RUN_ID_RE.fullmatch(path.name) or not (path / "run.json").is_file():
            continue
        log = RunLog(path)
        try:
            state = log.read_state()
        except (OSError, ValueError):
            unreadable.append(path.name)
            continue
        cfg = _config(log)
        rows.append(
            RunRow(
                run_id=path.name,
                status=display_status(state, log),
                title=title_of(state.work_item),
                branch=state.branch,
                stage=stage_label(state, cfg),
                cost_usd=state.cost_usd,
                budget_usd=cfg.limits.budget_usd_per_task if cfg else None,
                started_at=state.started_at,
                age=age(state.started_at, now),
                work_item_file=state.work_item_file,
            )
        )
    rows.sort(key=lambda r: r.started_at, reverse=True)
    return RunList(rows, unreadable)
