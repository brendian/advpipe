"""The web UI (U1): access token, CSRF, Host check, the runs list, and `advpipe ui`."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fakes import TASK_MD_BLOCKING, FakeAgentRunner, happy_scripts, writes  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from advpipe import cli as cli_module  # noqa: E402
from advpipe.config import Config, Limits  # noqa: E402
from advpipe.models import RunState, Stage, Status  # noqa: E402
from advpipe.orchestrator import Orchestrator  # noqa: E402
from advpipe.runlog import RunLog  # noqa: E402
from advpipe.runner import Role  # noqa: E402
from advpipe.ui.app import create_ui_app  # noqa: E402
from advpipe.ui.runs import age, load_runs, title_of  # noqa: E402
from advpipe.ui.security import LOOPBACK_HOSTS, cookie_name, host_name  # noqa: E402

TOKEN = "test-token-0123456789"
BASE = "http://127.0.0.1:8765"


def make_app(repo: Path, **kwargs: Any) -> FastAPI:
    return create_ui_app(repo, Path("work-items"), TOKEN, **kwargs)


def anon(repo: Path) -> TestClient:
    return TestClient(make_app(repo), base_url=BASE, follow_redirects=False)


def logged_in(repo: Path) -> TestClient:
    client = TestClient(make_app(repo), base_url=BASE)
    assert client.get(f"/?token={TOKEN}").status_code == 200
    return client


def csrf_of(page: str) -> str:
    match = re.search(r'hx-headers=\'\{"X-CSRF-Token": "([^"]+)"\}\'', page)
    assert match, "no CSRF token in page"
    return match.group(1)


def fake_run(repo: Path, run_id: str, *, status: Status, stage: Stage, **fields: Any) -> RunLog:
    """A run directory written directly, for statuses a finished fake run can't leave."""
    log = RunLog.for_run(repo, run_id)
    state = RunState(run_id=run_id, repo=str(repo), status=status, stage=stage, **fields)
    log.write_state(state)
    log.write_config(Config(limits=Limits(budget_usd_per_task=8.0, max_rounds_code=3)))
    return log


# --------------------------------------------------------------------------- access token


def test_no_token_is_401_everywhere(target_repo: Path) -> None:
    client = anon(target_repo)
    for path in ("/", "/runs/list", "/static/htmx.min.js", "/static/app.css", "/nope"):
        r = client.get(path)
        assert r.status_code == 401, path
        assert "?token=" in r.text
    client.cookies.set(cookie_name(TOKEN), "wrong")
    assert client.get("/").status_code == 401


def test_wrong_token_is_401_and_sets_no_cookie(target_repo: Path) -> None:
    r = anon(target_repo).get("/?token=wrong")
    assert r.status_code == 401
    assert "set-cookie" not in r.headers


def test_token_sets_cookie_and_strips_it_from_the_url(target_repo: Path) -> None:
    client = anon(target_repo)
    r = client.get(f"/?show=running&token={TOKEN}")
    assert r.status_code == 303
    assert r.headers["location"] == "/?show=running"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{cookie_name(TOKEN)}={TOKEN};")
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/" in cookie
    # The cookie alone now works, on pages and static files.
    assert client.get("/").status_code == 200
    assert client.get("/static/htmx.min.js").status_code == 200


def test_login_redirect_never_leaves_the_site(target_repo: Path) -> None:
    client = anon(target_repo)
    for path in ("//evil.example/", "/\\evil.example/", "///evil.example"):
        r = client.get(f"{path}?token={TOKEN}")
        location = r.headers["location"]
        assert location.startswith("/") and not location.startswith(("//", "/\\")), location


def test_two_servers_use_different_cookies() -> None:
    assert cookie_name("a") != cookie_name("b")
    assert re.fullmatch(r"advpipe_[0-9a-f]{12}", cookie_name(TOKEN))


def test_token_in_query_does_not_bypass_csrf(target_repo: Path) -> None:
    # A POST carrying ?token= is checked like any other POST: cookie and CSRF header.
    assert anon(target_repo).post(f"/?token={TOKEN}").status_code == 401


# --------------------------------------------------------------------------- Host, CSRF, headers


def test_unknown_host_is_rejected(target_repo: Path) -> None:
    client = logged_in(target_repo)
    r = client.get("/", headers={"host": "evil.example:8765"})
    assert r.status_code == 400
    assert client.get("/", headers={"host": "localhost:8765"}).status_code == 200
    assert client.get("/", headers={"host": "[::1]:8765"}).status_code == 200


def test_host_check_can_be_turned_off_for_exposed_servers(target_repo: Path) -> None:
    client = TestClient(make_app(target_repo, allowed_hosts=None), base_url="http://10.0.0.5")
    assert client.get(f"/?token={TOKEN}").status_code == 200


def test_host_name() -> None:
    assert host_name("127.0.0.1:8765") == "127.0.0.1"
    assert host_name("LOCALHOST") == "localhost"
    assert host_name("[::1]:8765") == "::1"
    assert host_name("::1") == "::1"
    assert "evil.example" not in LOOPBACK_HOSTS


def test_state_changing_requests_need_csrf(target_repo: Path) -> None:
    app = make_app(target_repo)

    @app.post("/_probe")
    def probe() -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app, base_url=BASE)
    assert client.post("/_probe").status_code == 401  # not logged in: 401 before CSRF
    page = client.get(f"/?token={TOKEN}").text
    csrf = csrf_of(page)
    assert client.post("/_probe").status_code == 403
    assert client.post("/_probe", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post("/_probe", headers={"X-CSRF-Token": csrf}).json() == {"ok": True}
    assert client.delete("/_probe").status_code == 403  # any unsafe method, not just POST


def test_csrf_token_differs_per_server(target_repo: Path) -> None:
    one = csrf_of(logged_in(target_repo).get("/").text)
    two = csrf_of(logged_in(target_repo).get("/").text)
    assert one != two and len(one) >= 32


def test_security_headers_on_every_response(target_repo: Path) -> None:
    for r in (anon(target_repo).get("/"), logged_in(target_repo).get("/")):
        csp = r.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "unsafe" not in csp
        assert "frame-ancestors 'none'" in csp
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer"


def test_no_api_docs_or_websockets(target_repo: Path) -> None:
    client = logged_in(target_repo)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/"):
        pass


# --------------------------------------------------------------------------- layout and static


def test_layout_loads_vendored_htmx_and_sse(target_repo: Path) -> None:
    client = logged_in(target_repo)
    page = client.get("/").text
    assert '<script src="/static/htmx.min.js" defer></script>' in page
    assert '<script src="/static/htmx-ext-sse.min.js" defer></script>' in page
    assert "https://" not in page.split("<body")[0]  # no CDN
    assert 'aria-current="page"' in page and target_repo.name in page
    htmx = client.get("/static/htmx.min.js")
    assert htmx.status_code == 200 and "htmx" in htmx.text[:2000]
    assert 'defineExtension("sse"' in client.get("/static/htmx-ext-sse.min.js").text
    assert client.get("/static/app.css").status_code == 200


def test_static_files_cannot_escape_the_folder(target_repo: Path) -> None:
    client = logged_in(target_repo)
    for path in ("/static/../app.py", "/static/%2e%2e/app.py", "/static/..%2fapp.py"):
        r = client.get(path)
        assert r.status_code in (400, 401, 404), path
        assert "create_ui_app" not in r.text


# --------------------------------------------------------------------------- runs list


async def real_runs(repo: Path, config: Config) -> None:
    """Two runs driven by the real orchestrator with fake agents: complete and needs_human."""
    await Orchestrator(
        config, FakeAgentRunner(happy_scripts()), repo, "add a clamp function", "r-complete"
    ).run()
    scripts = happy_scripts()
    scripts[Role.SPEC_WRITER] = [writes({"task.md": TASK_MD_BLOCKING})]
    await Orchestrator(
        config, FakeAgentRunner(scripts), repo, "## clamp with questions\nmore", "r-needs"
    ).run()


def test_empty_state_says_what_to_do(target_repo: Path) -> None:
    page = logged_in(target_repo).get("/").text
    assert "No runs yet" in page
    assert "advpipe run --repo" in page and "--item" in page


async def test_runs_list_from_real_runs(config: Config, target_repo: Path) -> None:
    await real_runs(target_repo, config)
    page = logged_in(target_repo).get("/").text

    complete = row(page, "r-complete")
    assert "complete" in complete and "add a clamp function" in complete
    assert "advpipe/add-clamp-function" in complete
    assert "DONE" in complete and "$0." in complete
    assert "/ $" not in complete  # budget is shown only while running
    assert "ready for you to review" in complete  # the status tooltip

    needs = row(page, "r-needs")
    assert "needs you" in needs and "SPEC" in needs
    assert "clamp with questions" in needs and "##" not in needs
    # Newest first.
    assert page.index('id="run-r-needs"') < page.index('id="run-r-complete"')


def row(page: str, run_id: str) -> str:
    start = page.index(f'id="run-{run_id}"')
    return page[start : page.index("</tr>", start)]


def test_live_statuses_cost_budget_and_round(target_repo: Path) -> None:
    now = datetime.now(UTC)
    live = fake_run(
        target_repo,
        "r-live",
        status=Status.RUNNING,
        stage=Stage.CODE,
        round=2,
        cost_usd=1.1,
        work_item="S03 stock changes",
        branch="advpipe/s03-stock",
        started_at=now - timedelta(minutes=3),
    )
    live.acquire_lock(os.getpid())  # this test process is alive: the run counts as active
    fake_run(
        target_repo,
        "r-stopped",
        status=Status.RUNNING,
        stage=Stage.TESTS,
        round=1,
        work_item="interrupted one",
        started_at=now - timedelta(hours=2),
    )
    fake_run(
        target_repo, "r-broke", status=Status.BUDGET_EXCEEDED, stage=Stage.FINAL_GATES,
        work_item="spent it all", started_at=now - timedelta(days=2),
    )  # fmt: skip
    page = logged_in(target_repo).get("/").text

    r = row(page, "r-live")
    assert "running" in r and "CODE r2/3" in r
    assert "$1.10" in r and "/ $8.00" in r
    assert "3 min ago" in r
    s = row(page, "r-stopped")
    assert "stopped" in s and "TESTS r1/2" in s and "advpipe resume" in s and "2 h ago" in s
    b = row(page, "r-broke")
    assert "over budget" in b and "FINAL CHECKS" in b and "2 days ago" in b


def test_filters(target_repo: Path) -> None:
    fake_run(target_repo, "r-done", status=Status.COMPLETE, stage=Stage.DONE, work_item="done")
    fake_run(target_repo, "r-err", status=Status.ERROR, stage=Stage.CODE, work_item="broken")
    fake_run(target_repo, "r-int", status=Status.RUNNING, stage=Stage.CODE, work_item="paused")
    client = logged_in(target_repo)

    def ids(show: str) -> set[str]:
        return set(re.findall(r'id="run-([^"]+)"', client.get(f"/?show={show}").text))

    assert ids("all") == {"r-done", "r-err", "r-int"}
    assert ids("complete") == {"r-done"}
    assert ids("needs_you") == {"r-err", "r-int"}
    assert ids("running") == set()
    assert "No running runs right now" in client.get("/?show=running").text
    assert ids("bogus") == ids("all")  # unknown filter: show everything
    page = client.get("/?show=complete").text
    assert re.search(r'aria-current="true">Complete <span class="count">1<', page)
    assert re.search(r'Needs you <span class="count">2<', page)


def test_list_fragment_polls_and_picks_up_new_runs(target_repo: Path) -> None:
    client = logged_in(target_repo)
    first = client.get("/runs/list?show=needs_you")
    assert first.status_code == 200
    assert "<html" not in first.text  # a fragment, not a page
    assert 'hx-get="/runs/list?show=needs_you"' in first.text
    assert 'hx-trigger="every 3s"' in first.text and 'hx-swap="outerHTML"' in first.text
    assert "No runs yet" in first.text
    fake_run(target_repo, "r-new", status=Status.NEEDS_HUMAN, stage=Stage.REVIEW, work_item="x")
    assert 'id="run-r-new"' in client.get("/runs/list?show=needs_you").text
    assert anon(target_repo).get("/runs/list").status_code == 401


def test_agent_text_is_escaped(target_repo: Path) -> None:
    evil = '<script>alert(1)</script><img src=x onerror="x()">'
    fake_run(
        target_repo, "r-xss", status=Status.COMPLETE, stage=Stage.DONE,
        work_item=evil, branch='advpipe/"><script>b()</script>',
    )  # fmt: skip
    page = logged_in(target_repo).get("/").text
    assert "<script>alert" not in page and "<img src=x" not in page and "<script>b()" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_unreadable_run_does_not_break_the_list(target_repo: Path) -> None:
    fake_run(target_repo, "r-ok", status=Status.COMPLETE, stage=Stage.DONE, work_item="fine")
    bad = target_repo / ".advpipe" / "runs" / "r-bad"
    bad.mkdir()
    (bad / "run.json").write_text("")  # e.g. written by an older advpipe, caught mid-write
    page = logged_in(target_repo).get("/").text
    assert 'id="run-r-ok"' in page
    assert "read run.json for: r-bad" in page


def test_runs_list_helpers() -> None:
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    assert age(now - timedelta(seconds=5), now) == "just now"
    assert age(now - timedelta(minutes=59), now) == "59 min ago"
    assert age(now - timedelta(days=1), now) == "1 day ago"
    assert age(now + timedelta(minutes=5), now) == "just now"  # clock skew
    assert title_of("\n\n# Add stock levels\nbody") == "Add stock levels"
    assert title_of("   ") == "(empty work item)"
    assert len(title_of("x" * 200)) == 80


def test_load_runs_without_runs_dir(tmp_path: Path) -> None:
    runs = load_runs(tmp_path)
    assert runs.rows == [] and runs.unreadable == []


def test_run_json_writes_are_atomic(target_repo: Path) -> None:
    log = fake_run(target_repo, "r-a", status=Status.COMPLETE, stage=Stage.DONE, work_item="a")
    log.write_state(log.read_state())
    assert sorted(p.name for p in log.root.iterdir()) == ["config.json", "run.json"]


# --------------------------------------------------------------------------- advpipe ui


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace uvicorn.run: record what would be served instead of serving it."""
    import uvicorn

    calls: list[dict[str, Any]] = []

    def fake_run(app: FastAPI, **kwargs: Any) -> None:
        calls.append({"app": app, **kwargs})

    monkeypatch.setattr(uvicorn, "run", fake_run)
    return calls


def test_ui_command_defaults(target_repo: Path, served: list[dict[str, Any]]) -> None:
    result = CliRunner().invoke(cli_module.app, ["ui", "--repo", str(target_repo)])
    assert result.exit_code == 0, result.output
    (call,) = served
    assert call["host"] == "127.0.0.1" and call["port"] == 8765
    token = re.search(r"open: http://127\.0\.0\.1:8765/\?token=(\S+)", result.output)
    assert token, result.output
    ui = call["app"].state.ui
    assert ui.repo == target_repo.resolve()
    assert ui.items_dir == target_repo.resolve() / "work-items"
    # The printed link logs in.
    client = TestClient(call["app"], base_url=BASE)
    assert client.get(f"/?token={token.group(1)}").status_code == 200
    assert client.get("/", headers={"host": "evil.example"}).status_code == 400


def test_ui_command_options(target_repo: Path, served: list[dict[str, Any]]) -> None:
    args = ["ui", "--repo", str(target_repo), "--port", "9000", "--items-dir", "tasks/server"]
    assert CliRunner().invoke(cli_module.app, args).exit_code == 0
    assert served[0]["port"] == 9000
    assert served[0]["app"].state.ui.items_dir == target_repo.resolve() / "tasks" / "server"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::"])
def test_ui_refuses_exposed_host(
    target_repo: Path, served: list[dict[str, Any]], host: str
) -> None:
    result = CliRunner().invoke(cli_module.app, ["ui", "--repo", str(target_repo), "--host", host])
    assert result.exit_code == 2
    assert "--i-know-this-is-exposed" in result.output
    assert served == []


def test_ui_exposed_host_with_flag_warns(target_repo: Path, served: list[dict[str, Any]]) -> None:
    args = ["ui", "--repo", str(target_repo), "--host", "0.0.0.0", "--i-know-this-is-exposed"]
    result = CliRunner().invoke(cli_module.app, args)
    assert result.exit_code == 0, result.output
    assert "WARNING" in result.output
    assert served[0]["host"] == "0.0.0.0"
    assert served[0]["app"].state.ui is not None


def test_ui_rejects_missing_repo(tmp_path: Path, served: list[dict[str, Any]]) -> None:
    result = CliRunner().invoke(cli_module.app, ["ui", "--repo", str(tmp_path / "nope")])
    assert result.exit_code == 2 and served == []
