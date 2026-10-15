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
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from advpipe import control, decisions
from advpipe.config import Config, load_config
from advpipe.levels import (
    LEVEL_INFO,
    SHAPES,
    LevelInfo,
    dropped_safeguards,
    level_number,
    max_agent_calls,
    parse_level,
    roles_for,
)
from advpipe.models import Level, RunState
from advpipe.runlog import check_run_id
from advpipe.ui.items import ItemPathError, item_path
from advpipe.workitem import SCAN_FAILURES, WorkItem, WorkItemError, load_work_item, switch
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
    "needs_answers": frozenset({"answer", "clean"}),
    "needs_decision": frozenset({"decide", "clean"}),
    "paused": frozenset({"decide", "clean"}),
    "handed_over": frozenset({"clean"}),
    "ended": frozenset({"clean"}),
    "finished": frozenset({"clean"}),
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


def answer_args(
    repo: Path, run_id: str, answers: dict[str, str], bypass: Iterable[str] = ()
) -> list[str]:
    """`advpipe answer ... --detach`. Each answer is one argument in the ``--answer=ID=TEXT``
    form, so an answer starting with "-" can't be read as an option; each question the person
    lets the agent decide is a ``--bypass=ID``."""
    args = ["answer", run_id, "--repo", str(repo), "--detach"]
    args += [f"--answer={qid}={text}" for qid, text in answers.items()]
    return args + [f"--bypass={qid}" for qid in bypass]


def decide_args(repo: Path, run_id: str, choice: str, note: str = "") -> list[str]:
    """`advpipe decide <run> CHOICE`. A choice that carries on resumes the run in the
    background (``--detach``); one that ends it (takeover, stop) is done by the command itself.
    The note, if any, is one ``--note=TEXT`` argument."""
    args = ["decide", run_id, choice, "--repo", str(repo)]
    if choice not in decisions.ENDING:
        args.append("--detach")
    if note:
        args.append(f"--note={note}")
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
    dirty: bool = False  # a run finished as is: its worktree holds uncommitted work


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
    dirty = False
    if worktree and state.ended_by == "finish":
        try:
            dirty = bool(git(Path(worktree), "status", "--porcelain").strip())
        except (GitError, OSError):
            dirty = True  # can't tell: ask before deleting
    return CleanInfo(branch, unmerged, worktree, dirty)


# --------------------------------------------------------------------------- new run


SCAN_CHOICES = ("default", "on", "off")


@dataclass(frozen=True)
class RunForm:
    """The New run dialog's fields, as text."""

    source: str = "item"  # "item" (a work-item file) or "task" (typed in)
    item: str = ""  # path inside the work-items folder
    task: str = ""
    name: str = ""  # branch name override ('' : front matter, else the first line)
    config: str = ""  # config override, relative to the repo
    level: str = ""  # the slider, "1" to "5" ('' : the level the run would get without it)
    scan: str = "default"  # the security scan: "default" (the level's, or the item's), "on", "off"
    scan_failures: str = ""  # "ask", "ignore" or "block" ('' : the item's or the config's)
    bypass: bool | None = None  # "don't stop for questions" (None: the item's or the config's)


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
    # How much checking, all worked out the way `advpipe run` does (command line over front
    # matter over config over defaults).
    level: Level = Level.FULL
    inherited_level: Level = Level.FULL  # what the run gets without the slider
    level_source: str = "the default"  # where inherited_level comes from
    scan: bool = True  # the effective security scan
    scan_default: bool = True  # what "default" means for the chosen level
    scan_source: str = "the level's default"
    failures: str = "ask"  # what a failing scan means below full
    inherited_failures: str = "ask"
    bypass: bool = False
    inherited_bypass: bool = False
    agents: int = 0
    calls: tuple[int, int] = (0, 0)  # happy path, worst case
    dropped: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def budget_text(self) -> str:
        """The budget as shown, and as the form sends it back to confirm it was seen."""
        return f"{self.budget:.2f}" if self.budget is not None else ""

    @property
    def info(self) -> LevelInfo:
        return LEVEL_INFO[self.level]

    @property
    def number(self) -> int:
        return level_number(self.level)

    @property
    def scan_text(self) -> str:
        return "on" if self.scan else "off"

    @property
    def scan_asks(self) -> bool:
        """The scan policy matters: the scan is on below full (at full the security reviewer
        triages it and nobody is asked)."""
        return self.scan and self.level is not Level.FULL

    @property
    def shown_text(self) -> str:
        """The level, scan, policy and Bypass the summary shows, sent back by the form, so a run
        never starts on settings nobody saw (as for the budget)."""
        return ";".join(
            [self.level.value, self.scan_text, self.failures, "bypass" if self.bypass else "ask"]
        )


def _single_line(label: str, value: str) -> str | None:
    if any(c in value for c in "\r\n\0"):
        return f"The {label} must be a single line."
    return None


def plan_run(repo: Path, items_dir: Path, form: RunForm) -> RunPlan:
    """Check the New run form and work out the command. ``repo`` and ``items_dir`` are resolved.

    Command-line options override front matter, as in `advpipe run --item`, so a field that's
    left empty isn't passed and the item's front matter (or the default) applies. The one
    exception is the level, which is always passed: a run never starts at a level nobody saw.
    """
    lines = (_single_line("branch name", form.name), _single_line("config", form.config))
    errors = [e for e in lines if e]
    warnings: list[str] = []
    args = ["run", "--repo", str(repo), "--detach"]
    body = item_name = item_config = ""
    item: WorkItem | None = None
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
    cfg: Config | None = None
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

    checking = _how_much_checking(form, item, cfg or Config(), config_label, errors)
    args += ["--level", checking.level.value]
    if form.scan == "on":
        args.append("--security-scan")
    elif form.scan == "off":
        args.append("--no-security-scan")
    if checking.failures != checking.inherited_failures:
        args += ["--scan-failures", checking.failures]
    if checking.bypass != checking.inherited_bypass:
        args.append("--bypass-questions" if checking.bypass else "--no-bypass-questions")

    name = form.name or item_name
    branch = BRANCH_PREFIX + slugify(name or body) if name or body else ""
    if branch and branch_exists(repo, branch):
        warnings.append(f"Branch {branch} already exists, so the run makes {branch}-2 or similar.")
    if form.name:
        args += ["--name", form.name]
    if form.source == "task" and body:
        args += ["--", body]  # "--": a task starting with "-" isn't read as an option
    limits = (cfg or Config()).limits
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
        agents=len(roles_for(checking.level)),
        calls=max_agent_calls(checking.level, limits),
        dropped=dropped_safeguards(checking.level, checking.scan),
        level=checking.level,
        inherited_level=checking.inherited_level,
        level_source=checking.level_source,
        scan=checking.scan,
        scan_default=checking.scan_default,
        scan_source=checking.scan_source,
        failures=checking.failures,
        inherited_failures=checking.inherited_failures,
        bypass=checking.bypass,
        inherited_bypass=checking.inherited_bypass,
    )


@dataclass(frozen=True)
class _Checking:
    level: Level
    inherited_level: Level
    level_source: str
    scan: bool
    scan_default: bool
    scan_source: str
    failures: str
    inherited_failures: str
    bypass: bool
    inherited_bypass: bool


def _how_much_checking(
    form: RunForm, item: WorkItem | None, cfg: Config, config_label: str, errors: list[str]
) -> _Checking:
    """The level, scan, scan policy and Bypass the run would get: the form over the work item's
    front matter over the config over the defaults, as `advpipe run` works them out. Problems
    (a value the form could only send if it was tampered with) go to ``errors``."""
    pipeline = cfg.pipeline
    from_config = config_label or "the config"
    if item is not None and item.level:
        inherited, source = parse_level(item.level), "the work item's front matter"
    else:
        inherited, source = pipeline.level, f"{from_config}"
    level = inherited
    if form.level:
        try:
            level = parse_level(form.level)
        except ValueError:
            errors.append("The level must be 1 (Basic) to 5 (Full).")
    if form.scan not in SCAN_CHOICES:
        errors.append("The security scan must be Default, On or Off.")
    if form.scan_failures and form.scan_failures not in SCAN_FAILURES:
        errors.append("If the scan finds issues: choose Ask me, Ignore or Block.")

    if item is not None and item.security_scan:
        configured, scan_source = switch(item.security_scan), "the work item's front matter"
    elif pipeline.security_scan is not None:
        configured, scan_source = pipeline.security_scan, from_config
    else:
        configured, scan_source = SHAPES[level].security_scan, "the level's default"
    scan = {"on": True, "off": False}.get(form.scan, configured)

    inherited_failures = (
        item.scan_failures if item is not None and item.scan_failures else pipeline.scan_failures
    )
    failures = form.scan_failures if form.scan_failures in SCAN_FAILURES else inherited_failures

    inherited_bypass = (
        switch(item.bypass_questions)
        if item is not None and item.bypass_questions
        else pipeline.bypass_questions
    )
    bypass = inherited_bypass if form.bypass is None else form.bypass
    return _Checking(
        level,
        inherited,
        source,
        scan,
        configured,
        scan_source,
        failures,
        inherited_failures,
        bypass,
        inherited_bypass,
    )
