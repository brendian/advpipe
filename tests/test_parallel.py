from __future__ import annotations

from pathlib import Path

from conftest import git
from fakes import FakeAgentRunner, happy_scripts, sign_scripts

from advpipe.config import Config
from advpipe.models import Status
from advpipe.orchestrator import Orchestrator, run_many
from advpipe.runner import Role

ITEMS = ["add clamp", "add sign"]


async def run_two(
    config: Config, repo: Path, parallel: int
) -> tuple[list[Orchestrator], list[tuple[str, Role]]]:
    events: list[tuple[str, Role]] = []
    orchestrators: list[Orchestrator] = []

    def make(item: str) -> Orchestrator:
        scripts = happy_scripts() if item == "add clamp" else sign_scripts()
        runner = FakeAgentRunner(scripts, name=item, events=events)
        orch = Orchestrator(config, runner, repo, item)
        orchestrators.append(orch)
        return orch

    states = await run_many(ITEMS, make, parallel)
    assert [s.work_item for s in states] == ITEMS  # results come back in input order
    return orchestrators, events


def tree(repo: Path, branch: str) -> list[str]:
    return git(repo, "ls-tree", "-r", "--name-only", branch).split()


async def test_parallel_runs_do_not_interfere(config: Config, target_repo: Path) -> None:
    orchs, events = await run_two(config, target_repo, parallel=2)
    clamp, sign = (o.state for o in orchs)

    assert clamp.status is Status.COMPLETE, clamp.notes
    assert sign.status is Status.COMPLETE, sign.notes
    assert clamp.run_id != sign.run_id and clamp.branch != sign.branch

    # They really ran concurrently: the second started before the first finished.
    names = [name for name, _ in events]
    assert names.index("add sign") < len(names) - 1 - names[::-1].index("add clamp")

    # Each branch has only its own work.
    clamp_files, sign_files = tree(target_repo, clamp.branch), tree(target_repo, sign.branch)
    assert "tests/test_clamp.py" in clamp_files and "mathutils/sign.py" not in clamp_files
    assert "mathutils/sign.py" in sign_files and "tests/test_clamp.py" not in sign_files
    assert "def clamp" not in git(target_repo, "show", f"{sign.branch}:mathutils/core.py")

    # Separate run logs and budgets; the user's tree is untouched.
    assert orchs[0].runlog.root != orchs[1].runlog.root
    assert "sign" in (orchs[1].runlog.root / "task.md").read_text()
    assert clamp.cost_usd == sign.cost_usd == 0.07
    assert git(target_repo, "status", "--porcelain") == ""
    assert git(target_repo, "branch", "--show-current").strip() == "main"


async def test_parallel_one_runs_sequentially(config: Config, target_repo: Path) -> None:
    orchs, events = await run_two(config, target_repo, parallel=1)
    assert all(o.state.status is Status.COMPLETE for o in orchs)
    names = [name for name, _ in events]
    assert names == sorted(names, key=ITEMS.index)  # all of item 1, then all of item 2
