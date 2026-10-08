"""Markdown to HTML for agent-written text (reports, task.md, author replies).

Agent output is untrusted: raw HTML in it is escaped, never passed through, and markdown-it's
link validation drops ``javascript:``, ``vbscript:``, ``file:`` and ``data:`` links. The page's
Content-Security-Policy is a second line of defence (no inline script, images from 'self').
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.rules_core import StateCore
from markupsafe import Markup

_ALIGN = {f"text-align:{side}": f"align-{side}" for side in ("left", "center", "right")}


def _align_classes(state: StateCore) -> None:
    """Table column alignment as a class, not a style attribute: the Content-Security-Policy
    (style-src 'self') blocks inline styles."""
    for token in state.tokens:
        if token.type in ("th_open", "td_open"):
            style = token.attrs.pop("style", None)
            if isinstance(style, str) and style in _ALIGN:
                token.attrSet("class", _ALIGN[style])


_md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")
_md.core.ruler.push("align_classes", _align_classes)


def markdown(text: str) -> Markup:
    """Render ``text`` as HTML that is safe to put in a page."""
    return Markup(_md.render(text))  # safe: with html=False, markdown-it escapes raw HTML


# --------------------------------------------------------------------------- task.md

# The two task.md sections the page highlights (see prompts/spec-writer.md for the layout).
_TASK_SECTIONS = {"acceptance criteria": "ac", "open questions": "oq"}
# Same prefix forms as stages.blocking_open_questions: "BLOCKING:", "**BLOCKING:**", ...
_BLOCKING = re.compile(r"(?:\*\*)?BLOCKING(?::\*\*|\*\*:|:)", re.IGNORECASE)


@dataclass(frozen=True)
class TaskMd:
    html: Markup
    criteria: int  # top-level items under "## Acceptance criteria"
    questions: int  # top-level items under "## Open questions", not counting "None"
    blocking: int  # those marked BLOCKING: the pipeline stops for a person on these
    sections: frozenset[str]  # which of "ac" and "oq" task.md has


def _item_text(tokens: list[Any], i: int) -> str:
    """The first paragraph of the list item opened at ``tokens[i]``, as written."""
    if i + 2 < len(tokens) and tokens[i + 1].type == "paragraph_open":
        return str(tokens[i + 2].content)
    return ""


def _mark_task_sections(state: StateCore) -> None:
    """Classes on the acceptance-criteria and open-question headings and items, and counts
    of them in ``env["task"]``."""
    counts: dict[str, Any] = state.env.setdefault("task", {})
    counts.update(ac=0, oq=0, blocking=0, found=set())
    section: str | None = None
    depth = 0
    tokens = state.tokens
    for i, token in enumerate(tokens):
        if token.type == "heading_open" and token.tag in ("h1", "h2"):
            title = tokens[i + 1].content.strip().lower() if token.tag == "h2" else ""
            section = _TASK_SECTIONS.get(title)
            depth = 0
            if section:
                token.attrJoin("class", f"task-{section}")
                counts["found"].add(section)
        elif section is None:
            continue
        elif token.type in ("bullet_list_open", "ordered_list_open"):
            depth += 1
        elif token.type in ("bullet_list_close", "ordered_list_close"):
            depth -= 1
        elif token.type == "list_item_open" and depth == 1:
            text = _item_text(tokens, i).strip()
            if text.lower().rstrip(".") == "none":
                continue
            counts[section] += 1
            token.attrJoin("class", section)
            if section == "oq" and _BLOCKING.match(text):
                counts["blocking"] += 1
                token.attrJoin("class", "blocking")


_task_md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")
_task_md.core.ruler.push("align_classes", _align_classes)
_task_md.core.ruler.push("task_sections", _mark_task_sections)


def task_markdown(text: str) -> TaskMd:
    """Render task.md like ``markdown``, marking its acceptance criteria and open questions."""
    env: dict[str, Any] = {}
    html = Markup(_task_md.render(text, env))  # safe: html=False, as in markdown()
    counts = env["task"]
    return TaskMd(
        html=html,
        criteria=counts["ac"],
        questions=counts["oq"],
        blocking=counts["blocking"],
        sections=frozenset(counts["found"]),
    )
