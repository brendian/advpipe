"""Command line interface: `advpipe run`."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from advpipe.config import load_config
from advpipe.models import Status
from advpipe.orchestrator import Orchestrator
from advpipe.runner import SdkAgentRunner

app = typer.Typer(no_args_is_help=True, help="Adversarial multi-agent coding pipeline.")


@app.callback()
def main() -> None:
    """Adversarial multi-agent coding pipeline."""


@app.command()
def run(
    work_item: Annotated[str, typer.Argument(help="The feature or bug fix to build.")],
    repo: Annotated[Path, typer.Option(help="Target git repository.")] = Path("."),
    config: Annotated[
        Path | None, typer.Option(help="pipeline.toml (default: <repo>/pipeline.toml).")
    ] = None,
) -> None:
    """Drive one work item through the pipeline, leaving a branch for human review."""
    cfg = load_config(config, repo)
    orchestrator = Orchestrator(cfg, SdkAgentRunner(), repo, work_item)
    state = asyncio.run(orchestrator.run())
    typer.echo(f"status: {state.status.value}")
    typer.echo(f"branch: {state.branch}")
    typer.echo(f"cost:   ${state.cost_usd:.2f}")
    typer.echo(f"report: {orchestrator.runlog.root / 'report.md'}")
    if state.status is not Status.COMPLETE:
        raise typer.Exit(code=1)
