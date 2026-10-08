"""Make the web UI screenshots in docs/screenshots/ (used by the README).

Builds a demo repo in a temporary folder with runs made by the orchestrator and the fake agent
runner from tests/fakes.py (no API calls), serves the UI on 127.0.0.1, and takes the shots with
headless Chromium. Needs the `ui` and `dev` extras and a `chromium` on PATH.

    .venv/bin/python scripts/ui_screenshots.py [--out docs/screenshots] [--keep]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import uvicorn  # noqa: E402
from fakes import (  # noqa: E402
    IMPL_OK,
    TASK_MD,
    TASK_MD_BLOCKING,
    FakeAgentRunner,
    author_response,
    finding,
    happy_scripts,
    interrupt,
    verdict_fail,
    verdict_pass,
    writes,
)

from advpipe.config import Config, Gates, Limits  # noqa: E402
from advpipe.models import RunState, Stage, Status  # noqa: E402
from advpipe.orchestrator import Orchestrator  # noqa: E402
from advpipe.runlog import RunLog  # noqa: E402
from advpipe.runner import Role  # noqa: E402
from advpipe.ui.app import create_ui_app  # noqa: E402

TOKEN = "screenshot-token"
PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"

ITEMS = {
    "server/s01-clamp.md": "---\nname: s01-clamp\n---\nAdd clamp(x, lo, hi) to mathutils.\n",
    "server/s02-swap.md": "Decide what clamp does when lo > hi.\n",
    "mobile/m01-sign.md": "---\nname: m01-sign\n---\nAdd sign(x) to mathutils.\n",
}


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=demo", "-c", "user.email=demo@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def make_repo(root: Path) -> Path:
    repo = root / "home-inventory"
    fixture = ROOT / "tests" / "fixtures" / "sample_repo"
    shutil.copytree(fixture, repo, ignore=shutil.ignore_patterns("__pycache__", ".*_cache"))
    for rel, text in ITEMS.items():
        (repo / "work-items" / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / "work-items" / rel).write_text(text)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "baseline")
    (repo / "work-items" / "server" / "s02-swap.md").write_text(
        "Decide what clamp does when lo > hi: swap them, or raise.\n"
    )
    return repo


async def make_runs(repo: Path) -> None:
    config = Config(
        gates=Gates(
            test=[sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            types=[],
            lint=[],
            security=[],
        ),
        limits=Limits(budget_usd_per_task=8.0),
    )
    item = str(repo / "work-items" / "server" / "s01-clamp.md")

    # A run that needed a second coding round, then passed everything.
    scripts = happy_scripts()
    issue = finding(
        "F1",
        claim="clamp(5, 10, 0) returns 10 instead of raising ValueError, as AC3 requires.",
        criterion="AC3",
    )
    scripts[Role.CODE_CRITIC] = [verdict_fail(issue), verdict_pass()]
    scripts[Role.CODER] = [
        writes(IMPL_OK, "Implemented clamp in mathutils/core.py."),
        writes(IMPL_OK, author_response(**{"F1": ("fixed", "clamp now raises when lo > hi")})),
    ]
    await Orchestrator(
        config,
        FakeAgentRunner(scripts, cost_per_call=0.21),
        repo,
        ITEMS["server/s01-clamp.md"].split("---\n")[-1],
        "20261008-091500-clamp",
        name="s01-clamp",
        work_item_file=item,
    ).run()

    # A run whose spec has a blocking question: it stops for a person.
    scripts = happy_scripts()
    scripts[Role.SPEC_WRITER] = [writes({"task.md": TASK_MD_BLOCKING}, "wrote task.md")]
    await Orchestrator(
        config,
        FakeAgentRunner(scripts, cost_per_call=0.33),
        repo,
        "Decide what clamp does when lo > hi.",
        "20261008-101200-swap",
        name="s02-swap",
        work_item_file=str(repo / "work-items" / "server" / "s02-swap.md"),
    ).run()

    # A run that ran out of budget, written directly.
    log = RunLog.for_run(repo, "20261008-103000-sign")
    log.write_state(
        RunState(
            run_id="20261008-103000-sign",
            repo=str(repo),
            work_item="Add sign(x) to mathutils.",
            status=Status.BUDGET_EXCEEDED,
            stage=Stage.CODE,
            branch="advpipe/m01-sign",
            cost_usd=2.04,
        )
    )
    log.write_config(Config(limits=Limits(budget_usd_per_task=2.0)))

    # A run still going: interrupted mid-CODE, with this process holding its lock.
    scripts = happy_scripts()
    scripts[Role.SPEC_WRITER] = [writes({"task.md": TASK_MD.replace("clamp", "clamp_all")})]
    scripts[Role.CODE_CRITIC] = [interrupt]
    with contextlib.suppress(asyncio.CancelledError):
        await Orchestrator(
            config,
            FakeAgentRunner(scripts, cost_per_call=0.27),
            repo,
            "Add clamp_all(values, lo, hi), clamping every value in a list.",
            "20261008-110500-clamp-all",
            name="s03-clamp-all",
        ).run()
    RunLog.for_run(repo, "20261008-110500-clamp-all").acquire_lock(os.getpid())


def serve(repo: Path) -> uvicorn.Server:
    app = create_ui_app(repo, Path("work-items"), TOKEN)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            urllib.request.urlopen(f"{BASE}/static/app.css")  # 401 means it's up
        except urllib.error.HTTPError:
            return server
        except OSError:
            time.sleep(0.1)
    raise SystemExit("the UI didn't start")


def shoot(url: str, out: Path, width: int, height: int, *, dark: bool) -> None:
    sep = "&" if "?" in url else "?"
    with tempfile.TemporaryDirectory() as profile:
        args = [
            "chromium",
            "--headless",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--user-data-dir={profile}",
            f"--window-size={width},{height}",
            "--virtual-time-budget=3000",
            f"--screenshot={out}",
        ]
        if dark:
            args.append("--force-dark-mode")
        subprocess.run([*args, f"{BASE}{url}{sep}token={TOKEN}"], check=True, capture_output=True)
    print(f"wrote {out}")


SHOTS = [
    # (file name, path, width, height, dark)
    ("runs.png", "/", 1280, 520, False),
    ("run-detail.png", "/runs/20261008-091500-clamp", 1280, 1400, False),
    ("run-needs-you.png", "/runs/20261008-101200-swap", 1280, 900, False),
    ("spec-context.png", "/runs/20261008-091500-clamp/context", 1280, 1100, False),
    ("work-items.png", "/items", 1280, 640, False),
    ("item-editor.png", "/items/edit?path=server/s01-clamp.md", 1280, 900, False),
    ("new-run.png", "/runs/new?item=server/s01-clamp.md", 1280, 900, False),
    ("help.png", "/help", 1280, 1100, False),
    ("runs-dark.png", "/", 1280, 520, True),
    ("run-detail-tablet-dark.png", "/runs/20261008-091500-clamp", 768, 1300, True),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "screenshots")
    parser.add_argument("--keep", action="store_true", help="keep the demo repo and print it")
    parser.add_argument("--only", nargs="*", help="file names to make (default: all)")
    parser.add_argument(
        "--width", type=int, help="take every shot at this width instead (to check a layout)"
    )
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="advpipe-demo-"))
    try:
        repo = make_repo(root)
        asyncio.run(make_runs(repo))
        server = serve(repo)
        for name, url, width, height, dark in SHOTS:
            if args.only and name not in args.only:
                continue
            if args.width:
                name = f"{Path(name).stem}-{args.width}.png"
                height, width = height * width // args.width, args.width
            shoot(url, args.out / name, width, height, dark=dark)
        server.should_exit = True
    finally:
        if args.keep:
            print(f"demo repo: {repo}")
        else:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
