"""FakeAgentRunner with scripted responses per role, so the orchestrator runs without API calls."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from advpipe.runner import AgentRequest, AgentResult, Role

# A scripted step: a literal reply, or a function that may edit req.cwd and returns the reply.
Step = str | Callable[[AgentRequest], str]


class FakeAgentRunner:
    def __init__(self, scripts: dict[Role, list[Step]], cost_per_call: float = 0.01) -> None:
        self.scripts = {role: list(steps) for role, steps in scripts.items()}
        self.cost_per_call = cost_per_call
        self.calls: list[AgentRequest] = []

    async def run(self, request: AgentRequest) -> AgentResult:
        self.calls.append(request)
        queue = self.scripts.get(request.role, [])
        if not queue:
            raise AssertionError(f"unexpected call to {request.role.value}")
        step = queue.pop(0)
        text = step(request) if callable(step) else step
        return AgentResult(text=text, cost_usd=self.cost_per_call)

    def calls_for(self, role: Role) -> list[AgentRequest]:
        return [c for c in self.calls if c.role == role]


# --------------------------------------------------------------------------- builders


def writes(files: dict[str, str], reply: str = "done") -> Step:
    """A step that writes files (relative to the worktree) and replies with ``reply``."""

    def step(req: AgentRequest) -> str:
        for rel, content in files.items():
            path = req.cwd / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        return reply

    return step


def finding(
    fid: str = "F1",
    *,
    severity: str = "blocking",
    category: str = "correctness",
    file: str | None = "mathutils/core.py",
    line: int | None = 10,
    criterion: str | None = "AC2",
    claim: str = "something is wrong",
) -> dict[str, Any]:
    return {
        "id": fid,
        "severity": severity,
        "category": category,
        "file": file,
        "line": line,
        "criterion": criterion,
        "claim": claim,
        "evidence": "see code",
    }


def verdict_pass(*minor: dict[str, Any]) -> str:
    return json.dumps({"verdict": "PASS", "findings": list(minor)})


def verdict_fail(*findings: dict[str, Any]) -> str:
    return json.dumps({"verdict": "FAIL", "findings": list(findings)})


def author_response(**actions: tuple[str, str]) -> str:
    """author_response(**{"C1-F1": ("disputed", "AC3 allows it")})"""
    return json.dumps(
        {
            "responses": [
                {"finding_id": fid, "action": action, "note": text, "reason": text}
                for fid, (action, text) in actions.items()
            ]
        }
    )


def rulings(**decisions: str) -> str:
    return json.dumps(
        {"rulings": [{"finding_id": f, "decision": d, "reason": "r"} for f, d in decisions.items()]}
    )


# --------------------------------------------------------------------------- sample content

TASK_MD = """# Task: Add clamp

## Goal
Add clamp(x, lo, hi).

## Acceptance criteria
- [ ] AC1: `from mathutils import clamp` works.
- [ ] AC2: returns x within [lo, hi], lo below, hi above.
- [ ] AC3: lo > hi raises ValueError.

## Constraints
- None

## In scope
- mathutils/

## Out of scope
- None

## Open questions
- None
"""

TASK_MD_BLOCKING = TASK_MD.replace(
    "## Open questions\n- None", "## Open questions\n- BLOCKING: should lo > hi swap or raise?"
)

TEST_CLAMP = """import pytest

from mathutils import clamp


def test_within() -> None:  # AC2
    assert clamp(5, 0, 10) == 5


def test_below_and_above() -> None:  # AC2
    assert clamp(-1, 0, 10) == 0
    assert clamp(11, 0, 10) == 10


def test_lo_gt_hi_raises() -> None:  # AC3
    with pytest.raises(ValueError):
        clamp(1, 10, 0)
"""

TRIVIAL_TEST = """def test_nothing() -> None:
    assert True
"""

CORE_HEADER = '''"""Core numeric helpers."""

from collections.abc import Sequence


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    """Return a / b, or ``default`` if b is zero."""
    if b == 0:
        return default
    return a / b


def mean(values: Sequence[float]) -> float:
    """Return the arithmetic mean of ``values``."""
    if not values:
        raise ValueError("values must not be empty")
    return sum(values) / len(values)
'''

CLAMP_OK = '''

def clamp(x: float, lo: float, hi: float) -> float:
    """Return x restricted to [lo, hi]."""
    if lo > hi:
        raise ValueError("lo must be <= hi")
    return max(lo, min(x, hi))
'''

CLAMP_WRONG = '''

def clamp(x: float, lo: float, hi: float) -> float:
    """Return x restricted to [lo, hi]."""
    return x
'''

INIT = '''"""Small numeric helpers."""

from mathutils.core import clamp, mean, safe_div

__all__ = ["clamp", "mean", "safe_div"]
'''

IMPL_OK = {"mathutils/core.py": CORE_HEADER + CLAMP_OK, "mathutils/__init__.py": INIT}
IMPL_WRONG = {"mathutils/core.py": CORE_HEADER + CLAMP_WRONG, "mathutils/__init__.py": INIT}


def happy_scripts() -> dict[Role, list[Step]]:
    """Every stage passes on the first round."""
    return {
        Role.SPEC_WRITER: [writes({"task.md": TASK_MD}, "wrote task.md")],
        Role.TEST_AUTHOR: [writes({"tests/test_clamp.py": TEST_CLAMP}, "AUTHOR-REASONING tests")],
        Role.TEST_CRITIC: [verdict_pass()],
        Role.CODER: [writes(IMPL_OK, "AUTHOR-REASONING code")],
        Role.CODE_CRITIC: [verdict_pass()],
        Role.STANDARDS_REVIEWER: [verdict_pass()],
        Role.SECURITY_REVIEWER: [verdict_pass()],
    }
