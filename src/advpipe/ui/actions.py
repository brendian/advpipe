"""Actions on runs from the web UI: start, cancel, resume and clean up.

Each action runs the advpipe CLI in a subprocess (``advpipe run --detach``, ``cancel``,
``resume --detach``, ``clean``) with an argument list, never a shell, so the UI does exactly
what the same command does in a terminal, checks included. Runs started here are ordinary
background processes: they keep going if the UI stops.

Values from the browser only ever become single arguments: a work-item path that passed
``item_path``, a config file inside the repo, a branch name (advpipe slugifies it), a budget
parsed as a number, and a one-off task passed after ``--``.
"""

from __future__ import annotations

import math
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from advpipe import control
from advpipe.config import load_config
from advpipe.models import RunState
from advpipe.runlog import check_run_id
from advpipe.ui.items import ItemPathError, item_path
from advpipe.workitem import WorkItemError, load_work_item
from advpipe.workspace import BRANCH_PREFIX, GitError, branch_exists, git, slugify, unmerged_commits

TIMEOUT_SECONDS = 60  # each command only checks things and starts a process (or runs git)
# A one-off task becomes one command-line argument, and Linux caps those at 128 KiB.
MAX_TASK_BYTES = 64_000

# Which actions make sense for each displayed status (ui.runs.STATUSES keys). The CLI checks
# again; this decides which buttons to show and which requests to refuse outright.
VALID: dict[str, frozenset[str]] = {
    "running": frozenset({"cancel"}),
    "stopped": frozenset({"resume", "clean"}),
    "error": frozenset({"resume", "clean"}),
    "budget_exceeded": frozenset({"resume", "clean"}),
    "needs_human": frozenset({"clean"}),
    "complete": frozenset(),
}


def allowed(status: str, action: str) -> bool:
    return action in VALID.get(status, frozenset())


# --------------------------------------------------------------------------- the CLI


@dataclass(frozen=True)
class CliResult:
    ok: bool
    stdout: str
    message: str  # what went wrong (stderr), or what the command said it did


def call_advpipe(repo: Path, args: list[str]) -> CliResult:
    """Run ``advpipe <args>`` and wait for it. ``args`` is a list; there is no shell."""
    argv = [*control.child_command(), *args]
    env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200"}
    try:
        proc = subprocess.run(
            argv,
            cwd=repo,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return CliResult(False, "", f"advpipe didn't answer within {TIMEOUT_SECONDS} seconds.")
    except OSError as e:
        return CliResult(False, "", f"couldn't start advpipe: {e}")
    if proc.returncode != 0:
        said = proc.stderr.strip() or proc.stdout.strip()
        return CliResult(False, proc.stdout, said or f"advpipe exited with {proc.returncode}")
    return CliResult(True, proc.stdout, proc.stdout.strip())


def started_run_id(result: CliResult) -> str:
    """The run id that ``run --detach`` / ``resume --detach`` printed on its first line."""
    first = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    return check_run_id(first)  # ValueError if it printed something else


# --------------------------------------------------------------------------- cancel, resume, clean


def cancel_args(repo: Path, run_id: str) -> list[str]:
    return ["cancel", run_id, "--repo", str(repo)]


def resume_args(repo: Path, run_id: str, budget: float | None) -> list[str]:
    args = ["resume", run_id, "--repo", str(repo), "--detach"]
    if budget is not None:
        args += ["--budget", f"{budget:.2f}"]
    return args


def clean_args(repo: Path, run_id: str, *, force: bool, logs: bool) -> list[str]:
    args = ["clean", run_id, "--repo", str(repo)]
    if force:
        args.append("--force")
    if logs:
        args.append("--logs")
    return args


def suggested_budget(cost_usd: float, budget_usd: float | None) -> float:
    """A bigger budget for a run that ran out: double it, and at least $5 more than spent."""
    return float(math.ceil(max(budget_usd * 2, cost_usd + 5) if budget_usd else 30))


def parse_budget(text: str, state: RunState, *, required: bool) -> float | None:
    """The new total budget typed in the resume form. Raises ValueError saying what's wrong."""
    text = text.strip().removeprefix("$").strip()
    if not text:
        if required:
            raise ValueError("This run ran out of budget: give it a bigger one to continue.")
        return None
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"{text!r} isn't an amount in dollars.") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError("The budget must be a positive amount in dollars.")
    if value <= state.cost_usd:
        raise ValueError(
            f"The budget is the total for the run, and it has spent ${state.cost_usd:.2f} "
            "already: give it more than that."
        )
    return round(value, 2)


@dataclass(frozen=True)
class CleanInfo:
    """What `advpipe clean` would remove, for the confirmation page."""

    branch: str  # '' if there is none (any more)
    unmerged: int | None  # commits on the branch that no other branch has; None: unknown
    worktree: str  # '' if there is none (any more)


def clean_info(repo: Path, state: RunState) -> CleanInfo:
    worktree = state.worktree if state.worktree and Path(state.worktree).exists() else ""
    branch, unmerged = "", None
    try:
        if state.branch and branch_exists(repo, state.branch):
            branch = state.branch
            top = Path(git(repo, "rev-parse", "--show-toplevel").strip())
            unmerged = unmerged_commits(top, branch)
    except (GitError, OSError):
        pass
    return CleanInfo(branch, unmerged, worktree)


# --------------------------------------------------------------------------- new run


@dataclass(frozen=True)
class RunForm:
    """The New run dialog's fields, as text."""

    source: str = "item"  # "item" (a work-item file) or "task" (typed in)
    item: str = ""  # path inside the work-items folder
    task: str = ""
    name: str = ""  # branch name override ('' : front matter, else the first line)
    config: str = ""  # config override, relative to the repo


@dataclass(frozen=True)
class RunPlan:
    """What starting the run would do, worked out the way `advpipe run` would."""

    form: RunForm
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)  # for advpipe; empty when there are errors
    budget: float | None = None
    config_label: str = ""
    branch: str = ""
    item_name: str = ""  # front matter, shown as the defaults the fields override
    item_config: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def budget_text(self) -> str:
        """The budget as shown, and as the form sends it back to confirm it was seen."""
        return f"{self.budget:.2f}" if self.budget is not None else ""


def _single_line(label: str, value: str) -> str | None:
    if any(c in value for c in "\r\n\0"):
        return f"The {label} must be a single line."
    return None


def plan_run(repo: Path, items_dir: Path, form: RunForm) -> RunPlan:
    """Check the New run form and work out the command. ``repo`` and ``items_dir`` are resolved.

    Command-line options override front matter, as in `advpipe run --item`, so a field that's
    left empty isn't passed and the item's front matter (or the default) applies.
    """
    lines = (_single_line("branch name", form.name), _single_line("config", form.config))
    errors = [e for e in lines if e]
    warnings: list[str] = []
    args = ["run", "--repo", str(repo), "--detach"]
    body = item_name = item_config = ""
    if form.source == "item":
        if not form.item:
            errors.append("Pick a work item, or choose to type a one-off task.")
        else:
            try:
                path = item_path(items_dir, form.item)
                if not path.is_file():
                    raise FileNotFoundError(form.item)
                item = load_work_item(path)
            except ItemPathError as e:
                errors.append(f"Work item: {e}")
            except FileNotFoundError:
                errors.append(f"There's no work item {form.item} any more.")
            except WorkItemError as e:
                errors.append(f"{form.item} can't be run as it is: {e}. Fix it in the editor.")
            else:
                body, item_name, item_config = item.body, item.name or "", item.config or ""
                args += ["--item", str(path)]
    elif form.source == "task":
        body = form.task.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not body:
            errors.append("The task is empty: say what to build or fix.")
        elif "\0" in body:
            errors.append("The task contains a NUL character.")
        elif len(body.encode()) > MAX_TASK_BYTES:
            errors.append(
                f"The task is over {MAX_TASK_BYTES // 1000} kB. Save it as a work item instead."
            )
    else:
        errors.append("Choose a work item or a one-off task.")

    budget, config_label = None, ""
    chosen = form.config or item_config
    try:
        if chosen:
            where = "set here" if form.config else "from the work item's front matter"
            config_path = (repo / chosen).resolve()
            if not config_path.is_relative_to(repo):
                raise ValueError(f"{chosen} is outside the repo")
            if not config_path.is_file():
                raise ValueError(f"{chosen} doesn't exist")
            cfg = load_config(config_path)
            config_label = f"{chosen} ({where})"
            if form.config:
                args += ["--config", str(config_path)]
        else:
            cfg = load_config(None, repo)
            has_file = (repo / "pipeline.toml").is_file()
            config_label = "pipeline.toml" if has_file else "advpipe's defaults (no pipeline.toml)"
        budget = cfg.limits.budget_usd_per_task
    except (OSError, ValueError) as e:  # TOML and validation errors are ValueErrors
        first = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
        errors.append(f"Config: {first}")

    name = form.name or item_name
    branch = BRANCH_PREFIX + slugify(name or body) if name or body else ""
    if branch and branch_exists(repo, branch):
        warnings.append(f"Branch {branch} already exists, so the run makes {branch}-2 or similar.")
    if form.name:
        args += ["--name", form.name]
    if form.source == "task" and body:
        args += ["--", body]  # "--": a task starting with "-" isn't read as an option
    return RunPlan(
        form=form,
        errors=errors,
        warnings=warnings,
        args=args if not errors else [],
        budget=budget,
        config_label=config_label,
        branch=branch,
        item_name=item_name,
        item_config=item_config,
    )
