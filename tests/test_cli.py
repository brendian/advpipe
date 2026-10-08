from __future__ import annotations

from pathlib import Path

from fakes import FakeAgentRunner, happy_scripts
from typer.testing import CliRunner

from advpipe.cli import app
from advpipe.config import Config
from advpipe.orchestrator import Orchestrator

cli = CliRunner()


async def test_status_and_report(config: Config, target_repo: Path) -> None:
    await Orchestrator(
        config, FakeAgentRunner(happy_scripts()), target_repo, "add clamp", "r1"
    ).run()

    listing = cli.invoke(app, ["status", "--repo", str(target_repo)])
    assert listing.exit_code == 0
    assert "r1" in listing.output and "complete" in listing.output and "add clamp" in listing.output

    one = cli.invoke(app, ["status", "r1", "--repo", str(target_repo)])
    assert one.exit_code == 0
    assert "status:   complete" in one.output
    assert "done:     spec, tests, code, review" in one.output

    rep = cli.invoke(app, ["report", "r1", "--repo", str(target_repo)])
    assert rep.exit_code == 0 and "# advpipe run r1" in rep.output


def test_status_unknown_run(target_repo: Path) -> None:
    result = cli.invoke(app, ["status", "nope", "--repo", str(target_repo)])
    assert result.exit_code == 2
    assert cli.invoke(app, ["status", "--repo", str(target_repo)]).output.strip() == "no runs"


def test_run_requires_exactly_one_source(tmp_path: Path) -> None:
    items = tmp_path / "items.txt"
    items.write_text("a\n")
    assert cli.invoke(app, ["run", "--repo", str(tmp_path)]).exit_code != 0
    both = cli.invoke(app, ["run", "x", "--from-file", str(items), "--repo", str(tmp_path)])
    assert both.exit_code != 0


def test_resume_refusal_exit_code(target_repo: Path) -> None:
    result = cli.invoke(app, ["resume", "nope", "--repo", str(target_repo)])
    assert result.exit_code == 2
