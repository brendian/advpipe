"""The web UI (U6): Help page, error pages, empty states, themes, keyboard and layout."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from test_ui import anon, csrf_of, fake_run, logged_in  # noqa: E402

from advpipe.config import Config  # noqa: E402
from advpipe.models import Stage, Status  # noqa: E402
from advpipe.runner import Role  # noqa: E402
from advpipe.ui.app import HERE, NAV  # noqa: E402
from advpipe.ui.detail import STEP_STATES  # noqa: E402
from advpipe.ui.routes.help import ROLE_JOBS, STATUS_NEXT  # noqa: E402
from advpipe.ui.runs import STATUSES  # noqa: E402

PAGES = ("/", "/items", "/items/new", "/runs/new", "/help")

# --------------------------------------------------------------------------- help page


def test_help_needs_the_token(target_repo: Path) -> None:
    assert anon(target_repo).get("/help").status_code == 401


def test_help_is_in_the_nav(target_repo: Path) -> None:
    assert ("Help", "/help") in NAV
    page = logged_in(target_repo).get("/help").text
    assert '<a href="/help" aria-current="page">Help</a>' in page
    assert '<a href="/" aria-current' not in page


def test_help_explains_every_step_status_and_role(target_repo: Path) -> None:
    r = logged_in(target_repo).get("/help")
    assert r.status_code == 200
    page = r.text
    for anchor in ("steps", "agents", "checks", "statuses", "pages", "safety", "words"):
        assert f'id="{anchor}"' in page and f'href="#{anchor}"' in page
    for step in ("SPEC", "TESTS", "CODE", "REVIEW", "ARBITER", "FINAL CHECKS"):
        assert f"<strong>{step}.</strong>" in page
    for label, _ in STEP_STATES.values():
        assert f"<dt>{label}</dt>" in page
    for key, info in STATUSES.items():
        assert f'class="chip chip-{key}"' in page
        assert info.label in page
    for role in Role:
        assert f"<strong>{role.value}</strong>" in page
    for name in ("test", "types", "lint", "security"):
        assert f"<code>{name}</code>:" in page
    assert "never merges or pushes" in page and "127.0.0.1" in page


def test_help_shows_the_real_defaults(target_repo: Path) -> None:
    page = logged_in(target_repo).get("/help").text
    defaults = Config()
    assert f"Up to {defaults.limits.max_rounds_tests} rounds" in page
    assert f"Up to\n      {defaults.limits.max_rounds_code} rounds" in page
    assert f"${defaults.limits.budget_usd_per_task:.2f}" in page
    assert f'<code class="nowrap">{defaults.models.author}</code>' in page
    assert f'<code class="nowrap">{defaults.models.critic}</code>' in page
    # Writers and read-only roles are labelled from the tool lists the runner uses.
    rows = re.findall(r"<strong>([a-z-]+)</strong><div class=\"sub muted\">([^<]+)</div>", page)
    assert dict(rows) == {
        "spec-writer": "changes files",
        "test-author": "changes files",
        "coder": "changes files",
        "test-critic": "read-only",
        "code-critic": "read-only",
        "standards-reviewer": "read-only",
        "security-reviewer": "read-only",
        "arbiter": "read-only",
    }


def test_help_tables_cover_everything() -> None:
    assert set(STATUS_NEXT) == set(STATUSES)
    assert set(ROLE_JOBS) == set(Role)


def test_help_is_get_only(target_repo: Path) -> None:
    client = logged_in(target_repo)
    assert client.post("/help").status_code == 403  # no CSRF token
    csrf = csrf_of(client.get("/help").text)
    r = client.post("/help", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 405 and "Not allowed" in r.text


# --------------------------------------------------------------------------- error pages


def test_unknown_page_is_an_html_page_with_the_nav(target_repo: Path) -> None:
    r = logged_in(target_repo).get("/no/such/page")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("text/html")
    assert "Page not found" in r.text and '<nav aria-label="Main">' in r.text
    assert "content-security-policy" in r.headers


def test_unknown_page_without_token_is_still_401(target_repo: Path) -> None:
    r = anon(target_repo).get("/no/such/page")
    assert r.status_code == 401 and "Page not found" not in r.text


def test_error_page_does_not_echo_the_url(target_repo: Path) -> None:
    r = logged_in(target_repo).get("/%3Cscript%3Ealert(1)%3C/script%3E")
    assert r.status_code == 404
    assert "<script>alert" not in r.text and "alert(1)" not in r.text


# --------------------------------------------------------------------------- empty states


def test_empty_runs_page_points_to_work_items_and_help(target_repo: Path) -> None:
    page = logged_in(target_repo).get("/").text
    assert "No runs yet" in page
    assert '<a href="/items">Write one</a>' in page
    assert '<a href="/help">how advpipe works</a>' in page


def test_run_without_events_says_why(target_repo: Path) -> None:
    fake_run(target_repo, "r-old", status=Status.COMPLETE, stage=Stage.DONE, work_item="x")
    page = logged_in(target_repo).get("/runs/r-old").text
    assert "No progress was recorded for this run" in page
    assert "The timeline and report below still show what happened" in page


def test_live_run_without_events_says_it_is_starting(target_repo: Path) -> None:
    log = fake_run(target_repo, "r-live", status=Status.RUNNING, stage=Stage.INIT, work_item="x")
    log.acquire_lock()  # this process: alive
    try:
        page = logged_in(target_repo).get("/runs/r-live").text
    finally:
        log.release_lock()
    assert "No events yet: the run is just starting" in page


# --------------------------------------------------------------------------- layout and theme


def test_every_page_has_skip_link_theme_button_and_no_inline_code(target_repo: Path) -> None:
    client = logged_in(target_repo)
    for path in PAGES:
        page = client.get(path).text
        assert '<a class="skip-link" href="#content">Skip to content</a>' in page, path
        assert '<main id="content" tabindex="-1">' in page, path
        assert 'id="theme-toggle" hidden>' in page, path
        # The theme is set before the stylesheet's colours are used: theme.js isn't deferred.
        head = page.split("</head>")[0]
        assert '<script src="/static/theme.js"></script>' in head, path
        # The CSP forbids inline script and style, so pages must not rely on them.
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", page), path
        assert " style=" not in page and "<style" not in page, path


def test_theme_script_and_toggle_are_served(target_repo: Path) -> None:
    client = logged_in(target_repo)
    theme = client.get("/static/theme.js")
    assert theme.status_code == 200
    assert 'setAttribute("data-theme"' in theme.text and "advpipe-theme" in theme.text
    app_js = client.get("/static/app.js").text
    assert 'getElementById("theme-toggle")' in app_js


def css() -> str:
    return (HERE / "static" / "app.css").read_text()


def test_every_colour_has_a_light_and_a_dark_value() -> None:
    root = css().split(":root {", 1)[1].split("}", 1)[0]
    colours = re.findall(r"(--[a-z-]+):\s*([^;]+);", root)
    assert len(colours) >= 10
    for name, value in colours:
        assert re.fullmatch(r"light-dark\(#[0-9a-f]{6}, #[0-9a-f]{6}\)", value), name
    assert ':root[data-theme="light"] { color-scheme: light; }' in css()
    assert ':root[data-theme="dark"] { color-scheme: dark; }' in css()


def test_buttons_use_theme_colours_not_fixed_white() -> None:
    assert "#ffffff;" not in css().split("* { box-sizing")[1]


def test_keyboard_focus_is_always_visible() -> None:
    text = css()
    assert ":focus-visible { outline: 2px solid var(--accent)" in text
    assert "outline: none" not in text.replace("main:focus { outline: none; }", "")
    assert ".skip-link:focus" in text


def test_layout_adapts_to_tablet_width() -> None:
    text = css()
    assert "@media (max-width: 900px)" in text
    tablet = text.split("@media (max-width: 900px)", 1)[1]
    assert ".items-table .actions" in tablet and "white-space: normal" in tablet
