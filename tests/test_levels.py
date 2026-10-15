"""Pipeline levels, L0 (docs/LEVELS_PLAN.md): levels as data, config and front matter, the
security-scan toggle, resume, and the Adversarial level."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from conftest import git
from fakes import (
    IMPL_OK,
    IMPL_WRONG,
    TASK_MD,
    TEST_CLAMP,
    FakeAgentRunner,
    Step,
    defect_finding,
    happy_scripts,
    interrupt,
    verdict_fail,
    writes,
)
from pydantic import ValidationError
from typer.testing import CliRunner

import advpipe.cli as cli_module
from advpipe.cli import app
from advpipe.config import Config, Limits, Pipeline, load_config
from advpipe.levels import (
    LEVEL_INFO,
    LEVEL_ORDER,
    SHAPES,
    Level,
    dropped_safeguards,
    level_number,
    max_agent_calls,
    parse_level,
    roles_for,
    scan_on,
    steps_for,
)
from advpipe.models import RunState, Stage, Status
from advpipe.orchestrator import NotResumable, Orchestrator
from advpipe.runlog import RunLog, list_runs
from advpipe.runner import Role
from advpipe.workitem import WorkItemError, parse_work_item

cli = CliRunner()
PASS_SCAN = [sys.executable, "-c", "print('scan clean')"]
FAIL_SCAN = [sys.executable, "-c", "import sys; print('rule: shell=True at x.py:1'); sys.exit(1)"]


def with_pipeline(config: Config, **settings: object) -> Config:
    return config.model_copy(update={"pipeline": Pipeline.model_validate(settings)})


def with_scan(config: Config, command: list[str]) -> Config:
    gates = config.gates.model_copy(update={"security": command})
    return config.model_copy(update={"gates": gates})


async def run(
    config: Config, repo: Path, runner: FakeAgentRunner, run_id: str = "t1"
) -> tuple[RunState, Path]:
    orch = Orchestrator(config, runner, repo, "add a clamp(x, lo, hi) function", run_id, name="t1")
    state = await orch.run()
    return state, orch.runlog.root


def events(logdir: Path) -> list[dict[str, object]]:
    lines = (logdir / "events.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


# --------------------------------------------------------------------------- levels as data


def test_every_level_has_a_shape_and_info() -> None:
    assert list(Level) == LEVEL_ORDER
    assert set(SHAPES) == set(LEVEL_INFO) == set(Level)
    assert [level_number(level) for level in LEVEL_ORDER] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize(
    ("value", "level"),
    [
        ("basic", Level.BASIC),
        ("Reviewer", Level.REVIEWER),
        (" tests ", Level.TESTS),
        ("4", Level.ADVERSARIAL),
        (5, Level.FULL),
        (1, Level.BASIC),
    ],
)
def test_parse_level(value: str | int, level: Level) -> None:
    assert parse_level(value) is level


@pytest.mark.parametrize("value", ["", "fullest", "0", "6", 0, 6, -1, True])
def test_parse_level_refuses(value: str | int) -> None:
    with pytest.raises(ValueError, match="unknown level"):
        parse_level(value)


def test_roles_and_steps() -> None:
    assert [len(roles_for(level)) for level in LEVEL_ORDER] == [1, 2, 4, 6, 8]
    assert roles_for(Level.BASIC) == [Role.SOLO]
    assert roles_for(Level.REVIEWER) == [Role.SOLO, Role.CODE_CRITIC]
    assert set(roles_for(Level.FULL)) == set(Role) - {Role.SOLO}
    assert Role.STANDARDS_REVIEWER not in roles_for(Level.ADVERSARIAL)
    assert Role.SECURITY_REVIEWER not in roles_for(Level.ADVERSARIAL)
    assert Role.TEST_CRITIC not in roles_for(Level.TESTS)
    assert Role.ARBITER not in roles_for(Level.TESTS)
    full = [Stage.SPEC, Stage.TESTS, Stage.CODE, Stage.REVIEW, Stage.ARBITER, Stage.FINAL_GATES]
    assert steps_for(Level.FULL) == full
    assert steps_for(Level.ADVERSARIAL) == [
        Stage.SPEC,
        Stage.TESTS,
        Stage.CODE,
        Stage.ARBITER,
        Stage.FINAL_GATES,
    ]
    assert steps_for(Level.TESTS) == [
        Stage.SPEC,
        Stage.TESTS,
        Stage.CODE,
        Stage.REVIEW,
        Stage.FINAL_GATES,
    ]
    assert steps_for(Level.REVIEWER) == [Stage.SOLO, Stage.REVIEW, Stage.FINAL_GATES]
    assert steps_for(Level.BASIC) == [Stage.SOLO, Stage.FINAL_GATES]


def test_dropped_safeguards() -> None:
    assert dropped_safeguards(Level.FULL, True) == []
    assert dropped_safeguards(Level.FULL, False) == ["No security scan"]
    for level in LEVEL_ORDER[:-1]:
        dropped = dropped_safeguards(level, False)
        assert "No security review" in dropped and "No security scan" in dropped, level
        assert "No security scan" not in dropped_safeguards(level, True)
    assert any("One agent writes" in line for line in dropped_safeguards(Level.BASIC, False))
    assert not any("One agent writes" in line for line in dropped_safeguards(Level.TESTS, False))
    assert not any("arbiter" in line for line in dropped_safeguards(Level.BASIC, False))
    assert any("arbiter" in line for line in dropped_safeguards(Level.TESTS, False))
    assert not any("arbiter" in line for line in dropped_safeguards(Level.ADVERSARIAL, False))


def test_scan_defaults() -> None:
    assert [SHAPES[level].security_scan for level in LEVEL_ORDER] == [False] * 4 + [True]


def test_max_agent_calls_happy_paths() -> None:
    happy = [max_agent_calls(level, Limits())[0] for level in LEVEL_ORDER]
    assert happy == [1, 2, 4, 5, 7]
    assert max_agent_calls(Level.FULL, Limits()) == (7, 30)
    assert max_agent_calls(Level.ADVERSARIAL, Limits()) == (5, 23)
    more = Limits(max_rounds_tests=3, max_rounds_code=4)
    assert max_agent_calls(Level.FULL, more)[1] == 30 + 3 + 3
    for level in LEVEL_ORDER:
        lo, hi = max_agent_calls(level, Limits())
        assert 1 <= lo <= hi


# --------------------------------------------------------------------------- config


def test_pipeline_config_defaults(tmp_path: Path) -> None:
    config = Config()
    assert config.pipeline.level is Level.FULL
    assert config.pipeline.security_scan is None
    assert config.pipeline.scan_failures == "ask"
    assert config.limits.max_fix_passes == 3


@pytest.mark.parametrize(
    ("value", "level"),
    [('"adversarial"', "adversarial"), ("4", "adversarial"), ('"2"', "reviewer")],
)
def test_pipeline_config_level_by_name_or_number(tmp_path: Path, value: str, level: str) -> None:
    (tmp_path / "pipeline.toml").write_text(
        f"[pipeline]\nlevel = {value}\nsecurity_scan = true\n[limits]\nmax_fix_passes = 1\n"
    )
    config = load_config(repo=tmp_path)
    assert config.pipeline.level == level
    assert config.pipeline.security_scan is True
    assert config.limits.max_fix_passes == 1
    # The name is what's stored.
    assert json.loads(config.model_dump_json())["pipeline"]["level"] == level


@pytest.mark.parametrize(
    "toml",
    [
        '[pipeline]\nlevel = "fullest"\n',
        "[pipeline]\nlevel = 0\n",
        "[pipeline]\nlevel = true\n",
        '[pipeline]\nscan_failures = "sometimes"\n',
        '[pipeline]\nbypass = "yes"\n',
        "[limits]\nmax_fix_passes = 0\n",
    ],
)
def test_pipeline_config_refused(tmp_path: Path, toml: str) -> None:
    (tmp_path / "pipeline.toml").write_text(toml)
    with pytest.raises(ValidationError):
        load_config(repo=tmp_path)


# --------------------------------------------------------------------------- front matter


def test_front_matter_run_settings() -> None:
    item = parse_work_item(
        "---\nlevel: 4\nsecurity_scan: Yes\nscan_failures: block\n---\nAdd clamp.\n"
    )
    assert (item.level, item.security_scan, item.scan_failures) == ("4", "Yes", "block")


@pytest.mark.parametrize(
    ("line", "error"),
    [
        ("level: huge", "s.md:3: 'level': unknown level 'huge'"),
        ("security_scan: maybe", "s.md:3: 'security_scan': expected yes or no"),
        ("scan_failures: sometimes", "s.md:3: 'scan_failures': expected one of ask, ignore"),
    ],
)
def test_front_matter_errors_name_file_and_line(line: str, error: str) -> None:
    with pytest.raises(WorkItemError, match=error.replace("'", ".")):
        parse_work_item(f"---\nname: x\n{line}\n---\nAdd clamp.\n", "s.md")


# --------------------------------------------------------------------------- precedence (CLI)


def _cli_setup(repo: Path, monkeypatch: pytest.MonkeyPatch, toml: str = "") -> None:
    gates = (
        f'[gates]\ntest = ["{sys.executable}", "-m", "pytest", "-q", "-p", "no:cacheprovider"]\n'
    )
    gates += f"types = []\nlint = []\nsecurity = {json.dumps(PASS_SCAN)}\n"
    (repo / "pipeline.toml").write_text(gates + toml)
    monkeypatch.setattr(cli_module, "SdkAgentRunner", lambda: FakeAgentRunner(happy_scripts()))


def _cli_run(repo: Path, *args: str, front: str = "") -> RunState:
    item = repo / "item.md"
    item.write_text(f"---\n{front}---\nAdd clamp.\n" if front else "Add clamp.\n")
    result = cli.invoke(app, ["run", "--item", str(item), "--repo", str(repo), "-q", *args])
    assert result.exit_code == 0, result.output
    (state,) = list_runs(repo)
    return state


@pytest.mark.parametrize(
    ("toml", "front", "args", "level", "scan"),
    [
        ("", "", [], Level.FULL, True),  # nothing anywhere: full, with the scan
        ('[pipeline]\nlevel = "adversarial"\n', "", [], Level.ADVERSARIAL, False),
        ("[pipeline]\nlevel = 4\nsecurity_scan = true\n", "", [], Level.ADVERSARIAL, True),
        ('[pipeline]\nlevel = "adversarial"\n', "level: full\n", [], Level.FULL, True),
        ("[pipeline]\nlevel = 5\n", "level: full\n", ["--level", "4"], Level.ADVERSARIAL, False),
        ("[pipeline]\nsecurity_scan = false\n", "", [], Level.FULL, False),
        ("[pipeline]\nsecurity_scan = false\n", "security_scan: yes\n", [], Level.FULL, True),
        ("", "security_scan: yes\n", ["--no-security-scan"], Level.FULL, False),
        ("", "level: adversarial\n", ["--security-scan"], Level.ADVERSARIAL, True),
    ],
)
def test_precedence(
    target_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    toml: str,
    front: str,
    args: list[str],
    level: Level,
    scan: bool,
) -> None:
    """The command line wins over front matter, which wins over [pipeline], which wins over
    the level's default."""
    _cli_setup(target_repo, monkeypatch, toml)
    state = _cli_run(target_repo, *args, front=front)
    assert state.level is level
    assert state.security_scan is scan
    saved = RunLog.for_run(target_repo, state.run_id).read_config()
    assert saved is not None and saved.pipeline.level is level  # config.json says it too


def test_scan_failures_precedence(target_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _cli_setup(target_repo, monkeypatch, '[pipeline]\nscan_failures = "ignore"\n')
    state = _cli_run(target_repo, "--scan-failures", "block", front="scan_failures: ask\n")
    saved = RunLog.for_run(target_repo, state.run_id).read_config()
    assert saved is not None and saved.pipeline.scan_failures == "block"


@pytest.mark.parametrize(
    ("args", "front", "toml", "error"),
    [
        (["--level", "huge"], "", "", "unknown level 'huge'"),
        (["--scan-failures", "maybe"], "", "", "use one of ask, ignore, block"),
    ],
)
def test_run_refuses_bad_level_settings(
    target_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    front: str,
    toml: str,
    error: str,
) -> None:
    _cli_setup(target_repo, monkeypatch, toml)
    item = target_repo / "item.md"
    item.write_text(f"---\n{front}---\nAdd clamp.\n" if front else "Add clamp.\n")
    for detach in ([], ["--detach"]):
        cmd = ["run", "--item", str(item), "--repo", str(target_repo), *args, *detach]
        result = cli.invoke(app, cmd)
        assert result.exit_code == 2
        assert error in " ".join(result.output.split()).replace("│", "").replace("  ", " ")
    assert not (target_repo / ".advpipe" / "runs").exists()  # nothing was started


def test_detach_passes_settings_on(target_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[list[str]] = []
    monkeypatch.setattr(cli_module, "start_detached", lambda log, args: started.append(args) or 1)
    args = ["run", "add clamp", "--repo", str(target_repo), "--detach"]
    flags = ["--level", "adversarial", "--security-scan", "--scan-failures", "block"]
    assert cli.invoke(app, [*args, *flags]).exit_code == 0
    assert cli.invoke(app, [*args, "--no-security-scan"]).exit_code == 0
    assert cli.invoke(app, args).exit_code == 0
    with_flags, no_scan, plain = started
    assert with_flags[with_flags.index("--level") + 1] == "adversarial"
    assert "--security-scan" in with_flags
    assert with_flags[with_flags.index("--scan-failures") + 1] == "block"
    assert "--no-security-scan" in no_scan
    assert not {"--level", "--security-scan", "--no-security-scan", "--scan-failures"} & set(plain)


# --------------------------------------------------------------------------- the scan toggle


async def test_scan_on_at_full_by_default(config: Config, target_repo: Path) -> None:
    state, logdir = await run(
        with_scan(config, PASS_SCAN), target_repo, FakeAgentRunner(happy_scripts())
    )
    assert state.status is Status.COMPLETE, state.notes
    assert state.level is Level.FULL and state.security_scan is True
    scanner = json.loads((logdir / "review" / "scanner.json").read_text())
    assert scanner["skipped"] is False and "scan clean" in scanner["output"]
    # At full the review stage runs the scan, not the authors' checks or the final gates.
    final = json.loads((logdir / "final-gates.json").read_text())
    assert [g["name"] for g in final] == ["test", "types", "lint"]
    report = (logdir / "report.md").read_text()
    assert "- **Level:** full\n" in report
    assert "Not done at this level" not in report


async def test_scan_off_at_full(config: Config, target_repo: Path) -> None:
    cfg = with_pipeline(with_scan(config, FAIL_SCAN), security_scan=False)
    runner = FakeAgentRunner(happy_scripts())
    state, logdir = await run(cfg, target_repo, runner)
    assert state.status is Status.COMPLETE, state.notes
    assert state.security_scan is False
    scanner = json.loads((logdir / "review" / "scanner.json").read_text())
    assert scanner["skipped"] is True and scanner["skip_reason"] == "turned off for this run"
    (security,) = runner.calls_for(Role.SECURITY_REVIEWER)  # still reviews, from the diff
    assert "skipped (turned off for this run)" in security.prompt
    report = (logdir / "report.md").read_text()
    assert "- **Level:** full (5 of 5): 8 agents · security scan off" in report
    assert "  - No security scan" in report
    assert "Security scanner not run (turned off for this run)" in report


async def test_scan_off_at_adversarial_by_default(config: Config, target_repo: Path) -> None:
    cfg = with_pipeline(with_scan(config, FAIL_SCAN), level="adversarial")
    runner = FakeAgentRunner(happy_scripts())
    state, logdir = await run(cfg, target_repo, runner)
    assert state.status is Status.COMPLETE, state.notes
    assert state.security_scan is False
    final = json.loads((logdir / "final-gates.json").read_text())
    assert [g["name"] for g in final] == ["test", "types", "lint"]
    assert "security" not in runner.calls_for(Role.CODER)[0].prompt


async def test_scan_on_below_full_runs_with_the_checks(config: Config, target_repo: Path) -> None:
    cfg = with_pipeline(with_scan(config, PASS_SCAN), level="adversarial", security_scan=True)
    runner = FakeAgentRunner(happy_scripts())
    state, logdir = await run(cfg, target_repo, runner)
    assert state.status is Status.COMPLETE, state.notes
    final = json.loads((logdir / "final-gates.json").read_text())
    assert [g["name"] for g in final] == ["test", "types", "lint", "security"]
    code_round = json.loads((logdir / "stage-code" / "round-1" / "gates.json").read_text())
    assert [g["name"] for g in code_round] == ["test", "types", "lint", "security"]
    # The coder is told about the scan command, so it can run it.
    assert "- security: `" in runner.calls_for(Role.CODER)[0].prompt
    # 'ask' is the default: a scan that passes never asks.
    assert state.scan_policy == "ask" and state.decision is None
    report = (logdir / "report.md").read_text()
    assert "- **Level:** adversarial (4 of 5): 6 agents · security scan on" in report


async def test_scan_hits_block_below_full(config: Config, target_repo: Path) -> None:
    """With the policy block, scan hits are failing checks: back to the coder, then the
    usual end of the stage."""
    cfg = with_pipeline(
        with_scan(config, FAIL_SCAN), level="adversarial", security_scan=True, scan_failures="block"
    )
    scripts = happy_scripts()
    scripts[Role.CODER] = [writes(IMPL_OK)] + ["tried"] * 3
    scripts[Role.CODE_CRITIC] = [verdict_fail()] * 3
    runner = FakeAgentRunner(scripts)
    state, _ = await run(cfg, target_repo, runner)
    assert state.status is Status.NEEDS_HUMAN
    assert [f.criterion for f in state.open_findings] == ["gate:security"]
    assert "shell=True" in state.open_findings[0].evidence
    assert "G-security" in runner.calls_for(Role.CODER)[1].prompt


@pytest.mark.parametrize("level", ["adversarial", "full"])
async def test_scanner_not_installed_is_skipped(
    config: Config, target_repo: Path, level: str
) -> None:
    cfg = with_pipeline(
        with_scan(config, ["advpipe-no-such-scanner", "--error"]), level=level, security_scan=True
    )
    state, logdir = await run(cfg, target_repo, FakeAgentRunner(happy_scripts()))
    assert state.status is Status.COMPLETE, state.notes
    notes = [n for n in state.notes if "advpipe-no-such-scanner not installed" in n]
    assert len(notes) == 1, state.notes  # said once, however often it was skipped
    assert "advpipe-no-such-scanner not installed" in (logdir / "report.md").read_text()


# --------------------------------------------------------------------------- old runs, resume


def test_old_run_json_loads_as_full_with_the_scan() -> None:
    data = json.loads(RunState(run_id="r", work_item="w", repo="/r").model_dump_json())
    for key in ("level", "security_scan", "scan_policy"):
        del data[key]
    state = RunState.model_validate(data)
    assert state.level is Level.FULL and state.security_scan is None
    assert scan_on(state) is True


def adversarial_interrupted_scripts() -> dict[Role, list[Step]]:
    scripts = happy_scripts()
    scripts[Role.CODE_CRITIC] = [interrupt]
    del scripts[Role.STANDARDS_REVIEWER], scripts[Role.SECURITY_REVIEWER]
    return scripts


async def test_resume_keeps_level_and_scan(config: Config, target_repo: Path) -> None:
    cfg = with_pipeline(config, level="adversarial", security_scan=True)
    orch = Orchestrator(
        cfg, FakeAgentRunner(adversarial_interrupted_scripts()), target_repo, "add clamp", "r1"
    )
    with pytest.raises(asyncio.CancelledError):
        await orch.run()

    # A config without [pipeline] (or matching it) is fine; the run keeps its settings.
    scripts: dict[Role, list[Step]] = {
        Role.CODER: [writes(IMPL_OK)],
        Role.CODE_CRITIC: ['{"verdict": "PASS"}'],
    }
    resumed = Orchestrator.resume(FakeAgentRunner(scripts), target_repo, "r1", config=config)
    assert resumed.config.pipeline.level is Level.ADVERSARIAL
    assert resumed.config.pipeline.security_scan is True
    state = await resumed.run()
    assert state.status is Status.COMPLETE, state.notes
    assert state.level is Level.ADVERSARIAL and state.security_scan is True
    assert "review" not in state.commits
    start = [e for e in events(resumed.runlog.root) if e["kind"] == "run_start"][-1]
    assert (start["level"], start["security_scan"]) == ("adversarial", True)


@pytest.mark.parametrize(
    ("settings", "error"),
    [
        ({"level": "full"}, "run r1 is at level adversarial; a resumed run keeps its level"),
        ({"level": 4, "security_scan": False}, "has the security scan on; a resumed run keeps"),
    ],
)
async def test_resume_refuses_a_config_that_changes_them(
    config: Config, target_repo: Path, settings: dict[str, object], error: str
) -> None:
    cfg = with_pipeline(config, level="adversarial", security_scan=True)
    orch = Orchestrator(
        cfg, FakeAgentRunner(adversarial_interrupted_scripts()), target_repo, "add clamp", "r1"
    )
    with pytest.raises(asyncio.CancelledError):
        await orch.run()
    with pytest.raises(NotResumable, match=error):
        Orchestrator.resume(
            FakeAgentRunner({}), target_repo, "r1", config=with_pipeline(config, **settings)
        )
    other = target_repo / "other.toml"
    other.write_text('[pipeline]\nlevel = "full"\n')
    for detach in ([], ["--detach"]):
        cmd = ["resume", "r1", "--repo", str(target_repo), "--config", str(other), *detach]
        result = cli.invoke(app, cmd)
        assert result.exit_code == 2
        assert "a resumed run keeps its level" in result.output


# --------------------------------------------------------------------------- Adversarial


async def test_adversarial_run(config: Config, target_repo: Path) -> None:
    cfg = with_pipeline(config, level="adversarial")
    runner = FakeAgentRunner(happy_scripts())
    state, logdir = await run(cfg, target_repo, runner)

    assert state.status is Status.COMPLETE, state.notes
    assert state.stage is Stage.DONE
    assert not runner.calls_for(Role.STANDARDS_REVIEWER)
    assert not runner.calls_for(Role.SECURITY_REVIEWER)
    assert {c.role for c in runner.calls} <= set(roles_for(Level.ADVERSARIAL))
    assert len(runner.calls) == max_agent_calls(Level.ADVERSARIAL, cfg.limits)[0]
    assert list(state.commits) == ["spec", "tests", "code"]
    assert not (logdir / "review").exists()
    log = git(target_repo, "log", "--format=%s", "main..advpipe/t1").splitlines()
    assert log == ["advpipe: implementation", "advpipe: tests", "advpipe: spec (task.md)"]

    report = (logdir / "report.md").read_text()
    assert "- **Level:** adversarial (4 of 5): 6 agents · security scan off" in report
    assert "  - No standards review\n  - No security review\n  - No security scan" in report
    start = next(e for e in events(logdir) if e["kind"] == "run_start")
    assert start["message"] == "run t1 started at level adversarial; branch advpipe/t1"
    assert (start["level"], start["security_scan"]) == ("adversarial", False)


async def test_full_run_start_reads_as_before(config: Config, target_repo: Path) -> None:
    state, logdir = await run(config, target_repo, FakeAgentRunner(happy_scripts()))
    start = next(e for e in events(logdir) if e["kind"] == "run_start")
    assert start["message"] == "run t1 started; branch advpipe/t1"
    assert (start["level"], start["security_scan"]) == ("full", True)
    assert state.scan_policy == "ask"  # unused at full; no note about it
    assert not any("policy" in n for n in state.notes)


def worst_case_scripts(review: bool) -> dict[Role, list[Step]]:
    """Every critic, reviewer and arbiter reply is unparseable, every stage reaches its cap,
    and the code critic's one parseable verdict triggers the test-fix pass."""
    junk = "not json"
    scripts: dict[Role, list[Step]] = {
        Role.SPEC_WRITER: [writes({"task.md": TASK_MD})],
        # tests rounds 1-2, the tests stage's final pass, the code stage's test-fix pass
        Role.TEST_AUTHOR: [writes({"tests/test_clamp.py": TEST_CLAMP}), "r2", "final", "fix"],
        Role.TEST_CRITIC: [junk] * 4,
        Role.ARBITER: [junk] * (6 if review else 4),
        Role.CODER: [writes(IMPL_WRONG), "r2", "r3", writes(IMPL_OK)]
        + (["review"] if review else []),
        Role.CODE_CRITIC: [junk, verdict_fail(defect_finding())] + [junk] * 4,
    }
    if review:
        scripts[Role.STANDARDS_REVIEWER] = [junk] * 2
        scripts[Role.SECURITY_REVIEWER] = [junk] * 2
    return scripts


@pytest.mark.parametrize("level", [Level.FULL, Level.ADVERSARIAL])
async def test_worst_case_agent_calls(config: Config, target_repo: Path, level: Level) -> None:
    """max_agent_calls' worst case is what the orchestrator really does when everything
    fails as late as it can and the run still ends complete."""
    cfg = with_pipeline(config, level=level.value)
    runner = FakeAgentRunner(worst_case_scripts(review=level is Level.FULL))
    state, _ = await run(cfg, target_repo, runner)
    assert state.status is Status.COMPLETE, (state.notes, state.open_findings)
    assert len(runner.calls) == max_agent_calls(level, cfg.limits)[1]
    assert all(not steps for steps in runner.scripts.values())  # every scripted call happened


# --------------------------------------------------------------------------- CLI output


async def test_summary_line(
    config: Config, target_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state, logdir = await run(config, target_repo, FakeAgentRunner(happy_scripts()), "f1")
    cli_module._summary(state, RunLog(logdir))
    assert capsys.readouterr().out.splitlines()[0] == "f1  complete  $0.07  advpipe/t1"

    adv = with_pipeline(config, level="adversarial")
    state, logdir = await run(adv, target_repo, FakeAgentRunner(happy_scripts()), "a1")
    cli_module._summary(state, RunLog(logdir))
    assert (
        capsys.readouterr().out.splitlines()[0]
        == "a1  complete  $0.05  advpipe/t1-2  [adversarial]"
    )

    state.level, state.security_scan = Level.FULL, False
    cli_module._summary(state, RunLog(logdir))
    assert capsys.readouterr().out.splitlines()[0].endswith("  [full, no scan]")
    state.level, state.security_scan = Level.ADVERSARIAL, True
    cli_module._summary(state, RunLog(logdir))
    assert capsys.readouterr().out.splitlines()[0].endswith("  [adversarial, scan]")

    one = cli.invoke(app, ["status", "a1", "--repo", str(target_repo)])
    assert "level:    adversarial (4 of 5), security scan off" in one.output


def test_levels_command(tmp_path: Path) -> None:
    (tmp_path / "pipeline.toml").write_text('[pipeline]\nlevel = "adversarial"\n')
    result = cli.invoke(app, ["levels", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    out = result.output
    for level in LEVEL_ORDER:
        assert f"{level_number(level)}  {LEVEL_INFO[level].label} ({level.value})" in out
    assert "4  Adversarial (adversarial)  (default)" in out
    assert "1  Basic (basic)\n" in out and "not available" not in out
    assert "5  Full (full)\n" in out
    assert "7 agent calls if all goes well, at most 30" in out
    assert "1 agent: solo; 1 agent call if all goes well, at most 1" in out
    full = out[out.index("5  Full") :]
    assert "not done" not in full.split("\n\n")[0]

    (tmp_path / "pipeline.toml").write_text("[limits]\nmax_rounds_code = 1\n")
    result = cli.invoke(app, ["levels", "--repo", str(tmp_path)])
    assert "5  Full (full)  (default)" in result.output
    assert "at most 24" in result.output  # two fewer code rounds of 3 calls


# --------------------------------------------------------------------------- web UI editor


def test_ui_editor_keeps_run_settings(tmp_path: Path) -> None:
    """The editor has no fields for level and the scan yet: saving keeps the file's."""
    pytest.importorskip("fastapi")
    from advpipe.ui.items import from_form, read_draft, save_item

    items = tmp_path / "work-items"
    items.mkdir()
    text = "---\nname: s1\nlevel: adversarial\nsecurity_scan: yes\n---\nAdd clamp.\n"
    (items / "s1.md").write_text(text)
    draft = read_draft(items, "s1.md")
    assert save_item(items, from_form("s1.md", "s1", "", "Add clamp.", draft.base)) is False
    assert (items / "s1.md").read_text() == text
    assert save_item(items, from_form("s1.md", "s2", "", "Add clamp.", draft.base)) is True
    saved = parse_work_item((items / "s1.md").read_text())
    assert (saved.name, saved.level, saved.security_scan) == ("s2", "adversarial", "yes")
