"""The web UI (U5): actions. Start a run (New run dialog), cancel, resume and clean up.

The actions run the advpipe CLI as a subprocess. Most tests swap ``subprocess.run`` (for
advpipe's argument lists only) and check the exact list. The end-to-end test runs the real
CLI with the fake agent runner (tests/advpipe_fake_child.py), so no API calls are made.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from conftest import git

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from httpx import Response  # noqa: E402
from test_control import FAKE_CHILD, wait_for  # noqa: E402
from test_ui import BASE, csrf_of, fake_run, logged_in, make_app  # noqa: E402
from test_ui_detail import EVIL, no_raw_html  # noqa: E402

import advpipe.control as control  # noqa: E402
from advpipe.models import Stage, Status  # noqa: E402
from advpipe.runlog import RunLog  # noqa: E402
from advpipe.ui import actions  # noqa: E402
from advpipe.ui.actions import RunForm, plan_run  # noqa: E402

ADVPIPE = "advpipe-under-test"  # what control.child_command returns in these tests


@dataclass
class Cli:
    """Records advpipe calls instead of running them; answers with ``reply``."""

    calls: list[list[str]] = field(default_factory=list)
    kwargs: list[dict[str, Any]] = field(default_factory=list)
    reply: subprocess.CompletedProcess[str] = field(
        default_factory=lambda: subprocess.CompletedProcess([], 0, "20261008-120000-abc123\n", "")
    )

    def args(self) -> list[str]:
        """The one call made, without the program."""
        assert len(self.calls) == 1, self.calls
        assert self.calls[0][0] == ADVPIPE
        return self.calls[0][1:]


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> Iterator[Cli]:
    fake = Cli()
    real_run = subprocess.run

    def run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(argv, list) and argv[:1] == [ADVPIPE]:
            assert not args, "options are passed by keyword"
            fake.calls.append(list(argv))
            fake.kwargs.append(kwargs)
            return fake.reply
        return real_run(argv, *args, **kwargs)  # git, used by the pages

    monkeypatch.setattr(control, "child_command", lambda: [ADVPIPE])
    monkeypatch.setattr(actions.subprocess, "run", run)
    yield fake


class Browser:
    """A logged-in client that sends the CSRF header the way htmx does."""

    def __init__(self, repo: Path) -> None:
        self.client = logged_in(repo)
        self.csrf = csrf_of(self.client.get("/").text)

    def get(self, url: str) -> Response:
        return self.client.get(url)

    def post(self, url: str, data: dict[str, str] | None = None) -> Response:
        return self.client.post(url, data=data or {}, headers={"X-CSRF-Token": self.csrf})


def redirected_to(r: Response) -> str:
    assert r.status_code == 200, r.text
    return r.headers["hx-redirect"]


@pytest.fixture
def repo(target_repo: Path) -> Path:
    """The sample repo with a pipeline.toml ($7.50 budget), another config, and work items."""
    (target_repo / "pipeline.toml").write_text("[limits]\nbudget_usd_per_task = 7.5\n")
    (target_repo / "cheap.toml").write_text("[limits]\nbudget_usd_per_task = 2\n")
    items = target_repo / "work-items" / "server"
    items.mkdir(parents=True)
    (items / "s01.md").write_text("---\nname: s01-clamp\n---\nAdd clamp.\n")
    (items / "s02.md").write_text("---\nconfig: cheap.toml\n---\nAdd stock.\n")
    (items / "broken.md").write_text("---\nnope: x\n---\nBody.\n")
    return target_repo.resolve()


# --------------------------------------------------------------------------- starting a run


def test_new_run_dialog_shows_the_budget_before_starting(repo: Path, cli: Cli) -> None:
    b = Browser(repo)
    page = b.get("/runs/new?item=server/s01.md").text
    assert "This can spend up to <strong>$7.50</strong>" in page
    assert 'name="budget" value="7.50"' in page  # sent back to confirm it was seen
    assert "advpipe/s01-clamp" in page
    assert '<option value="server/s01.md" selected>' in page
    assert "Start (up to $7.50)" in page
    # The item's front matter picks the config, so the budget follows it.
    page = b.post("/runs/new/check", {"source": "item", "item": "server/s02.md"}).text
    assert "up to <strong>$2.00</strong>" in page and "cheap.toml (from the work item" in page
    # An override in the form wins over front matter.
    form = {"source": "item", "item": "server/s01.md", "config": "cheap.toml"}
    assert "$2.00" in b.post("/runs/new/check", form).text
    assert cli.calls == []  # looking is free


def test_start_from_a_work_item(repo: Path, cli: Cli) -> None:
    b = Browser(repo)
    r = b.post("/runs/start", {"source": "item", "item": "server/s01.md", "budget": "7.50"})
    assert redirected_to(r) == "/runs/20261008-120000-abc123?notice=started"
    item = repo / "work-items" / "server" / "s01.md"
    assert cli.args() == ["run", "--repo", str(repo), "--detach", "--item", str(item)]
    kwargs = cli.kwargs[0]
    assert kwargs.get("shell", False) is False and kwargs["cwd"] == repo
    assert kwargs["stdin"] is subprocess.DEVNULL and kwargs["timeout"] > 0


def test_start_a_one_off_task_with_overrides(repo: Path, cli: Cli) -> None:
    b = Browser(repo)
    task = "--help; rm -rf ~ $(touch /tmp/x)\r\nsecond line"
    form = {
        "source": "task",
        "item": "server/s01.md",  # ignored: the source is the typed task
        "task": task,
        "name": "--my branch",
        "config": "cheap.toml",
        "budget": "2.00",
    }
    redirected_to(b.post("/runs/start", form))
    assert cli.args() == [
        "run", "--repo", str(repo), "--detach",
        "--config", str(repo / "cheap.toml"),
        "--name", "--my branch",
        "--", "--help; rm -rf ~ $(touch /tmp/x)\nsecond line",
    ]  # fmt: skip


def test_start_refuses_a_budget_nobody_saw(repo: Path, cli: Cli) -> None:
    b = Browser(repo)
    form = {"source": "item", "item": "server/s01.md", "budget": "2.00"}
    r = b.post("/runs/start", form)
    assert r.status_code == 422
    assert "the budget is $7.50, not the $2.00 shown" in r.text
    assert 'name="budget" value="7.50"' in r.text  # the form now shows the real one
    assert b.post("/runs/start", {**form, "budget": ""}).status_code == 422
    assert cli.calls == []


@pytest.mark.parametrize(
    ("form", "problem"),
    [
        ({"source": "item", "item": ""}, "Pick a work item"),
        ({"source": "item", "item": "server/nope.md"}, "no work item server/nope.md"),
        ({"source": "item", "item": "server/broken.md"}, "can&#39;t be run as it is"),
        ({"source": "item", "item": "../pipeline.md"}, "Work item:"),
        ({"source": "item", "item": "%2e%2e/pipeline.md"}, "Work item:"),
        ({"source": "item", "item": "/etc/passwd.md"}, "Work item:"),
        ({"source": "item", "item": "server/../../x.md"}, "Work item:"),
        ({"source": "task", "task": "  \r\n "}, "The task is empty"),
        ({"source": "task", "task": "x" * 70_000}, "Save it as a work item"),
        ({"source": "task", "task": "a\0b"}, "NUL"),
        ({"source": "shell", "task": "x"}, "Choose a work item or a one-off task"),
        ({"source": "task", "task": "x", "config": "../outside.toml"}, "outside the repo"),
        ({"source": "task", "task": "x", "config": "/etc/hosts"}, "outside the repo"),
        ({"source": "task", "task": "x", "config": "missing.toml"}, "doesn&#39;t exist"),
        ({"source": "task", "task": "x", "name": "a\nb"}, "single line"),
    ],
)
def test_start_rejects_bad_forms(repo: Path, cli: Cli, form: dict[str, str], problem: str) -> None:
    r = Browser(repo).post("/runs/start", {"budget": "7.50", **form})
    assert r.status_code == 422, r.text
    assert problem in r.text
    assert "Not started" in r.text and "<html" not in r.text  # the form, to swap in
    assert cli.calls == []


def test_invalid_config_is_reported(repo: Path, cli: Cli) -> None:
    (repo / "bad.toml").write_text("[limits]\nbudget_usd_per_task = -1\n")
    plan = plan_run(repo, repo / "work-items", RunForm(source="task", task="x", config="bad.toml"))
    assert not plan.ok and plan.args == [] and plan.errors[0].startswith("Config:")


def test_start_shows_what_advpipe_said_when_it_refuses(repo: Path, cli: Cli) -> None:
    cli.reply = subprocess.CompletedProcess([], 2, "", f"Error: {EVIL}\n")
    r = Browser(repo).post("/runs/start", {"source": "task", "task": "x", "budget": "7.50"})
    assert r.status_code == 422 and "advpipe refused to start it" in r.text
    no_raw_html(r.text)


def test_start_needs_a_run_id_back(repo: Path, cli: Cli) -> None:
    cli.reply = subprocess.CompletedProcess([], 0, "../../etc\n", "")
    r = Browser(repo).post("/runs/start", {"source": "task", "task": "x", "budget": "7.50"})
    assert r.status_code == 422 and "printed no run id" in r.text


def test_run_buttons_on_work_items(repo: Path, cli: Cli) -> None:
    b = Browser(repo)
    listing = b.get("/items").text
    assert 'href="/runs/new?item=server%2Fs01.md">Run</a>' in listing
    assert "/runs/new?item=server%2Fbroken.md" not in listing  # it can't be run as it is
    editor = b.get("/items/edit?path=server%2Fs01.md").text
    assert 'href="/runs/new?item=server%2Fs01.md">Run…</a>' in editor
    assert 'name="then" value="run"' in editor
    # "Save & run" saves, then goes to the dialog (which shows the budget); it never starts.
    form = {"path": "server/s01.md", "name": "s01-clamp", "config": "", "body": "Add clamp."}
    r = b.post("/items/save", {**form, "then": "run"})
    assert redirected_to(r) == "/runs/new?item=server%2Fs01.md"
    r = b.post("/items/new", {**form, "path": "server/s03", "then": "run"})
    assert redirected_to(r) == "/runs/new?item=server%2Fs03.md"
    assert cli.calls == []


def test_new_run_is_on_the_runs_page(repo: Path, cli: Cli) -> None:
    page = Browser(repo).get("/").text
    assert 'href="/runs/new">New run</a>' in page


# --------------------------------------------------------------------------- cancel, resume, clean


def run_with(repo: Path, status: Status, run_id: str = "r1", live: bool = False) -> RunLog:
    log = fake_run(
        repo, run_id, status=status, stage=Stage.CODE, work_item="x", branch="advpipe/x",
        cost_usd=10.0,
    )  # fmt: skip
    if live:
        log.acquire_lock(os.getpid())  # this test process is alive: the run is live
    return log


def test_cancel(repo: Path, cli: Cli) -> None:
    run_with(repo, Status.RUNNING, live=True)
    r = Browser(repo).post("/runs/r1/cancel")
    assert redirected_to(r) == "/runs/r1?notice=cancelled"
    assert cli.args() == ["cancel", "r1", "--repo", str(repo)]


def test_resume(repo: Path, cli: Cli) -> None:
    run_with(repo, Status.RUNNING)  # no live process: stopped
    r = Browser(repo).post("/runs/r1/resume", {"budget": ""})
    assert redirected_to(r) == "/runs/r1?notice=resumed"
    assert cli.args() == ["resume", "r1", "--repo", str(repo), "--detach"]


def test_resume_over_budget_needs_a_bigger_one(repo: Path, cli: Cli) -> None:
    run_with(repo, Status.BUDGET_EXCEEDED)  # spent $10 of $8
    b = Browser(repo)
    for bad, problem in [
        ("", "give it a bigger one"),
        ("9.99", "spent $10.00 already"),
        ("abc", "isn&#39;t an amount"),
        ("nan", "positive amount"),
        ("inf", "positive amount"),
        ("-5", "positive amount"),
    ]:
        r = b.post("/runs/r1/resume", {"budget": bad})
        assert r.status_code == 422 and problem in r.text, (bad, r.text)
    assert cli.calls == []
    redirected_to(b.post("/runs/r1/resume", {"budget": "$ 25.5"}))
    assert cli.args() == ["resume", "r1", "--repo", str(repo), "--detach", "--budget", "25.50"]


def test_clean_asks_first(repo: Path, cli: Cli) -> None:
    run_with(repo, Status.NEEDS_HUMAN)
    b = Browser(repo)
    page = b.get("/runs/r1/clean").text
    assert "Clean up run <code>r1</code>?" in page
    assert 'name="confirm" value="yes"' in page and 'name="logs" value="yes"' in page
    assert b.post("/runs/r1/clean").status_code == 400  # not confirmed
    assert cli.calls == []

    r = b.post("/runs/r1/clean", {"confirm": "yes"})
    assert redirected_to(r) == "/runs/r1?notice=cleaned"
    assert cli.args() == ["clean", "r1", "--repo", str(repo)]


def test_clean_with_force_and_logs(repo: Path, cli: Cli) -> None:
    run_with(repo, Status.ERROR)
    r = Browser(repo).post("/runs/r1/clean", {"confirm": "yes", "force": "yes", "logs": "yes"})
    assert redirected_to(r) == "/?removed=r1"
    assert cli.args() == ["clean", "r1", "--repo", str(repo), "--force", "--logs"]


def test_clean_page_warns_about_unmerged_commits(repo: Path, cli: Cli) -> None:
    git(repo, "checkout", "-q", "-b", "advpipe/x")
    (repo / "new.txt").write_text("work\n")
    git(repo, "add", "new.txt")
    git(repo, "commit", "-q", "-m", "work")
    git(repo, "checkout", "-q", "main")
    run_with(repo, Status.ERROR)
    page = Browser(repo).get("/runs/r1/clean").text
    assert "<strong>1 commit</strong> that no other branch has" in page
    assert 'name="force" value="yes"' in page


@pytest.mark.parametrize(
    ("status", "live", "action"),
    [
        (Status.RUNNING, False, "cancel"),  # stopped: nothing to cancel
        (Status.COMPLETE, False, "cancel"),
        (Status.RUNNING, True, "resume"),  # still going
        (Status.COMPLETE, False, "resume"),
        (Status.NEEDS_HUMAN, False, "resume"),
        (Status.RUNNING, True, "clean"),  # cancel first
        (Status.COMPLETE, False, "clean"),  # the branch is the result
    ],
)
def test_invalid_actions_are_rejected(
    repo: Path, cli: Cli, status: Status, live: bool, action: str
) -> None:
    run_with(repo, status, live=live)
    r = Browser(repo).post(f"/runs/r1/{action}", {"confirm": "yes", "budget": "50"})
    assert r.status_code == 409, r.text
    assert "Reload the page" in r.text
    assert cli.calls == []


def test_actions_on_missing_or_bad_runs(repo: Path, cli: Cli) -> None:
    b = Browser(repo)
    for url in ("/runs/nope/cancel", "/runs/nope/resume", "/runs/..%2e/clean", "/runs/.x/cancel"):
        assert b.post(url, {"confirm": "yes"}).status_code in (404, 405), url
    assert b.get("/runs/nope/clean").status_code == 404
    assert cli.calls == []


def test_clean_page_refuses_complete_and_running_runs(repo: Path, cli: Cli) -> None:
    run_with(repo, Status.COMPLETE, "done")
    run_with(repo, Status.RUNNING, "live", live=True)
    b = Browser(repo)
    assert "its branch is the result" in b.get("/runs/done/clean").text
    assert "cancel it first" in b.get("/runs/live/clean").text
    assert b.get("/runs/done/clean").status_code == 409


def test_failed_action_shows_advpipe_message(repo: Path, cli: Cli) -> None:
    run_with(repo, Status.ERROR)
    cli.reply = subprocess.CompletedProcess([], 2, "", f"branch has 2 commits {EVIL}\n")
    r = Browser(repo).post("/runs/r1/clean", {"confirm": "yes"})
    assert r.status_code == 422 and "advpipe refused: branch has 2 commits" in r.text
    no_raw_html(r.text)


def test_timeout_and_missing_program(repo: Path, cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    run_with(repo, Status.ERROR)

    def slow(argv: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(actions.subprocess, "run", slow)
    r = Browser(repo).post("/runs/r1/resume", {"budget": ""})
    assert r.status_code == 422 and "didn&#39;t answer within" in r.text
    monkeypatch.undo()  # back to the real subprocess.run, with a program that doesn't exist
    monkeypatch.setattr(control, "child_command", lambda: [str(repo / "no-such-advpipe")])
    r = Browser(repo).post("/runs/r1/resume", {"budget": ""})
    assert r.status_code == 422 and "couldn&#39;t start advpipe" in r.text


# --------------------------------------------------------------------------- security


def test_actions_need_csrf_and_the_token(repo: Path, cli: Cli) -> None:
    run_with(repo, Status.RUNNING, "live", live=True)
    run_with(repo, Status.ERROR, "dead")
    posts = {
        "/runs/start": {"source": "task", "task": "x", "budget": "7.50"},
        "/runs/live/cancel": {},
        "/runs/dead/resume": {"budget": "50"},
        "/runs/dead/clean": {"confirm": "yes", "logs": "yes"},
        "/runs/new/check": {"source": "task", "task": "x"},
    }
    anon = TestClient(make_app(repo), base_url=BASE, follow_redirects=False)
    client = logged_in(repo)
    for url, data in posts.items():
        assert anon.post(url, data=data).status_code == 401, url
        assert client.post(url, data=data).status_code == 403, url
        assert client.post(url, data=data, headers={"X-CSRF-Token": "x"}).status_code == 403
        # GET never acts (the state-changing routes are POST only); /clean's GET is the
        # confirmation page.
        assert client.get(url).status_code in (200, 404, 405), url
    assert cli.calls == []
    assert RunLog.for_run(repo, "dead").root.is_dir()


# --------------------------------------------------------------------------- buttons per status


def buttons(page: str, run_id: str = "r1") -> set[str]:
    found = set()
    if f'hx-post="/runs/{run_id}/cancel"' in page:
        found.add("cancel")
    if f'hx-post="/runs/{run_id}/resume"' in page:
        found.add("resume")
    if f'href="/runs/{run_id}/clean"' in page:
        found.add("clean")
    return found


@pytest.mark.parametrize(
    ("status", "live", "expected"),
    [
        (Status.RUNNING, True, {"cancel"}),
        (Status.RUNNING, False, {"resume", "clean"}),
        (Status.COMPLETE, False, set()),
        (Status.NEEDS_HUMAN, False, {"clean"}),
        (Status.BUDGET_EXCEEDED, False, {"resume", "clean"}),
        (Status.ERROR, False, {"resume", "clean"}),
    ],
)
def test_buttons_only_for_valid_actions(
    repo: Path, cli: Cli, status: Status, live: bool, expected: set[str]
) -> None:
    run_with(repo, status, live=live)
    b = Browser(repo)
    assert buttons(b.get("/runs/r1").text) == expected
    assert buttons(b.get("/runs/r1/body").text) == expected  # the live refresh too
    in_list = "clean" in expected
    assert ('href="/runs/r1/clean"' in b.get("/runs/list").text) is in_list


def test_budget_exceeded_suggests_a_bigger_budget(repo: Path, cli: Cli) -> None:
    run_with(repo, Status.BUDGET_EXCEEDED)  # $10 spent, $8 budget
    page = Browser(repo).get("/runs/r1").text
    assert 'name="budget" inputmode="decimal"' in page and 'value="16" required' in page
    assert "it has spent $10.00 and ran out" in page


def test_cancel_asks_first(repo: Path, cli: Cli) -> None:
    run_with(repo, Status.RUNNING, live=True)
    assert 'hx-confirm="Stop this run' in Browser(repo).get("/runs/r1").text


def test_notices_are_fixed_texts(repo: Path, cli: Cli) -> None:
    run_with(repo, Status.ERROR)
    b = Browser(repo)
    assert "Resumed in the background" in b.get("/runs/r1?notice=resumed").text
    assert EVIL not in b.get(f"/runs/r1?notice={EVIL}").text
    assert "Cleaned up run <code>r1</code>" in b.get("/?removed=r1").text
    assert "Cleaned up run" not in b.get("/?removed=../x").text


# --------------------------------------------------------------------------- starting up


def test_run_starting_up_then_shown(repo: Path, cli: Cli) -> None:
    log = RunLog.for_run(repo, "r-new")
    log.acquire_lock(os.getpid())  # as start_detached does, before the child writes run.json
    (log.root / "console.log").write_text("starting\n")
    b = Browser(repo)
    page = b.get("/runs/r-new?notice=started").text
    assert "Starting up" in page and "Started in the background" in page
    assert 'hx-get="/runs/r-new/starting" hx-trigger="every 1s"' in page
    assert "Starting up" in b.get("/runs/r-new/starting").text

    run_with(repo, Status.RUNNING, "r-new", live=True)  # run.json appears
    r = b.get("/runs/r-new/starting")
    assert r.headers["hx-refresh"] == "true"
    assert "Timeline" in b.get("/runs/r-new").text


def test_run_that_died_while_starting_shows_its_output(repo: Path, cli: Cli) -> None:
    log = RunLog.for_run(repo, "r-dead")
    log.root.mkdir(parents=True)
    (log.root / "console.log").write_text("".join(f"line {i}\n" for i in range(100)) + EVIL)
    page = Browser(repo).get("/runs/r-dead").text
    assert "stopped before it got going" in page and "every 1s" not in page
    assert "line 99" in page and "line 10\n" not in page  # only the end
    no_raw_html(page)


# --------------------------------------------------------------------------- end to end


@pytest.fixture
def fake_child(repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Actions run the real CLI, whose runs use the fake agent runner, blocked at the coder
    until the returned release file exists."""
    (repo / "pipeline.toml").write_text(
        f'[gates]\ntest = ["{sys.executable}", "-m", "pytest", "-q", "-p", "no:cacheprovider"]\n'
        "types = []\nlint = []\nsecurity = []\n[limits]\nbudget_usd_per_task = 7.5\n"
    )
    release = tmp_path / "release"
    monkeypatch.setattr(control, "child_command", lambda: [sys.executable, str(FAKE_CHILD)])
    monkeypatch.setenv("ADVPIPE_FAKE_BLOCK", "coder")
    monkeypatch.setenv("ADVPIPE_FAKE_RELEASE", str(release))
    return release


def events_text(log: RunLog) -> str:
    path = log.root / "events.jsonl"
    return path.read_text() if path.is_file() else ""


def test_start_cancel_resume_end_to_end(repo: Path, fake_child: Path) -> None:
    b = Browser(repo)
    r = b.post("/runs/start", {"source": "item", "item": "server/s01.md", "budget": "7.50"})
    run_url = redirected_to(r).split("?")[0]
    run_id = run_url.rsplit("/", 1)[-1]
    log = RunLog.for_run(repo, run_id)
    wait_for(lambda: "coder working" in events_text(log))
    page = b.get(run_url).text
    assert "● running" in page and buttons(page, run_id) == {"cancel"}
    state = log.read_state()
    assert state.branch == "advpipe/s01-clamp"
    assert state.work_item_file == str(repo / "work-items" / "server" / "s01.md")

    redirected_to(b.post(f"{run_url}/cancel"))
    wait_for(lambda: log.lock_holder() is None)
    page = b.get(run_url).text
    assert "■ stopped" in page and buttons(page, run_id) == {"resume", "clean"}

    fake_child.touch()  # let the coder finish this time
    redirected_to(b.post(f"{run_url}/resume", {"budget": ""}))
    wait_for(lambda: log.lock_holder() is None and log.read_state().status is not Status.RUNNING)
    state = log.read_state()
    assert state.status is Status.COMPLETE, state.notes
    assert "Interrupted during CODE" in state.notes
    assert buttons(b.get(run_url).text, run_id) == set()
