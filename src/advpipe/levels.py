"""Pipeline levels: how much checking a run gets (docs/LEVELS_PLAN.md).

A level is a name for a **shape**: a few switches the orchestrator reads. Everything the CLI,
the report and the UI say about a level is derived from ``SHAPES`` here, so they can't drift
apart. A person picks the level (command line, work-item front matter, config); no agent can
change it, and it never changes during a run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from advpipe.models import Level, RunState, Stage
from advpipe.runner import Role

if TYPE_CHECKING:
    from advpipe.config import Limits

__all__ = [
    "LEVEL_INFO",
    "LEVEL_ORDER",
    "SHAPES",
    "Level",
    "LevelInfo",
    "Shape",
    "dropped_safeguards",
    "level_number",
    "max_agent_calls",
    "level_tag",
    "parse_level",
    "roles_for",
    "scan_on",
    "steps_for",
]


class Shape(BaseModel, frozen=True):
    solo: bool  # one agent writes task.md, tests and code in one stage
    critics: bool  # tests and code stages are author ⇄ critic loops
    review: Literal["none", "single", "full"]
    arbiter: bool  # disputed / still-open findings go to the arbiter
    security_scan: bool  # the default for the security-scan toggle


# fmt: off
SHAPES: dict[Level, Shape] = {
    Level.BASIC:       Shape(solo=True,  critics=False, review="none",   arbiter=False, security_scan=False),  # noqa: E501
    Level.REVIEWER:    Shape(solo=True,  critics=False, review="single", arbiter=False, security_scan=False),  # noqa: E501
    Level.TESTS:       Shape(solo=False, critics=False, review="single", arbiter=False, security_scan=False),  # noqa: E501
    Level.ADVERSARIAL: Shape(solo=False, critics=True,  review="none",   arbiter=True,  security_scan=False),  # noqa: E501
    Level.FULL:        Shape(solo=False, critics=True,  review="full",   arbiter=True,  security_scan=True),  # noqa: E501
}
# fmt: on

LEVEL_ORDER: list[Level] = [
    Level.BASIC,
    Level.REVIEWER,
    Level.TESTS,
    Level.ADVERSARIAL,
    Level.FULL,
]


class LevelInfo(BaseModel, frozen=True):
    label: str
    good_for: str
    description: str


LEVEL_INFO: dict[Level, LevelInfo] = {
    Level.BASIC: LevelInfo(
        label="Basic",
        good_for="typos, renames, a one-line fix, a tiny refactor",
        description="One agent writes task.md, tests and code in one pass; the checks decide.",
    ),
    Level.REVIEWER: LevelInfo(
        label="Reviewer",
        good_for="small bug fixes you'll skim",
        description="One agent does the work, then a second agent reviews the change once.",
    ),
    Level.TESTS: LevelInfo(
        label="Tests",
        good_for="a contained change with clear behaviour",
        description="Spec, tests written first and shown to fail, code, then one review.",
    ),
    Level.ADVERSARIAL: LevelInfo(
        label="Adversarial",
        good_for="a feature with real logic",
        description="Spec, then tests and code each checked by a critic, with the arbiter "
        "settling disputes. No reviewers at the end.",
    ),
    Level.FULL: LevelInfo(
        label="Full",
        good_for="risky, security-relevant or user-facing work",
        description="The whole pipeline: spec, tests and code with critics, standards and "
        "security reviewers, the arbiter, and the security scan.",
    ),
}


def level_number(level: Level) -> int:
    """1 (basic) to 5 (full): the slider position."""
    return LEVEL_ORDER.index(level) + 1


def parse_level(value: str | int) -> Level:
    """A level from its name or its number (1 to 5). Raises ValueError otherwise."""
    if isinstance(value, int) and not isinstance(value, bool):
        if 1 <= value <= len(LEVEL_ORDER):
            return LEVEL_ORDER[value - 1]
    elif isinstance(value, str):
        text = value.strip().lower()
        if text.isdigit():
            return parse_level(int(text))
        try:
            return Level(text)
        except ValueError:
            pass
    names = ", ".join(level.value for level in LEVEL_ORDER)
    raise ValueError(f"unknown level {value!r}: use one of {names}, or 1 to {len(LEVEL_ORDER)}")


def roles_for(level: Level) -> list[Role]:
    """The roles a run at ``level`` can call, in pipeline order."""
    shape = SHAPES[level]
    roles: list[Role] = []
    if shape.solo:
        roles.append(Role.SOLO)
    else:
        roles += [Role.SPEC_WRITER, Role.TEST_AUTHOR]
        if shape.critics:
            roles.append(Role.TEST_CRITIC)
        roles.append(Role.CODER)
    if shape.critics or shape.review == "single":
        roles.append(Role.CODE_CRITIC)
    if shape.review == "full":
        roles += [Role.STANDARDS_REVIEWER, Role.SECURITY_REVIEWER]
    if shape.arbiter:
        roles.append(Role.ARBITER)
    return roles


def steps_for(level: Level) -> list[Stage]:
    """The timeline steps of a run at ``level``."""
    shape = SHAPES[level]
    steps = [Stage.SOLO] if shape.solo else [Stage.SPEC, Stage.TESTS, Stage.CODE]
    if shape.review != "none":
        steps.append(Stage.REVIEW)
    if shape.arbiter:
        steps.append(Stage.ARBITER)
    return [*steps, Stage.FINAL_GATES]


def scan_on(state: RunState) -> bool:
    """Whether the security scan runs in this run: its own setting, else its level's default
    (a run recorded before levels existed is full, so on)."""
    if state.security_scan is not None:
        return state.security_scan
    return SHAPES[state.level].security_scan


def level_tag(state: RunState) -> str:
    """Short words for the run's level, e.g. "adversarial" or "full, no scan", with the scan
    only when it isn't the level's default. Empty for full with the scan on (the default), so
    output for those runs reads as it did before levels existed."""
    scan = scan_on(state)
    if state.level is Level.FULL and scan:
        return ""
    if scan == SHAPES[state.level].security_scan:
        return state.level.value
    return f"{state.level.value}, {'scan' if scan else 'no scan'}"


def dropped_safeguards(level: Level, security_scan: bool) -> list[str]:
    """What a run at ``level`` doesn't do that a full run with the scan does, in plain words.
    Empty for full with the scan on."""
    shape = SHAPES[level]
    dropped: list[str] = []
    if shape.solo:
        dropped.append(
            "One agent writes task.md, tests and code, so tests aren't written before the "
            "code and shown to fail"
        )
    if not shape.critics:
        if shape.review == "none":
            dropped.append("No second agent checks the change")
        elif shape.solo:
            dropped.append("No critic while the work is done: one review at the end")
        else:
            dropped.append("No critics in the test and code steps: one review at the end")
    if shape.review != "full":
        dropped += ["No standards review", "No security review"]
    if not shape.arbiter and (shape.critics or shape.review != "none"):
        dropped.append("No arbiter: open findings are always sent back to be fixed")
    if not security_scan:
        dropped.append("No security scan")
    return dropped


def max_agent_calls(level: Level, limits: Limits) -> tuple[int, int]:
    """(happy path, worst case) agent calls between two person decisions at ``level``.

    The worst case counts one JSON retry per critic, reviewer and arbiter call, the code
    stage's one test-fix pass, and the arbiter and final author pass of each stage's settling.
    tests/test_levels.py checks it against a run that fails every round.
    """
    shape = SHAPES[level]
    if shape.solo:
        happy = worst = 1
    else:
        per_round = 3 if shape.critics else 1  # the author, plus a critic and its retry
        settle = (2 if shape.arbiter else 0) + 1  # arbiter and its retry, final author pass
        happy = 1 + 2 * (2 if shape.critics else 1)
        worst = 1 + (limits.max_rounds_tests * per_round + settle)
        worst += limits.max_rounds_code * per_round + settle
        if shape.critics:
            worst += 1  # test-fix pass: needs a code critic's test-defect finding
    if shape.review != "none":
        reviewers = 2 if shape.review == "full" else 1
        settle = (2 if shape.arbiter else 0) + 1
        happy += reviewers
        worst += 2 * reviewers + settle
    return happy, worst
