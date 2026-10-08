"""Command line interface: `advpipe run | resume | cancel | clean | status | report | gates`."""

from __future__ import annotations

import asyncio
import signal
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from advpipe.config import load_config
from advpipe.control import CONSOLE_LOG, ControlError, cancel_run, clean_run, start_detached
from advpipe.gates import run_gates
from advpipe.models import RunState, Status
from advpipe.orchestrator import NotResumable, Orchestrator, new_run_id, run_many
from advpipe.runlog import RunLog, check_run_id, list_runs
from advpipe.runner import SdkAgentRunner
from advpipe.workitem import WorkItemError, load_work_item

app = typer.Typer(no_args_is_help=True, help="Adversarial multi-agent coding pipeline.")

RepoOpt = Annotated[Path, typer.Option(help="Target git repository.")]
ConfigOpt = Annotated[
    Path | None, typer.Option(help="pipeline.toml (default: <repo>/pipeline.toml).")
]
QuietOpt = Annotated[bool, typer.Option("--quiet", "-q", help="Don't print live progress.")]
KeepOpt = Annotated[
    bool, typer.Option(help="Keep the worktree after a successful run (it's removed by default).")
]


def _run_id_arg(value: str | None) -> str | None:
    """Run ids are directory names: reject anything that could leave .advpipe/runs/."""
    if value is None:
        return None
    try:
        return check_run_id(value)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e


RunIdArg = Annotated[str, typer.Argument(callback=_run_id_arg, help="Run id (see advpipe status).")]


@app.callback()
def main() -> None:
    """Adversarial multi-agent coding pipeline."""


def make_progress(quiet: bool, label: str = "") -> Callable[[str], None]:
    """Progress printer: timestamped lines on stderr (stdout stays for the final summary)."""
    if quiet:
        return lambda message: None
    prefix = f"[{label}] " if label else ""

    def emit(message: str) -> None:
        print(f"{datetime.now():%H:%M:%S} {prefix}{message}", file=sys.stderr, flush=True)

    return emit


def _summary(state: RunState, runlog: RunLog) -> None:
    typer.echo(f"{state.run_id}  {state.status.value}  ${state.cost_usd:.2f}  {state.branch}")
    typer.echo(f"  report: {runlog.root / 'report.md'}")


def _short(text: str, width: int) -> str:
    """First line of ``text``, cut to ``width`` characters."""
    first = text.strip().splitlines()[0] if text.strip() else ""
    return first if len(first) <= width else first[: width - 3] + "..."


def _read_items(path: Path) -> list[str]:
    lines = (line.strip() for line in path.read_text().splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def _interruptible() -> None:
    """Make SIGINT raise KeyboardInterrupt even if this process was started with it ignored
    (e.g. by a shell's `&`), so `advpipe cancel` always works."""
    if signal.getsignal(signal.SIGINT) is signal.SIG_IGN:
        signal.signal(signal.SIGINT, signal.default_int_handler)


def _detach(
    repo: Path,
    run_id: str,
    *,
    work_item: str | None,
    item: Path | None,
    config: Path | None,
    name: str | None,
    keep_worktree: bool,
) -> None:
    """Start the same run in a background process; print its run id and return at once."""
    args = ["run", "--repo", str(repo.resolve()), "--run-id", run_id]
    if config is not None:
        args += ["--config", str(config.resolve())]
    if name is not None:
        args += ["--name", name]
    if keep_worktree:
        args.append("--keep-worktree")
    if item is not None:
        args += ["--item", str(item.resolve())]
    else:
        assert work_item is not None
        args += ["--", work_item]  # "--": a work item starting with "-" isn't an option
    runlog = RunLog.for_run(repo.resolve(), run_id)
    pid = start_detached(runlog, args)
    typer.echo(run_id)
    typer.echo(
        f"started in the background (pid {pid}); output: {runlog.root / CONSOLE_LOG}\n"
        f"follow it: advpipe status {run_id}   stop it: advpipe cancel {run_id}",
        err=True,
    )


@app.command()
def run(
    work_item: Annotated[
        str | None, typer.Argument(help="The feature or bug fix to build.")
    ] = None,
    repo: RepoOpt = Path("."),
    config: ConfigOpt = None,
    item: Annotated[
        Path | None,
        typer.Option(
            help="Read the work item from this markdown file (front matter may set name and "
            "config).",
            dir_okay=False,
        ),
    ] = None,
    from_file: Annotated[
        Path | None, typer.Option(help="Run every non-empty line of this file as a work item.")
    ] = None,
    parallel: Annotated[int, typer.Option(min=1, help="Work items to run at once.")] = 1,
    keep_worktree: KeepOpt = False,
    quiet: QuietOpt = False,
    name: Annotated[
        str | None,
        typer.Option(
            help="Branch name: advpipe/<name>. Default: derived from the work item's first line."
        ),
    ] = None,
    detach: Annotated[
        bool,
        typer.Option(
            help="Run in the background: print the run id and return. Output goes to the run's "
            "console.log."
        ),
    ] = False,
    run_id: Annotated[
        str | None, typer.Option(hidden=True, callback=_run_id_arg, help="Use this run id.")
    ] = None,
) -> None:
    """Drive work items through the pipeline, leaving a branch per item for human review."""
    item_file = ""
    if sum(x is not None for x in (work_item, item, from_file)) != 1:
        raise typer.BadParameter("give exactly one of WORK_ITEM, --item or --from-file")
    if from_file is not None:
        items = _read_items(from_file)
        if not items:
            raise typer.BadParameter(f"no work items in {from_file}")
    elif item is not None:
        try:
            parsed = load_work_item(item)
        except WorkItemError as e:
            raise typer.BadParameter(str(e)) from e
        items, item_file = [parsed.body], str(item.resolve())
        # Command-line options win over front matter. Its config path is relative to the repo.
        name = name if name is not None else parsed.name
        if config is None and parsed.config is not None:
            config = repo / parsed.config
            if not config.is_file():
                raise typer.BadParameter(f"config file not found: {config} (set in {item})")
    else:
        assert work_item is not None
        if not work_item.strip():
            raise typer.BadParameter(
                'the work item is empty. If you used "$(cat FILE)", check that FILE exists, '
                "or use --item FILE."
            )
        items = [work_item]
    if name is not None and len(items) > 1:
        raise typer.BadParameter("--name works with a single work item, not --from-file")
    if (detach or run_id is not None) and len(items) > 1:
        raise typer.BadParameter("--detach works with a single work item, not --from-file")
    if run_id is not None and (RunLog.for_run(repo.resolve(), run_id).root / "run.json").exists():
        raise typer.BadParameter(f"run {run_id} already exists")
    cfg = load_config(config, repo)
    if detach:
        _detach(
            repo,
            run_id or new_run_id(),
            work_item=work_item,
            item=item,
            config=config,
            name=name,
            keep_worktree=keep_worktree,
        )
        return
    _interruptible()
    orchestrators: list[Orchestrator] = []

    def make(item: str) -> Orchestrator:
        # Several runs share the terminal: prefix their lines with "item N" to tell them apart.
        label = f"item {len(orchestrators) + 1}" if len(items) > 1 else ""
        orch = Orchestrator(
            cfg,
            SdkAgentRunner(),
            repo,
            item,
            keep_worktree=keep_worktree,
            progress=make_progress(quiet, label),
            name=name,
            run_id=run_id,
            work_item_file=item_file,
        )
        orchestrators.append(orch)
        return orch

    try:
        states = asyncio.run(run_many(items, make, parallel))
    except KeyboardInterrupt:
        raise typer.Exit(code=130) from None  # the run already said how to resume
    for orch, state in zip(orchestrators, states, strict=True):
        _summary(state, orch.runlog)
    if any(s.status is not Status.COMPLETE for s in states):
        raise typer.Exit(code=1)


@app.command()
def resume(
    run_id: RunIdArg,
    repo: RepoOpt = Path("."),
    config: ConfigOpt = None,
    budget: Annotated[
        float | None, typer.Option(help="New per-task budget in USD (e.g. after budget_exceeded).")
    ] = None,
    keep_worktree: KeepOpt = False,
    quiet: QuietOpt = False,
) -> None:
    """Resume an interrupted run at the first stage that didn't finish."""
    cfg = load_config(config) if config else None
    try:
        orch = Orchestrator.resume(
            SdkAgentRunner(),
            repo,
            run_id,
            config=cfg,
            budget_usd=budget,
            keep_worktree=keep_worktree,
            progress=make_progress(quiet),
        )
    except NotResumable as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=2) from e
    _interruptible()
    try:
        state = asyncio.run(orch.run())
    except KeyboardInterrupt:
        raise typer.Exit(code=130) from None
    _summary(state, orch.runlog)
    if state.status is not Status.COMPLETE:
        raise typer.Exit(code=1)


@app.command()
def cancel(run_id: RunIdArg, repo: RepoOpt = Path(".")) -> None:
    """Stop an active run at its next safe point. It stays resumable (advpipe resume)."""
    try:
        pid = cancel_run(repo.resolve(), run_id)
    except ControlError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=2) from e
    typer.echo(f"sent interrupt to pid {pid}; run {run_id} stops at its next safe point")
    typer.echo(f"check with: advpipe status {run_id}   continue with: advpipe resume {run_id}")


@app.command()
def clean(
    run_id: RunIdArg,
    repo: RepoOpt = Path("."),
    force: Annotated[
        bool,
        typer.Option(help="Delete the branch even if it has commits no other branch has."),
    ] = False,
    logs: Annotated[
        bool, typer.Option(help="Also delete the run's log directory (.advpipe/runs/<id>).")
    ] = False,
) -> None:
    """Remove the worktree and branch of a run that isn't complete (e.g. a failed run)."""
    try:
        done = clean_run(repo.resolve(), run_id, force=force, logs=logs)
    except ControlError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=2) from e
    for line in done:
        typer.echo(line)


@app.command()
def status(
    run_id: Annotated[
        str | None, typer.Argument(callback=_run_id_arg, help="Run id (default: list all runs).")
    ] = None,
    repo: RepoOpt = Path("."),
) -> None:
    """List runs, or show one run's state."""
    repo = repo.resolve()
    if run_id is None:
        runs = list_runs(repo)
        if not runs:
            typer.echo("no runs")
        for s in runs:
            active = " (active)" if RunLog.for_run(repo, s.run_id).lock_holder() else ""
            item = _short(s.work_item, 50)
            typer.echo(
                f"{s.run_id}  {s.status.value + active:<16} {s.stage.value:<12} "
                f"${s.cost_usd:>6.2f}  {item}"
            )
        return
    runlog = RunLog.for_run(repo, run_id)
    if not (runlog.root / "run.json").is_file():
        typer.echo(f"no run {run_id}", err=True)
        raise typer.Exit(code=2)
    s = runlog.read_state()
    holder = runlog.lock_holder()
    lines = [
        f"run:      {s.run_id}",
        f"status:   {s.status.value}" + (f" (active, pid {holder})" if holder else ""),
        f"stage:    {s.stage.value}" + (f" round {s.round}" if s.round else ""),
        f"item:     {_short(s.work_item, 100)}",
        f"branch:   {s.branch}",
        f"cost:     ${s.cost_usd:.2f}",
        f"done:     {', '.join(s.commits) or '-'}",
        f"open:     {len(s.open_findings)} blocking, {len(s.minor_findings)} minor",
        f"updated:  {s.updated_at:%Y-%m-%d %H:%M:%S} UTC",
    ]
    typer.echo("\n".join(lines))


@app.command()
def report(run_id: RunIdArg, repo: RepoOpt = Path(".")) -> None:
    """Print a run's report.md."""
    path = RunLog.for_run(repo.resolve(), run_id).root / "report.md"
    if not path.is_file():
        typer.echo(f"no report for run {run_id}", err=True)
        raise typer.Exit(code=2)
    typer.echo(path.read_text(), nl=False)


GATE_NAMES = ("test", "types", "lint", "security")


@app.command()
def gates(
    repo: RepoOpt = Path("."),
    config: ConfigOpt = None,
    only: Annotated[
        str, typer.Option(help="Comma-separated gates to run: test, types, lint, security.")
    ] = "test,types,lint",
) -> None:
    """Run the configured gates once. Exits 2 on failure (for use as a Claude Code hook)."""
    names = [n.strip() for n in only.split(",") if n.strip()]
    unknown = [n for n in names if n not in GATE_NAMES]
    if unknown:
        raise typer.BadParameter(f"unknown gate(s): {', '.join(unknown)}")
    cfg = load_config(config, repo)
    results = asyncio.run(run_gates(cfg, repo.resolve(), names))
    failed = [r for r in results if not r.passed]
    for r in results:
        if r.passed:
            typer.echo(f"{r.name}: {'skipped (' + r.skip_reason + ')' if r.skipped else 'pass'}")
    # Failures go to stderr: a PostToolUse hook exiting 2 feeds stderr back to Claude.
    for r in failed:
        typer.echo(r.summary(), err=True)
    if failed:
        raise typer.Exit(code=2)
