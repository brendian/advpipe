"""Markdown to HTML for agent-written text (reports, task.md, author replies).

Agent output is untrusted: raw HTML in it is escaped, never passed through, and markdown-it's
link validation drops ``javascript:``, ``vbscript:``, ``file:`` and ``data:`` links. The page's
Content-Security-Policy is a second line of defence (no inline script, images from 'self').
"""

from __future__ import annotations

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
