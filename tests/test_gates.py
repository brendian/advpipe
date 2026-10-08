from __future__ import annotations

import sys
from pathlib import Path

from advpipe.gates import (
    TAIL_LINES,
    GateResult,
    gate_findings,
    red_gate_findings,
    run_gate,
    tail,
)


async def test_empty_command_is_skipped(tmp_path: Path) -> None:
    r = await run_gate("lint", [], tmp_path, 10)
    assert r.skipped and r.passed
    assert gate_findings([r]) == []


async def test_missing_optional_tool_is_skipped(tmp_path: Path) -> None:
    r = await run_gate("security", ["definitely-not-a-tool-xyz"], tmp_path, 10)
    assert r.skipped
    assert "not installed" in r.skip_reason


async def test_missing_required_tool_fails(tmp_path: Path) -> None:
    r = await run_gate("lint", ["definitely-not-a-tool-xyz"], tmp_path, 10)
    assert not r.passed and r.returncode == 127


async def test_failed_gate_becomes_blocking_finding(tmp_path: Path) -> None:
    code = "import sys; print('\\n'.join(map(str, range(200)))); sys.exit(3)"
    r = await run_gate("test", [sys.executable, "-c", code], tmp_path, 10)
    assert r.returncode == 3
    (f,) = gate_findings([r])
    assert f.severity == "blocking" and f.category == "correctness"
    assert f.criterion == "gate:test" and f.is_grounded
    assert f.evidence.splitlines()[-1] == "199"
    assert f.evidence.splitlines()[0] == "140"  # only the tail is kept


async def test_passing_gate_no_finding(tmp_path: Path) -> None:
    r = await run_gate("test", [sys.executable, "-c", "pass"], tmp_path, 10)
    assert r.passed and gate_findings([r]) == []


async def test_gate_timeout(tmp_path: Path) -> None:
    r = await run_gate("test", [sys.executable, "-c", "import time; time.sleep(5)"], tmp_path, 1)
    assert r.returncode == 124 and not r.passed


def test_tail_limits_lines() -> None:
    assert len(tail("\n".join(["x"] * 500)).splitlines()) == TAIL_LINES


def _result(rc: int | None, skipped: bool = False) -> GateResult:
    return GateResult(name="test", command=["pytest"], returncode=rc, skipped=skipped)


def test_red_gate_requires_new_tests() -> None:
    (f,) = red_gate_findings(_result(1), [])
    assert f.id == "G-tests-missing"


def test_red_gate_requires_failure() -> None:
    (f,) = red_gate_findings(_result(0), ["tests/test_x.py"])
    assert "pass before implementation" in f.claim


def test_red_gate_skipped_is_blocking() -> None:
    (f,) = red_gate_findings(_result(None, skipped=True), ["tests/test_x.py"])
    assert f.id == "G-tests-skipped"


def test_red_gate_failing_tests_ok() -> None:
    assert red_gate_findings(_result(1), ["tests/test_x.py"]) == []
