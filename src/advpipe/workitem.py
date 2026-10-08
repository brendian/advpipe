"""Work-item files: a markdown body with an optional front-matter block of defaults.

```markdown
---
name: s03-stock
config: pipeline.server.toml
---
Add stock level changes with a change history...
```

Only simple ``key: value`` lines are parsed (no YAML). Unknown or repeated keys are an error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

FENCE = "---"
# Front-matter keys and what they set. Command-line options override them.
KEYS = {
    "name": "branch name: advpipe/<name>",
    "config": "pipeline config file, relative to the repo root",
}
_LINE = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)")


class WorkItemError(ValueError):
    pass


@dataclass(frozen=True)
class WorkItem:
    body: str
    name: str | None = None
    config: str | None = None


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
            fields[key] = value
        body_lines = lines[end + 1 :]
    body = "\n".join(body_lines).strip()
    if not body:
        raise WorkItemError(f"{source}: the work item is empty")
    return WorkItem(body=body, name=fields.get("name"), config=fields.get("config"))


def load_work_item(path: Path) -> WorkItem:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise WorkItemError(f"can't read work item {path}: {e}") from e
    return parse_work_item(text, str(path))
