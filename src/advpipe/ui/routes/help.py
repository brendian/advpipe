"""The Help page: the pipeline in plain words. Built from the same tables the other pages and
the pipeline use (statuses, step states, roles, limits), so it can't drift from them."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from advpipe.config import Config
from advpipe.levels import (
    LEVEL_INFO,
    LEVEL_ORDER,
    SHAPES,
    dropped_safeguards,
    level_number,
    max_agent_calls,
    roles_for,
    steps_for,
)
from advpipe.runner import ROLE_MODEL, ROLE_TOOLS, Role
from advpipe.ui.context import CHECK_HELP, CHECKS, ROLE_INPUTS
from advpipe.ui.detail import STEP_STATES
from advpipe.ui.runs import STATUSES
from advpipe.ui.state import ui_config

router = APIRouter()

# What to do about each status, in plain words. Keys match ui.runs.STATUSES.
STATUS_NEXT = {
    "running": "Watch it, or cancel it from its page. It carries on if you close the browser.",
    "stopped": "Resume it from its page, or clean it up if you don't want it any more.",
    "complete": "Read the change, then merge the branch yourself. The run page has the commands.",
    "needs_human": (
        'Open the run and read "What\'s left open". Finish the branch by hand, or clean it up '
        "and start a new run with a clearer work item."
    ),
    "needs_answers": (
        "Open the run and answer the spec's questions in the form there. The same run carries "
        "on: the spec writer revises task.md with your answers, then the usual steps run."
    ),
    "needs_decision": (
        "Open the run and choose: another fix pass (with a note, if you like) or take over; "
        "ignore or block the security scan's hits; or begin work and let the agent decide the "
        "questions. The same run carries on."
    ),
    "paused": (
        "Open the run. A usage limit (or the Console credit balance) ran out and the work so "
        "far is kept. Add extra usage or credits, or wait for the reset, then press Try again; "
        "or finish it as it is. Nothing retries by itself."
    ),
    "finished": (
        "Look at the uncommitted work in its working copy (the run page has the commands). "
        "Clean it up when you're done; cleaning asks before deleting uncommitted work."
    ),
    "handed_over": (
        "Check the branch out and finish it by hand: the run page has the command. Clean it up "
        "when you're done."
    ),
    "ended": "Nothing to do: you stopped it. Clean it up, or start a new run.",
    "budget_exceeded": "Resume it with a bigger budget, or clean it up.",
    "error": "Read the report for the error, then resume it, or clean it up.",
}

# Which role does what, in plain words. Keys are every Role.
ROLE_JOBS: dict[Role, str] = {
    Role.SPEC_WRITER: "Turns your work item into task.md: numbered acceptance criteria, scope "
    "and open questions.",
    Role.TEST_AUTHOR: "Writes tests for the acceptance criteria before any code exists. It "
    "sees task.md only.",
    Role.TEST_CRITIC: "Looks for missing, weak or wrong tests.",
    Role.CODER: "Changes the code until the tests and checks pass. It may not edit the tests.",
    Role.SOLO: "At the Basic and Reviewer levels, does everything in one pass: writes task.md, "
    "the tests and the code, or stops with blocking questions for you.",
    Role.CODE_CRITIC: "Looks for bugs, unmet criteria and tests that pass for the wrong reason.",
    Role.STANDARDS_REVIEWER: "Checks the change follows the repo's standards doc and stays in "
    "scope.",
    Role.SECURITY_REVIEWER: "Checks the change for security problems, with the scanner's output.",
    Role.ARBITER: "Decides, finding by finding, whether to fix or dismiss what's still open "
    "after a step's last round or the review.",
}


# The design rules (CLAUDE.md) and what each level does with them (docs/LEVELS_PLAN.md §3.3).
RULES = [
    "1. The orchestrator is code, not an agent",
    "2. Tests before code (the test author sees task.md only)",
    "3. One writer at a time; reviewers are read-only",
    "4. Critics get artifacts, not reasoning",
    "5. Every loop is bounded",
    "6. Critics may PASS; findings are grounded",
    "7. A failed check is a blocking finding",
]


def _levels() -> list[dict[str, Any]]:
    """One row per level, from levels.py: the slider's five positions."""
    limits = Config().limits
    rows = []
    for level in LEVEL_ORDER:
        shape, info = SHAPES[level], LEVEL_INFO[level]
        happy, worst = max_agent_calls(level, limits)
        rows.append(
            {
                "number": level_number(level),
                "name": level.value,
                "label": info.label,
                "good_for": info.good_for,
                "description": info.description,
                "agents": len(roles_for(level)),
                "steps": " → ".join(
                    "FINAL CHECKS" if step.value == "FINAL_GATES" else step.value
                    for step in steps_for(level)
                ),
                "calls": f"{happy}" if happy == worst else f"{happy} to {worst}",
                "scan": "on" if shape.security_scan else "off",
                "dropped": dropped_safeguards(level, shape.security_scan),
            }
        )
    return rows


def _rule_marks() -> list[tuple[str, list[str]]]:
    """For each design rule, its mark at each level: ✔, ✘ one agent, or n/a."""
    marks = []
    for n, rule in enumerate(RULES, 1):
        row = []
        for level in LEVEL_ORDER:
            shape = SHAPES[level]
            if n == 2 and shape.solo:
                row.append("✘ one agent")
            elif n in (4, 6) and not shape.critics and shape.review == "none":
                row.append("n/a")
            else:
                row.append("✔")
        marks.append((rule, row))
    return marks


@router.get("/help", response_class=HTMLResponse)
def help_page(request: Request) -> HTMLResponse:
    defaults = Config()
    roles = [
        {
            "role": role.value,
            "job": ROLE_JOBS[role],
            "model": getattr(defaults.models, ROLE_MODEL[role]),
            "writes": "Edit" in ROLE_TOOLS[role] or "Write" in ROLE_TOOLS[role],
            "inputs": ROLE_INPUTS[role],
        }
        for role in Role
    ]
    return ui_config(request).templates.TemplateResponse(
        request,
        "help.html",
        {
            "nav_current": "/help",
            "statuses": [(key, info, STATUS_NEXT[key]) for key, info in STATUSES.items()],
            "step_states": list(STEP_STATES.values()),
            "roles": roles,
            "checks": [(name, CHECK_HELP[name]) for name in CHECKS],
            "limits": defaults.limits,
            "levels": _levels(),
            "rule_marks": _rule_marks(),
            "level_labels": [LEVEL_INFO[level].label for level in LEVEL_ORDER],
        },
    )
