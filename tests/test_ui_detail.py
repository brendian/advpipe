"""The web UI (U2): run detail page, timeline, report, next steps, and the live event stream."""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fakes import (  # noqa: E402
    IMPL_OK,
    IMPL_WRONG,
    TASK_MD_BLOCKING,
    FakeAgentRunner,
    finding,
    happy_scripts,
    rulings,
    verdict_fail,
    writes,
)
from test_ui import anon, fake_run, logged_in  # noqa: E402

from advpipe.config import Config  # noqa: E402
from advpipe.models import Finding, RunState, Stage, Status  # noqa: E402
from advpipe.orchestrator import Orchestrator  # noqa: E402
from advpipe.runlog import EVENTS_FILE, RunLog  # noqa: E402
from advpipe.runner import Role  # noqa: E402
from advpipe.ui import detail as detail_module  # noqa: E402
from advpipe.ui import live  # noqa: E402
from advpipe.ui.detail import build_timeline, next_steps  # noqa: E402

EVIL = '<script>alert(1)</script><img src=x onerror="x()">'


async def orchestrate(
    config: Config, repo: Path, run_id: str, scripts: dict[Role, Any], item: str = "add clamp"
) -> RunState:
    return await Orchestrator(config, FakeAgentRunner(scripts), repo, item, run_id).run()


def steps_of(page: str) -> dict[str, str]:
    """Timeline strip: {step label: its state label}."""
    strip = page[page.index('<ol class="timeline">') :]
    strip = strip[: strip.index("</ol>")]
    found = re.findall(
        r'<span class="step-label">([^<]+)</span>\s*<span class="step-state">([^<]+)</span>', strip
    )
    return dict(found)


def no_raw_html(page: str) -> None:
    assert "<script>alert" not in page
    assert "<img src=x" not in page
    assert 'onerror="x()"' not in page


# --------------------------------------------------------------------------- pages from real runs


async def test_complete_run_page(config: Config, target_repo: Path) -> None:
    await orchestrate(config, target_repo, "r-ok", happy_scripts())
    client = logged_in(target_repo)

    # The runs list links to the page.
    assert 'href="/runs/r-ok"' in client.get("/").text
    page = client.get("/runs/r-ok").text

    assert "<h1>add clamp</h1>" in page
    assert "✔ complete" in page and "ready for you to review" in page
    assert '<code id="branch-name">advpipe/add-clamp</code>' in page
    assert steps_of(page) == {
        "SPEC": "done",
        "TESTS": "done",
        "CODE": "done",
        "REVIEW": "done",
        "ARBITER": "not needed",
        "FINAL CHECKS": "done",
    }
    # Round panels: author reply, the diff the critic judged, checks, verdicts.
    assert "What the coder replied" in page and "AUTHOR-REASONING code" in page
    clamp_line = "+def clamp(x: float, lo: float, hi: float) -&gt; float:"
    assert f'<span class="d-add">{clamp_line}</span>' in page
    assert "The diff the critic judged" in page and "The diff the reviewers judged" in page
    assert "At this stage the new tests are meant to fail" in page
    assert re.search(r'tag-fail">fail \(exit \d\)</span> test', page)  # red tests, stage 2
    assert 'Code critic <span class="tag tag-pass">PASS</span>' in page
    assert "Standards reviewer" in page and "Security reviewer" in page
    assert "<code>task.md</code>: the spec" in page and "<h2>Acceptance criteria</h2>" in page
    # Report, rendered from markdown.
    assert "<h1>advpipe run r-ok</h1>" in page and "<strong>Status:</strong> complete" in page
    assert "<table>" in page  # the cost table
    # Next steps, with copy buttons, and nothing live.
    repo = str(target_repo)
    assert f"git -C {repo} merge advpipe/add-clamp" in page
    assert f"git -C {repo} branch -d advpipe/add-clamp" in page
    assert re.search(rf"git -C {re.escape(repo)} log -p --reverse [0-9a-f]{{12}}\.\.advpipe/", page)
    assert 'data-copy="cmd-1"' in page and 'data-copy="branch-name"' in page
    assert "sse-connect" not in page and "hx-trigger" not in page.split('id="run-head"')[1][:200]
    # The log so far, from events.jsonl.
    assert "== SPEC" in page and "coder done in" in page
    assert '<script src="/static/app.js" defer></script>' in page


async def test_needs_human_spec_question(config: Config, target_repo: Path) -> None:
    scripts = happy_scripts()
    scripts[Role.SPEC_WRITER] = [writes({"task.md": TASK_MD_BLOCKING}, "two questions")]
    await orchestrate(config, target_repo, "r-q", scripts)
    page = logged_in(target_repo).get("/runs/r-q").text

    assert "⚠ needs you" in page
    steps = steps_of(page)
    assert steps["SPEC"] == "stopped here"
    assert steps["TESTS"] == steps["CODE"] == steps["FINAL CHECKS"] == "not reached"
    assert "What's left open" in page and "should lo &gt; hi swap or raise?" in page
    assert '<details class="step-panel step-stopped" id="step-SPEC" open>' in page
    assert f"advpipe clean r-q --repo {target_repo}" in page
    assert "See what it did so far" in page
    assert f"git -C {target_repo} merge" not in page


async def test_arbiter_and_final_pass(config: Config, target_repo: Path) -> None:
    scripts = happy_scripts()
    scripts[Role.CODER] = [writes(IMPL_OK), "{}", "{}", writes(IMPL_WRONG, "broke it")]
    scripts[Role.CODE_CRITIC] = [
        verdict_fail(finding("F1", line=n, claim=f"problem {n}")) for n in (1, 2, 3)
    ]
    scripts[Role.ARBITER] = [rulings(**{"C3-F1": "fix"})]
    await orchestrate(config, target_repo, "r-arb", scripts)
    page = logged_in(target_repo).get("/runs/r-arb").text

    steps = steps_of(page)
    # Stopped during the arbiter's final pass, which was called from the code stage.
    assert steps["SPEC"] == steps["TESTS"] == "done"
    assert steps["CODE"] == steps["ARBITER"] == "stopped here"
    assert steps["REVIEW"] == steps["FINAL CHECKS"] == "not reached"
    assert "3 of 3 rounds" in page
    for n in (1, 2, 3):
        assert f"problem {n}" in page
    assert "<code>C3-F1</code>" in page and ">fix</span>" in page
    assert "Final fix pass (code)" in page and "broke it" in page
    assert "Checks after the fix" in page
    assert "G-test" in page.split("What's left open")[1]  # the failing gate is left open


# --------------------------------------------------------------------------- every status


@pytest.mark.parametrize(
    ("status", "stage", "label", "command"),
    [
        (Status.RUNNING, Stage.CODE, "● running", "advpipe cancel r-s"),
        (Status.COMPLETE, Stage.DONE, "✔ complete", "merge advpipe/x"),
        (Status.NEEDS_HUMAN, Stage.REVIEW, "⚠ needs you", "advpipe clean r-s"),
        (Status.BUDGET_EXCEEDED, Stage.CODE, "$ over budget", "advpipe resume r-s"),
        (Status.ERROR, Stage.TESTS, "✖ error", "advpipe resume r-s"),
    ],
)
def test_pages_render_for_every_status(
    target_repo: Path, status: Status, stage: Stage, label: str, command: str
) -> None:
    log = fake_run(
        target_repo, "r-s", status=status, stage=stage, round=2, work_item="the task",
        branch="advpipe/x", base_commit="a" * 40, cost_usd=2.5,
    )  # fmt: skip
    if status is Status.RUNNING:
        log.acquire_lock(os.getpid())  # this test process is alive: the run is live
    client = logged_in(target_repo)
    r = client.get("/runs/r-s")
    assert r.status_code == 200
    page = r.text
    assert label in page and command in page
    assert "$2.50" in page and "of $8.00" in page and "<meter" in page
    live_page = status is Status.RUNNING
    assert ('sse-connect="/runs/r-s/events?offset=0"' in page) is live_page
    assert ('hx-trigger="sse:refresh delay:300ms"' in page) is live_page
    assert ("The report is written when the run finishes" in page) is live_page
    assert (client.get("/runs/r-s/body").status_code) == 200
    if status is Status.BUDGET_EXCEEDED:
        assert "--budget 16" in page  # twice the run's $8 budget


def test_stopped_run_offers_resume(target_repo: Path) -> None:
    # "running" in run.json, but no live process: interrupted.
    fake_run(target_repo, "r-int", status=Status.RUNNING, stage=Stage.CODE, work_item="x")
    page = logged_in(target_repo).get("/runs/r-int").text
    assert "■ stopped" in page and "advpipe resume r-int" in page
    assert "sse-connect" not in page
    assert steps_of(page)["CODE"] == "stopped here"
    assert steps_of(page)["REVIEW"] == "not yet"  # resumable: still to come


def test_live_run_page_shows_current_step(target_repo: Path) -> None:
    log = fake_run(
        target_repo, "r-live", status=Status.RUNNING, stage=Stage.TESTS, round=1,
        work_item="x", commits={"spec": "abc"},
    )  # fmt: skip
    log.acquire_lock(os.getpid())
    log.append_event({"ts": "2026-10-08T04:41:02.512Z", "kind": "stage", "message": "== SPEC"})
    page = logged_in(target_repo).get("/runs/r-live").text
    steps = steps_of(page)
    assert steps["SPEC"] == "done" and steps["TESTS"] == "in progress"
    assert steps["ARBITER"] == "only if needed"
    assert "Live log" in page and "new lines appear as they happen" in page
    size = (log.root / EVENTS_FILE).stat().st_size
    assert f'sse-connect="/runs/r-live/events?offset={size}"' in page  # continues from here
    assert 'sse-swap="log" hx-swap="beforeend"' in page and 'sse-close="end"' in page


# --------------------------------------------------------------------------- the fragment


def test_body_fragment_swaps_head_and_main_out_of_band(target_repo: Path) -> None:
    log = fake_run(target_repo, "r-f", status=Status.RUNNING, stage=Stage.SPEC, work_item="x")
    log.acquire_lock(os.getpid())
    r = logged_in(target_repo).get("/runs/r-f/body")
    assert r.status_code == 200 and "<html" not in r.text
    assert r.text.lstrip().startswith('<section id="run-head"')
    assert 'hx-get="/runs/r-f/body"' in r.text
    assert '<section id="run-main" hx-swap-oob="true">' in r.text
    assert 'id="log"' not in r.text  # the log is appended to, never re-rendered


# --------------------------------------------------------------------------- escaping


async def test_agent_text_is_escaped_everywhere(config: Config, target_repo: Path) -> None:
    await orchestrate(config, target_repo, "r-x", happy_scripts())
    root = RunLog.for_run(target_repo, "r-x").root
    md_evil = f"{EVIL}\n\n[click](javascript:alert(2)) <b onmouseover=y()>bold</b>"
    (root / "stage-code" / "round-1" / "author.txt").write_text(md_evil)
    (root / "task.md").write_text(md_evil)
    (root / "report.md").write_text(md_evil)
    (root / "stage-code" / "round-1" / "diff.patch").write_text(f"+{EVIL}\n")
    critic = {"verdict": "FAIL", "findings": [finding("F1", claim=EVIL) | {"evidence": EVIL}]}
    (root / "stage-code" / "round-1" / "critic.json").write_text(json.dumps(critic))
    gates = [{"name": "test", "command": [EVIL], "returncode": 1, "output": EVIL}]
    (root / "stage-code" / "round-1" / "gates.json").write_text(json.dumps(gates))
    (root / "stage-tests" / "round-1" / "critic.json").write_text(f"not json {EVIL}")
    state = RunLog(root).read_state()
    state.open_findings = [Finding(id="X", severity="blocking", category="scope", claim=EVIL)]
    state.status = Status.NEEDS_HUMAN
    state.work_item = EVIL
    state.branch = 'advpipe/"><script>b()</script>'
    RunLog(root).write_state(state)
    RunLog(root).append_event({"kind": 'x" onclick="y', "stage": EVIL, "message": EVIL})

    client = logged_in(target_repo)
    page = client.get("/runs/r-x").text
    no_raw_html(page)
    assert "<script>b()" not in page
    assert 'href="javascript:' not in page and "<b onmouseover" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert 'class="ev ev-other"' in page  # an odd event kind can't inject attributes
    assert "unreadable" in page  # the bad critic.json, shown raw (escaped)
    no_raw_html(client.get("/runs/r-x/body").text)


def test_next_steps_quote_paths(tmp_path: Path) -> None:
    repo = tmp_path / "my repo; rm -rf ~"
    state = RunState(
        run_id="r1", work_item="x", repo=str(repo), branch="advpipe/x$(id)",
        base_commit="b" * 40, worktree=str(repo / "wt dir"),
    )  # fmt: skip
    commands = [s.command for s in next_steps(state, "complete", repo, 8.0)]
    assert f"git -C '{repo}' merge 'advpipe/x$(id)'" in commands
    assert f"git -C '{repo}' worktree remove '{repo}/wt dir'" in commands
    needs = [s.command for s in next_steps(state, "needs_human", repo, 8.0)]
    assert f"cd '{repo}/wt dir'" in needs
    assert f"advpipe clean r1 --repo '{repo}'" in needs


# --------------------------------------------------------------------------- odd and missing files


def test_unknown_and_invalid_runs_are_404(target_repo: Path) -> None:
    client = logged_in(target_repo)
    r = client.get("/runs/nope")
    assert r.status_code == 404 and "no run nope" in r.text
    for path in ("/runs/..%2f..%2fetc", "/runs/%2e%2e", "/runs/.hidden", "/runs/-x/body"):
        assert client.get(path).status_code == 404, path
    assert client.get("/runs/nope/body").status_code == 404
    assert client.get("/runs/nope/events").status_code == 404
    assert client.get("/runs/%2e%2e/events").status_code == 404


def test_detail_routes_need_the_token(target_repo: Path) -> None:
    fake_run(target_repo, "r-a", status=Status.COMPLETE, stage=Stage.DONE, work_item="x")
    client = anon(target_repo)
    for path in ("/runs/r-a", "/runs/r-a/body", "/runs/r-a/events", "/static/app.js"):
        assert client.get(path).status_code == 401, path


def test_unreadable_run_json_is_explained(target_repo: Path) -> None:
    bad = target_repo / ".advpipe" / "runs" / "r-bad"
    bad.mkdir(parents=True)
    (bad / "run.json").write_text("{")
    r = logged_in(target_repo).get("/runs/r-bad")
    assert r.status_code == 500 and "couldn&#39;t be parsed" in r.text


def test_run_without_any_round_files(target_repo: Path) -> None:
    fake_run(target_repo, "r-new", status=Status.ERROR, stage=Stage.INIT, work_item="x")
    page = logged_in(target_repo).get("/runs/r-new").text
    assert "(not created yet)" in page  # no branch
    assert "No events recorded" in page
    assert "This run has no <code>report.md</code>" in page
    assert set(steps_of(page).values()) == {"not yet", "only if needed"}


async def test_long_diff_is_cut(
    config: Config, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await orchestrate(config, target_repo, "r-d", happy_scripts())
    monkeypatch.setattr(detail_module, "MAX_DIFF_LINES", 5)
    page = logged_in(target_repo).get("/runs/r-d").text
    assert "Showing the first 5 of" in page
    assert ".advpipe/runs/r-d/stage-code/round-1/diff.patch" in page


def test_timeline_from_files_alone(target_repo: Path) -> None:
    log = fake_run(target_repo, "r-t", status=Status.RUNNING, stage=Stage.CODE, round=2,
                   work_item="x", commits={"spec": "a", "tests": "b"})  # fmt: skip
    for n in (1, 2):
        log.write_text(f"stage-code/round-{n}/author.txt", f"reply {n}")
    log.write_text("stage-code/round-10/author.txt", "reply 10")  # numeric order, not text
    timeline = build_timeline(log, log.read_state(), log.read_config(), live=True)
    code = next(s for s in timeline if s.stage is Stage.CODE)
    assert [r.title for r in code.rounds] == ["Round 1", "Round 2", "Round 10"]
    assert code.state == "current" and code.summary == "3 of 3 rounds"
    assert code.rounds[0].diff is None and code.rounds[0].critics == []


# --------------------------------------------------------------------------- the event stream


def event(n: int) -> dict[str, object]:
    return {"ts": f"2026-10-08T04:41:{n:02d}.000Z", "kind": "agent_done", "stage": "CODE",
            "round": 1, "message": f"event {n}"}  # fmt: skip


def parse_sse(text: str) -> list[dict[str, str]]:
    out = []
    for block in text.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if line.startswith(":") or ": " not in line:
                continue
            key, value = line.split(": ", 1)
            fields[key] = fields[key] + "\n" + value if key in fields else value
        if "event" in fields:
            out.append(fields)
    return out


async def test_stream_yields_appended_events_in_order(target_repo: Path) -> None:
    log = fake_run(target_repo, "r-sse", status=Status.RUNNING, stage=Stage.CODE, work_item="x")
    log.acquire_lock(os.getpid())
    log.append_event(event(1))
    stream = live.stream_events(log, 0, poll=0.01)
    assert await anext(stream) == "retry: 3000\n\n"
    first = parse_sse(await anext(stream))[0]
    assert first["event"] == "log" and "event 1" in first["data"]
    assert int(first["id"]) == (log.root / EVENTS_FILE).stat().st_size
    assert parse_sse(await anext(stream))[0]["event"] == "refresh"

    # Events appended while the stream is open arrive in order, including a half-written line
    # once it's complete.
    log.append_event(event(2))
    log.append_event(event(3))
    with (log.root / EVENTS_FILE).open("a") as f:
        f.write(json.dumps(event(4))[:20])
    got = [parse_sse(await anext(stream))[0] for _ in range(3)]
    assert [g["event"] for g in got] == ["log", "log", "refresh"]
    assert "event 2" in got[0]["data"] and "event 3" in got[1]["data"]
    with (log.root / EVENTS_FILE).open("a") as f:
        f.write(json.dumps(event(4))[20:] + "\n")
    assert "event 4" in parse_sse(await anext(stream))[0]["data"]
    assert parse_sse(await anext(stream))[0]["event"] == "refresh"

    # The driving process ends: the last events, a refresh, then `end`, and the stream stops.
    log.append_event(event(5) | {"kind": "status", "final": True})
    log.release_lock()
    rest = [parse_sse(chunk)[0] async for chunk in stream]
    assert [r["event"] for r in rest] == ["log", "refresh", "end"]
    assert "event 5" in rest[0]["data"]
    assert int(rest[2]["id"]) == (log.root / EVENTS_FILE).stat().st_size


async def test_stream_sends_keepalives_while_quiet(target_repo: Path) -> None:
    log = fake_run(target_repo, "r-q", status=Status.RUNNING, stage=Stage.CODE, work_item="x")
    log.acquire_lock(os.getpid())
    stream = live.stream_events(log, 0, poll=0.01, keepalive=0.03)
    await anext(stream)  # retry
    assert await asyncio.wait_for(anext(stream), 2) == ": keepalive\n\n"
    await stream.aclose()


def test_event_stream_over_http(target_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live, "POLL_SECONDS", 0.01)
    log = fake_run(target_repo, "r-h", status=Status.RUNNING, stage=Stage.CODE, work_item="x")
    log.acquire_lock(os.getpid())
    log.append_event(event(1))
    start = (log.root / EVENTS_FILE).stat().st_size

    def driver() -> None:  # the "run": two more events, then it ends
        time.sleep(0.2)
        log.append_event(event(2))
        log.append_event(event(3))
        time.sleep(0.2)
        log.release_lock()

    thread = threading.Thread(target=driver)
    client = logged_in(target_repo)
    thread.start()
    with client.stream("GET", f"/runs/r-h/events?offset={start}") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["cache-control"] == "no-store"
        body = "".join(r.iter_text())
    thread.join()
    events = parse_sse(body)
    logs = [e["data"] for e in events if e["event"] == "log"]
    assert len(logs) == 2  # event 1 was before the offset
    assert "event 2" in logs[0] and "event 3" in logs[1]
    assert events[-1]["event"] == "end"


def test_reconnect_resumes_from_last_event_id(target_repo: Path) -> None:
    log = fake_run(target_repo, "r-re", status=Status.COMPLETE, stage=Stage.DONE, work_item="x")
    for n in (1, 2, 3):
        log.append_event(event(n))
    client = logged_in(target_repo)
    everything = parse_sse(client.get("/runs/r-re/events").text)
    assert [e["event"] for e in everything] == ["log", "log", "log", "refresh", "end"]
    after_first = everything[0]["id"]
    again = parse_sse(client.get("/runs/r-re/events", headers={"Last-Event-ID": after_first}).text)
    assert ["event 2" in again[0]["data"], "event 3" in again[1]["data"]] == [True, True]
    assert len([e for e in again if e["event"] == "log"]) == 2


def test_offset_into_the_middle_of_a_line_skips_to_the_next(target_repo: Path) -> None:
    log = fake_run(target_repo, "r-mid", status=Status.COMPLETE, stage=Stage.DONE, work_item="x")
    log.append_event(event(1))
    log.append_event(event(2))
    client = logged_in(target_repo)
    for offset in (5, 10**9, -3):
        events = parse_sse(client.get(f"/runs/r-mid/events?offset={offset}").text)
        logs = [e["data"] for e in events if e["event"] == "log"]
        if offset == 5:
            assert len(logs) == 1 and "event 2" in logs[0]
        elif offset < 0:
            assert len(logs) == 2
        else:
            assert logs == []


def test_read_events_and_html() -> None:
    ev = live.LogEvent(10, "verdict", "2026-10-08T04:41:02.512Z", "CODE", 2, "a\nb <c>")
    assert ev.time == "04:41:02" and ev.where == "CODE r2"
    html = str(ev.html())
    assert "\n" not in html and "a b &lt;c&gt;" in html and 'class="ev ev-verdict"' in html
    assert live.LogEvent(0, "x", "", "INIT", 0, "m").where == ""
    assert live.sse("log", "a\nb", 7) == "event: log\nid: 7\ndata: a\ndata: b\n\n"
    assert live.sse("end") == "event: end\ndata: \n\n"


def test_read_events_without_file(tmp_path: Path) -> None:
    assert live.read_events(tmp_path / "events.jsonl", 0) == ([], 0)
    assert live.line_start(tmp_path / "events.jsonl", 50) == 0


def test_markdown_is_safe_and_csp_clean() -> None:
    from advpipe.ui.render import markdown

    html = str(markdown(f"| a | b |\n|---|--:|\n| 1 | 2 |\n\n{EVIL}\n\n[x](javascript:alert(1))"))
    assert 'class="align-right"' in html and "style=" not in html  # CSP: no inline styles
    no_raw_html(html)
    assert 'href="javascript:' not in html
