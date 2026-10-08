# advpipe

**Hand it a coding task. Get back a git branch with tests, an implementation, and a review
report, produced by AI agents that check each other's work.**

```sh
advpipe run --repo ~/code/myproject "add a clamp(x, lo, hi) function to mathutils"
```

```
20261008-025103-397075  complete  $0.41  advpipe/add-clamp-x-lo-hi-function
  report: ~/code/myproject/.advpipe/runs/20261008-025103-397075/report.md
```

---

- [What is this?](#what-is-this)
- [How it works (the short version)](#how-it-works-the-short-version)
- [Requirements](#requirements)
- [Install](#install)
- [Quick start](#quick-start)
- [A worked example](#a-worked-example)
- [Reviewing the result](#reviewing-the-result)
- [Commands](#commands)
- [Configuration](#configuration)
- [What it costs](#what-it-costs)
- [When *not* to use it](#when-not-to-use-it)
- [Safety](#safety)
- [Using it inside Claude Code (no Python)](#using-it-inside-claude-code-no-python)
- [Run your checks after every edit (optional hook)](#run-your-checks-after-every-edit-optional-hook)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)
- [Developing advpipe](#developing-advpipe)

---

## What is this?

When you ask a single AI agent to write code, it marks its own homework: it writes the code,
decides it looks fine, and tells you it's done. advpipe doesn't trust that. It splits the job
across several agents with different roles, and makes them **check each other**:

- one agent writes a precise spec with acceptance criteria
- one writes tests from the spec *before any code exists*, and another tries to poke holes in
  those tests
- one writes the code, and another hunts for concrete bugs in it
- two reviewers look at the finished change, one for code quality and one for security
- if agents disagree, an arbiter settles it

Real checks (your test suite, type checker, linter) run between every step, and **a failing
check always wins over an agent's opinion**.

At the end you get a **git branch** with one commit per stage and a **report**. advpipe never
merges, pushes, or opens pull requests. You review the branch and decide.

It's built on the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk) and uses Claude
models.

## How it works (the short version)

```
your task
   │
   ▼
 1. Spec        writes task.md: goal, acceptance criteria (AC1, AC2, ...), scope, open questions
   │            (if a question needs a human answer, stops here, before any code)
   ▼
 2. Tests       test author ⇄ test critic           up to 2 rounds
   │            check: the new tests must exist and must FAIL (nothing is implemented yet)
   ▼
 3. Code        coder ⇄ code critic                 up to 3 rounds
   │            check: tests, type checker and linter all pass
   ▼
 4. Review      standards reviewer + security reviewer, in parallel
   │
   ▼
 5. Arbiter     only if disagreements or problems remain: rules "fix" or "dismiss" on each
   │
   ▼
 6. Done        complete → a branch ready for your review
                otherwise → "needs_human", with the open problems listed in the report
```

Some rules the tool enforces in code, not just by asking the agents nicely:

- **Critics can't edit files.** They don't have editing tools. They can only report problems.
- **Critics only see the work, not the author's explanation of it**, so they can't be talked
  into agreeing.
- **Problems must be specific:** a file and line, or a numbered acceptance criterion. Vague
  complaints are thrown away.
- **The coder can't change the tests.** If it tries, the change is undone and counted against
  it.
- **Everything is bounded:** limited rounds per stage, limited steps per agent, and a dollar
  budget per task.

Want the details? See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Requirements

- **Python 3.11 or newer**
- **git**
- **Access to Claude** through the Claude Agent SDK: an Anthropic API key
  (`ANTHROPIC_API_KEY`) or a signed-in Claude Code installation. See the
  [Agent SDK docs](https://code.claude.com/docs/en/agent-sdk) for the options.
- **A git repository to work on**, ideally with tests. advpipe works best on projects with a
  test suite and a quick way to run it.
- *Optional:* [semgrep](https://semgrep.dev) for the security scan. Without it, the security
  reviewer works from the code changes alone, and the report says so.

## Install

advpipe isn't on PyPI. Install it from source:

```sh
git clone <this-repo-url> advpipe
cd advpipe
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .
advpipe --help
```

With [uv](https://docs.astral.sh/uv/): `uv venv && uv pip install -e .`

## Quick start

1. **Commit or stash your work** in the project you want to change. advpipe starts from your
   current commit, and uncommitted changes aren't included.

2. **Tell advpipe how to check your project.** Create `pipeline.toml` in the project root (copy
   [`pipeline.example.toml`](pipeline.example.toml)) and set the commands that run your tests,
   type checker and linter:

   ```toml
   [gates]
   test  = ["pytest", "-q"]
   types = ["mypy", "."]
   lint  = ["ruff", "check", "."]
   ```

   If your project doesn't use one of these, set it to `[]` to skip it. Then check the commands
   work, before spending anything on agents:

   ```sh
   advpipe gates --repo path/to/project
   ```

3. **Run it:**

   ```sh
   advpipe run --repo path/to/project "describe the change you want"
   ```

   Describe the change the way you'd write a ticket for a colleague. The spec writer turns it
   into precise acceptance criteria. A typical small task takes a few minutes.

   For anything longer than a sentence, write it in a markdown file and pass the file:
   `advpipe run --repo path/to/project --item work-items/s03-stock.md` (see
   [work-item files](#work-item-files)).

4. **Read the report** (its path is printed at the end) and **review the branch** (see
   [Reviewing the result](#reviewing-the-result)).

## A worked example

Take a tiny Python package, `mathutils`, with `mean()` and `safe_div()`, a pytest suite, mypy
and ruff. We ask for a new function:

```sh
advpipe run --repo ./mathutils "add a clamp(x, lo, hi) function"
```

Here's what happened in a real run:

**1. Spec.** The spec writer read the package and its `CLAUDE.md` conventions, then wrote
`task.md` with acceptance criteria such as:

```markdown
- [ ] AC2: When `lo <= x <= hi`, `clamp(x, lo, hi)` returns `x`
- [ ] AC5: Boundaries are inclusive: `clamp(0, 0, 10) == 0` and `clamp(10, 0, 10) == 10`
- [ ] AC8: If `lo > hi`, raises `ValueError` whose message contains both "lo" and "hi"
- [ ] AC10: If `x` is NaN, the result is NaN
```

It picked sensible answers to ambiguous points ("lo > hi raises rather than swapping") and
listed them under *Open questions*. None was marked `BLOCKING:`, so the run continued.

**2. Tests.** The test author wrote `tests/test_clamp.py`, with tests for every acceptance
criterion. advpipe ran pytest and confirmed the new tests **failed**
(`ImportError: cannot import name 'clamp'`), so they really test the new feature. The test
critic returned PASS, with one minor note.

**3. Code.** The coder implemented `clamp` in `mathutils/core.py` and exported it. advpipe
checked that no test file had been touched, then ran pytest, mypy and ruff, and all passed.
The code critic found nothing blocking.

**4. Review.** The standards and security reviewers both returned PASS.

**5. Result:**

```
20261008-025103-397075  complete  $0.41  advpipe/add-clamp-x-lo-hi-function
```

```sh
$ git log --oneline main..advpipe/add-clamp-x-lo-hi-function
1305504 advpipe: implementation
1cc1e52 advpipe: tests
9ebc6ff advpipe: spec (task.md)
```

The report's cost table showed where the money went. Writing the tests (author plus critic)
was the most expensive part, and the spec was next.

## Reviewing the result

Every run leaves a branch and a report. You're the final reviewer. The branch is named after
the task, e.g. "add a clamp(x, lo, hi) function" becomes `advpipe/add-clamp-x-lo-hi-function`, or
you choose it with `--name`. The final summary and the report both show it.

```sh
advpipe report <run-id> --repo path/to/project     # read the report
git -C path/to/project log -p main..advpipe/<branch> # read the change, stage by stage
git -C path/to/project merge advpipe/<branch>       # accept it
```

**What's in the report:**

| Section | Meaning |
|---|---|
| **Status** | `complete`: all checks pass and no blocking problems are open. `needs_human`: something needs you (see below). `budget_exceeded` or `error`: the run stopped early and can usually be [resumed](#commands). |
| **Rounds used** | How many back-and-forth rounds each stage needed. Many rounds is a hint to look closer. |
| **Open blocking findings** | Problems that weren't resolved. Empty on a `complete` run. |
| **Minor findings** | Non-blocking suggestions that were deliberately not acted on. Worth skimming. |
| **Arbiter rulings** | Which disputes were settled and why. |
| **Cost by stage** | Agent calls, dollars and share of the total for each stage. |
| **Notes** | E.g. "security scanner not run", or that a run was resumed. |

**If the status is `needs_human`**, the open findings say why. Common cases:
- **The spec had blocking questions.** Answer them by rewording your task more precisely, then
  start a new run.
- **Checks still failed after the arbiter's final fix pass.** Look at the branch: the remaining
  work is often small.

**Merging:** if you're happy with the branch, merge it the way you normally would. You may want
to delete `task.md` first, or keep it as documentation.

**Cleaning up:** after a `complete` run the working copy is removed automatically, so only the
branch is left. Delete the branch with `git branch -D advpipe/<branch>` when you're done. For a
run that failed or that you don't want, `advpipe clean <run-id>` removes its working copy and
branch (see [`advpipe clean`](#advpipe-clean)). Run logs live in `.advpipe/` in your project.
That folder is excluded from git automatically, and you can delete it at any time.

## Commands

### `advpipe run`

Run one or more tasks.

```sh
advpipe run "task description" [--repo PATH] [--config FILE] [--name NAME] [--keep-worktree]
advpipe run --item work-items/s03-stock.md [--repo PATH] [--detach]
advpipe run --from-file tasks.txt --parallel 3 [--repo PATH]
```

- `--repo`: the project to work on (default: current directory). Must be a git repository.
- `--config`: config file (default: `<repo>/pipeline.toml`, falling back to built-in defaults).
- `--item FILE`: read the task from a markdown file. The file can set defaults for `--name` and
  `--config` in a front-matter block (see [work-item files](#work-item-files)). Prefer this to
  `"$(cat FILE)"`, which silently passes an empty task if the file doesn't exist.
- `--from-file`: run each non-empty line of a file as a separate task. Lines starting with `#`
  are skipped.
- `--parallel N`: with `--from-file`, run up to N tasks at once. Each task gets its own branch,
  working copy and budget, so they don't interfere.
- `--name NAME`: name the branch `advpipe/NAME`. By default the name comes from the task's first
  line, with filler words dropped and cut to 40 characters (`advpipe/fix-refund-rounding-bug`).
  If that branch already exists, `-2`, `-3`, ... is appended.
- `--keep-worktree`: don't delete the working copy after a successful run.
- `--quiet` / `-q`: don't print live progress.
- `--detach`: run in the background. advpipe prints the run id and returns at once; the run
  keeps going even if you close the terminal. Its progress goes to
  `.advpipe/runs/<run-id>/console.log`. Follow it with `advpipe status <run-id>`, stop it with
  `advpipe cancel <run-id>`. Works with a single task (a description or `--item`).

Give exactly one of a task description, `--item` or `--from-file`.

While it runs, advpipe prints timestamped progress to stderr: each stage, each agent starting
and finishing (with its cost and the run total so far), check results, critic verdicts and
arbiter rulings. With several tasks, each line is prefixed with `[item N]`. When the run
finishes, a one-line summary per task goes to stdout, so `advpipe run ... > result.txt` captures
just the summary. The same progress is also written, one JSON object per line, to
`.advpipe/runs/<run-id>/events.jsonl` (see [the run directory](docs/ARCHITECTURE.md#persistence-resume-and-locking)).

Exit code: 0 if every task completed, 1 otherwise, 130 if it was interrupted (Ctrl-C or
`advpipe cancel`). With `--detach`, 0 once the run has started.

#### Work-item files

A work item is a markdown file. Its text is the task. An optional front-matter block at the very
top sets defaults for that task:

```markdown
---
name: s03-stock
config: pipeline.server.toml
---
Add stock level changes with a change history...
```

- `name`: the branch name, as with `--name` (here `advpipe/s03-stock`).
- `config`: the config file, relative to the repo root, as with `--config`.

Only these two keys are allowed, one `key: value` per line; anything else is an error, so typos
don't go unnoticed. Options on the command line win over front matter. A file with no text after
the front matter is rejected. The run remembers which file it came from (`work_item_file` in
`run.json`).

### `advpipe status`

```sh
advpipe status [--repo PATH]             # list all runs
advpipe status <run-id> [--repo PATH]    # details of one run
```

Shows status, current stage and round, cost so far, and which stages are finished. You can run
it in a second terminal while a run is going; `run.json` is updated after every agent call.

### `advpipe report`

```sh
advpipe report <run-id> [--repo PATH]
```

Prints the run's `report.md`.

### `advpipe resume`

```sh
advpipe resume <run-id> [--repo PATH] [--budget USD] [--config FILE] [--quiet]
```

Continue a run that was interrupted (Ctrl-C, crash, closed laptop), stopped by an error, or ran
out of budget. It picks up at the first stage that didn't finish; finished stages aren't redone
or paid for again. Use `--budget` to raise the limit after `budget_exceeded`. Completed runs and
`needs_human` runs can't be resumed. Start a new run instead.

### `advpipe cancel`

```sh
advpipe cancel <run-id> [--repo PATH]
```

Stops a running run, whether it's in another terminal or started with `--detach`. It's the same
as pressing Ctrl-C in the run's terminal: the run stops at its next safe point (when the current
agent call or check is interrupted), keeps its finished stages, and shows as `running` in
`advpipe status`. Continue it later with `advpipe resume <run-id>`. With `--from-file`, all tasks
share one process, so cancelling one cancels them all.

### `advpipe clean`

```sh
advpipe clean <run-id> [--repo PATH] [--force] [--logs]
```

Removes the working copy and the `advpipe/...` branch of a run you don't want, such as one that
failed or stopped at `needs_human`. The run's log stays (with a note) unless you pass `--logs`.

It refuses:

- **complete runs**: their branch is the result. Delete it with `git branch -D` when you're done.
- **active runs**: stop them first with `advpipe cancel`.
- **branches with commits no other branch has**, unless you pass `--force`. It shows the
  `git log` command to look at them first.

### `advpipe ui`

```sh
pip install -e '.[ui]'     # once: the web UI's extra packages
advpipe ui [--repo PATH] [--port 8765] [--items-dir work-items]
```

Serves a small web page for watching runs in your browser. It prints a link like
`http://127.0.0.1:8765/?token=...`: open that. The page lists every run in the repo, newest
first, with its status, stage and round, cost (against the budget while it's running) and age.
It refreshes itself every few seconds, so runs started from a terminal (or with `--detach`) show
up and move along by themselves. Hover over a status to see what it means. Filters show all
runs, running ones, ones that need you (`needs you`, `stopped`, `over budget`, `error`), or
complete ones.

Click a run to open its page:

- **Header:** status (hover for what it means), stage, cost against the budget, time, and the
  branch with a copy button. If the run needs you, *What's left open* lists the findings.
- **Next steps:** the commands to run next, each with a copy button. For a complete run:
  read the change, list the files, merge, delete the branch. Otherwise: resume, cancel or
  clean up. The UI never runs them; you do.
- **Log:** the run's progress lines. While the run is going, new lines appear as they happen,
  and the header and timeline update by themselves.
- **Timeline:** SPEC → TESTS → CODE → REVIEW → ARBITER → FINAL CHECKS, each marked done, in
  progress, stopped here, not yet, or not needed. Open a step to see its rounds: what the
  author replied, the check results (with output), the exact diff the critic judged, and the
  critic's verdict with its findings. SPEC also shows `task.md`; ARBITER shows its rulings.
- **Report:** `report.md`, once the run has finished.

- It only reads the run files. Closing it doesn't affect any run.
- It's for this machine only: it listens on `127.0.0.1`, and every request needs the token from
  the link (it's kept in a cookie after the first visit). A new token is made each time you
  start it. `--host` with anything else is refused unless you also pass
  `--i-know-this-is-exposed`. Don't: the UI will be able to start runs, which spend money and run
  commands.
- One repo per `advpipe ui`. To watch several repos, start one per repo on different ports.
- `--items-dir` is where your work-item files live, relative to the repo. (Managing them from the
  UI comes in a later version; see [docs/UI_PLAN.md](docs/UI_PLAN.md).)

### `advpipe gates`

```sh
advpipe gates [--repo PATH] [--only test,types,lint,security]
```

Runs your configured checks once and prints the results. It exits with code 2 if any fail.
Handy on its own, and used by the [optional hook](#run-your-checks-after-every-edit-optional-hook).

## Configuration

Put a `pipeline.toml` in your project root. Every setting is optional; the defaults are shown.
Misspelled keys are reported as errors rather than silently ignored.

```toml
[models]
author  = "claude-opus-5-5"     # spec writer, test author, coder
critic  = "claude-sonnet-5-5"   # test critic, code critic, both reviewers
arbiter = "claude-opus-5-5"

[limits]
max_rounds_tests    = 2      # test author ⇄ critic rounds
max_rounds_code     = 3      # coder ⇄ critic rounds
max_turns_per_agent = 40     # steps a single agent may take
budget_usd_per_task = 15.0   # hard stop for the whole run
gate_timeout_s      = 600    # max seconds per check command

[gates]
# Commands run from the project root. [] = skip this check.
test     = ["pytest", "-q"]
types    = ["mypy", "."]
lint     = ["ruff", "check", "."]
security = ["semgrep", "--error", "--config", "auto"]   # skipped if not installed

[paths]
tests         = ["tests/"]   # files the coder is not allowed to change
standards_doc = "CLAUDE.md"  # your coding conventions, given to the standards reviewer
```

**Tips:**
- **Gates are just commands**, so any language works: `["npm", "test"]`, `["go", "test", "./..."]`,
  `["cargo", "clippy"]`. If a tool lives in a virtualenv, give its full path, e.g.
  `[".venv/bin/pytest", "-q"]`. Note that each run happens in a fresh checkout under
  `.advpipe/worktrees/`, which won't contain untracked folders like `.venv`, so an absolute
  path is safest.
- **Write down your conventions.** The standards reviewer judges against `standards_doc`, and
  agents also read your project's `CLAUDE.md`. A few lines on naming, error handling and test
  style noticeably improve results.
- **Critics use a different, cheaper model on purpose.** A different model is more likely to
  catch the author's blind spots.

## What it costs

You pay for the Claude API usage of every agent call. A run makes **at least 7 agent calls**
(spec, test author, test critic, coder, code critic, two reviewers). Each extra round adds two
more, and the arbiter adds one or two.

As a reference point, the [worked example](#a-worked-example) (a small, well-defined function,
every stage passing first time) cost **$0.41** and took about two minutes. Expect bigger,
vaguer or more tangled tasks to cost several times that, mostly through extra rounds and agents
reading more code. That's why there's a hard budget per task (default **$15**): the run stops
with `budget_exceeded` the moment it's crossed, and you can resume it with a higher budget if
you want it to finish.

Every report includes a per-stage cost table, so you can see where money goes on your own
projects. Lowering `max_rounds_*` or pointing `author` at a cheaper model are the main levers.

## When *not* to use it

advpipe trades money and time for confidence. That's a bad trade for:

- **Routine changes:** renames, dependency bumps, copy edits, config tweaks. Use a single agent
  plus one quick review, or just do it yourself.
- **Exploratory work** where you don't know what you want yet. The pipeline needs a task that
  can be pinned down to acceptance criteria.
- **Projects without a usable test command.** The whole design leans on tests as ground truth.
  Without them, it's opinions all the way down.
- **Big, multi-part features.** Split them into independent tasks and run them separately
  (maybe with `--parallel`). advpipe handles one well-scoped change per run.
- **Changes needing judgement calls only you can make**, such as product decisions or API design
  trade-offs. The spec writer will flag these as blocking questions and stop, which is the
  right outcome, but you could have skipped the run.

It shines on **well-defined features and bug fixes in tested codebases**, where a second pair
of eyes catches real mistakes.

## Safety

Read this before pointing advpipe at anything important.

- **Agents run shell commands without asking.** The test author and coder can run any shell
  command (normally to run tests or inspect files), with *your* user's permissions, starting in
  the run's working copy. advpipe pre-approves their tools so runs never stall on a prompt. There is no
  sandbox. **Only use advpipe on code you trust, on a machine where that's acceptable.**
- **Your working directory is never modified.** All work happens in a separate git worktree on
  its own branch.
- **Nothing leaves your machine through git.** advpipe never pushes, merges, or opens pull
  requests.
- **Your code is sent to the Claude API** as part of the agents' work, as with any AI coding
  tool.
- **Review before merging.** The pipeline makes your review faster; it doesn't replace it.
- **The web UI is local only.** `advpipe ui` listens on `127.0.0.1` and needs the token it
  prints. Don't expose it to a network.

## Using it inside Claude Code (no Python)

This repository also contains a version of the pipeline that runs *inside* an interactive
[Claude Code](https://code.claude.com) session, with no install:

- `.claude/agents/`: the eight roles as Claude Code subagents
- `.claude/skills/adversarial-build/`: the procedure, as a skill

Copy both folders into your project's `.claude/` directory (or your personal `~/.claude/`),
restart Claude Code, and ask:

```
/adversarial-build add a clamp(x, lo, hi) function
```

The trade-off: in this version, Claude follows the procedure as instructions, so the rules are
only as reliable as the model's instruction-following. The `advpipe` CLI enforces them in code.
Use the skill for quick interactive work, and the CLI when you want the guarantees.

## Run your checks after every edit (optional hook)

Separately from the pipeline, you can have Claude Code run your checks every time it edits a
file during normal sessions. [`examples/claude-hooks.json`](examples/claude-hooks.json) contains
a ready-made hook:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "advpipe gates --repo \"$CLAUDE_PROJECT_DIR\" --only types,lint",
            "timeout": 120
          }
        ]
      }
    ]
  }
}
```

Merge it into `.claude/settings.json` (shared with your team), `.claude/settings.local.json`
(just you), or `~/.claude/settings.json` (all your projects). After each edit, `advpipe gates`
runs your type checker and linter. If one fails, it exits with code 2, and Claude Code shows
the failure output to Claude so it can fix the problem straight away.

`advpipe` must be on the `PATH` that Claude Code sees. Running the full test suite after every
edit is usually too slow, which is why the example uses `--only types,lint`. Adjust it to taste.
If your Claude Code version has no `MultiEdit` tool, leaving it in the matcher does no harm.

## Troubleshooting

**`git worktree add` failed / "not a git repository"**: `--repo` must point inside a git
repository that has at least one commit.

**A check fails with "command not found" (exit 127)**: the command in `[gates]` isn't on your
`PATH`. Use an absolute path. Remember each run happens in a fresh checkout without untracked
folders like `.venv`.

**Every run ends with the tests stage failing "tests don't exercise the change"**: your test
command passed before anything was implemented. Make sure `[gates] test` actually runs the new
test files (check `testpaths` or the equivalent in your test runner's config).

**The run stopped at the spec with `needs_human`**: the spec writer found a question it
shouldn't guess the answer to. It's listed under *Open blocking findings*. Answer it in your
task description and run again.

**`budget_exceeded`**: `advpipe resume <run-id> --budget 30` continues where it stopped.

**I pressed Ctrl-C** (or ran `advpipe cancel`): `advpipe status` shows the run as `running`.
Run `advpipe resume <run-id>`.

**A run started with `--detach` seems stuck**: look at `.advpipe/runs/<run-id>/console.log`
and `advpipe status <run-id>`.

**`unknown key '...' (known keys: name, config)`**: a work-item file's front matter may only
set `name` and `config`.

**"run is still being driven by pid N"**: another advpipe process is working on that run. Wait
for it, or stop it first.

**The security reviewer says no scanner ran**: install semgrep, or set `security = []` to skip
the scan on purpose.

## FAQ

**Which languages does it support?** Any language, as long as your checks can be run as shell
commands. The defaults assume Python.

**Can I see what each agent said?** Yes. `.advpipe/runs/<run-id>/` contains every agent's
output, the exact diff each critic judged (`diff.patch`), each critic's verdict, each round's
check results, and a timeline of events (`events.jsonl`).

**What if one of the generated tests is wrong?** The coder isn't allowed to change tests, so if
a test is itself broken (say, invalid SQL in the test), the code critic reports it as a
`test-defect`. The test author then gets one chance to fix that test, the checks run again, and
the run continues. If the test still fails, the run ends `needs_human` and you fix it by hand.

**Why do agents sometimes "dispute" findings?** An author may push back on a critic, e.g.
"AC3 explicitly allows this". If the critic raises the same point again, the arbiter decides.
Disputes are never argued back and forth; and failing checks can't be disputed at all.

**Can I change how an agent behaves?** Yes. Each role's instructions are in
`src/advpipe/prompts/<role>.md`. Keep `.claude/agents/<role>.md` in sync; a test checks this.

**Does it work offline?** No. The agents run on the Claude API.

## Developing advpipe

```sh
pip install -e '.[dev,ui]'   # without the ui extra, the web UI tests are skipped
pytest                    # the full pipeline is tested with scripted fake agents: no API calls
ruff check . && ruff format --check .
mypy --strict src/
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the code is organised, and
[CLAUDE.md](CLAUDE.md) for the project's design rules.
