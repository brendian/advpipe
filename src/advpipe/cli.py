"""Command line interface: `advpipe run | resume | status | report`."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from advpipe.config import load_config
from advpipe.models import RunState, Status
from advpipe.orchestrator import NotResumable, Orchestrator, run_many
from advpipe.runlog import RunLog, list_runs
from advpipe.runner import SdkAgentRunner

app = typer.Typer(no_args_is_help=True, help="Adversarial multi-agent coding pipeline.")

RepoOpt = Annotated[Path, typer.Option(help="Target git repository.")]
ConfigOpt = Annotated[
    Path | None, typer.Option(help="pipeline.toml (default: <repo>/pipeline.toml).")
]
KeepOpt = Annotated[
    bool, typer.Option(help="Keep the worktree after a successful run (it's removed by default).")
]


@app.callback()
def main() -> None:
    """Adversarial multi-agent coding pipeline."""


def _summary(state: RunState, runlog: RunLog) -> None:
    typer.echo(f"{state.run_id}  {state.status.value}  ${state.cost_usd:.2f}  {state.branch}")
    typer.echo(f"  report: {runlog.root / 'report.md'}")


def _read_items(path: Path) -> list[str]:
    lines = (line.strip() for line in path.read_text().splitlines())
    return [line for line in lines if line and not line.startswith("#")]


@app.command()
def run(
    work_item: Annotated[
        str | None, typer.Argument(help="The feature or bug fix to build.")
    ] = None,
    repo: RepoOpt = Path("."),
    config: ConfigOpt = None,
    from_file: Annotated[
        Path | None, typer.Option(help="Run every non-empty line of this file as a work item.")
    ] = None,
    parallel: Annotated[int, typer.Option(min=1, help="Work items to run at once.")] = 1,
    keep_worktree: KeepOpt = False,
) -> None:
    """Drive work items through the pipeline, leaving a branch per item for human review."""
    if from_file is not None and work_item is None:
        items = _read_items(from_file)
    elif work_item is not None and from_file is None:
        items = [work_item]
    else:
        raise typer.BadParameter("give exactly one of WORK_ITEM or --from-file")
    cfg = load_config(config, repo)
    orchestrators: list[Orchestrator] = []

    def make(item: str) -> Orchestrator:
        orch = Orchestrator(cfg, SdkAgentRunner(), repo, item, keep_worktree=keep_worktree)
        orchestrators.append(orch)
        return orch

    states = asyncio.run(run_many(items, make, parallel))
    for orch, state in zip(orchestrators, states, strict=True):
        _summary(state, orch.runlog)
    if any(s.status is not Status.COMPLETE for s in states):
        raise typer.Exit(code=1)


@app.command()
def resume(
    run_id: str,
    repo: RepoOpt = Path("."),
    config: ConfigOpt = None,
    budget: Annotated[
        float | None, typer.Option(help="New per-task budget in USD (e.g. after budget_exceeded).")
    ] = None,
    keep_worktree: KeepOpt = False,
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
        )
    except NotResumable as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=2) from e
    state = asyncio.run(orch.run())
    _summary(state, orch.runlog)
    if state.status is not Status.COMPLETE:
        raise typer.Exit(code=1)


@app.command()
def status(
    run_id: Annotated[str | None, typer.Argument()] = None, repo: RepoOpt = Path(".")
) -> None:
    """List runs, or show one run's state."""
    repo = repo.resolve()
    if run_id is None:
        runs = list_runs(repo)
        if not runs:
            typer.echo("no runs")
        for s in runs:
            active = " (active)" if RunLog.for_run(repo, s.run_id).lock_holder() else ""
            item = s.work_item if len(s.work_item) <= 50 else s.work_item[:47] + "..."
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
        f"item:     {s.work_item}",
        f"branch:   {s.branch}",
        f"cost:     ${s.cost_usd:.2f}",
        f"done:     {', '.join(s.commits) or '-'}",
        f"open:     {len(s.open_findings)} blocking, {len(s.minor_findings)} minor",
        f"updated:  {s.updated_at:%Y-%m-%d %H:%M:%S} UTC",
    ]
    typer.echo("\n".join(lines))


@app.command()
def report(run_id: str, repo: RepoOpt = Path(".")) -> None:
    """Print a run's report.md."""
    path = RunLog.for_run(repo.resolve(), run_id).root / "report.md"
    if not path.is_file():
        typer.echo(f"no report for run {run_id}", err=True)
        raise typer.Exit(code=2)
    typer.echo(path.read_text(), nl=False)
