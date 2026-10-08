"""The Help page: the pipeline in plain words. Built from the same tables the other pages and
the pipeline use (statuses, step states, roles, limits), so it can't drift from them."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from advpipe.config import Config
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
        'Open the run and read "What\'s left open". Answer a blocking question in the work item '
        "and start a new run, or finish the branch by hand, or clean it up."
    ),
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
    Role.CODE_CRITIC: "Looks for bugs, unmet criteria and tests that pass for the wrong reason.",
    Role.STANDARDS_REVIEWER: "Checks the change follows the repo's standards doc and stays in "
    "scope.",
    Role.SECURITY_REVIEWER: "Checks the change for security problems, with the scanner's output.",
    Role.ARBITER: "Decides, finding by finding, whether to fix or dismiss what's still open "
    "after a step's last round or the review.",
}


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
        },
    )
