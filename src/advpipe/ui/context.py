"""What the "Spec & context" tab shows: everything the agents of one run worked from.

The work item, ``task.md``, the config and checks, the standards doc, and the role prompts,
each with who sees it. The "who sees" text describes what stages.py actually puts in each
agent's prompt; keep the two in step. As in detail.py, a missing or unreadable file is shown
as such rather than failing the page.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from markupsafe import Markup
from pydantic import ValidationError

from advpipe.config import Config, Paths
from advpipe.models import RunState
from advpipe.runlog import RunLog
from advpipe.runner import ROLE_MODEL, ROLE_TOOLS, Role, load_prompt
from advpipe.stages import ALL_GATES
from advpipe.ui.detail import read_text
from advpipe.ui.render import TaskMd, markdown, task_markdown
from advpipe.ui.runs import STATUSES, StatusInfo, display_status, title_of
from advpipe.workspace import GitError, git

# Who sees each block, in plain words. Critics never see an author's reasoning.
SEEN_BY = {
    "work_item": (
        "The spec writer only. Every later agent works from task.md instead, never from the "
        "work item itself."
    ),
    "task_md": (
        "Written by the spec writer. Every other agent gets it in its prompt: the test author, "
        "test critic, coder, code critic, both reviewers and the arbiter. Critics cite its "
        "acceptance criteria (AC1, AC2, ...) in their findings."
    ),
    "config": (
        "No agent. The orchestrator uses it for the models, round caps, turn limit, budget and "
        "check commands."
    ),
    "checks": (
        "The test author and coder get the test, types and lint commands in their prompt and "
        "run them. Critics, reviewers and the arbiter get the check output, never the author's "
        "reasoning. The security scanner's output goes to the security reviewer."
    ),
    "standards": (
        "In its prompt: the standards reviewer only. Separately, Claude Code loads the repo's "
        "CLAUDE.md into every agent's context (advpipe uses the project's settings)."
    ),
    "prompts": (
        "Each role gets its own prompt as its system prompt. Shown as installed now: a run "
        "started with an older advpipe may have used different text."
    ),
}

# What each role gets in its (user) prompt, from stages.py.
ROLE_INPUTS: dict[Role, str] = {
    Role.SPEC_WRITER: "the work item",
    Role.TEST_AUTHOR: (
        "task.md and the check commands; in later rounds, the findings to address and the "
        "latest check output"
    ),
    Role.TEST_CRITIC: "task.md, the diff of the new tests, and the check output",
    Role.CODER: (
        "task.md, the check commands and the list of test files; in later rounds, the findings "
        "to address and the latest check output"
    ),
    Role.CODE_CRITIC: "task.md, the list of test files, the diff, and the check output",
    Role.STANDARDS_REVIEWER: "task.md, the full diff, the check output, and the standards doc",
    Role.SECURITY_REVIEWER: (
        "task.md, the full diff, the check output, and the security scanner's output"
    ),
    Role.ARBITER: (
        "task.md, the current diff, the check output, and the open findings with each author's "
        "response"
    ),
}

CHECK_HELP = {
    "test": "the test suite",
    "types": "the type checker",
    "lint": "the linter",
    "security": "the security scanner, run once before the review",
}
# The pipeline's checks, then the scanner (which runs only before the review).
CHECKS = [*ALL_GATES, "security"]

# A git revision from run.json, before it goes on a git command line (an argument list, never
# a shell): no leading '-', so it can't be read as an option.
_REV_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]{0,199}")


@dataclass(frozen=True)
class WorkItemView:
    text: str
    html: Markup | None  # None: the work item is empty
    file: str  # the file it came from (`advpipe run --item`), or ""


@dataclass(frozen=True)
class ConfigView:
    config: Config | None  # None: missing or unreadable
    raw: str | None  # config.json as written (None: missing)
    error: str = ""  # why it couldn't be read


@dataclass(frozen=True)
class CheckView:
    name: str
    command: str  # "" when the check is off
    help: str


@dataclass(frozen=True)
class StandardsView:
    path: str  # relative to the repo, from the config
    html: Markup | None  # None: no such file
    source: str  # where this copy came from


@dataclass(frozen=True)
class RoleView:
    role: Role
    name: str  # "code critic"
    model: str
    tools: list[str]
    inputs: str
    prompt: Markup | None  # None: the prompt file couldn't be read


@dataclass(frozen=True)
class RunContext:
    run_id: str
    state: RunState
    status: str  # a STATUSES key
    title: str
    work_item: WorkItemView
    task: TaskMd | None  # None: no task.md yet
    config: ConfigView
    checks: list[CheckView]
    standards: StandardsView
    roles: list[RoleView]
    seen_by: dict[str, str]

    @property
    def info(self) -> StatusInfo:
        return STATUSES[self.status]

    @property
    def live(self) -> bool:
        return self.status == "running"


def _config(log: RunLog) -> ConfigView:
    raw = read_text(log.root / "config.json")
    if raw is None:
        return ConfigView(None, None)
    try:
        cfg = Config.model_validate_json(raw)
    except ValidationError as e:
        return ConfigView(None, raw, f"{e.error_count()} problem(s): {e.errors()[0]['msg']}")
    # Show it re-indented, in case it was written compactly.
    return ConfigView(cfg, json.dumps(json.loads(raw), indent=2))


def _checks(cfg: Config) -> list[CheckView]:
    return [
        CheckView(name, shlex.join(getattr(cfg.gates, name)), CHECK_HELP.get(name, ""))
        for name in CHECKS
    ]


def _safe_repo_path(path: str) -> str | None:
    """``path`` from config.json if it's a plain relative path inside the repo, else None."""
    p = PurePosixPath(path)
    if not path or p.is_absolute() or ".." in p.parts or path.startswith("-"):
        return None
    return p.as_posix()


def _standards(repo: Path, state: RunState, cfg: Config | None) -> StandardsView:
    """The standards doc as the run saw it: the reviewers read it from the run's working copy,
    so prefer the run's branch, then the commit it started from, then the repo as it is now."""
    configured = (cfg.paths if cfg else Paths()).standards_doc
    path = _safe_repo_path(configured)
    if path is None:
        return StandardsView(configured, None, "its path isn't inside the repo")
    for rev, where in (
        (state.branch, f"on the run's branch {state.branch}"),
        (state.base_commit, f"at the commit the run started from ({state.base_commit[:12]})"),
    ):
        if rev and _REV_RE.fullmatch(rev) and ".." not in rev:
            try:
                text = git(repo, "cat-file", "blob", f"{rev}:{path}")
            except GitError:
                continue
            return StandardsView(path, markdown(text), where)
    file = (repo / path).resolve()
    if file.is_relative_to(repo.resolve()) and file.is_file():
        return StandardsView(path, markdown(file.read_text(errors="replace")), "in the repo now")
    return StandardsView(path, None, "")


def _roles(cfg: Config | None) -> list[RoleView]:
    models = (cfg or Config()).models
    views = []
    for role in Role:
        try:
            prompt: Markup | None = markdown(load_prompt(role))
        except OSError:
            prompt = None
        views.append(
            RoleView(
                role=role,
                name=role.value.replace("-", " "),
                model=getattr(models, ROLE_MODEL[role]),
                tools=list(ROLE_TOOLS[role]),
                inputs=ROLE_INPUTS[role],
                prompt=prompt,
            )
        )
    return views


def load_context(repo: Path, run_id: str) -> RunContext | None:
    """The spec & context tab for ``run_id``, or None if there is no such run.

    Raises ValueError for an invalid run id, and OSError/ValueError if run.json can't be read.
    """
    log = RunLog.for_run(repo, run_id)
    if not (log.root / "run.json").is_file():
        return None
    state = log.read_state()
    config = _config(log)
    task_text = read_text(log.root / "task.md")
    return RunContext(
        run_id=state.run_id,
        state=state,
        status=display_status(state, log),
        title=title_of(state.work_item, width=120),
        work_item=WorkItemView(
            text=state.work_item,
            html=markdown(state.work_item) if state.work_item.strip() else None,
            file=state.work_item_file,
        ),
        task=task_markdown(task_text) if task_text is not None else None,
        config=config,
        checks=_checks(config.config) if config.config else [],
        standards=_standards(repo, state, config.config),
        roles=_roles(config.config),
        seen_by=SEEN_BY,
    )
