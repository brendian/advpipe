from __future__ import annotations

import json
import sys
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


def test_gates_command(tmp_path: Path) -> None:
    ok, bad = "import sys; sys.exit(0)", "import sys; print('boom'); sys.exit(1)"
    (tmp_path / "pipeline.toml").write_text(
        f'[gates]\ntest = ["{sys.executable}", "-c", "{ok}"]\n'
        f'lint = ["{sys.executable}", "-c", "{bad}"]\ntypes = []\n'
    )
    passing = cli.invoke(app, ["gates", "--repo", str(tmp_path), "--only", "test,types"])
    assert passing.exit_code == 0
    assert "test: pass" in passing.output and "types: skipped" in passing.output

    failing = cli.invoke(app, ["gates", "--repo", str(tmp_path)])
    assert failing.exit_code == 2
    assert "lint: FAIL" in failing.stderr and "boom" in failing.stderr

    assert cli.invoke(app, ["gates", "--repo", str(tmp_path), "--only", "nope"]).exit_code != 0


def test_example_hook_config_is_valid() -> None:
    hooks = json.loads(
        (Path(__file__).parent.parent / "examples" / "claude-hooks.json").read_text()
    )
    (entry,) = hooks["hooks"]["PostToolUse"]
    assert "Edit" in entry["matcher"]
    assert entry["hooks"][0]["command"].startswith("advpipe gates")


def _fake_sdk_runner(monkeypatch: object) -> None:
    from fakes import happy_scripts as scripts

    import advpipe.cli as cli_module

    monkeypatch.setattr(cli_module, "SdkAgentRunner", lambda: FakeAgentRunner(scripts()))  # type: ignore[attr-defined]


def _write_config(repo: Path) -> None:
    (repo / "pipeline.toml").write_text(
        f'[gates]\ntest = ["{sys.executable}", "-m", "pytest", "-q", "-p", "no:cacheprovider"]\n'
        "types = []\nlint = []\nsecurity = []\n"
    )


def test_run_prints_live_progress_to_stderr(target_repo: Path, monkeypatch: object) -> None:
    _fake_sdk_runner(monkeypatch)
    _write_config(target_repo)
    result = cli.invoke(app, ["run", "add clamp", "--repo", str(target_repo)])
    assert result.exit_code == 0, result.output
    assert "== SPEC" in result.stderr and "spec-writer working" in result.stderr
    assert "started; branch advpipe/" in result.stderr
    assert "complete" in result.stdout and "== SPEC" not in result.stdout


def test_run_quiet_suppresses_progress(target_repo: Path, monkeypatch: object) -> None:
    _fake_sdk_runner(monkeypatch)
    _write_config(target_repo)
    result = cli.invoke(app, ["run", "add clamp", "--repo", str(target_repo), "--quiet"])
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert "complete" in result.stdout


def test_parallel_progress_lines_are_labelled(target_repo: Path, monkeypatch: object) -> None:
    _fake_sdk_runner(monkeypatch)
    _write_config(target_repo)
    items = target_repo.parent / "items.txt"
    items.write_text("add clamp\nadd clamp again\n")
    result = cli.invoke(
        app, ["run", "--from-file", str(items), "--repo", str(target_repo), "--parallel", "2"]
    )
    assert result.exit_code == 0, result.output
    assert "[item 1] == SPEC" in result.stderr and "[item 2] == SPEC" in result.stderr


async def test_status_shows_first_line_of_long_items(config: Config, target_repo: Path) -> None:
    item = "Create the storage layer for the thing.\n\n- lots of detail\n- more detail"
    await Orchestrator(config, FakeAgentRunner(happy_scripts()), target_repo, item, "r9").run()
    one = cli.invoke(app, ["status", "r9", "--repo", str(target_repo)])
    assert "item:     Create the storage layer for the thing.\n" in one.output
    assert "lots of detail" not in one.output
    listing = cli.invoke(app, ["status", "--repo", str(target_repo)])
    assert listing.output.rstrip().endswith("Create the storage layer for the thing.")


def test_run_name_sets_branch(target_repo: Path, monkeypatch: object) -> None:
    _fake_sdk_runner(monkeypatch)
    _write_config(target_repo)
    result = cli.invoke(
        app, ["run", "add clamp", "--repo", str(target_repo), "--name", "s01-database", "-q"]
    )
    assert result.exit_code == 0, result.output
    assert "advpipe/s01-database" in result.stdout


def test_run_name_rejected_with_from_file(tmp_path: Path) -> None:
    items = tmp_path / "items.txt"
    items.write_text("a\nb\n")
    result = cli.invoke(
        app, ["run", "--from-file", str(items), "--repo", str(tmp_path), "--name", "x"]
    )
    assert result.exit_code != 0
