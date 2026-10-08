"""Run configured check commands and turn failures into blocking findings."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel

from advpipe.config import Config
from advpipe.models import Finding

TAIL_LINES = 60
TAIL_CHARS = 4000

# Gates whose tool may be absent: skipped (and reported) rather than failed.
OPTIONAL_GATES = frozenset({"security"})


class GateResult(BaseModel):
    name: str
    command: list[str]
    returncode: int | None = None
    output: str = ""
    skipped: bool = False
    skip_reason: str = ""

    @property
    def passed(self) -> bool:
        return self.skipped or self.returncode == 0

    def summary(self) -> str:
        if self.skipped:
            return f"### {self.name}: skipped ({self.skip_reason})"
        status = "PASS" if self.passed else f"FAIL (exit {self.returncode})"
        cmd = " ".join(self.command)
        return f"### {self.name}: {status}\n$ {cmd}\n```\n{self.output}\n```"


def tail(text: str) -> str:
    lines = text.rstrip().splitlines()[-TAIL_LINES:]
    return "\n".join(lines)[-TAIL_CHARS:]


def _run_sync(name: str, command: list[str], cwd: Path, timeout: int) -> GateResult:
    if not command:
        return GateResult(name=name, command=command, skipped=True, skip_reason="not configured")
    if shutil.which(command[0]) is None:
        if name in OPTIONAL_GATES:
            return GateResult(
                name=name, command=command, skipped=True, skip_reason=f"{command[0]} not installed"
            )
        return GateResult(
            name=name, command=command, returncode=127, output=f"{command[0]}: command not found"
        )
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        return GateResult(
            name=name, command=command, returncode=124, output=tail(out + "\n[timed out]")
        )
    return GateResult(
        name=name,
        command=command,
        returncode=proc.returncode,
        output=tail(proc.stdout + proc.stderr),
    )


async def run_gate(name: str, command: list[str], cwd: Path, timeout: int) -> GateResult:
    return await asyncio.to_thread(_run_sync, name, command, cwd, timeout)


async def run_gates(config: Config, cwd: Path, names: list[str]) -> list[GateResult]:
    """Run the named gates sequentially (they may share caches or build output)."""
    results = []
    for name in names:
        command: list[str] = getattr(config.gates, name)
        results.append(await run_gate(name, command, cwd, config.limits.gate_timeout_s))
    return results


def gate_finding(result: GateResult) -> Finding:
    return Finding(
        id=f"G-{result.name}",
        severity="blocking",
        category="correctness",
        criterion=f"gate:{result.name}",
        claim=f"Gate '{result.name}' failed (exit {result.returncode})",
        evidence=result.output,
    )


def gate_findings(results: list[GateResult]) -> list[Finding]:
    """Ground truth beats opinion: every failed gate is a blocking finding."""
    return [gate_finding(r) for r in results if not r.passed]


def red_gate_findings(result: GateResult, new_test_files: list[str]) -> list[Finding]:
    """Inverted TESTS gate: new tests must exist and must fail before implementation."""
    if not new_test_files:
        return [
            Finding(
                id="G-tests-missing",
                severity="blocking",
                category="coverage",
                criterion="gate:test",
                claim="No test files were added or changed",
                evidence="git diff shows no changes under the configured test paths",
            )
        ]
    if result.skipped:
        return [
            Finding(
                id="G-tests-skipped",
                severity="blocking",
                category="coverage",
                criterion="gate:test",
                claim="Test gate is not configured, so tests can't be shown to fail",
            )
        ]
    if result.returncode == 0:
        return [
            Finding(
                id="G-tests-green",
                severity="blocking",
                category="coverage",
                criterion="gate:test",
                claim="Tests don't exercise the change: they pass before implementation",
                evidence=result.output,
            )
        ]
    return []


def format_gates(results: list[GateResult]) -> str:
    return "\n\n".join(r.summary() for r in results) or "(no gates run)"
