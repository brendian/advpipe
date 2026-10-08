from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fakes import FakeAgentRunner, happy_scripts
from typer.testing import CliRunner

import advpipe.cli as cli_module
from advpipe.cli import app
from advpipe.runlog import list_runs
from advpipe.workitem import WorkItem, WorkItemError, load_work_item, parse_work_item

cli = CliRunner()


def test_plain_file_has_no_front_matter() -> None:
    assert parse_work_item("Add clamp.\n\nMore detail.\n") == WorkItem(
        body="Add clamp.\n\nMore detail."
    )


def test_front_matter_sets_defaults() -> None:
    text = "---\nname: s03-stock\n\nconfig:  pipeline.server.toml \n---\n\nAdd stock changes.\n"
    item = parse_work_item(text)
    assert item == WorkItem(
        body="Add stock changes.", name="s03-stock", config="pipeline.server.toml"
    )


def test_horizontal_rule_later_in_body_is_body() -> None:
    item = parse_work_item("Intro\n\n---\n\nname: not front matter\n")
    assert item.name is None and "name: not front matter" in item.body


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("---\nname: x\nbudget: 3\n---\nbody\n", "unknown key 'budget'"),
        ("---\nname: x\nname: y\n---\nbody\n", "'name' is given twice"),
        ("---\nname:\n---\nbody\n", "'name' has no value"),
        ("---\njust words\n---\nbody\n", "expected 'key: value'"),
        ("---\nname: x\nbody without a closing fence\n", "never ends"),
        ("---\nname: x\n---\n", "the work item is empty"),
        ("---\nname: x\n---\n   \n\n", "the work item is empty"),
        ("", "the work item is empty"),
        ("  \n\n", "the work item is empty"),
    ],
)
def test_parse_errors(text: str, error: str) -> None:
    with pytest.raises(WorkItemError, match=error):
        parse_work_item(text, "item.md")


def test_error_names_file_and_line(tmp_path: Path) -> None:
    path = tmp_path / "item.md"
    path.write_text("---\nname: x\ncolour: red\n---\nbody\n")
    with pytest.raises(WorkItemError, match=r"item\.md:3: unknown key 'colour'"):
        load_work_item(path)
    with pytest.raises(WorkItemError, match="can't read"):
        load_work_item(tmp_path / "missing.md")


# --------------------------------------------------------------------------- advpipe run --item


def _setup(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "SdkAgentRunner", lambda: FakeAgentRunner(happy_scripts()))
    gates = (
        f'[gates]\ntest = ["{sys.executable}", "-m", "pytest", "-q", "-p", "no:cacheprovider"]\n'
        "types = []\nlint = []\nsecurity = []\n"
    )
    (repo / "pipeline.toml").write_text(gates)
    (repo / "pipeline.alt.toml").write_text(gates + "[limits]\nbudget_usd_per_task = 7.0\n")
    (repo / "pipeline.other.toml").write_text(gates + "[limits]\nbudget_usd_per_task = 9.0\n")


def _item(repo: Path, text: str) -> Path:
    path = repo.parent / "items" / "s03.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)
    return path


def _budget(repo: Path, run_id: str) -> float:
    config = json.loads((repo / ".advpipe" / "runs" / run_id / "config.json").read_text())
    return float(config["limits"]["budget_usd_per_task"])


def test_run_item_uses_front_matter(target_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup(target_repo, monkeypatch)
    item = _item(target_repo, "---\nname: s03-stock\nconfig: pipeline.alt.toml\n---\nAdd clamp.\n")
    result = cli.invoke(app, ["run", "--item", str(item), "--repo", str(target_repo), "-q"])
    assert result.exit_code == 0, result.output
    (state,) = list_runs(target_repo)
    assert state.branch == "advpipe/s03-stock"
    assert state.work_item == "Add clamp."  # front matter is not part of the work item
    assert state.work_item_file == str(item.resolve())
    assert _budget(target_repo, state.run_id) == 7.0


def test_run_item_command_line_overrides(
    target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(target_repo, monkeypatch)
    item = _item(target_repo, "---\nname: s03-stock\nconfig: pipeline.alt.toml\n---\nAdd clamp.\n")
    other = target_repo / "pipeline.other.toml"
    args = ["run", "--item", str(item), "--repo", str(target_repo), "-q"]
    result = cli.invoke(app, [*args, "--name", "mine", "--config", str(other)])
    assert result.exit_code == 0, result.output
    (state,) = list_runs(target_repo)
    assert state.branch == "advpipe/mine"
    assert _budget(target_repo, state.run_id) == 9.0


def test_run_item_without_front_matter(target_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup(target_repo, monkeypatch)
    item = _item(target_repo, "Add a clamp function\n\nwith details\n")
    result = cli.invoke(app, ["run", "--item", str(item), "--repo", str(target_repo), "-q"])
    assert result.exit_code == 0, result.output
    (state,) = list_runs(target_repo)
    assert state.branch == "advpipe/add-clamp-function"
    assert _budget(target_repo, state.run_id) == 15.0  # <repo>/pipeline.toml, default limits


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("---\nname: x\n---\n\n", "the work item is empty"),
        ("---\nowner: me\n---\nAdd clamp.\n", "unknown key 'owner'"),
        ("---\nconfig: nope.toml\n---\nAdd clamp.\n", "config file not found"),
    ],
)
def test_run_item_rejects_bad_files(target_repo: Path, text: str, error: str) -> None:
    item = _item(target_repo, text)
    result = cli.invoke(app, ["run", "--item", str(item), "--repo", str(target_repo)])
    assert result.exit_code != 0
    assert error in result.output
    assert not (target_repo / ".advpipe" / "runs").exists()  # nothing was started


def test_run_item_is_exclusive_with_other_sources(target_repo: Path) -> None:
    item = _item(target_repo, "Add clamp.\n")
    for extra in (["add clamp"], ["--from-file", str(item)]):
        result = cli.invoke(app, ["run", "--item", str(item), *extra, "--repo", str(target_repo)])
        assert result.exit_code != 0
        assert "exactly one of" in result.output
