"""The web UI (U4): work items. The list, the editor, rename and delete, on real files."""

from __future__ import annotations

import html
import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from conftest import git  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from httpx import Response  # noqa: E402
from test_ui import BASE, TOKEN, anon, csrf_of, fake_run, logged_in, make_app  # noqa: E402
from test_ui_detail import EVIL, no_raw_html  # noqa: E402

from advpipe.models import Stage, Status  # noqa: E402
from advpipe.ui.items import ItemPathError, item_path, load_items  # noqa: E402
from advpipe.workitem import WorkItem, load_work_item  # noqa: E402

ODD = "---\nconfig:   pipeline.toml\n\nname: s03-stock\n---\n\n# Stock changes\n\nTrack them.\n\n"


@pytest.fixture
def items_dir(target_repo: Path) -> Path:
    path = target_repo / "work-items"
    path.mkdir()
    return path


class Browser:
    """A logged-in client that sends the CSRF header the way htmx does."""

    def __init__(self, repo: Path) -> None:
        self.client = logged_in(repo)
        self.csrf = csrf_of(self.client.get("/items").text)

    def get(self, url: str) -> Response:
        return self.client.get(url)

    def post(self, url: str, data: dict[str, str]) -> Response:
        return self.client.post(url, data=data, headers={"X-CSRF-Token": self.csrf})


def form_of(page: str) -> dict[str, str]:
    """The editor's field values as a browser would send them (textarea lines as CRLF)."""
    fields = {
        name: html.unescape(value)
        for name, value in re.findall(r'<input[^>]* name="(\w+)" value="([^"]*)"', page)
    }
    body = re.search(r'<textarea name="body"[^>]*>(.*?)</textarea>', page, re.S)
    assert body, "no textarea"
    fields["body"] = html.unescape(body.group(1)).replace("\n", "\r\n")
    return fields


def redirected_to(r: Response) -> str:
    assert r.status_code == 200, r.text
    return r.headers["hx-redirect"]


# --------------------------------------------------------------------------- the round trip


def test_create_edit_rename_delete_round_trip(target_repo: Path, items_dir: Path) -> None:
    b = Browser(target_repo)

    # Create, in a folder that doesn't exist yet; ".md" is added.
    r = b.post(
        "/items/new",
        {"path": "server/s03-stock", "name": "s03-stock", "config": "", "body": "Add stock.\r\n"},
    )
    assert redirected_to(r) == "/items/edit?path=server%2Fs03-stock.md&notice=created"
    file = items_dir / "server" / "s03-stock.md"
    assert file.read_text() == "---\nname: s03-stock\n---\nAdd stock.\n"
    assert load_work_item(file) == WorkItem(body="Add stock.", name="s03-stock")

    page = b.get("/items/edit?path=server%2Fs03-stock.md&notice=created").text
    assert "Created." in page and "Not run yet." in page
    form = form_of(page)
    assert form["path"] == "server/s03-stock.md" and form["name"] == "s03-stock"

    # Edit: new body and a config.
    (target_repo / "pipeline.server.toml").write_text("[limits]\nbudget_usd_per_task = 4.0\n")
    form.update(config="pipeline.server.toml", body="Add stock.\r\n\r\nWith history.\r\n")
    r = b.post("/items/save", form)
    assert redirected_to(r) == "/items/edit?path=server%2Fs03-stock.md&notice=saved"
    assert file.read_text() == (
        "---\nname: s03-stock\nconfig: pipeline.server.toml\n---\nAdd stock.\n\nWith history.\n"
    )
    assert "Saved." in b.get("/items/edit?path=server%2Fs03-stock.md&notice=saved").text

    # Rename into another folder.
    assert 'value="server/s03-stock.md"' in b.get("/items/rename?path=server/s03-stock.md").text
    r = b.post("/items/rename", {"path": "server/s03-stock.md", "new_path": "done/s03"})
    assert redirected_to(r) == "/items/edit?path=done%2Fs03.md&notice=renamed"
    moved = items_dir / "done" / "s03.md"
    assert not file.exists() and moved.read_text().endswith("With history.\n")

    # Delete asks first, then deletes.
    ask = b.get("/items/delete?path=done/s03.md")
    assert ask.status_code == 200
    assert "Delete <code>done/s03.md</code>?" in ask.text
    assert "can't be undone" in ask.text  # never committed
    assert 'name="confirm" value="yes"' in ask.text
    assert moved.exists()  # asking deleted nothing
    r = b.post("/items/delete", {"path": "done/s03.md", "confirm": "yes"})
    assert redirected_to(r) == "/items?deleted=done%2Fs03.md"
    assert not moved.exists()
    listing = b.get("/items?deleted=done%2Fs03.md").text
    assert "Deleted <code>done/s03.md</code>." in listing


def test_delete_without_confirming_deletes_nothing(target_repo: Path, items_dir: Path) -> None:
    (items_dir / "a.md").write_text("A\n")
    b = Browser(target_repo)
    for data in ({"path": "a.md"}, {"path": "a.md", "confirm": "no"}):
        assert b.post("/items/delete", data).status_code == 400
    assert (items_dir / "a.md").exists()
    # The list and the editor only link to the question, never straight to the deletion.
    for page in (b.get("/items").text, b.get("/items/edit?path=a.md").text):
        assert 'href="/items/delete?path=a.md"' in page
        assert 'hx-post="/items/delete"' not in page


def test_delete_page_says_what_git_still_has(target_repo: Path, items_dir: Path) -> None:
    (items_dir / "kept.md").write_text("Kept\n")
    (items_dir / "edited.md").write_text("Edited\n")
    git(target_repo, "add", "-A")
    git(target_repo, "commit", "-q", "-m", "items")
    (items_dir / "edited.md").write_text("Edited again\n")
    b = Browser(target_repo)
    assert "git checkout -- work-items/kept.md" in b.get("/items/delete?path=kept.md").text
    assert "aren't committed" in b.get("/items/delete?path=edited.md").text


# --------------------------------------------------------------------------- front matter


def test_front_matter_round_trips_unchanged(target_repo: Path, items_dir: Path) -> None:
    # Odd but valid layout: keys out of order, extra spaces and blank lines.
    file = items_dir / "s03.md"
    file.write_text(ODD)
    b = Browser(target_repo)
    form = form_of(b.get("/items/edit?path=s03.md").text)
    assert (form["name"], form["config"]) == ("s03-stock", "pipeline.toml")
    assert form["body"] == "# Stock changes\r\n\r\nTrack them."

    r = b.post("/items/save", form)
    assert redirected_to(r).endswith("notice=unchanged")
    assert file.read_text() == ODD  # byte for byte

    # A canonical file survives an edit-save cycle too, and a real change is written.
    form["body"] += "\r\nMore."
    redirected_to(b.post("/items/save", form))
    text = file.read_text()
    assert text == "---\nname: s03-stock\nconfig: pipeline.toml\n---\n" + (
        "# Stock changes\n\nTrack them.\nMore.\n"
    )
    again = form_of(b.get("/items/edit?path=s03.md").text)
    assert redirected_to(b.post("/items/save", again)).endswith("notice=unchanged")
    assert file.read_text() == text


def test_clearing_a_field_removes_it(target_repo: Path, items_dir: Path) -> None:
    file = items_dir / "s03.md"
    file.write_text(ODD)
    b = Browser(target_repo)
    form = form_of(b.get("/items/edit?path=s03.md").text)
    form.update(name="", config="")
    redirected_to(b.post("/items/save", form))
    assert file.read_text() == "# Stock changes\n\nTrack them.\n"


def test_a_broken_file_opens_and_saves_fixed(target_repo: Path, items_dir: Path) -> None:
    file = items_dir / "bad.md"
    file.write_text("---\nname: s03\ncolour: red\n---\nBody text.\n")
    b = Browser(target_repo)
    listing = b.get("/items").text
    assert "Can't be run as it is" in listing and "unknown key" in listing
    page = b.get("/items/edit?path=bad.md").text
    assert "this file can't be run" in page and "colour" in page
    form = form_of(page)
    assert form["name"] == "s03" and form["body"] == "Body text."
    redirected_to(b.post("/items/save", form))
    assert file.read_text() == "---\nname: s03\n---\nBody text.\n"


def test_save_refuses_to_overwrite_changes_made_elsewhere(
    target_repo: Path, items_dir: Path
) -> None:
    file = items_dir / "a.md"
    file.write_text("First\n")
    b = Browser(target_repo)
    form = form_of(b.get("/items/edit?path=a.md").text)
    file.write_text("Changed in a text editor\n")
    form["body"] = "Mine"
    r = b.post("/items/save", form)
    assert r.status_code == 422
    assert "changed on disk since you opened it" in r.text
    assert '<form id="item-form"' in r.text and "Mine" in r.text  # the text isn't lost
    assert file.read_text() == "Changed in a text editor\n"

    file.unlink()
    r = b.post("/items/save", form)
    assert r.status_code == 422 and "deleted or moved" in r.text
    assert not file.exists()


# --------------------------------------------------------------------------- checks


def test_empty_body_is_not_saved(target_repo: Path, items_dir: Path) -> None:
    b = Browser(target_repo)
    r = b.post("/items/new", {"path": "empty", "name": "", "config": "", "body": " \r\n "})
    assert r.status_code == 422
    assert "Not saved" in r.text and "The body is empty" in r.text
    assert not (items_dir / "empty.md").exists()

    (items_dir / "a.md").write_text("A\n")
    form = form_of(b.get("/items/edit?path=a.md").text)
    form["body"] = ""
    assert b.post("/items/save", form).status_code == 422
    assert (items_dir / "a.md").read_text() == "A\n"


def test_multiline_fields_are_not_saved(target_repo: Path, items_dir: Path) -> None:
    b = Browser(target_repo)
    r = b.post("/items/new", {"path": "x", "name": "a\r\ncolour: red", "body": "X"})
    assert r.status_code == 422 and "must be a single line" in r.text
    assert not (items_dir / "x.md").exists()


def test_preview_and_inline_checks(target_repo: Path, items_dir: Path) -> None:
    b = Browser(target_repo)
    git(target_repo, "branch", "advpipe/s03-stock")

    r = b.post("/items/preview", {"name": "s03 stock", "config": "nope.toml", "body": "# Hi"})
    assert r.status_code == 200
    assert "<h1>Hi</h1>" in r.text
    assert "Branch advpipe/s03-stock already exists" in r.text
    assert "advpipe/s03-stock-2" in r.text
    assert "Config file nope.toml doesn&#39;t exist" in r.text

    (target_repo / "broken.toml").write_text("[limits]\nbudget = 1\n")
    r = b.post("/items/preview", {"config": "broken.toml", "body": "Add stock"})
    assert "Config file broken.toml has errors" in r.text
    assert "a run would make <code>advpipe/add-stock</code>" in r.text
    assert "already exists" not in r.text

    r = b.post("/items/preview", {"config": "../outside.toml", "body": "x"})
    assert "outside the repo" in r.text

    r = b.post("/items/preview", {"body": ""})
    assert "The body is empty" in r.text and "Nothing to preview yet" in r.text

    r = b.post("/items/preview", {"body": "x"})
    assert "No config set: runs use advpipe&#39;s defaults" in r.text
    (target_repo / "pipeline.toml").write_text("")
    assert "runs use pipeline.toml" in b.post("/items/preview", {"body": "x"}).text

    # The editor page shows the same checks without waiting for typing.
    (items_dir / "a.md").write_text("---\nconfig: nope.toml\n---\nA\n")
    page = b.get("/items/edit?path=a.md").text
    assert "Config file nope.toml doesn&#39;t exist" in page
    assert 'hx-post="/items/preview"' in page


def test_agent_style_html_in_items_is_escaped(target_repo: Path, items_dir: Path) -> None:
    (items_dir / "evil.md").write_text(f"---\nname: {EVIL}\n---\n# {EVIL}\n\n{EVIL}\n")
    b = Browser(target_repo)
    for page in (
        b.get("/items").text,
        b.get("/items/edit?path=evil.md").text,
        b.post("/items/preview", {"name": EVIL, "config": EVIL, "body": EVIL}).text,
    ):
        no_raw_html(page)
    assert form_of(b.get("/items/edit?path=evil.md").text)["name"] == EVIL


# --------------------------------------------------------------------------- the list


def test_list_groups_by_folder_with_last_run_and_git(target_repo: Path, items_dir: Path) -> None:
    (items_dir / "server").mkdir()
    (items_dir / "mobile").mkdir()
    (items_dir / "top.md").write_text("Top item\n")
    (items_dir / "server" / "s01.md").write_text("# Database\n")
    (items_dir / "server" / "s02.md").write_text("---\nname: items\n---\nItems\n")
    (items_dir / "mobile" / "m01.md").write_text("Mobile one\n")
    (items_dir / "notes.txt").write_text("not an item\n")
    (items_dir / ".hidden").mkdir()
    (items_dir / ".hidden" / "h.md").write_text("hidden\n")
    git(target_repo, "add", "-A")
    git(target_repo, "commit", "-q", "-m", "items")
    (items_dir / "server" / "s02.md").write_text("---\nname: items\n---\nItems, changed\n")
    (items_dir / "server" / "s03.md").write_text("New one\n")

    s01 = str((items_dir / "server" / "s01.md").resolve())
    fake_run(
        target_repo,
        "20261008-010000-aaaa",
        status=Status.NEEDS_HUMAN,
        stage=Stage.ARBITER,
        work_item="Database",
        work_item_file=s01,
    )
    later = fake_run(
        target_repo,
        "20261008-020000-bbbb",
        status=Status.COMPLETE,
        stage=Stage.DONE,
        work_item="Database",
        work_item_file=s01,
    )
    state = later.read_state()
    later.write_state(state.model_copy(update={"started_at": state.started_at.replace(year=2030)}))

    listing = load_items(target_repo, items_dir.resolve())
    assert [folder for folder, _ in listing.groups] == ["", "mobile", "server"]
    rows = {row.rel: row for _, rows in listing.groups for row in rows}
    assert list(rows) == [
        "top.md",
        "mobile/m01.md",
        "server/s01.md",
        "server/s02.md",
        "server/s03.md",
    ]
    assert rows["server/s01.md"].title == "Database"
    assert rows["server/s02.md"].name == "items"
    last = rows["server/s01.md"].last_run
    assert last is not None and last.run_id == "20261008-020000-bbbb"  # the newest run
    assert rows["top.md"].last_run is None
    assert {rel: row.git for rel, row in rows.items()} == {
        "top.md": "",
        "mobile/m01.md": "",
        "server/s01.md": "",
        "server/s02.md": "modified",
        "server/s03.md": "untracked",
    }

    page = Browser(target_repo).get("/items").text
    assert (
        page.index("Top level")
        < page.index("<code>mobile/</code>")
        < page.index("<code>server/</code>")
    )
    assert 'href="/runs/20261008-020000-bbbb" class="chip chip-complete"' in page
    assert "never run" in page and "uncommitted" in page and "not in git" in page
    assert 'href="/items/new?folder=server"' in page
    assert "h.md" not in page and "notes.txt" not in page
    assert '<a href="/items" aria-current="page">Work items</a>' in page


def test_list_empty_states(target_repo: Path) -> None:
    b = Browser(target_repo)
    page = b.get("/items").text
    assert "no <code>work-items/</code> folder in this repo yet" in page
    assert 'href="/items/new"' in page
    (target_repo / "work-items").mkdir()
    assert "No work items in <code>work-items/</code> yet" in b.get("/items").text


def test_new_item_page(target_repo: Path, items_dir: Path) -> None:
    (items_dir / "server").mkdir()
    (target_repo / "pipeline.server.toml").write_text("")
    page = Browser(target_repo).get("/items/new?folder=server").text
    assert 'name="path" value="server/"' in page
    assert '<option value="server/">' in page
    assert '<option value="pipeline.server.toml">' in page
    assert 'hx-post="/items/new"' in page and 'name="base"' not in page


def test_creating_over_an_existing_file_is_refused(target_repo: Path, items_dir: Path) -> None:
    (items_dir / "a.md").write_text("Original\n")
    r = Browser(target_repo).post("/items/new", {"path": "a.md", "body": "New"})
    assert r.status_code == 422 and "already exists" in r.text
    assert (items_dir / "a.md").read_text() == "Original\n"


def test_rename_problems(target_repo: Path, items_dir: Path) -> None:
    (items_dir / "a.md").write_text("A\n")
    (items_dir / "b.md").write_text("B\n")
    b = Browser(target_repo)
    r = b.post("/items/rename", {"path": "a.md", "new_path": "b"})
    assert r.status_code == 422 and "b.md already exists" in r.text
    r = b.post("/items/rename", {"path": "a.md", "new_path": "a.md"})
    assert r.status_code == 422 and "its name already" in r.text
    assert b.post("/items/rename", {"path": "gone.md", "new_path": "c"}).status_code == 404
    assert (items_dir / "a.md").read_text() == "A\n" and (items_dir / "b.md").read_text() == "B\n"


# --------------------------------------------------------------------------- paths and security


@pytest.mark.parametrize(
    "rel",
    [
        "",
        "../secret.md",
        "a/../../secret.md",
        "/etc/secret.md",
        "..",
        ".hidden.md",
        "a/.git/x.md",
        "a\\..\\secret.md",
        "x.txt",
        "x.md/",
        ".md",
        "a//b.md",
        "C:secret.md",
        "a\x00.md",
        "a/b/c/d/e/f.md",
        "%2e%2e/secret.md",
    ],
)
def test_item_path_rejects(tmp_path: Path, rel: str) -> None:
    with pytest.raises(ItemPathError):
        item_path(tmp_path, rel)


def test_item_path_accepts_plain_names(tmp_path: Path) -> None:
    for rel in ("a.md", "server/s03-stock.md", "My task v2.md", "a/b/c/d/e.md", "x_y+z.md"):
        assert item_path(tmp_path, rel) == tmp_path / rel


def test_symlinks_out_of_the_folder_are_refused(target_repo: Path, items_dir: Path) -> None:
    outside = target_repo / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("SECRET\n")
    (items_dir / "link.md").symlink_to(outside / "secret.md")
    (items_dir / "dir").symlink_to(outside, target_is_directory=True)
    (items_dir / "inside.md").write_text("Inside\n")
    (items_dir / "alias.md").symlink_to(items_dir / "inside.md")  # stays inside: fine

    with pytest.raises(ItemPathError):
        item_path(items_dir, "link.md")
    with pytest.raises(ItemPathError):
        item_path(items_dir, "dir/secret.md")
    with pytest.raises(ItemPathError):
        item_path(items_dir, "dir/new.md")
    assert item_path(items_dir, "alias.md").read_text() == "Inside\n"

    b = Browser(target_repo)
    listing = b.get("/items").text
    assert "SECRET" not in listing and "link.md" in listing  # listed as skipped only
    assert "Not shown" in listing
    for url in ("/items/edit?path=link.md", "/items/edit?path=dir/secret.md"):
        r = b.get(url)
        assert r.status_code == 400 and "SECRET" not in r.text
    assert b.post("/items/new", {"path": "dir/new", "body": "x"}).status_code == 422
    assert not (outside / "new.md").exists()
    r = b.post("/items/save", {"path": "link.md", "body": "pwned"})
    assert r.status_code == 400
    assert b.post("/items/delete", {"path": "link.md", "confirm": "yes"}).status_code == 400
    assert (outside / "secret.md").read_text() == "SECRET\n"


def test_traversal_is_rejected_by_every_route(target_repo: Path, items_dir: Path) -> None:
    secret = target_repo / "secret.md"
    secret.write_text("SECRET\n")
    (items_dir / "a.md").write_text("A\n")
    b = Browser(target_repo)
    for query in (
        "../secret.md",
        "%2e%2e%2fsecret.md",
        "%2E%2E/secret.md",
        str(secret),
        "..%5csecret.md",
    ):
        for page in ("edit", "rename", "delete"):
            r = b.get(f"/items/{page}?path={query}")
            assert r.status_code == 400, (page, query)
            assert "SECRET" not in r.text
    for bad in ("../secret.md", str(secret)):
        assert b.post("/items/save", {"path": bad, "body": "pwned"}).status_code == 400
        assert b.post("/items/delete", {"path": bad, "confirm": "yes"}).status_code == 400
        assert b.post("/items/rename", {"path": bad, "new_path": "stolen"}).status_code == 400
        assert b.post("/items/rename", {"path": "a.md", "new_path": bad}).status_code == 422
        r = b.post("/items/new", {"path": bad, "body": "pwned"})
        assert r.status_code == 422 and "File name:" in r.text
    assert secret.read_text() == "SECRET\n"
    assert (items_dir / "a.md").read_text() == "A\n"
    assert not (items_dir / "stolen.md").exists()
    assert sorted(p.name for p in items_dir.iterdir()) == ["a.md"]


def test_missing_items_are_404(target_repo: Path, items_dir: Path) -> None:
    b = Browser(target_repo)
    for page in ("edit", "rename", "delete"):
        assert b.get(f"/items/{page}?path=nope.md").status_code == 404
    assert b.post("/items/delete", {"path": "nope.md", "confirm": "yes"}).status_code == 404


def test_item_routes_need_token_and_csrf(target_repo: Path, items_dir: Path) -> None:
    (items_dir / "a.md").write_text("A\n")
    client = anon(target_repo)
    for url in ("/items", "/items/new", "/items/edit?path=a.md", "/items/delete?path=a.md"):
        assert client.get(url).status_code == 401, url
    posts = {
        "/items/new": {"path": "b", "body": "B"},
        "/items/save": {"path": "a.md", "body": "changed"},
        "/items/preview": {"body": "x"},
        "/items/rename": {"path": "a.md", "new_path": "c"},
        "/items/delete": {"path": "a.md", "confirm": "yes"},
    }
    for url, data in posts.items():
        assert client.post(url, data=data).status_code == 401, url
    # Logged in, but without the CSRF header (a form posted from another site, say).
    session = TestClient(make_app(target_repo), base_url=BASE)
    session.get(f"/?token={TOKEN}")
    for url, data in posts.items():
        assert session.post(url, data=data).status_code == 403, url
        assert session.post(url, data=data, headers={"X-CSRF-Token": "x"}).status_code == 403
    # State changes are POST only.
    for url in ("/items/save", "/items/preview"):
        assert session.get(url).status_code == 405, url
    assert sorted(p.name for p in items_dir.iterdir()) == ["a.md"]
    assert (items_dir / "a.md").read_text() == "A\n"


def test_forms_post_with_htmx_so_csrf_is_sent(target_repo: Path, items_dir: Path) -> None:
    (items_dir / "a.md").write_text("A\n")
    b = Browser(target_repo)
    pages = [
        b.get(u).text
        for u in (
            "/items/new",
            "/items/edit?path=a.md",
            "/items/rename?path=a.md",
            "/items/delete?path=a.md",
        )
    ]
    for page in pages:
        assert 'hx-headers=\'{"X-CSRF-Token": "' in page
        forms = re.findall(r"<form[^>]*>", page)
        assert forms and all("hx-post=" in f for f in forms), forms
    # base.html lets htmx swap in a 422 (a form with its problems) but not other errors.
    assert '{"code": "422", "swap": true}' in pages[0]


def test_oversized_body_is_refused(target_repo: Path, items_dir: Path) -> None:
    r = Browser(target_repo).post("/items/new", {"path": "big", "body": "x" * 300_000})
    assert r.status_code == 422
    assert not (items_dir / "big.md").exists()
