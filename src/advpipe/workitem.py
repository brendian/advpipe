"""Work-item files: a markdown body with an optional front-matter block of defaults.

```markdown
---
name: s03-stock
config: pipeline.server.toml
level: adversarial
---
Add stock level changes with a change history...
```

Only simple ``key: value`` lines are parsed (no YAML). Unknown or repeated keys are an error.
``format_work_item`` writes the same layout back (the web UI's editor uses it).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from advpipe.levels import parse_level

FENCE = "---"
# Front-matter keys and what they set. Command-line options override them.
KEYS = {
    "name": "branch name: advpipe/<name>",
    "config": "pipeline config file, relative to the repo root",
    "level": "how much checking: basic, reviewer, tests, adversarial, full (or 1-5)",
    "security_scan": "run the security scan: yes or no (default: on at level full only)",
    "scan_failures": "below level full, what a failing security scan means: ask, ignore, block",
    "bypass_questions": "yes: the agent answers blocking questions itself, without asking you",
}
# The keys that set how the run goes (the rest say where it starts from).
RUN_KEYS = ("level", "security_scan", "scan_failures", "bypass_questions")
SWITCH = {"yes": True, "true": True, "no": False, "false": False}
SCAN_FAILURES = ("ask", "ignore", "block")
_LINE = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)")


class WorkItemError(ValueError):
    pass


def switch(value: str) -> bool:
    """A front-matter yes/no value. Raises ValueError otherwise."""
    try:
        return SWITCH[value.strip().lower()]
    except KeyError:
        raise ValueError(f"expected yes or no, got {value!r}") from None


def _scan_failures(value: str) -> str:
    if value not in SCAN_FAILURES:
        raise ValueError(f"expected one of {', '.join(SCAN_FAILURES)}, got {value!r}")
    return value


# Checks for the keys whose values are more than text. Each raises ValueError.
_CHECKS: dict[str, Callable[[str], object]] = {
    "level": parse_level,
    "security_scan": switch,
    "scan_failures": _scan_failures,
    "bypass_questions": switch,
}


@dataclass(frozen=True)
class WorkItem:
    body: str
    name: str | None = None
    config: str | None = None
    # As written in the file (checked when parsed): parse_level() and switch() read them.
    level: str | None = None
    security_scan: str | None = None
    scan_failures: str | None = None
    bypass_questions: str | None = None


def parse_work_item(text: str, source: str = "work item") -> WorkItem:
    """Split optional front matter from the body. Raises WorkItemError on any problem."""
    lines = text.splitlines()
    fields: dict[str, str] = {}
    body_lines = lines
    if lines and lines[0].strip() == FENCE:
        try:
            end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == FENCE)
        except StopIteration:
            raise WorkItemError(
                f"{source}: front matter starts with '---' but never ends"
            ) from None
        for n, raw in enumerate(lines[1:end], 2):
            if not raw.strip():
                continue
            match = _LINE.fullmatch(raw.strip())
            if not match:
                raise WorkItemError(f"{source}:{n}: expected 'key: value', got {raw.strip()!r}")
            key, value = match.group(1), match.group(2).strip()
            if key not in KEYS:
                known = ", ".join(KEYS)
                raise WorkItemError(f"{source}:{n}: unknown key {key!r} (known keys: {known})")
            if key in fields:
                raise WorkItemError(f"{source}:{n}: {key!r} is given twice")
            if not value:
                raise WorkItemError(f"{source}:{n}: {key!r} has no value")
            if key in _CHECKS:
                try:
                    _CHECKS[key](value)
                except ValueError as e:
                    raise WorkItemError(f"{source}:{n}: {key!r}: {e}") from None
            fields[key] = value
        body_lines = lines[end + 1 :]
    body = "\n".join(body_lines).strip()
    if not body:
        raise WorkItemError(f"{source}: the work item is empty")
    return WorkItem(body=body, **{key: fields.get(key) for key in KEYS})


def load_work_item(path: Path) -> WorkItem:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise WorkItemError(f"can't read work item {path}: {e}") from e
    return parse_work_item(text, str(path))


def format_work_item(item: WorkItem) -> str:
    """The file text for ``item``: front matter for the fields that are set, then the body.
    ``parse_work_item`` reads it back as an equal ``WorkItem`` (body stripped). Raises
    WorkItemError if it couldn't: an empty body, or a value that's blank or not one line."""
    body = item.body.strip()
    if not body:
        raise WorkItemError("the work item is empty")
    fields: list[str] = []
    for key in KEYS:
        value = getattr(item, key)
        if value is None:
            continue
        if not value.strip() or value != value.strip() or "\n" in value or "\r" in value:
            raise WorkItemError(f"{key!r} must be one line of text with no spaces around it")
        fields.append(f"{key}: {value}")
    # A body whose first line is '---' would read as front matter: give it an empty block.
    if fields or body.splitlines()[0].strip() == FENCE:
        return "\n".join([FENCE, *fields, FENCE, body]) + "\n"
    return body + "\n"


def salvage_work_item(text: str) -> tuple[dict[str, str], str]:
    """Best-effort split of a file ``parse_work_item`` rejects, so an editor can open it to fix
    it: the known front-matter keys it can read (first value wins), and the body. Front matter
    that never closes is left in the body."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != FENCE:
        return {}, text.strip()
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == FENCE), None)
    if end is None:
        return {}, text.strip()
    fields: dict[str, str] = {}
    for raw in lines[1:end]:
        match = _LINE.fullmatch(raw.strip())
        if match and match.group(1) in KEYS and match.group(2).strip():
            fields.setdefault(match.group(1), match.group(2).strip())
    return fields, "\n".join(lines[end + 1 :]).strip()
