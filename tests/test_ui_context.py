"""The web UI (U3): the run's "Spec & context" tab, and the task.md renderer."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from conftest import git  # noqa: E402
from fakes import TASK_MD, TASK_MD_BLOCKING, happy_scripts, writes  # noqa: E402
from test_ui import anon, fake_run, logged_in  # noqa: E402
from test_ui_detail import EVIL, no_raw_html, orchestrate  # noqa: E402

from advpipe.config import Config, Paths  # noqa: E402
from advpipe.models import RunState, Stage, Status  # noqa: E402
from advpipe.runlog import RunLog  # noqa: E402
from advpipe.runner import Role  # noqa: E402
from advpipe.ui.context import SEEN_BY, _standards  # noqa: E402
from advpipe.ui.render import task_markdown  # noqa: E402

BLOCKS = ("ctx-work-item", "ctx-task", "ctx-config", "ctx-standards", "ctx-prompts")


def block(page: str, block_id: str) -> str:
    """The HTML of one <section> of the page."""
    start = page.index(f'id="{block_id}"')
    return page[start : page.index("</section>", start)]


def seen(text: str) -> str:
    """``text`` as it appears in the page (autoescaped)."""
    return text.replace("'", "&#39;")


# --------------------------------------------------------------------------- a complete run


async def test_every_block_from_a_complete_run(config: Config, target_repo: Path) -> None:
    await orchestrate(config, target_repo, "r-ok", happy_scripts())
    client = logged_in(target_repo)
    page = client.get("/runs/r-ok/context").text

    # Both tabs link to each other; this one is current.
    assert '<a href="/runs/r-ok/context" aria-current="page">Spec &amp; context</a>' in page
    assert 'href="/runs/r-ok/context"' in client.get("/runs/r-ok").text
    assert '<a href="/runs/r-ok" aria-current="page">Progress</a>' in client.get("/runs/r-ok").text
    assert "✔ complete" in page

    for block_id in BLOCKS:
        assert f'id="{block_id}"' in page, block_id
    for key, text in SEEN_BY.items():
        assert seen(text) in page, key

    work = block(page, "ctx-work-item")
    assert "<p>add clamp</p>" in work

    task = block(page, "ctx-task")
    assert '<span class="tag">3 acceptance criteria</span>' in task
    assert '<span class="tag">0 open questions</span>' in task and "blocking" not in task.lower()
    assert '<h2 class="task-ac">Acceptance criteria</h2>' in task
    assert '<li class="ac">[ ] AC1: <code>from mathutils import clamp</code> works.</li>' in task
    assert '<h2 class="task-oq">Open questions</h2>' in task

    cfg = block(page, "ctx-config")
    assert "<dd>$5.00</dd>" in cfg and "tests ≤ 2, code ≤ 3" in cfg
    assert "-m pytest -q -p no:cacheprovider</code>" in cfg
    assert '<td>lint</td><td><span class="muted">off (no command)</span>' in cfg
    assert "<td>security</td>" in cfg
    assert "&#34;budget_usd_per_task&#34;: 5.0" in cfg  # the raw config.json, escaped

    standards = block(page, "ctx-standards")
    assert "<code>CLAUDE.md</code>" in standards
    assert "on the run&#39;s branch advpipe/add-clamp" in standards
    assert "<h1>mathutils</h1>" in standards

    prompts = block(page, "ctx-prompts")
    for role in Role:
        assert f'id="role-{role.value}"' in prompts, role
    assert prompts.count("claude-opus-5-5") == 4  # spec writer, test author, coder, arbiter
    assert prompts.count("claude-sonnet-5-5") == 4  # the critics and reviewers
    assert prompts.count('<span class="tag">read-only</span>') == 5
    assert "You are the <strong>spec writer</strong>" in prompts
    assert "<strong>Gets in its prompt:</strong> the work item." in prompts


async def test_blocking_questions_are_highlighted(config: Config, target_repo: Path) -> None:
    scripts = happy_scripts()
    scripts[Role.SPEC_WRITER] = [writes({"task.md": TASK_MD_BLOCKING}, "one question")]
    await orchestrate(config, target_repo, "r-q", scripts)
    task = block(logged_in(target_repo).get("/runs/r-q/context").text, "ctx-task")

    assert '<span class="tag">1 open question</span>' in task
    assert '<span class="tag tag-blocking">1 blocking</span>' in task
    assert "stop the pipeline after the spec" in task
    assert '<li class="oq blocking">BLOCKING: should lo &gt; hi swap or raise?</li>' in task


async def test_standards_doc_falls_back_after_branch_is_gone(
    config: Config, target_repo: Path
) -> None:
    state = await orchestrate(config, target_repo, "r-ok", happy_scripts())
    git(target_repo, "branch", "-D", state.branch)
    standards = block(logged_in(target_repo).get("/runs/r-ok/context").text, "ctx-standards")
    assert f"at the commit the run started from ({state.base_commit[:12]})" in standards
    assert "<h1>mathutils</h1>" in standards


# --------------------------------------------------------------------------- empty states


def test_missing_files_show_clear_empty_states(target_repo: Path) -> None:
    log = fake_run(target_repo, "r-empty", status=Status.ERROR, stage=Stage.SPEC, work_item="  ")
    (log.root / "config.json").unlink()
    (target_repo / "CLAUDE.md").unlink()
    r = logged_in(target_repo).get("/runs/r-empty/context")
    assert r.status_code == 200
    page = r.text

    assert "This run's work item is empty" in block(page, "ctx-work-item")
    assert "it stopped before the spec writer wrote one" in block(page, "ctx-task")
    assert "This run has no <code>config.json</code>" in block(page, "ctx-config")
    standards = block(page, "ctx-standards")
    assert "There's no <code>CLAUDE.md</code> in the repo" in standards
    prompts = block(page, "ctx-prompts")
    assert "Models shown are advpipe's defaults" in prompts
    assert prompts.count('id="role-') == len(Role)


def test_task_md_not_written_yet_while_live(target_repo: Path) -> None:
    log = fake_run(target_repo, "r-live", status=Status.RUNNING, stage=Stage.SPEC, work_item="x")
    log.acquire_lock(os.getpid())  # this test process "drives" the run
    page = logged_in(target_repo).get("/runs/r-live/context").text
    assert "the spec writer is still working on it" in block(page, "ctx-task")
    assert "This run is still going" in page


def test_task_md_without_criteria_warns(target_repo: Path) -> None:
    log = fake_run(target_repo, "r-noac", status=Status.ERROR, stage=Stage.TESTS, work_item="x")
    log.write_text("task.md", "# Task: x\n\n## Goal\nSomething.\n")
    task = block(logged_in(target_repo).get("/runs/r-noac/context").text, "ctx-task")
    assert 'has no "Acceptance criteria" section' in task
    assert '<span class="tag">0 acceptance criteria</span>' in task


def test_unreadable_config_is_shown_raw(target_repo: Path) -> None:
    log = fake_run(target_repo, "r-badcfg", status=Status.ERROR, stage=Stage.SPEC, work_item="x")
    log.write_text("config.json", '{"limits": {"nope": 1}, "x": "' + EVIL.replace('"', "'") + '"}')
    cfg = block(logged_in(target_repo).get("/runs/r-badcfg/context").text, "ctx-config")
    assert "<code>config.json</code> couldn't be read:" in cfg
    assert "&#34;nope&#34;: 1" in cfg
    no_raw_html(cfg)


# --------------------------------------------------------------------------- safety


def test_everything_from_files_is_escaped(target_repo: Path) -> None:
    (target_repo / "CLAUDE.md").write_text(f"# Rules\n\n{EVIL}\n")
    log = fake_run(
        target_repo,
        "r-evil",
        status=Status.NEEDS_HUMAN,
        stage=Stage.SPEC,
        work_item=f"do it {EVIL}",
        work_item_file=f"/items/{EVIL}.md",
    )
    log.write_text("task.md", TASK_MD.replace("Add clamp(x, lo, hi).", EVIL))
    page = logged_in(target_repo).get("/runs/r-evil/context").text
    no_raw_html(page)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in block(page, "ctx-work-item")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in block(page, "ctx-task")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in block(page, "ctx-standards")


def test_needs_token_and_a_real_run(target_repo: Path) -> None:
    fake_run(target_repo, "r-1", status=Status.COMPLETE, stage=Stage.DONE, work_item="x")
    assert anon(target_repo).get("/runs/r-1/context").status_code == 401
    client = logged_in(target_repo)
    assert client.get("/runs/r-1/context").status_code == 200
    assert client.post("/runs/r-1/context").status_code in (403, 405)
    for path in ("/runs/nope/context", "/runs/%2e%2e/context", "/runs/.hidden/context"):
        assert client.get(path).status_code == 404, path
    assert client.get("/runs/..%2f..%2fetc/context").status_code == 404


def test_standards_doc_never_leaves_the_repo(target_repo: Path, tmp_path: Path) -> None:
    (tmp_path / "secret.md").write_text("TOP SECRET")
    state = RunState(run_id="r", repo=str(target_repo), work_item="x")
    for bad in ("../secret.md", str(tmp_path / "secret.md"), "-p"):
        view = _standards(target_repo, state, Config(paths=Paths(standards_doc=bad)))
        assert view.html is None and "isn't inside the repo" in view.source, bad


def test_standards_doc_rejects_option_like_revisions(target_repo: Path, tmp_path: Path) -> None:
    """Branch and commit come from run.json; one that looks like a git option is never used."""
    out = tmp_path / "pwned"
    state = RunState(
        run_id="r", repo=str(target_repo), work_item="x", branch=f"--output={out}", base_commit="-p"
    )
    view = _standards(target_repo, state, Config())
    assert not out.exists()
    assert view.source == "in the repo now" and view.html is not None


# --------------------------------------------------------------------------- task.md rendering


def test_task_markdown_counts_and_classes() -> None:
    text = """# Task: t

## Acceptance criteria
- AC1: one
  - a detail, not a criterion
- AC2: two

```
## Open questions
- BLOCKING: inside a code block, not a question
```

## Open questions
- **BLOCKING:** first
- BLOCKING: second
- a default was chosen
- None
"""
    t = task_markdown(text)
    assert (t.criteria, t.questions, t.blocking) == (2, 3, 2)
    assert t.sections == {"ac", "oq"}
    assert t.html.count('<li class="ac">') == 2
    assert t.html.count('class="oq blocking"') == 2
    assert '<li class="oq">a default was chosen</li>' in t.html
    assert "<li>None</li>" in t.html


def test_task_markdown_escapes_html() -> None:
    t = task_markdown(f"## Acceptance criteria\n- AC1: {EVIL}\n")
    assert t.criteria == 1
    no_raw_html(t.html)


def test_runlog_untouched_by_the_page(target_repo: Path) -> None:
    """The tab only reads: the run directory is the same before and after."""
    log = fake_run(target_repo, "r-ro", status=Status.COMPLETE, stage=Stage.DONE, work_item="x")
    before = {p: p.read_bytes() for p in log.root.rglob("*") if p.is_file()}
    logged_in(target_repo).get("/runs/r-ro/context")
    root = RunLog.for_run(target_repo, "r-ro").root
    after = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert before == after
