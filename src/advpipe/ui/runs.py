"""What the runs list shows, read from .advpipe/runs/ (files are the source of truth)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from advpipe import answers, decisions
from advpipe.config import Config
from advpipe.levels import LEVEL_INFO, level_number
from advpipe.models import Level, RunState, Stage, Status
from advpipe.runlog import RUN_ID_RE, RunLog, runs_dir


@dataclass(frozen=True)
class StatusInfo:
    label: str
    icon: str
    help: str


# Keyed by RunState.status, plus display-only statuses derived from run.json: "stopped" (status
# "running" with no live process, or a decision made but not acted on yet), "needs_answers"
# (needs_human, waiting on answers to blocking spec questions: answers.awaiting_answers, or
# answers saved but not resumed yet), "needs_decision" (needs_human at a decision point nobody
# has chosen at: decisions.awaiting_decision), "paused" (that, at a usage limit), "handed_over"
# (the person took over), "ended" (the person chose to stop at "Begin work?") and "finished" (the
# person finished a paused run as it was).
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
    "needs_answers": StatusInfo(
        "needs answers",
        "?",
        "The spec has questions only you can answer. Answer them on the run's page (or with "
        "advpipe answer <run id>) and the same run carries on.",
    ),
    "needs_decision": StatusInfo(
        "needs a decision",
        "◆",
        "The run stopped to ask you to choose: another fix pass or take over, ignore or block "
        "security scan hits, or begin work without the answers. Choose on the run's page (or "
        "with advpipe decide <run id>) and the same run carries on.",
    ),
    "paused": StatusInfo(
        "paused: usage limit",
        "⏸",
        "A usage limit (or the Console credit balance) ran out. The run is paused with its work "
        "kept. Add usage or credits, or wait for the reset, then press Try again on the run's "
        "page (or advpipe decide <run id> retry), or finish it as it is.",
    ),
    "handed_over": StatusInfo(
        "handed over",
        "→",
        "You took over. The branch has the work as it was, and the working copy is removed. "
        "Check the branch out and finish it by hand.",
    ),
    "ended": StatusInfo(
        "ended",
        "■",
        "You chose to stop at this point. Nothing was written to the branch, and the run can't "
        "be resumed.",
    ),
    "finished": StatusInfo(
        "finished as is",
        "■",
        "You finished this run as it was when a usage limit stopped it. Nothing was committed: "
        "the work is in its working copy, uncommitted. The run can't be resumed.",
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
        frozenset(
            {
                "stopped",
                "needs_human",
                "needs_answers",
                "needs_decision",
                "paused",
                "budget_exceeded",
                "error",
            }
        ),
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
    level: str = "full"  # the run's level (a levels.Level value)

    @property
    def info(self) -> StatusInfo:
        return STATUSES[self.status]

    @property
    def level_label(self) -> str:
        """'Tests (3 of 5)', for the level chip."""
        return level_text(self.level)


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


def level_text(level: str) -> str:
    """'Tests (3 of 5)' for a level value; the value itself if it isn't a known level."""
    try:
        parsed = Level(level)
    except ValueError:
        return level
    return f"{LEVEL_INFO[parsed].label} ({level_number(parsed)} of 5)"


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
    # A live process holding run.lock is driving the run, whatever run.json says yet: e.g. a
    # background `resume` or `answer --detach` that hasn't written "running" so far.
    if log.lock_holder() is not None:
        return "running"
    if state.status is Status.RUNNING:
        return "stopped"
    if state.status is Status.NEEDS_HUMAN:
        if state.ended_by == "takeover":
            return "handed_over"
        if state.ended_by == "finish":
            return "finished"
        if state.ended_by:
            return "ended"
        if decisions.awaiting_decision(state):
            d = state.decision
            return "paused" if d is not None and d.kind == "usage_limit" else "needs_decision"
        if decisions.decision_ready(state):
            return "stopped"  # a choice is made; resuming applies it
        current, cap = answers.migrate(state), answers.max_spec_passes(log)
        if answers.awaiting_answers(current, cap) or answers.answers_ready(current, cap):
            return "needs_answers"
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
                level=state.level.value,
            )
        )
    rows.sort(key=lambda r: r.started_at, reverse=True)
    return RunList(rows, unreadable)
