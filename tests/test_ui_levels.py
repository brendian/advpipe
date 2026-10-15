"""The web UI's side of pipeline levels (docs/LEVELS_PLAN.md §3.13, milestone L5): the New run
dialog's slider, scan and Bypass choices; the work-item editor's run settings; and the run
page, runs list, context tab and Help at every level.

Runs come from the real orchestrator with fake agents. Starting a run is checked by recording
the `advpipe run --detach` call (the ``cli`` fixture from test_ui_actions): no process starts.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

import test_ui_actions  # noqa: E402
from fakes import (  # noqa: E402
    IMPL_OK,
    TASK_MD,
    TEST_CLAMP,
    FakeAgentRunner,
    Step,
    happy_scripts,
    verdict_pass,
    writes,
)
from markupsafe import escape  # noqa: E402
from test_ui_actions import Browser, redirected_to  # noqa: E402
from test_ui_detail import steps_of  # noqa: E402

from advpipe.config import Config, Pipeline  # noqa: E402
from advpipe.levels import (  # noqa: E402
    LEVEL_INFO,
    LEVEL_ORDER,
    dropped_safeguards,
    level_number,
    max_agent_calls,
    roles_for,
    steps_for,
)
from advpipe.models import Level, Status  # noqa: E402
from advpipe.orchestrator import Orchestrator  # noqa: E402
from advpipe.runner import Role  # noqa: E402

cli = test_ui_actions.cli
repo = test_ui_actions.repo

SOLO_OK = writes({"task.md": TASK_MD, "tests/test_clamp.py": TEST_CLAMP, **IMPL_OK}, "solo done")


def shown(level: str, scan: str = "on", policy: str = "ask", bypass: str = "ask") -> str:
    return f"{level};{scan};{policy};{bypass}"


def item_form(**extra: str) -> dict[str, str]:
    return {"source": "item", "item": "server/s01.md", "budget": "7.50", **extra}


def write_item(repo: Path, name: str, front: str, body: str = "Add a clamp function.") -> None:
    (repo / "work-items" / "server" / name).write_text(f"---\n{front}\n---\n{body}\n")


# --------------------------------------------------------------------------- the dialog


def test_the_dialog_has_the_slider_and_the_choices(repo: Path, cli: test_ui_actions.Cli) -> None:
    page = Browser(repo).get("/runs/new?item=server/s01.md").text
    assert '<input type="range" name="level" min="1" max="5" step="1"' in page
    assert 'list="level-ticks"' in page
    for number, label in enumerate(("Basic", "Reviewer", "Tests", "Adversarial", "Full"), 1):
        assert f'<option value="{number}" label="{label}">' in page  # the datalist
        on = ' class="on"' if number == 5 else ""
        assert f"<li{on}>{label}</li>" in page  # the visible text
    for value in ("default", "on", "off"):
        assert f'name="scan" value="{value}"' in page
    for value in ("ask", "ignore", "block"):
        assert f'name="scan_failures" value="{value}"' in page
    assert 'name="bypass_seen" value="1"' in page
    assert 'type="checkbox" name="bypass" value="yes"' in page
    assert "Don&#39;t stop for questions" in page or "Don't stop for questions" in page
    assert 'name="shown"' in page  # sent back to confirm what was seen
    assert cli.calls == []


def slider_value(page: str) -> str:
    m = re.search(r'<input type="range" name="level"[^>]* value="(\d)"', page)
    assert m, page
    return m.group(1)


def test_the_slider_starts_at_the_inherited_level(repo: Path, cli: test_ui_actions.Cli) -> None:
    b = Browser(repo)
    assert slider_value(b.get("/runs/new?item=server/s01.md").text) == "5"  # full
    write_item(repo, "basic.md", "level: basic")
    assert slider_value(b.get("/runs/new?item=server/basic.md").text) == "1"
    (repo / "pipeline.toml").write_text('[pipeline]\nlevel = "tests"\n')
    assert slider_value(b.get("/runs/new?item=server/s01.md").text) == "3"
    assert slider_value(b.get("/runs/new?item=server/basic.md").text) == "1"  # front matter wins
    # The dialog says where the level comes from, and what overrides it.
    summary = b.post("/runs/new/check", {"source": "item", "item": "server/basic.md"}).text
    assert "Level basic, from the work item&#39;s front matter" in summary
    assert 'data-inherited-level="1"' in summary
    form = {"source": "item", "item": "server/basic.md", "level": "5"}
    summary = b.post("/runs/new/check", form).text
    assert "The slider overrides basic, from the work item&#39;s front matter" in summary
    assert 'data-inherited-level="1"' in summary  # app.js moves an untouched slider back to this


@pytest.mark.parametrize("level", LEVEL_ORDER)
def test_the_summary_for_every_level(repo: Path, level: Level, cli: test_ui_actions.Cli) -> None:
    info = LEVEL_INFO[level]
    scan = level is Level.FULL
    page = Browser(repo).post("/runs/new/check", item_form(level=str(level_number(level)))).text
    assert f'{info.label} <span class="muted">({level_number(level)} of 5)</span>' in page
    agents = len(roles_for(level))
    assert f"<strong>{agents} agent{'s' if agents != 1 else ''}</strong>" in page
    happy, worst = max_agent_calls(level, Config().limits)
    if happy == worst:
        assert f"{happy} agent call{'s' if happy != 1 else ''}, then it asks you" in page
    else:
        assert f"{happy} calls if all goes well, at most {worst} before it asks you" in page
    assert str(escape(info.good_for)) in page
    for line in dropped_safeguards(level, scan):
        assert f"<li>{escape(line)}</li>" in page
    if level is Level.FULL:
        assert "Not done at this level" not in page  # full with the scan drops nothing
    else:
        assert "Not done at this level" in page
    assert f'name="shown" value="{shown(level.value, "on" if scan else "off")}"' in page
    assert cli.calls == []  # looking is free


@pytest.mark.parametrize(
    ("scan", "level", "says", "on"),
    [
        (
            "default",
            "1",
            '<strong>off</strong>\n      <span class="muted">(the level&#39;s default)',
            False,
        ),
        (
            "default",
            "5",
            '<strong>on</strong>\n      <span class="muted">(the level&#39;s default)',
            True,
        ),
        ("on", "1", '<strong>on</strong>\n      <span class="muted">(set here)', True),
        ("off", "5", '<strong>off</strong>\n      <span class="muted">(set here)', False),
    ],
)
def test_the_summary_for_each_scan_choice(
    repo: Path, scan: str, level: str, says: str, on: bool, cli: test_ui_actions.Cli
) -> None:
    page = Browser(repo).post("/runs/new/check", item_form(level=level, scan=scan)).text
    assert says in page
    assert ("No security scan" in page) is (not on)
    if on and level != "5":
        assert "if it finds issues, <strong>it asks you</strong>" in page
    else:
        assert "if it finds issues" not in page  # only below full, with the scan on


def test_the_summary_follows_the_scan_policy_and_bypass(
    repo: Path, cli: test_ui_actions.Cli
) -> None:
    b = Browser(repo)
    form = item_form(level="2", scan="on", scan_failures="block")
    assert "they block the run until fixed" in b.post("/runs/new/check", form).text
    form = item_form(level="2", scan="on", scan_failures="ignore")
    assert "they&#39;re ignored" in b.post("/runs/new/check", form).text
    page = b.post("/runs/new/check", item_form(bypass_seen="1", bypass="yes")).text
    assert "Blocking questions will be answered by the agent without asking you" in page
    assert shown("full", "on", "ask", "bypass") in page
    page = b.post("/runs/new/check", item_form(bypass_seen="1")).text
    assert "Blocking questions will be answered" not in page


# --------------------------------------------------------------------------- starting


def test_start_always_passes_the_level(repo: Path, cli: test_ui_actions.Cli) -> None:
    item = repo / "work-items" / "server" / "s01.md"
    r = Browser(repo).post("/runs/start", item_form(shown=shown("full")))
    assert redirected_to(r).startswith("/runs/")
    assert cli.args() == [
        "run",
        "--repo",
        str(repo),
        "--detach",
        "--item",
        str(item),
        "--level",
        "full",
    ]


@pytest.mark.parametrize("level", LEVEL_ORDER)
def test_start_passes_each_level_by_name(
    repo: Path, level: Level, cli: test_ui_actions.Cli
) -> None:
    scan = "on" if level is Level.FULL else "off"
    form = item_form(level=str(level_number(level)), shown=shown(level.value, scan))
    redirected_to(Browser(repo).post("/runs/start", form))
    args = cli.args()
    assert args[args.index("--level") + 1] == level.value
    # The level's own scan default isn't passed: only a choice made here is.
    assert not {"--security-scan", "--no-security-scan"} & set(args)


def test_start_passes_the_scan_flag_only_when_chosen(repo: Path, cli: test_ui_actions.Cli) -> None:
    b = Browser(repo)
    redirected_to(b.post("/runs/start", item_form(level="1", scan="on", shown=shown("basic"))))
    assert "--security-scan" in cli.args()
    cli.calls.clear()
    form = item_form(level="5", scan="off", shown=shown("full", "off"))
    redirected_to(b.post("/runs/start", form))
    assert "--no-security-scan" in cli.args()
    cli.calls.clear()
    redirected_to(b.post("/runs/start", item_form(level="5", scan="default", shown=shown("full"))))
    assert not {"--security-scan", "--no-security-scan"} & set(cli.args())


def test_start_passes_the_scan_policy_only_when_it_differs(
    repo: Path, cli: test_ui_actions.Cli
) -> None:
    b = Browser(repo)
    form = item_form(level="2", scan="on", scan_failures="ask", shown=shown("reviewer", "on"))
    redirected_to(b.post("/runs/start", form))
    assert "--scan-failures" not in cli.args()  # "Ask me" is the default
    cli.calls.clear()
    form = item_form(
        level="2", scan="on", scan_failures="block", shown=shown("reviewer", "on", "block")
    )
    redirected_to(b.post("/runs/start", form))
    args = cli.args()
    assert args[args.index("--scan-failures") + 1] == "block"


def test_start_passes_bypass_only_when_ticked(repo: Path, cli: test_ui_actions.Cli) -> None:
    b = Browser(repo)
    form = item_form(shown=shown("full", "on", "ask", "bypass"), bypass_seen="1", bypass="yes")
    redirected_to(b.post("/runs/start", form))
    assert "--bypass-questions" in cli.args()
    cli.calls.clear()
    form = item_form(shown=shown("full"), bypass_seen="1")  # the box was in the form, unticked
    redirected_to(b.post("/runs/start", form))
    assert not {"--bypass-questions", "--no-bypass-questions"} & set(cli.args())


def test_settings_from_the_work_item_are_the_dialogs_starting_point(
    repo: Path, cli: test_ui_actions.Cli
) -> None:
    write_item(
        repo,
        "careful.md",
        "level: adversarial\nsecurity_scan: yes\nscan_failures: block\nbypass_questions: yes",
    )
    b = Browser(repo)
    page = b.get("/runs/new?item=server/careful.md").text
    assert slider_value(page) == "4"
    assert re.search(r'name="scan_failures" value="block" checked', page)
    assert re.search(r'name="bypass" value="yes" checked', page)
    summary = b.post("/runs/new/check", {"source": "item", "item": "server/careful.md"}).text
    assert 'name="shown" value="adversarial;on;block;bypass"' in summary
    # Starting with everything as the item has it passes only the level...
    form = {
        "source": "item", "item": "server/careful.md", "budget": "7.50", "level": "4",
        "scan_failures": "block", "bypass_seen": "1", "bypass": "yes",
        "shown": "adversarial;on;block;bypass",
    }  # fmt: skip
    redirected_to(b.post("/runs/start", form))
    args = cli.args()
    assert args[args.index("--level") + 1] == "adversarial"
    assert not {"--scan-failures", "--bypass-questions", "--security-scan"} & set(args)
    # ...and unticking the box says so.
    cli.calls.clear()
    form = {k: v for k, v in form.items() if k != "bypass"} | {"shown": "adversarial;on;block;ask"}
    redirected_to(b.post("/runs/start", form))
    assert "--no-bypass-questions" in cli.args()


def test_start_refuses_settings_nobody_saw(repo: Path, cli: test_ui_actions.Cli) -> None:
    b = Browser(repo)
    for form in (
        item_form(level="1", shown=shown("full")),  # the slider moved after the summary
        item_form(level="5", scan="off", shown=shown("full", "on")),
        item_form(bypass_seen="1", bypass="yes", shown=shown("full")),
        item_form(level="5", shown=""),
        item_form(level="5"),  # nothing confirmed at all
    ):
        r = b.post("/runs/start", form)
        assert r.status_code == 422, form
        assert "not what the summary showed" in r.text
    assert cli.calls == []


@pytest.mark.parametrize("level", ["0", "6", "-1", "abc", "1.5", "3; rm -rf /", "full\n"])
def test_start_refuses_a_bad_level(repo: Path, cli: test_ui_actions.Cli, level: str) -> None:
    r = Browser(repo).post("/runs/start", item_form(level=level, shown=shown("full")))
    # A name is fine ("full\n" is stripped); anything that isn't a level is refused unstarted.
    if level.strip() == "full":
        assert r.status_code == 200
        return
    assert r.status_code == 422 and "The level must be 1 (Basic) to 5 (Full)" in r.text
    assert cli.calls == []


def test_start_refuses_a_bad_scan_or_policy(repo: Path, cli: test_ui_actions.Cli) -> None:
    b = Browser(repo)
    r = b.post("/runs/start", item_form(scan="maybe", shown=shown("full")))
    assert r.status_code == 422 and "The security scan must be Default, On or Off" in r.text
    r = b.post("/runs/start", item_form(scan_failures="shrug", shown=shown("full")))
    assert r.status_code == 422 and "Ask me, Ignore or Block" in r.text
    assert cli.calls == []


def test_the_new_form_needs_the_token_and_csrf(repo: Path, cli: test_ui_actions.Cli) -> None:
    anon = test_ui_actions.TestClient(
        test_ui_actions.make_app(repo), base_url=test_ui_actions.BASE, follow_redirects=False
    )
    form = item_form(level="1", shown=shown("basic"))
    assert anon.post("/runs/start", data=form).status_code == 401
    client = test_ui_actions.logged_in(repo)
    assert client.post("/runs/start", data=form).status_code == 403
    assert cli.calls == []


# --------------------------------------------------------------------------- the editor


def editor_form(**extra: str) -> dict[str, str]:
    return {"path": "server/new.md", "name": "", "config": "", "body": "Do it.", **extra}


def test_the_editor_round_trips_the_run_settings(repo: Path) -> None:
    b = Browser(repo)
    settings = {
        "level": "tests", "security_scan": "yes", "scan_failures": "ignore",
        "bypass_questions": "no",
    }  # fmt: skip
    r = b.post("/items/new", editor_form(**settings))
    assert r.status_code == 200, r.text
    text = (repo / "work-items" / "server" / "new.md").read_text()
    assert text == (
        "---\nlevel: tests\nsecurity_scan: yes\nscan_failures: ignore\nbypass_questions: no\n---\n"
        "Do it.\n"
    )
    page = b.get("/items/edit?path=server/new.md").text
    assert '<option value="tests" selected>3. Tests</option>' in page
    assert '<option value="yes" selected>On</option>' in page
    assert '<option value="ignore" selected>Ignore</option>' in page
    assert '<option value="no" selected>No: ask me</option>' in page


def test_inherit_leaves_the_keys_out(repo: Path) -> None:
    b = Browser(repo)
    path = repo / "work-items" / "server" / "s01.md"
    write_item(repo, "s03.md", "level: basic\nsecurity_scan: no\nname: keep-me", "Body.")
    r = b.post(
        "/items/save",
        {
            "path": "server/s03.md", "name": "keep-me", "config": "", "body": "Body.",
            "level": "inherit", "security_scan": "inherit", "scan_failures": "inherit",
            "bypass_questions": "inherit",
        },
    )  # fmt: skip
    assert r.status_code == 200, r.text
    assert (
        repo / "work-items" / "server" / "s03.md"
    ).read_text() == "---\nname: keep-me\n---\nBody.\n"
    assert path.read_text()  # untouched


def test_a_form_without_the_settings_keeps_the_files(repo: Path) -> None:
    write_item(repo, "s04.md", "level: basic\nbypass_questions: yes", "Body.")
    r = Browser(repo).post(
        "/items/save", {"path": "server/s04.md", "name": "", "config": "", "body": "Body, edited."}
    )
    assert r.status_code == 200, r.text
    text = (repo / "work-items" / "server" / "s04.md").read_text()
    assert text == "---\nlevel: basic\nbypass_questions: yes\n---\nBody, edited.\n"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("level", "7"),
        ("level", "everything"),
        ("security_scan", "perhaps"),
        ("scan_failures", "panic"),
        ("bypass_questions", "sure"),
    ],
)
def test_the_editor_checks_the_run_settings(repo: Path, field: str, value: str) -> None:
    b = Browser(repo)
    r = b.post("/items/new", editor_form(**{field: value}))
    assert r.status_code == 422 and "fix the problems marked below" in r.text
    assert not (repo / "work-items" / "server" / "new.md").exists()
    preview = b.post("/items/preview", {"body": "Do it.", field: value}).text
    assert "check-error" in preview


def test_the_editor_warns_about_bypass(repo: Path) -> None:
    page = Browser(repo).post("/items/preview", {"body": "x", "bypass_questions": "yes"}).text
    assert "never stop for blocking questions" in page


# --------------------------------------------------------------------------- the run page


def scripts_for(level: Level) -> dict[Role, list[Step]]:
    full = happy_scripts()
    solo: dict[Role, list[Step]] = {Role.SOLO: [SOLO_OK]}
    return {
        Level.BASIC: solo,
        Level.REVIEWER: {**solo, Role.CODE_CRITIC: [verdict_pass()]},
        Level.TESTS: {
            r: full[r] for r in (Role.SPEC_WRITER, Role.TEST_AUTHOR, Role.CODER, Role.CODE_CRITIC)
        },
        Level.ADVERSARIAL: {
            r: full[r]
            for r in (
                Role.SPEC_WRITER,
                Role.TEST_AUTHOR,
                Role.TEST_CRITIC,
                Role.CODER,
                Role.CODE_CRITIC,
            )
        },
        Level.FULL: full,
    }[level]


def run_at(config: Config, repo: Path, level: Level, run_id: str = "r1") -> Path:
    cfg = config.model_copy(update={"pipeline": Pipeline(level=level)})
    runner = FakeAgentRunner(scripts_for(level))
    state = asyncio.run(Orchestrator(cfg, runner, repo, "add clamp", run_id, name=run_id).run())
    assert state.status is Status.COMPLETE, (level, state.notes, state.open_findings)
    return repo.resolve()


LABELS = {
    Level.BASIC: ["SOLO", "FINAL CHECKS"],
    Level.REVIEWER: ["SOLO", "REVIEW", "FINAL CHECKS"],
    Level.TESTS: ["SPEC", "TESTS", "CODE", "REVIEW", "FINAL CHECKS"],
    Level.ADVERSARIAL: ["SPEC", "TESTS", "CODE", "ARBITER", "FINAL CHECKS"],
    Level.FULL: ["SPEC", "TESTS", "CODE", "REVIEW", "ARBITER", "FINAL CHECKS"],
}


@pytest.mark.parametrize("level", LEVEL_ORDER)
def test_the_timeline_shows_only_the_levels_steps(
    config: Config, target_repo: Path, level: Level
) -> None:
    run_at(config, target_repo, level)
    page = Browser(target_repo).get("/runs/r1").text
    assert list(steps_of(page)) == LABELS[level]
    assert [s.value for s in steps_for(level)] == [
        "FINAL_GATES" if label == "FINAL CHECKS" else label for label in LABELS[level]
    ]
    assert "not reached" not in steps_of(page).values()
    # The header says which level and what it leaves out.
    assert f"{LEVEL_INFO[level].label} ({level_number(level)} of 5)" in page
    assert ("Not done at this level" in page) is (level is not Level.FULL)
    assert f"security scan {'on' if level is Level.FULL else 'off'}" in page


def test_solo_passes_render(config: Config, target_repo: Path) -> None:
    run_at(config, target_repo, Level.REVIEWER)
    page = Browser(target_repo).get("/runs/r1").text
    assert steps_of(page) == {"SOLO": "done", "REVIEW": "done", "FINAL CHECKS": "done"}
    assert "1 pass" in page and ">Pass 1<" in page
    assert "What the solo agent replied" in page and "solo done" in page
    assert "<code>task.md</code> as this pass left it" in page
    # The single review is one code-critic pass, with the diff it judged.
    assert "Single review" in page and "code critic (one review pass)" in page
    assert "there&#39;s no second round" in page


def test_rounds_without_a_critic_say_so(config: Config, target_repo: Path) -> None:
    run_at(config, target_repo, Level.TESTS)
    page = Browser(target_repo).get("/runs/r1").text
    assert page.count("No critic at this level: the checks decide.") == 2  # tests and code
    assert "Single review" in page


def test_full_rounds_do_not_say_no_critic(config: Config, target_repo: Path) -> None:
    run_at(config, target_repo, Level.FULL)
    page = Browser(target_repo).get("/runs/r1").text
    assert "No critic at this level" not in page and "Single review" not in page


def test_the_runs_list_shows_the_level_chip(config: Config, target_repo: Path) -> None:
    run_at(config, target_repo, Level.BASIC, "r-basic")
    run_at(config, target_repo, Level.FULL, "r-full")
    page = Browser(target_repo).get("/").text
    assert "Basic (1 of 5)" in page and "Full (5 of 5)" in page


def test_the_context_tab_shows_only_the_levels_roles(config: Config, target_repo: Path) -> None:
    run_at(config, target_repo, Level.BASIC)
    page = Browser(target_repo).get("/runs/r1/context").text
    prompts = page[page.index('id="ctx-prompts"') :]
    assert prompts.count('id="role-') == 1 and 'id="role-solo"' in prompts
    assert "<strong>Gets in its prompt:</strong> the work item and the check commands" in prompts
    # No sentence about critics where there are none.
    assert "The solo agent only" in page
    assert "critic" not in page[page.index('id="ctx-task"') : page.index('id="ctx-config"')].lower()


def test_the_context_tab_at_adversarial_has_no_reviewers(config: Config, target_repo: Path) -> None:
    run_at(config, target_repo, Level.ADVERSARIAL)
    page = Browser(target_repo).get("/runs/r1/context").text
    prompts = page[page.index('id="ctx-prompts"') :]
    for role in roles_for(Level.ADVERSARIAL):
        assert f'id="role-{role.value}"' in prompts
    assert 'id="role-standards-reviewer"' not in prompts
    assert 'id="role-solo"' not in prompts
    assert "no standards reviewer" in page


# --------------------------------------------------------------------------- help


def test_help_has_the_levels(repo: Path) -> None:
    page = Browser(repo).get("/help").text
    assert 'id="levels"' in page and 'href="#levels"' in page
    for level in LEVEL_ORDER:
        info = LEVEL_INFO[level]
        assert f"{level_number(level)}. {info.label}" in page
        assert str(escape(info.good_for)) in page
        assert str(escape(info.description)) in page
    # The rules table.
    for rule in ("Tests before code", "Critics get artifacts, not reasoning"):
        assert rule in page
    assert "✘ one agent" in page and "n/a" in page
    # The solo agent, Bypass, the scan policy and decision points are explained.
    assert "<strong>solo</strong>" in page
    for word in ("Bypass", "Begin work?", "take over", "ignored by you"):
        assert word in page
    for status in ("needs a decision", "handed over", "ended"):
        assert status in page
    assert 'id="asks"' in page


# --------------------------------------------------------------------------- no inline code


def test_the_new_pages_have_no_inline_script_or_style(repo: Path) -> None:
    b = Browser(repo)
    pages = [
        b.get("/runs/new?item=server/s01.md").text,
        b.get("/help").text,
        b.get("/items/new").text,
        b.post("/runs/new/check", item_form(level="2")).text,
    ]
    for page in pages:
        assert not re.search(r"<script(?![^>]*\bsrc=)", page), "inline script"
        assert " style=" not in page and "<style" not in page, "inline style"
        assert not re.search(r"\son(click|input|change)=", page), "inline handler"
