# Pipeline levels: plan

Today every run goes through the whole pipeline: spec, tests with a critic, code with a critic,
two reviewers, maybe the arbiter. That's right for a risky feature and too much for a one-line
bug fix or a rename. This plan adds a **level** to every run: five steps from one agent doing
everything in one pass, up to the full adversarial pipeline as it is now. In the web UI you pick
the level with a **slider**:

```
 Basic ──────── Reviewer ──────── Tests ──────── Adversarial ──────── Full
   1               2                3                  4                5
 1 agent        2 agents         4 agents           6 agents         8 agents
 a typo,        a small fix      a contained        a feature        risky or
 a rename       you'll skim      change             with logic       security-relevant
```

Alongside the levels, the plan makes the run **stop and ask the person** at the points where a
person should decide, instead of guessing or failing:

- **Blocking questions** (every level): answer them, or **Bypass** and let the agent decide;
  after 3 spec passes, **Begin work?** blows past whatever is left.
- **Failing checks the pipeline couldn't fix**: ask for **another fix pass**, or **take over**.
- **Security scan hits** below Full (the scan is its own toggle): **ignore** them, or **block**.
- **A usage or credit limit hit mid-run**: the run **pauses**, explains, and offers **try
  again** (after adding usage or credits) or **finish as is**.

It also shows **subscription usage** next to the budget when Claude Code is logged in with a
Claude subscription, and **warns** when it's using Console credits (real money).

```sh
$ advpipe run --level basic "Fix the typo in the refund error message"
20261014-091500-ab12cd  complete  $0.22  advpipe/fix-typo-refund-error-message  [basic]

$ advpipe run --level basic "Make refunds round half-up"
20261014-093000-cd34ef  needs_human  $0.41  advpipe/make-refunds-round-half-up  [basic]
  Checks are failing. Another fix pass:  advpipe decide 20261014-093000-cd34ef fix
                      Or take over:      advpipe decide 20261014-093000-cd34ef takeover

$ advpipe run --item work-items/s05-refund-window.md --level adversarial
20261014-100000-ef56ab  needs_human  $0.30  advpipe/s05-refund-window  [adversarial]
  2 blocking questions. Answer them with: advpipe answer 20261014-100000-ef56ab
  Or let the agent decide:               advpipe answer 20261014-100000-ef56ab --bypass
```

Nothing changes for anyone who doesn't ask for it: **the default level is `full`** (with the
security scan on, as today), and runs recorded before this feature are `full`. The new stops
only appear where today's pipeline would end the run, fail, or never get asked.

> **Status (2026-10-14): L0 done** (levels as data, `[pipeline]` config, front matter, CLI
> `--level`/`--security-scan`/`--scan-failures`, `advpipe levels`, the scan toggle, the
> report's Level line, and the Adversarial level). Where the code differs from the text below:
> - `Level` (and `ScanPolicy`) live in `models.py`, next to `Stage`, because `RunState` needs
>   them and `levels.py` imports `models`; `levels.py` re-exports `Level`.
> - `Role.SOLO` doesn't exist yet (adding it means a prompt, `.claude/agents/solo.md`, and the
>   UI's role tables: L3). So `roles_for` returns `list[str]` (`Role` values, which compare
>   equal to `Role` members) with `"solo"` for levels 1 and 2. L3 can tighten it to
>   `list[Role]`. `Stage.SOLO` was added now, for `steps_for`.
> - `bypass_questions` (config, front matter, CLI) waited for L2 rather than being accepted and
>   doing nothing.
> - Up-front `scan_failures = "ignore"` also falls back to `block` with a note in L0, like `ask`
>   (the run's `scan_policy` is stored as `block`, so a run resumed after L3 keeps blocking).
> - `advpipe levels` marks levels that can't be run yet ("not available yet") and the config's
>   default.
> - The web UI is unchanged (L5), except that saving a work item in the editor keeps its
>   `level`, `security_scan` and `scan_failures` lines (the editor has no fields for them yet).
>   Its timeline still shows the six fixed steps, so an Adversarial run's REVIEW step reads
>   "not reached".
> - `pyproject.toml` excludes this file from `ruff format`, which would otherwise re-align the
>   code blocks above.
>
> **L1 done** (2026-10-14): the Tests level runs (`adversarial_stage(..., critic=False)`,
> `_settle(..., arbiter=False)`, `stages.single_review_stage`, the code critic's "single review
> pass" paragraph); tests in `tests/test_tests_level.py`. Where the code differs from the text:
> - Without an arbiter, the final author pass is told "these findings are still open and must
>   be fixed" (not "the arbiter ruled ..."), and is charged to the stage being settled
>   (`stage-tests`, `stage-code` or `review`) rather than `arbiter`; the run doesn't enter
>   `Stage.ARBITER` (that level's timeline has no ARBITER step). The rulings go into
>   `state.rulings`, `ruling` events and the report, but no `arbiter.json` is written.
> - Single-review findings are prefixed `REV-` (the full review's are `STD-`/`SEC-`).
> - The critic prompt also lists the test files changed in the run under "part of the diff
>   below", and the code-critic paragraph asks it to judge those tests too (at level 3 there's
>   no test critic).
> - The web UI is still unchanged (L5): a Tests run's page renders, with the six fixed steps;
>   `review/single.json` isn't shown yet.

> **L2 done** (2026-10-14): Bypass for the spec writer (levels 3 to 5), up front and per
> stop; tests in `tests/test_bypass.py`. Where the code differs from the text:
> - `--bypass` can't take an optional value in Typer/Click, so `advpipe answer` rewrites a bare
>   `--bypass` to `--bypass=all` before parsing (`cli._AnswerCommand`); `--bypass Q2` (repeatable,
>   or `Q2,Q3`) works as planned. A bare `--bypass` covers questions that are still *pending*
>   (not ones already answered and waiting to be used; name those to replace their answers).
> - `advpipe run` has `--bypass-questions/--no-bypass-questions` (a pair, like the scan switch),
>   so the command line can switch off a config or front-matter `true`.
> - The `answers` event gets `bypassed` only when there are bypassed ids (so a run without Bypass
>   writes the same events as before); the agent's own answers add an `answers` event with
>   `agent: true`, and a `questions` event under Bypass up front has `bypassed: true`.
> - The helper for L3's solo stage is `stages.bypass_questions(ctx, role, questions,
>   previous_task_md, stage, rel_path)`; the spec stage calls it with `Role.SPEC_WRITER`. Its
>   stray-file revert uses `state.base_commit`, which is right for the spec stage; the solo stage
>   (which has no earlier commit either) can use it as is.
> - `answers_ready` also holds for a run whose only settled questions are bypassed ones with no
>   agent answer yet (`answers.awaiting_bypass`), so such a run is resumable.
> - The revision prompt marks the agent's own answers ("your own best judgement: the person
>   chose Bypass") but never gives the spec writer its earlier reasoning.
> - At the spec-pass cap without up-front Bypass the run still stops for good, as planned for
>   L2 (*Begin work?* comes with L3).
> - Not done here (later milestones): the UI form's Bypass boxes (L5), the skill (L4), and the
>   report/`status` text beyond the questions section (L4).

> **L3 done** (2026-10-14): decision points (`red_checks`, `scan_hits`, `spec_cap`;
> `usage_limit` waits for L6), the scan policy, `advpipe decide`, and the solo levels (Basic and
> Reviewer). Tests: `tests/test_decisions.py`, `tests/test_solo.py`,
> `tests/test_level_invariants.py` (the §4 invariants at all five levels). Where the code
> differs from the text:
> - `RunState.decision` is a property (the last of `RunState.decisions`), not a second field, so
>   the open decision can't drift from the list. `Decision.kind` has three values until L6.
>   `ended_by` is `""`, `takeover` or `stop`; *finish* comes with `usage_limit`.
> - The `decision` event's field is `decision_kind` (`kind` is `emit`'s own parameter), plus
>   `choice`, `decision_stage` and, when asked, `choices`.
> - A `scan_hits` decision comes from `decisions.DecisionNeeded`, raised by
>   `stages.checked_findings` from wherever the gates ran and caught in `Orchestrator.run`.
>   After `ignore`/`block` the stage restarts with the author's work kept and
>   `Context.judge_existing` set: its first round skips the author call and judges the work as
>   it is (so `ignore` costs no extra agent call). An earlier unapplied `fix` is dropped if a
>   scan decision interrupts its pass (only the latest decision is applied).
> - A decision is `applied` (and `fix_passes` counted) only once acted on, so a fix pass an
>   interrupt cut short reruns on a plain `resume`; for that, `Workspace.reopen(keep_changes=)`
>   is chosen by `decisions.pending_choice`, which doesn't need status `needs_human`.
> - For a failure inside `_settle` the run stays "in" ARBITER (as before) and the decision's
>   `stage` is the stage being settled; fix passes after it are charged to that stage's budget
>   key (`stage-tests`, `stage-code`, `review`, `solo`), and from FINAL_GATES to `fix`.
> - Under Bypass up front, the solo agent gets one more pass past `max_rounds_spec` (the pass
>   that asked did none of the work); if it still asks, a bypass call answers and the checks
>   decide, so a Basic run could in principle finish with little done. Level 3 isn't affected.
> - `check_available` and the `ask`→`block` fallback are gone. `roles_for` returns
>   `list[Role]`. The UI's role tables got a `solo` entry (the Help and Spec & context pages
>   list it); the rest of the UI is L5's. A `needs_human` decision shows there as "needs you".
> - Not done here: the `decisions/N.json` files and the report's `## Decisions` section (L4; the
>   report notes the open decision and its commands), the skill (L4), usage limits (L6), and
>   the terminal "decide later" is a prompt option, not a stored state.
> - Existing tests that ended a run `needs_human` on red checks kept passing as they were
>   (still `needs_human`); the ones changed were about the spec-pass cap ("stops for good" →
>   *Begin work?*), levels "not available yet", the `ask` fallback note, and the UI role tables.

> **L4 done** (2026-10-14): the report's `## Decisions` section and the `decisions/N.json`
> files, the scan-policy line (report and `status`), `status <id>` showing decisions, bypass and
> ignored hits, the skill rewritten for levels, Bypass, decision points and the solo procedure,
> and the docs. Tests: `tests/test_reports.py`. Where the code differs from the text:
> - Much of the README and `docs/ARCHITECTURE.md` text was already written by L0 to L3 (levels,
>   decision points, the rules table, `CLAUDE.md`'s rules line); L4 added what was missing
>   (the report sections, `status`, "when not to use", the Claude Code section, `BUILD_SPEC.md`'s
>   note, `ANSWERS_PLAN.md`'s update line, the persistence table).
> - `decisions/N.json` is written by `RunLog.write_state` (every decision, rewritten on each
>   state write), so it's current wherever a decision is recorded or applied.
> - The report's "finished as is" line comes with L6 (`usage_limit`).
> - The skill's test is a light text check (`tests/test_reports.py`), like the agents' sync test.

> **L5 done** (2026-10-14): the web UI for levels and decision points. Tests:
> `tests/test_ui_levels.py`, `tests/test_ui_decisions.py`. Where the code differs from the text:
> - The editor's level, scan, policy and Bypass are **drop-downs**, not a slider with an
>   "inherit" box: the first option is `inherit` (not an empty value: FastAPI reads an empty
>   form value as missing, so `inherit` is what means "leave the key out"; a form that doesn't
>   carry a setting at all keeps the file's). Bypass there is a yes/no/inherit select.
> - The New run dialog confirms what it showed with one hidden field, `shown`
>   (`level;scan;policy;bypass`), next to the budget one, and `POST /runs/start` refuses a
>   mismatch. The Bypass checkbox has a hidden `bypass_seen` beside it, since an unticked box
>   sends nothing; the scan-policy radios start at the item's or config's value, and
>   `--scan-failures`, `--bypass-questions` / `--no-bypass-questions` are passed only when the
>   choice differs from that (so "only when ticked" holds with the default config). The
>   scan radios are labelled *Default / On / Off* (the summary says what *Default* means and
>   where it comes from, rather than the label changing).
> - `app.js` (not "no new JavaScript") also moves an untouched slider to the level of the
>   chosen work item: without it a slider rendered for one item would silently override the
>   next item's front matter. The summary says when the slider overrides the item.
> - In the answer form, a ticked "Let the agent decide" box beats text left in its answer box,
>   and *Bypass all and continue* beats everything; the answer boxes aren't `required` (the
>   server checks, keeping typed answers on a refusal). Bypass is passed as `--bypass=Q2` per
>   question (the button sends every question's id), never a bare `--bypass`.
> - `takeover` and `stop` have confirmation pages (`GET /runs/<id>/decide/<choice>`); the
>   other choices post straight from the panel. Display statuses added: `needs_decision`,
>   `handed_over`, and `ended` (a *Begin work?* stop). A decision made but not acted on shows
>   as `stopped` (resume applies it).
> - Not done here: the usage line, the `paused` and `finished` panels and the Console-credits
>   warning (L6); the optional "typical cost at this level".

This plan is for **future Claude Code sessions**. Each milestone below (L0 to L6) is sized for
one session and ends with tests. See [How to use this plan](#how-to-use-this-plan-in-a-session).
Read `CLAUDE.md`, `docs/ARCHITECTURE.md` and `docs/ANSWERS_PLAN.md` first. Paths and names below
are from the code as of 2026-10-14; check them before relying on them.

> **This relaxes design rules in `BUILD_SPEC.md` §1 at the lower levels** (rules 2, 4 and 6
> only apply where there's a second agent to apply them to; see §3.3). Requested by Brendan on
> 2026-10-14. `BUILD_SPEC.md` §8 Phase 3 already says routine changes should use "a single
> agent plus one reviewer instead"; levels 1 and 2 make that possible inside advpipe. Rules 1,
> 5 and 7 hold at every level, and so does "never push, merge or open PRs".

> **Decisions (Brendan, 2026-10-14).** First round:
> 1. When checks fail at Basic, **ask the person** whether to run another fix pass or take
>    over; no automatic fix pass.
> 2. Blocking questions **always stop the run**, at every level, including the solo agent's.
>    At every such stop the person can choose **Bypass**: the agent answers with its best
>    judgement and the run goes on (§3.6).
> 3. The single review at levels 2 and 3 is done by `code-critic` (§3.4).
> 4. The security scan is **its own toggle**, on by default only at Full (§3.7).
> 5. When logged in with a Claude subscription, show **subscription usage** next to the budget
>    (§3.15).
>
> Second round:
> 6. Fix or take over applies at **every level**; at levels 3 to 5 only after the normal
>    rounds, arbiter and final pass failed to fix it (§3.5).
> 7. A person can ask for at most **3 fix passes** per run (`max_fix_passes`).
> 8. A usage or credit limit hit mid-run **pauses** the run, explains what happened, suggests
>    adding usage or credits, and offers **Try again** and **Finish as is** (the work left
>    uncommitted for the person) (§3.5, §3.15).
> 9. Security scan hits below Full: **ask** the person whether to blow past them or block the
>    run (§3.7).
>
> Third round:
> 10. *Take over* commits the work to the branch and removes the worktree, as designed (§3.5).
> 11. An ignore/block choice on scan hits **sticks for the rest of the run**: asked at most once.
> 12. **No automatic retry** at a usage limit's reset time; the person presses *Try again*.
> 13. *Block* on scan hits means **treat them like a failing test**: the author fixes them, the
>     scan reruns, and the run can still complete; only hits the author can't clear end at the
>     usual fix-or-take-over choice (§3.7).
>
> Fourth round (answering §8):
> 14. The levels are named **Basic, Reviewer, Tests, Adversarial, Full** (§3.2).
> 15. Bypass covers **only the stop it was chosen at**: a revision's new blocking questions stop
>     the run again and offer answering or Bypass again. After **3 spec passes** the person is
>     asked **"Begin work?"**; yes means every remaining and later question is blown past (§3.6).
> 16. Bypass up front (`--bypass-questions`, a New run checkbox): yes.
> 17. **Both logins** are supported: advpipe uses whatever Claude Code is logged in with, and
>     **warns** when that's Console credits, since those are real dollars (§3.15).
> 18. The solo agent uses the author model (Opus); one budget for every level; new runs default
>     to the config's or work item's level, else Full; no custom shapes.

---

## 1. Principles

These follow from the design rules in `CLAUDE.md`. Don't trade them away for convenience.

- **A person picks the level, never an agent.** The level comes from the command line, the
  work item's front matter, the config, or the UI's slider, in that order. No agent can raise
  or lower it, and the orchestrator never changes it mid-run. Rule 1 holds at every level:
  `Orchestrator._run` stays a plain, deterministic sequence; it just has fewer steps at lower
  levels.
- **A person makes every "stop or go on" call that code can't settle.** Blocking questions,
  failing checks the pipeline couldn't fix, scanner hits below Full, and usage limits all stop
  the run and ask. The orchestrator never bypasses, retries, ignores a scanner hit or waits out
  a limit on its own, unless the person chose so up front (`--bypass-questions`,
  `--scan-failures`).
- **One mechanism for asking.** Every stop that asks the person a choice (other than
  answering questions, which has its own path in `answers.py`) is a **decision point**,
  including *Begin work?* after the spec-pass cap,
  (§3.5): one data model, one command (`advpipe decide`), one UI panel.
- **Gates at every level.** Tests, types and lint run and must be green for `complete` at every
  level, including Basic (rule 7). A level removes agents, never checks.
- **Bounded at every level.** Each level has a computable maximum number of agent calls between
  two person decisions (§3.9), round caps, a cap on fix passes, and the same dollar budget
  (rule 5).
- **Levels are presets, not a new pipeline.** Every level is a combination of a few switches
  over the stages that already exist (§3.1). Levels 3 to 5 reuse `adversarial_stage`,
  `review_stage`, `arbiter_stage` and `_settle`; only Basic and Reviewer add a new role
  (`solo`) and a new stage. Don't fork the orchestrator per level.
- **One answering path.** Blocking questions at the solo levels, and Bypass at every level, go
  through `answers.py` and `advpipe answer`, as in `ANSWERS_PLAN.md`. Answers, whether from the
  person or the agent, end up in `task.md`'s `## Answers` section, written by code.
- **`full` is the default and its happy path stays as it is.** Its prompts, files, events and
  report don't change unless one of the new stops happens. Every existing test keeps passing,
  except those that assert `needs_human` as the *end* of a run with red gates: those now stop
  at a decision point, which still has status `needs_human` (§3.5).
- **Say what's off, say what happened.** Wherever a level is chosen or shown, say which
  safeguards it drops. Agent answers under Bypass and scanner hits a person ignored are always
  marked as such. A pause explains in plain words what limit was hit and what to do.
- **Same files, same commands.** A run at any level has the same run directory layout, the
  same `run.json`, `report.md`, branch and worktree handling, and works with `status`,
  `report`, `resume`, `cancel` and `clean`.

## 2. What happens today (the starting point)

Read these before changing anything:

- `orchestrator.Orchestrator._run`: `_open_workspace`, then `_spec` (unless `"spec"` is in
  `state.commits`), then TESTS (`adversarial_stage(ctx, "tests")` + `_settle`), CODE
  (`adversarial_stage(ctx, "code")` + `_settle`), REVIEW (`review_stage` + `_settle` with
  `Role.CODER`), then FINAL_GATES. Each stage is skipped on resume if its key is in
  `state.commits` (`spec`, `tests`, `code`, `review`).
- `orchestrator.Orchestrator._spec` and `_new_questions`: one spec pass (first spec, or a
  revision with answers); blocking questions are recorded in `state.questions` and stop the run
  with `needs_human`. At `limits.max_rounds_spec` the run stops for good. A re-asked question
  that's already answered doesn't stop the run ("its answer stands").
- `answers.py`: `_blocked_at_spec`, `awaiting_answers`, `answers_ready`, `migrate`,
  `not_waiting_message` all check `state.stage is Stage.SPEC` and `"spec" not in commits`.
  `check_answers` and `record_answers` validate and save answers; `with_answers_section`
  writes `## Answers` ("Answers from the person who started this run...").
- `orchestrator.Orchestrator._settle`: on a failed stage, always calls `arbiter_stage`, then
  `final_author_pass` for `fix` rulings; red gates after that → `needs_human` (the run ends).
  FINAL_GATES red → `needs_human` (the run ends).
- `stages.adversarial_stage(ctx, kind)`: one loop for both pairs; the critic is called every
  round. `StageKind = Literal["tests", "code"]`. The tests stage uses `red_gate_findings`; the
  code stage runs `test_file_guard` and `ALL_GATES`.
- `stages.review_stage`: runs the `security` scanner gate (`OPTIONAL_GATES`: skipped if not
  installed), then standards and security reviewers in parallel.
- `stages.call_agent` → `runner.SdkAgentRunner.run`: reads the `query()` stream, keeps the
  `ResultMessage` text and `total_cost_usd`. An agent error today surfaces as `is_error` or an
  exception, and the run ends `error`.
- `workspace.Workspace.reopen` (every resume): `git reset --hard` and `git clean -fd`, which
  throws away uncommitted work.
- `runner.Role`, `ROLE_TOOLS`, `ROLE_MODEL`: eight roles. Prompts in `src/advpipe/prompts/`,
  copied into `.claude/agents/` (`test_subagents_in_sync_with_prompts`).
- `config.Config`: `[models]`, `[limits]`, `[gates]`, `[paths]`, all `extra="forbid"`.
- `models.Stage`: `INIT, SPEC, TESTS, CODE, REVIEW, ARBITER, FINAL_GATES, DONE`.
- `workitem.KEYS`: the front-matter keys (`name`, `config`). Unknown keys are errors.
- `ui/runs.py display_status`: display-only statuses (`stopped`, `needs_answers`) derived from
  `run.json`. `ui/actions.py VALID`: actions per displayed status.
- `ui/detail.py TIMELINE` and `COMMIT_KEYS`: the six timeline steps, all hard-coded.
- `ui/context.py SEEN_BY`, `ROLE_INPUTS`; `ui/routes/help.py ROLE_JOBS`, `STATUS_NEXT`: must
  cover every role and status (a test checks Help's).
- `ui/actions.py plan_run` and `templates/_run_new_form.html`: the New run dialog. Its summary
  (`POST /runs/new/check`) re-renders on every `input` event in the form, and `POST
  /runs/start` refuses if the budget shown isn't the budget it works out now.
- `.claude/skills/adversarial-build/SKILL.md`: the procedure for the Claude Code version.

## 3. Design

### 3.1 The shape of a run (`src/advpipe/levels.py`, new)

A level is a name for a **shape**: a few switches the orchestrator reads.

```python
class Level(StrEnum):
    BASIC = "basic"
    REVIEWER = "reviewer"
    TESTS = "tests"
    ADVERSARIAL = "adversarial"
    FULL = "full"


class Shape(BaseModel, frozen=True):
    solo: bool              # one agent writes task.md, tests and code in one stage
    critics: bool           # tests and code stages are author ⇄ critic loops
    review: Literal["none", "single", "full"]
    arbiter: bool           # disputed / still-open findings go to the arbiter
    security_scan: bool     # the default for the security-scan toggle (§3.7)


SHAPES: dict[Level, Shape] = {
    Level.BASIC:       Shape(solo=True,  critics=False, review="none",   arbiter=False, security_scan=False),
    Level.REVIEWER:    Shape(solo=True,  critics=False, review="single", arbiter=False, security_scan=False),
    Level.TESTS:       Shape(solo=False, critics=False, review="single", arbiter=False, security_scan=False),
    Level.ADVERSARIAL: Shape(solo=False, critics=True,  review="none",   arbiter=True,  security_scan=False),
    Level.FULL:        Shape(solo=False, critics=True,  review="full",   arbiter=True,  security_scan=True),
}
```

Also in `levels.py`, all derived from `SHAPES` so the CLI, report, Help page and slider can't
drift apart:

- `LEVEL_ORDER: list[Level]`: slider order, 1 to 5. `level_number(level) -> int`;
  `parse_level(value: str | int) -> Level` (name or number; `ValueError` otherwise).
- `roles_for(level) -> list[Role]`: the roles a run at that level can call.
- `steps_for(level) -> list[Stage]`: the timeline steps (§3.11).
- `dropped_safeguards(level, security_scan) -> list[str]`: plain-language lines, e.g. "No
  second agent checks the tests or code", "Tests and code are written by the same agent, so
  tests aren't written before the code", "No security review", "No security scan". Empty for
  `full` with the scan on.
- `LEVEL_INFO: dict[Level, LevelInfo]`: label ("Basic"), one-line "good for" text, and a short
  description, for the CLI and UI.
- `max_agent_calls(level, limits) -> tuple[int, int]` (§3.9).

Custom shapes (e.g. Full without the standards reviewer) are **out of scope**; see §8. The
security scan is the one switch a person can set independently of the level (§3.7).

### 3.2 The five levels

Names and slugs (Brendan, 2026-10-14): **Basic** (`basic`), **Reviewer** (`reviewer`),
**Tests** (`tests`), **Adversarial** (`adversarial`), **Full** (`full`). In prose, say "the
Tests level" so it isn't confused with the TESTS stage.

| # | Level | Steps | Agents (roles) | Good for |
|---|---|---|---|---|
| 1 | **Basic** | SOLO → FINAL CHECKS | 1: `solo` | typos, renames, a one-line fix, a tiny refactor |
| 2 | **Reviewer** | SOLO → REVIEW → FINAL CHECKS | 2: `solo`, `code-critic` (one review pass) | small bug fixes you'll skim |
| 3 | **Tests** | SPEC → TESTS → CODE → REVIEW → FINAL CHECKS | 4: `spec-writer`, `test-author`, `coder`, `code-critic` (one review pass) | a contained change with clear behaviour |
| 4 | **Adversarial** | SPEC → TESTS ⇄ → CODE ⇄ → ARBITER → FINAL CHECKS | 6: `spec-writer`, `test-author`, `test-critic`, `coder`, `code-critic`, `arbiter` | a feature with real logic |
| 5 | **Full** | today's pipeline | 8: all roles | risky, security-relevant or user-facing work |

At every level: blocking questions stop the run (answer or Bypass); failing checks the
pipeline couldn't fix stop and ask (fix pass or take over); a usage limit pauses the run; and
the security scan runs if its toggle is on.

What each level adds over the one below:

- **1 → 2:** a second agent (on the critic model) reads the finished change once.
- **2 → 3:** separate agents and stages: a spec writer, tests written from `task.md` alone
  **before** the code and shown to fail (the red gate), a coder that can't touch the tests
  (the test-file guard), and automatic gate-driven retries within each stage's round cap
  before the person is asked (§3.5).
- **3 → 4:** critics inside the tests and code stages, with round caps, disputes and the
  arbiter, instead of one review at the end.
- **4 → 5:** the standards reviewer and the security reviewer after the code stage, and the
  security scan on by default, triaged by the security reviewer.

### 3.3 Design rules by level

| Rule (`CLAUDE.md`) | Basic | Reviewer | Tests | Adversarial | Full |
|---|---|---|---|---|---|
| 1. Orchestrator is code | ✔ | ✔ | ✔ | ✔ | ✔ |
| 2. Tests before code (test author sees `task.md` only) | ✘ one agent | ✘ one agent | ✔ | ✔ | ✔ |
| 3. One writer at a time; reviewers read-only | ✔ (one writer) | ✔ | ✔ | ✔ | ✔ |
| 4. Critics get artifacts, not reasoning | n/a | ✔ | ✔ | ✔ | ✔ |
| 5. Every loop bounded | ✔ | ✔ | ✔ | ✔ | ✔ |
| 6. Critics may PASS; grounded findings | n/a | ✔ | ✔ | ✔ | ✔ |
| 7. Failed gate = blocking finding | ✔ | ✔ | ✔ | ✔ | ✔ |

Rule 7 at every level, with one person-made exception: below Full, a person may choose to
**ignore** security scan hits for a run (§3.7). That's recorded and reported; no agent can make
that choice.

Put this table in `docs/ARCHITECTURE.md` and the README, and update `CLAUDE.md`'s design rules
with one line: "Rules 2, 4 and 6 apply at the levels that have the agents they govern
(`docs/LEVELS_PLAN.md`); the default level, `full`, applies all of them."

### 3.4 New pieces for levels 1 and 2: the solo agent

**Role.** `Role.SOLO = "solo"`, tools `WRITER`, model tier `author`. New prompt
`src/advpipe/prompts/solo.md`, copied to `.claude/agents/solo.md` (inherits the session model,
like the other authors). The prompt says, in this order:

1. Read the relevant code. Write `task.md` first, in the usual format (`BUILD_SPEC.md` §4.1),
   kept short: goal, acceptance criteria, constraints, open questions.
2. **If anything blocks the work** (a question only a person can answer), list it under
   `## Open questions` with the `BLOCKING:` prefix, exactly as the spec writer does, and
   **stop there**: don't write tests or code. The answers come back to you in a later pass.
   Don't use `BLOCKING:` for things you can reasonably decide; record those decisions under
   `## Assumptions` so the reviewer (person or agent) sees them.
3. Otherwise, write or update tests for each acceptance criterion that changes behaviour. A
   pure refactor may rely on the existing tests; say so in `task.md`.
4. Make the change: the smallest one that satisfies `task.md`.
5. Run the gate commands (given in the prompt) and fix what fails before you finish.
6. **Revising after answers** (the same rules as the spec writer's section of that name): when
   given answers, rewrite `task.md` to use them, don't re-ask answered questions, don't write an
   `## Answers` section yourself; then carry on from step 3.
7. When given findings (failing checks, or review findings at Reviewer), fix them and reply
   with an `AuthorResponse`, as the coder does.

**Stage.** `Stage.SOLO = "SOLO"` (new enum value; old `run.json` files never contain it).
`stages.solo_stage(ctx) -> StageResult`. One solo call per stage entry, no automatic retries:

```
prompt:   no answers yet          → solo_prompt(work_item, gate commands)
          answers not yet applied → solo_revision_prompt(work_item, previous task.md, Q&A, gates)
          a fix pass requested    → solo_fix_prompt(gate findings, gates, the person's note)
after:    task.md must exist        (else a blocking guard finding, criterion "guard:task-md")
          BLOCKING questions?       → revert every changed file except task.md (the agent
                                      wasn't allowed to write code yet), record the questions
                                      exactly as _spec does, stop: awaiting answers (§3.6)
          gates (run_gate_names, §3.8)
          scanner hits, policy ask  → decision point scan_hits (§3.7)
          green                     → stage passes
          red                       → decision point red_checks (§3.5)
```

- `ctx.task_md` is read from the worktree, written with the Answers section (if any) by code,
  and copied to the run directory (`runlog.write_text("task.md", ...)`) and
  `stage-solo/pass-N/task.md`, as `_spec` does, so the report, the reviewer, the answering code
  and the UI find it in the usual places.
- The spec-pass count and cap are shared with `_spec`: `state.spec_passes` counts solo passes
  that wrote `task.md` before the questions were settled, and `limits.max_rounds_spec` caps
  them. Factor the question handling out of `_spec` (recording questions, the Answers section,
  the cap note, the `questions` event) into one helper both stages call, rather than copying it.
- No test-file guard (the same agent owns both) and no red gate (tests and code arrive
  together).
- Commit when the stage passes: `state.commits["solo"] = ws.commit("advpipe: solo (task.md,
  tests, code)")`. Run-directory files: `stage-solo/pass-N/{author.txt, diff.patch,
  gates.json, task.md}`, plus `answers.json` for revision passes, `bypass.json` (§3.6) and
  `request.json` for fix passes a person asked for.

**Single review** (`review == "single"`, levels 2 and 3). `stages.single_review_stage(ctx) ->
StageResult`: one `code-critic` pass, no back-and-forth.

- The prompt is `critic_prompt` with the full diff since `state.base_commit` (`REVIEW_PATHS`,
  so `task.md` is excluded), the current gate output and the list of test files changed in the
  run. Add a short paragraph to `prompts/code-critic.md` (and its copy): "If the prompt says
  this is a single review pass, judge the whole change once; there won't be another round."
  The flag goes in the prompt text, never a different system prompt.
- Files: `review/{diff.patch, single.json}`. Charged to stage `review`.
- A `test-defect` finding is treated as an ordinary blocking finding (there's no separate test
  author to send it to at level 2, and at level 3 the coder's guard still protects the tests).
- Blocking findings get one automatic author pass (`solo` at level 2, `coder` at level 3) via
  `_settle(..., arbiter=False)` (§3.8), then the gates. Red gates after that → decision point
  `red_checks` (§3.5).

### 3.5 Decision points: the run stops and asks

A **decision point** is a stop where the run waits for a person to pick one of two choices.
There are four kinds:

| Kind | When | Choices |
|---|---|---|
| `red_checks` | checks are red and the pipeline has nothing automatic left to try | **fix** (one more pass, optional note) · **takeover** |
| `scan_hits` | the security scan failed below Full, with the policy `ask` (§3.7) | **ignore** (blow past) · **block** |
| `usage_limit` | a subscription usage limit or the Console credit balance ran out mid-run | **retry** · **finish** (as is) |
| `spec_cap` | the spec pass at `max_rounds_spec` still has blocking questions (§3.6) | **begin** (work, blowing past every question) · **stop** |

**`red_checks`: when.** It replaces every place where red gates end a run today, and at the
solo levels it also replaces automatic retries:

- **Basic and Reviewer:** after each solo pass with red gates (no automatic retry), after
  Reviewer's automatic review-fix pass if gates are still red, and at FINAL_GATES.
- **Tests, Adversarial, Full:** only after the normal machinery has had its go: the
  stage's round cap, then `_settle` (arbiter where the level has one, then the
  `final_author_pass`), and still red. Also at FINAL_GATES red. That's exactly where the run
  ends `needs_human` today; now it stops there and asks instead.
- **fix**: one more pass by the stage's author (`solo`; `test-author` for the tests stage;
  `coder` for code, review and final gates), with the failing checks' output and the person's
  note verbatim, if any (`fix_prompt` = `author_findings_prompt` plus "A person asked for this
  pass" and the note). No critic round after a person-requested pass: the gates decide, as
  after `final_author_pass`. Green → the run carries on from that stage (commit, next stage).
  Red → the same decision again. At most `limits.max_fix_passes` (default 3) per run; after
  that, `fix` is refused ("take over, or start a new run").
- **takeover**: the person fixes it by hand. advpipe commits the work as it is to the run's
  branch (`"advpipe: work in progress, handed over (checks failing)"`, skipped if nothing
  changed), removes the worktree (`Workspace.remove`; the branch stays) so the branch can be
  checked out, and ends the run. It prints `git switch <branch>` and the gate commands.

**`scan_hits`: ignore or block.** See §3.7.

**`spec_cap`: begin or stop.** See §3.6. Unlike the other kinds, nothing has been written but
`task.md` yet, so there's no worktree state to protect.

**`usage_limit`: retry or finish.** See §3.15 for detection. The run pauses with a plain
explanation of which limit was hit, when it resets (subscription) or that credits ran out
(Console), and what to do: add extra usage to the subscription or credits in the Console (the
links are constants in `usage.py`, checked in the session that builds it), or wait for the
reset, then try again.

- **retry**: resume the run. The interrupted stage starts again **with its partial work kept**
  (see "The worktree at a stop" below), and its author's first prompt says "A previous attempt
  at this step was cut off by a usage limit; the worktree has its partial changes; carry on
  from them." If the limit is still in force, the run pauses again: no automatic waiting or
  retry loop.
- **finish**: end the run **as is**. Nothing is committed and the worktree is kept with its
  uncommitted changes, for the person to do with as they please; the branch keeps the commits
  of the stages that finished. The run is final (not resumable). advpipe prints the worktree
  path, `git -C <worktree> status`, and how to clean up later (`advpipe clean`, which now
  refuses a worktree with uncommitted changes unless `--force`, and says so).

**The worktree at a stop.** `Workspace.reopen` resets the worktree on resume, which would throw
away the very work a person is deciding about. So at every decision point the worktree is left
exactly as it is, uncommitted, and resuming after a decision calls
`Workspace.reopen(..., keep_changes=True)`: no `reset --hard`, no `clean`. (If the worktree was
deleted meanwhile, it's recreated from the branch and a note says the uncommitted work was
lost.) `commits[<stage>]` is set only when a stage passes, as today, so a resumed run re-enters
the right stage.

**State** (`models.py`), with defaults so old `run.json` files load:

```python
class Decision(BaseModel):
    kind: Literal["red_checks", "scan_hits", "usage_limit", "spec_cap"]
    stage: Stage                       # where the run stopped
    asked_at: datetime
    explanation: str                   # plain words, shown to the person as is
    findings: list[Finding] = []       # failing checks or scanner hits
    usage: RateLimitSnapshot | None = None   # usage_limit, subscription (§3.15)
    billing: bool = False              # usage_limit, Console credits ran out
    choice: str = ""                   # set by the person
    note: str = ""                     # red_checks + fix: the person's note, verbatim
    decided_at: datetime | None = None
    applied: bool = False              # the choice has been acted on


class RunState(BaseModel):
    ...
    decision: Decision | None = None             # the open or last decision
    decisions: list[Decision] = Field(default_factory=list)   # every decision, in order
    fix_passes: int = 0
    ended_by: Literal["", "takeover", "finish", "stop"] = ""
```

All decision points have status `needs_human`, like today's stops, so `run.json` stays
compatible and scripts that look for `needs_human` keep working. The UI derives display
statuses from `decision` (§3.13).

**`decisions.py`** (new, next to `answers.py` and shaped like it):

- `CHOICES = {"red_checks": ("fix", "takeover"), "scan_hits": ("ignore", "block"),
  "usage_limit": ("retry", "finish"), "spec_cap": ("begin", "stop")}`.
- `awaiting_decision(state) -> bool`; `decision_ready(state) -> bool` (a continuing choice
  made but not applied).
- `decide(repo, run_id, choice, note="") -> RunState`: refuses (`DecisionError`, with a
  message fit to show a person) when the run isn't awaiting a decision, the choice isn't one of
  that kind's, a live process holds `run.lock`, the branch is gone, a note is given for any
  choice but `fix` or is over `MAX_NOTE_CHARS` (8,000, as for answers), or `fix` is past
  `max_fix_passes`. Under the lock: records the choice, appends a `decision` event. For
  `takeover`, `finish` and `stop` it acts at once (above) and sets `ended_by`; for the others it
  leaves the status `needs_human` until something resumes the run.
- `check_resumable` also accepts a `needs_human` run when `decision_ready(state)`. For a run
  awaiting a decision it raises `NotResumable` naming the choices:
  `"run X is waiting for a decision: advpipe decide X fix | takeover"`. A run with `ended_by`
  set is never resumable.
- On resume, the orchestrator applies the choice where the run stopped: `fix` runs the fix
  pass then the gates; `block` and `ignore` carry on with the scan policy set; `retry`
  restarts the interrupted stage; `begin` runs the bypass call and carries on from the spec.
  The decision is marked `applied` once that has happened, so
  an interrupted fix pass reruns on a plain `advpipe resume`, as interrupted revisions do.

**CLI** (`cli.py`), modelled on `advpipe answer`:

```sh
advpipe decide <run-id> CHOICE [--repo PATH] [--note TEXT]
               [--no-resume] [--detach] [--budget USD] [--quiet]
```

For continuing choices it calls `decide` and then resumes exactly as `advpipe resume` does
(same exit codes, `--detach` and `--budget` handling). `takeover` and `finish` end the run and
print what's where. Without `CHOICE`, if stdin is a terminal, it shows the explanation and asks
(`typer.prompt` with the two choices); otherwise it prints them and exits with code 2.

**Asking at once in the terminal.** A foreground `advpipe run`/`resume` that reaches a decision
point and has a terminal on stdin shows the explanation and asks right away: the two choices,
plus "decide later" (which leaves the run waiting, as a detached run does). A detached run just
stops and waits; the UI and `advpipe status` show the decision. `cli._summary` and
`status <id>` print the explanation and both commands while a run is waiting.

### 3.6 Blocking questions at every level, and Bypass

**Questions always stop the run.** At levels 3 to 5 the spec writer's blocking questions stop
the run exactly as today (`ANSWERS_PLAN.md`). At levels 1 and 2 the solo agent's do too
(§3.4). Generalise `answers.py` so "blocked at the spec" means "blocked at the stage that
writes `task.md`": `stage in (Stage.SPEC, Stage.SOLO)` and that stage's commit key (`spec` or
`solo`) not in `state.commits`. Use one helper, `answers.spec_stage(state) -> tuple[Stage,
str]`, everywhere `answers.py` and the UI check this today (`_blocked_at_spec`, `migrate`,
`not_waiting_message`, `ui/runs.py`, `ui/detail.py`). `advpipe answer` then works unchanged at
the solo levels. (Questions stay on their own path rather than becoming a decision point: they
need free-text answers per question, which `answers.py` already handles.)

**Bypass.** At every stop for blocking questions, at every level, the person can choose
**Bypass**: the agent answers with its best judgement and the run goes on.

- **Per question.** Every pending question must be either answered by the person or bypassed
  (the all-or-nothing rule from `ANSWERS_PLAN.md` §8 still holds; Bypass is a way to "answer").
  "Bypass all" is the common case.
- **State.** `OpenQuestion` gains `source: Literal["person", "agent"] = "person"`,
  `reasoning: str = ""` (the agent's one-paragraph reason) and `bypassed: bool = False` (the
  person chose Bypass; the agent's answer may not exist yet). Defaults keep old `run.json`
  files loading.
- **Recording.** `answers.record_answers(repo, run_id, answers, bypass: set[str])`: answers for
  some ids, Bypass for others; all the existing refusals apply, plus "an id can't be both
  answered and bypassed". Bypassed questions get `bypassed = True` and no answer yet.
- **Answering (code, then agent).** On resume, before the revision pass, if any question is
  bypassed without an answer, the orchestrator makes **one** JSON call to the role that asked
  (`spec-writer` or `solo`) with `bypass_prompt(work_item, task.md, questions)`: "a person has
  asked you to decide these yourself; answer each with your best judgement and a one-paragraph
  reason; don't edit files", returning
  `{"answers": [{"id": "Q1", "answer": "...", "reasoning": "..."}]}` (`models.BypassAnswers`,
  parsed with `call_json_agent`, so a malformed reply gets the usual one retry). Any file the
  call changed is reverted, as with the spec writer's stray files. A missing or still
  unparseable answer becomes "No answer given; use your best judgement and record the choice
  under Assumptions" with `reasoning` saying so, so the run can't stall. The answers are saved
  with `source = "agent"`, then the usual revision pass runs with them, exactly as for a
  person's answers. Charged to stage `spec` (or `solo`). Files: `spec/pass-N/bypass.json` (or
  `stage-solo/pass-N/bypass.json`).
- **The Answers section** (`with_answers_section`) marks agent answers so later agents and the
  human reviewer can tell them apart:

  ```markdown
  ## Answers
  Answers to the blocking questions. They override anything above that disagrees. Answers
  marked "agent's best judgement" were chosen by the agent because the person chose Bypass.
  - **Q1:** Should partial refunds round half-up or to even?
    **Answer:** half-up, like the invoices
  - **Q2:** Are refunds allowed after 90 days?
    **Answer (agent's best judgement):** No; reject with an error. Reason: the existing
    `RefundPolicy` already caps refunds at 90 days.
  ```

  A run with only person answers keeps today's wording, so runs that don't bypass are
  unchanged.
- **Bypass covers one stop.** Choosing Bypass answers the questions of that stop and nothing
  more. If the revision that follows asks new blocking questions, the run stops again and
  offers answering or Bypass again, exactly as the first time.
- **After 3 passes: "Begin work?"** When the spec pass at the cap (`max_rounds_spec`, default
  3) still has blocking questions, the run doesn't ask them one more time. It stops at a
  `spec_cap` decision point (§3.5): "The spec still has N blocking question(s) after 3 passes.
  Begin work anyway? The agent will decide these, and any question that comes up later in this
  run, itself." The questions are listed, so the person sees what's being decided for them.
  - **begin**: everything is blown past. The orchestrator gets the agent's answers to the
    pending questions (one bypass call), writes them into the Answers section without another
    revision pass, sets `state.bypass_questions = True` so no later question stops the run, and
    carries on. The pending questions now match answered ones, so the existing "its answer
    stands" rule lets them through.
  - **stop**: the run ends here (`ended_by = "stop"`), as a run stopped at the cap does today:
    nothing committed, the last `task.md` in the run directory. Not resumable.
  Answering more questions at the cap is refused, as today; the message points at the
  *Begin work?* choice (`advpipe decide <id> begin`).
- **Up front.** `advpipe run --bypass-questions` (and front matter `bypass_questions: yes`,
  `[pipeline] bypass_questions = true`, and a checkbox in the New run dialog, off by default)
  sets `state.bypass_questions` from the start, for people who don't want to be asked at all:
  every blocking question is bypassed as it comes up, each spec pass still capped by
  `max_rounds_spec`, and at the cap the run begins work without asking. Rarely the right
  choice; the UI says so.
- **CLI.** `advpipe answer <id> --bypass` bypasses every question not given with `--answer`;
  `--bypass Q2` (repeatable) bypasses only those. Interactive prompting accepts an empty answer
  followed by confirming "Bypass Q2? [y/N]". The answers-file format gains a line form
  `Q2: BYPASS`. The `answers` event gets a `bypassed` field with the ids.
- **What later agents see.** The bypass call's reply is never shown to later agents: only the
  Answers section in `task.md` (rule 4). The reasons are in `task.md` because the human
  reviewer needs them; they're the agent's stated reason, not its working.

### 3.7 The security scan toggle, and asking about hits

The security scan (the `[gates] security` command, semgrep by default) is a switch of its own,
set independently of the level:

- **Default from the level:** on at Full (as today), off at levels 1 to 4 (`Shape.security_scan`).
- **Override** (highest first): `--security-scan / --no-security-scan` on `advpipe run`;
  `security_scan: yes|no` in the work item's front matter; `[pipeline] security_scan = true |
  false` in the config (leave it out for "the level's default"); the level's default. The
  effective value is stored in `RunState.security_scan: bool | None = None` (None = an old
  run: use the level default, i.e. on for `full`) and doesn't change on resume.
- **At Full**, it runs in `review_stage` as today, triaged by the security reviewer; nothing
  is asked. With the toggle off, the scanner gate is skipped there and the security reviewer
  works from the diff alone (as when semgrep isn't installed today; the report says the scan
  was turned off).
- **At levels 1 to 4**, with the toggle on, it runs with the other gates wherever they run for
  the author (each solo pass, each code-stage round at levels 3 and 4, the final gates). There's
  no security reviewer to check that a hit is reachable, so what a failure means is the
  person's call, set by the **scan policy** `RunState.scan_policy: Literal["ask", "ignore",
  "block"]`:
  - **`ask`** (the default): the first time the scan fails in a run, the run stops at a
    `scan_hits` decision point, with the hits (rule, file, line, message from the output tail)
    and the explanation "The security scan reported N issue(s). No security reviewer checks
    them at this level, so some may not be real problems." The choices:
    - **ignore** ("blow past"): the hits are recorded as **minor** findings marked "ignored by
      you", and the run carries on. The scan keeps running; later hits in this run are recorded
      the same way without asking again (the policy becomes `ignore`).
    - **block**: the hits are treated **like a failing test** (Brendan, 2026-10-14). They
      become **blocking** gate findings and the run carries on: the author gets them (e.g.
      "semgrep: shell=True at billing/export.py:88") and fixes them in the normal way, the scan
      reruns, and a clean scan lets the run go on and still end `complete`. Only hits the
      author can't clear end at a `red_checks` decision point (fix or take over), like any
      other red check. Later hits in this run are blocking without asking (the policy becomes
      `block`).
  - **`ignore`** / **`block`**: chosen up front, so the run never stops to ask:
    `--scan-failures ask|ignore|block`, front matter `scan_failures:`, `[pipeline]
    scan_failures = "ask"`, and a choice in the New run dialog (shown when the scan is on below
    Full).
- Not installed: skipped, as today (`OPTIONAL_GATES`), and the report says so, at every level.
  Every person choice about scan hits is in the report.

### 3.8 Switches in the existing stages

- **`critics=False`** (level 3): `adversarial_stage(ctx, kind, critic=False)`. Each round runs
  the author, the guard (code stage) and the stage's gates exactly as today, writes
  `author.txt`, `diff.patch` and `gates.json`, and **skips the critic call** (no `critic.json`).
  Blocking findings are then gate and guard findings only, which go back to the author as
  today, up to the same caps (`max_rounds_tests`, `max_rounds_code`). The red gate still
  applies. The test-defect path needs a critic, so it never fires at level 3.
- **`arbiter=False`** (levels 1 to 3): `_settle(result, author, kind, arbiter=False)` skips
  `arbiter_stage` and rules every open finding `fix` itself, with the reason "no arbiter at
  level <name>" (recorded in `state.rulings`, so the report shows why), then runs
  `final_author_pass` as today. The author's `disputed` replies are recorded; the gates decide.
- **The end of `_settle`** at every level: red gates after `final_author_pass` → a
  `red_checks` decision point instead of ending the run (§3.5).
- **The author in `_settle`** for the review stage is `Role.CODER` today; at solo levels it is
  `Role.SOLO`. Pass it from `_run`; `final_author_pass` already takes the role.
- **Gate lists.** Replace the module constant `ALL_GATES` at its call sites with
  `run_gate_names(state)`: `["test", "types", "lint"]`, plus `"security"` when the scan is on
  at levels 1 to 4 (§3.7). At Full, `review_stage` keeps running it itself. The `security`
  results go through the scan policy before they become findings.

### 3.9 Bounded: the maximum number of agent calls

`max_agent_calls(level, limits) -> tuple[int, int]` returns (happy path, worst case **between
two person decisions**): every agent call the orchestrator can make between a start (or
resume) and the next stop, at that level with those limits. The worst case includes one JSON
retry per critic/reviewer/arbiter/bypass call, the test-fix pass, and one `final_author_pass`
per `_settle`. Happy paths with default limits: Basic 1, Reviewer 2, Tests 4, Adversarial
5, Full 7.

Across a whole run, the person-driven loops are bounded too: spec passes by `max_rounds_spec`,
fix passes by `limits.max_fix_passes` (default 3), and everything by the dollar budget. Retries
after a usage limit are each a person's choice, and stay within the budget.

A test asserts the worst case against a `FakeAgentRunner` script that fails every round at
every level, so the formula can't drift from the code. The CLI and the UI show it ("1 agent
call, then it asks you" at Basic); it's a clearer cost signal than dollars, which vary.

### 3.10 Config, CLI and work items

**Config.** A new table:

```toml
[pipeline]
level = "full"            # basic | reviewer | tests | adversarial | full (or 1-5)
# security_scan = true    # leave out to use the level's default (on at full only)
scan_failures = "ask"     # below full: ask | ignore | block
bypass_questions = false
```

`config.Pipeline(_Strict)` with `level: Level = Level.FULL`, `security_scan: bool | None =
None`, `scan_failures: Literal["ask", "ignore", "block"] = "ask"` and `bypass_questions: bool =
False`, and `Config.pipeline`. A `field_validator` accepts the slider numbers for `level`; the
name is always stored. These are part of the effective config, so they're saved in the run's
`config.json`. `Limits.max_fix_passes: PositiveInt = 3`. Add all of them to
`pipeline.example.toml` and the README's configuration block.

**RunState.** `level: Level = Level.FULL`, `security_scan: bool | None = None`, `scan_policy`
(from `scan_failures`, updated by a `scan_hits` decision), `bypass_questions: bool = False`,
the decision fields from §3.5 and the usage fields from §3.15. Defaults keep old `run.json`
files loading as `full`. Set from the effective config when the run is created.

**Order of precedence** (highest first), matching `--name` and `--config` today: the command
line → the work item's front matter → `[pipeline]` in the config → the defaults.

**CLI** (`cli.py`):

- `advpipe run ... --level LEVEL` (name or number), `--security-scan / --no-security-scan`,
  `--scan-failures ask|ignore|block`, `--bypass-questions`. With `--from-file` / `--parallel`,
  the same for all items unless an item's front matter says otherwise.
- `advpipe levels`: prints the table from §3.2 with each level's happy-path and worst-case
  agent calls for the current config (`--config`, `--repo` as elsewhere), the scan default, and
  what each level drops. Read-only, no API calls.
- `advpipe decide` (§3.5); `advpipe answer --bypass` (§3.6); `advpipe usage` (§3.15).
- `cli._summary` and `advpipe status <id>`: show the level, e.g. `... advpipe/fix-typo
  [basic]`. Leave the summary line of a `full` run with the scan on unchanged, so scripts
  parsing it keep working; show `[full, no scan]` when the scan is off.
- `advpipe resume` keeps the run's level, scan setting and bypass setting. `resume` has no
  `--level`; a `--config` that sets a different level or scan setting is refused ("run X is at
  level basic; a resumed run keeps its level. Start a new run to change it"). `resume` on a
  run waiting for a decision fails and names `advpipe decide`.

**Work items.** Add `level`, `security_scan`, `scan_failures` and `bypass_questions` to
`workitem.KEYS`, validated when loaded (`WorkItemError` with file and line otherwise;
`yes`/`no`/`true`/`false` for the switches). `format_work_item` writes them after `config`, in
`KEYS` order, when set.

### 3.11 Orchestrator (`orchestrator.py`)

`_run` stays linear. Sketch:

```python
shape = SHAPES[self.state.level]
done = self.state.commits

if shape.solo:
    if "solo" not in done:
        self._enter(Stage.SOLO)
        if not await self._solo(ctx):       # questions, bypass, fix requests, decision points
            return
        done["solo"] = ws.commit("advpipe: solo (task.md, tests, code)")
    else:
        ctx.task_md = (ws.path / "task.md").read_text()
    author = Role.SOLO
else:
    ...spec (with bypass), tests, code as today, passing critic=shape.critics and
    arbiter=shape.arbiter; _settle ends in a red_checks decision point instead of needs_human...
    author = Role.CODER

if shape.review != "none" and "review" not in done:
    self._enter(Stage.REVIEW)
    ...as today (fresh gates on resume); a pending fix choice runs the fix pass only...
    stage = review_stage(ctx) if shape.review == "full" else single_review_stage(ctx)
    if not await self._settle(await stage, author, "code", arbiter=shape.arbiter):
        return
    ...commit as today...

self._enter(Stage.FINAL_GATES)
...as today, with run_gate_names() and the scan policy; red → red_checks decision point...
```

- `StageKind` grows `"solo"` where `final_author_pass` and the prompts need it.
- `_ask(kind, findings, explanation, **extra)`: leaves the worktree as is, sets
  `state.decision`, stops `needs_human`, emits a `decision` event (`choice: "asked"`, the kind
  and the two commands). Every decision point goes through it.
- `_apply_decision()` at the top of `_run` (after `_open_workspace`): applies a decided,
  unapplied choice where the run stopped (§3.5).
- **Usage limits** surface from `call_agent` as `UsageLimitReached` (§3.15).
  `Orchestrator.run` catches it next to `BudgetExceeded`: it records the cost so far, then
  `_ask("usage_limit", ...)`. Like `BudgetExceeded`, it can come from any agent call, so it
  must leave `run.json` and `report.md` consistent from anywhere.
- `_open_workspace` emits the level and scan setting in the `run_start` event (`level`,
  `security_scan` fields) and message (`run X started at level basic; branch ...`), and calls
  `Workspace.reopen(..., keep_changes=True)` when resuming after a decision.
- `steps_for(level)` gives the timeline: `full` → today's six steps; `basic` → `SOLO,
  FINAL_GATES`; and so on (§3.2). ARBITER appears only where `shape.arbiter`.

### 3.12 Report, events and the run directory

- `runlog.render_report`: under the status, `Level: basic (1 of 5): 1 agent · security scan
  off`, then "Not done at this level:" with `dropped_safeguards(...)`. For `full` with the scan
  on, only the `Level: full` line.
- `## Questions and answers`: agent answers marked "(agent's best judgement, you chose
  Bypass)" with their reasons.
- `## Decisions`: every decision point, in order: kind, stage, the explanation, the choice and
  note. For a run waiting on one, its explanation and both commands under **Notes**. For a
  taken-over run: "Handed over to you at <stage>; the branch has the work as it was." For a
  finished run: "Finished as is at <stage>; the uncommitted work is in <worktree>."
- Scanner hits a person ignored are listed under minor findings, marked "ignored by you".
- New run-directory paths: `stage-solo/pass-N/{author.txt, diff.patch, gates.json, task.md,
  answers.json, bypass.json, request.json}`, `spec/pass-N/bypass.json`, `review/single.json`,
  `decisions/N.json` (each decision as asked and as decided), and `fix-pass-N/{author.txt,
  gates.json}` for person-requested fix passes outside the solo stage. Add them to the
  persistence table in `docs/ARCHITECTURE.md`.
- Events: new kinds `decision` (`kind`, `choice`: `asked` or the choice made, `stage`) and
  `usage` (§3.15). `answers` gets `bypassed` (ids); `run_start` gets `level` and
  `security_scan`. Add them to `events.EventKind` and the events table.

### 3.13 Web UI

**The New run dialog** (`_run_new_form.html`) gets a fieldset "How much checking" (the call
counts in the sketch are illustrative; the real ones come from `max_agent_calls`):

```
How much checking                                         ┌───────────────────────────────┐
  Basic   Reviewer   Tests   Adversarial   Full         │ Tests (3 of 5)                │
  ──┼─────────┼────────●──────────┼───────────┼──         │ 4 agents · up to 13 calls     │
                                                          │ Spec, tests first, code, one  │
  Security scan  (•) Level default (off)  ( ) On  ( ) Off │ review.                       │
    If it finds issues  (•) Ask me  ( ) Ignore  ( ) Block │ Not done: no critic in the    │
  [ ] Don't stop for questions: let the agent decide      │ test and code steps; no       │
                                                          │ security review or scan.      │
  Usage: Claude subscription · 5-hour 62% · weekly 30%    │                               │
                                                          └───────────────────────────────┘
```

- **Slider:** `<input type="range" name="level" min="1" max="5" step="1" list="level-ticks">`
  with a `<datalist>` of the five labels, plus the labels as visible text under the track (a
  datalist alone doesn't show labels in every browser). It starts at the level the run would
  get without the slider: the chosen work item's front matter, else the config's, else Full.
- **Security scan:** three radio buttons, *Level default (on/off)*, *On*, *Off*. Three states
  rather than a checkbox, so the default can follow the slider without JavaScript; the
  "(on/off)" text comes from the summary fragment. *Level default* passes nothing; *On*/*Off*
  pass `--security-scan`/`--no-security-scan`. **If it finds issues** (*Ask me* / *Ignore* /
  *Block*) applies only when the scan is on below Full; the summary says so.
- **Bypass up front:** a checkbox, off by default, with a short warning in the summary when
  ticked ("Blocking questions will be answered by the agent without asking you").
- **Usage line:** the login kind and last known usage (§3.15), with a warning when a window is
  near or at its limit.
- The description on the right is part of the existing `#run-summary` fragment, which already
  re-renders on every `input` event in the form (`hx-trigger="input delay:400ms
  from:#run-form"`), so moving the slider updates it with **no new JavaScript**. It shows the
  label, number, agent count, `max_agent_calls`, the "good for" line, the effective scan
  setting and the dropped safeguards, all from `levels.py`.
- `app.js` (a file, as the CSP requires) sets `aria-valuetext` to the level's label on
  `input`, so a screen reader says "Tests" rather than "3". The summary is
  `aria-live="polite"` already. Without JavaScript everything still works.
- **Always passed.** `plan_run` always adds `--level <name>`. Like the budget, the level and
  the effective scan setting shown go in hidden fields, and `POST /runs/start` refuses (422,
  form re-rendered) if what's submitted differs from what was confirmed, so a run never starts
  at a level or scan setting nobody saw.

**Answering with Bypass** (the existing *needs answers* form, `ANSWERS_PLAN.md` §3.12): each
question gets a "Let the agent decide" checkbox next to its textarea (a ticked box makes the
textarea optional), and the form gets a second button, **Bypass all and continue**, beside
*Save answers and continue*. `answer_args` adds `--bypass=Q2` per ticked box, or `--bypass`
for the button. Validation is `answers.check_answers` with the bypass set, as before: a refused
form never starts a process. At the spec-pass cap, the form is replaced by the *Begin work?*
decision panel. Agent answers are shown with an "agent's best judgement" badge and their reason on
the run page, in the SPEC (or SOLO) panels and on the context tab.

**Decision points.** One **decision panel** in the run page header, drawn from
`RunState.decision`, with the explanation, the findings, and two buttons; `POST
/runs/{id}/decide` (CSRF as for every POST) with `choice` (validated against
`decisions.CHOICES[kind]`) and, for `fix`, a `note` → `advpipe decide <id> <choice> --detach
[--note=<text>]`; then redirect to the run page, which goes live. Per kind:

| Display status | Label | Panel |
|---|---|---|
| `needs_decision` (`red_checks`) | needs a decision | the failing checks (output tail), "fix pass N of 3", a note box, **Another fix pass** and **I'll take over** |
| `needs_decision` (`scan_hits`) | needs a decision | the hits, the "may not be real problems" explanation, **Ignore and continue** and **Block** (with one line on what each does) |
| `needs_decision` (`spec_cap`) | needs a decision | "Begin work?": the questions still open after 3 passes, what *begin* means (the agent decides them and every later question), **Begin work** and **Stop** |
| `paused` (`usage_limit`) | paused: usage limit | what limit was hit (subscription window and reset time, or Console credits), a link to add usage or credits, **Try again** and **Finish as is** |
| `handed_over` | handed over | the branch and the commands to continue by hand; no actions |
| `finished` | finished as is | the worktree path with its uncommitted changes, `git -C ... status`, and the clean-up command; no actions |

- *I'll take over* and *Finish as is* go through a confirmation page first, saying exactly what
  happens (commit and remove the worktree, or leave everything uncommitted and end the run),
  as *Clean up* does today.
- *Try again* says "If the limit is still in force, the run will pause again" and shows the
  reset time when known.
- `display_status` derives these from `decision` and `ended_by`. `needs_decision` and `paused`
  count under the "Needs you" filter; `handed_over` and `finished` don't. `VALID`:
  `needs_decision` and `paused` → `{"decide", "clean"}` (the choices themselves are checked
  against the kind); `handed_over`, `finished` → `{"clean"}`.

**Elsewhere in the UI:**

- **Work-item editor:** "Level" (the slider, plus "inherit from config", which leaves `level:`
  out of the front matter), "Security scan" and "If it finds issues" (the same choices), and
  "Don't stop for questions". `check_draft` validates them.
- **Runs list:** a level chip on every row; the header shows the usage line (§3.15).
- **Run detail:** the header shows the level, scan setting, dropped safeguards and cost/usage.
  The timeline is built from `steps_for(state.level)` instead of the fixed `TIMELINE` (keep
  `TIMELINE` as `steps_for(Level.FULL)`); `COMMIT_KEYS` gains `Stage.SOLO: "solo"`. Round
  panels for `stage-solo/pass-N/` (labelled "Pass 1", "Revision 1: with your answers to ...",
  "Fix pass 1 (you asked)"), `fix-pass-N/`, `review/single.json` and each decision reuse the
  existing panel templates. Rounds without a `critic.json` (level 3) say "no critic at this
  level" instead of "missing".
- **Spec & context tab:** show only the roles in `roles_for(level)`; `SEEN_BY` text that
  mentions critics is shown only when the level has them. Add `Role.SOLO` to `ROLE_INPUTS`.
- **Help page:** a "Levels" section with the §3.2 and §3.3 tables, built from `levels.py`, and
  short paragraphs on Bypass, the security scan and its policy, decision points, usage limits
  and the two kinds of login. Add `Role.SOLO` to `ROLE_JOBS` and the new display statuses to
  `STATUS_NEXT`.
- **Optional:** "typical cost at this level in this repo": the median `cost_usd` of this repo's
  complete runs at that level, shown when there are at least three. Only if L5 has time.

**Security checklist** (`UI_PLAN.md` §5) applies: the level from the browser is validated with
`parse_level`, the scan and policy choices against their values, decision choices against the
run's current kind, question ids against `Q\d+` and the run's pending set, and note and answer
lengths, before anything becomes an argument; arguments use the `--opt=value` form so text
starting with `-` can't look like an option; nothing reaches a shell; every new POST needs the
CSRF token; links to add usage or credits are fixed constants, never built from run data.

### 3.14 Claude Code skill (`.claude/skills/adversarial-build/SKILL.md`)

Add a **Level** section before the procedure:

- The user may name a level ("`/adversarial-build --level basic fix the typo in ...`"), the
  security scan ("with the security scan" / "no scan"), and "don't ask me questions" (bypass up
  front). Otherwise use `full` with the scan. Never pick a lower level yourself; if the work
  item looks bigger than the chosen level, say so once and continue at the chosen level.
- A table of which stages run at each level (from §3.2), and the solo procedure: launch
  `solo`; if `task.md` has `BLOCKING:` questions, revert anything else it wrote and **ask the
  user**, offering Bypass ("or say *bypass* and I'll have the agent decide"); with answers (or
  the agent's best-judgement answers from a bypass request to `solo`), relaunch `solo` for the
  revision and write the Answers section yourself, marking agent answers as in §3.6. At
  Reviewer, launch `code-critic` once with `task.md`, the diff and gate output ("single review
  pass"), and send blocking findings to `solo` for one fix pass. No arbiter below Adversarial.
- **Decision points, as questions to the user:** when checks are still red after what the
  level does automatically, ask "another fix pass (with an optional note) or take over?"; when
  the scan reports issues below Full, ask "ignore or block?". At most 3 fix passes.
- At levels 3 to 5, Stage 1's question handling also offers Bypass. At every level, Bypass
  covers only the questions asked at that stop; after the third spec pass, ask "Begin work?
  I'll have the agent decide the remaining questions and any later ones", and stop if the
  user says no.
- (A usage limit inside a Claude Code session is Claude Code's own business; the skill doesn't
  handle it.)
- The summary states the level, the scan setting, any bypassed questions, every decision, and
  what wasn't done.

Add `.claude/agents/solo.md`. Update the README section "Using it inside Claude Code".

### 3.15 Subscription usage next to the budget, and pausing at a limit

advpipe talks to Claude through the Agent SDK, which runs the Claude Code CLI with whatever
login that CLI has. With **Console credits** (an API key), `total_cost_usd` is real money and
the dollar budget means what it says. With a **Claude subscription** login, the same field is
an *API-equivalent* estimate: nothing is billed per call, and the limit that matters is the
subscription's usage windows (the 5-hour and weekly limits Claude Code shows in `/usage`). This
section makes advpipe show that usage when it can, say plainly which kind of login it's on, and
**pause** (§3.5) instead of failing when either kind of limit runs out.

**Both logins are supported** (Brendan, 2026-10-14). advpipe uses whatever the Claude Code CLI
is logged in with. It never asks for, reads or stores a key or token, and has no login flow of
its own. With **Console credits** it **warns**, because every call is real money:

- **When the login is known or likely Console before the run starts** (`ANTHROPIC_API_KEY` is
  set in the environment, or the last known login in `.advpipe/usage.json` is Console), the
  commands that start agent work (`run`, `resume`, `answer`, and `decide` choices that resume)
  print to stderr before starting: "Warning: this run uses Anthropic Console credits, which
  is real money. It can spend up to $15.00 (the budget; change it with --budget)." With
  `--parallel`, once, with the total of the budgets.
- **When it only turns out to be Console** from the first agent call's init message, the same
  warning is printed then, and a note is added to the run.
- It's a warning, not a confirmation prompt, so scripted and detached runs aren't held up.
- `report.md`'s login line says "Anthropic Console credits (real money)"; the summary line of a
  Console run keeps today's format.
- **Web UI:** when the login is known or likely Console, the New run dialog shows a warning box
  above *Start* ("This run uses Anthropic Console credits: it can spend up to $15.00 of real
  money"), and so do the *Resume*, *Try again* and answer/decide forms that restart work. The
  run page's cost meter is labelled "real money". The Help page explains the two logins.
- Subscription logins get no warning; their dollar figures are labelled "API-equivalent".

**What the SDK gives us** (`claude-agent-sdk` 0.2.164, checked 2026-10-14; check again before
building):

- `RateLimitEvent` messages in the `query()` stream, each with a `RateLimitInfo`: `status`
  (`allowed` / `allowed_warning` / `rejected`), `rate_limit_type` (`five_hour`, `seven_day`,
  `seven_day_opus`, `seven_day_sonnet`, `overage`), `utilization` (0.0–1.0), `resets_at` (Unix
  time), overage fields, and `raw`. The SDK docstring says the CLI emits one **when the status
  changes**, so they may be rare, and a run can finish without seeing any. Don't promise a live
  percentage that the SDK doesn't send.
- `AssistantMessage.error`, an `AssistantMessageError`: `"rate_limit"` and `"billing_error"`
  (the Console credit balance ran out) are the two that mean "a limit, not a bug".
- The init `SystemMessage` at the start of each `query()`. Claude Code's stream output has
  carried an `apiKeySource` field there; **verify in the session** whether this SDK version
  exposes it (`SystemMessage.data`) and what values mean "subscription login" versus "API key"
  before relying on it. Fallbacks, in order: `apiKeySource`, then "a `RateLimitEvent` arrived"
  (those only apply to subscription logins), then `ANTHROPIC_API_KEY` set in the environment
  (Console), else "unknown".
- Don't call undocumented endpoints (e.g. whatever `/usage` uses internally) and don't read
  Claude Code's credential files. Only what the SDK stream reports.

**Recording usage** (`runner.py`, `models.py`, `usage.py` new):

- `AgentResult` gains `auth: Literal["console", "subscription", "unknown"]` and
  `rate_limits: list[RateLimitSnapshot]` (every `RateLimitEvent` seen during that call, in
  order). `SdkAgentRunner.run` collects them from the stream next to `ResultMessage`.
  `FakeAgentRunner` can script both, and limit errors, so everything below is testable without
  API calls.
- `RateLimitSnapshot(BaseModel)`: `window`, `status`, `utilization: float | None`,
  `resets_at: datetime | None`, `seen_at: datetime`. Never store `raw` beyond these fields.
- `RunState.auth` (default `"unknown"`, so old runs load) and `RunState.rate_limits: dict[str,
  RateLimitSnapshot]` (the latest per window). `call_agent` updates both after each call.
- A repo-wide cache, `.advpipe/usage.json` (`usage.record(repo, snapshots)`, written with the
  same temp-file-and-rename as `run.json`, latest per window), so the UI can show the last
  known usage even with no run going. Each entry shows its age; one older than its window's
  `resets_at` is shown as "reset since; usage unknown".
- An event kind `usage` (`window`, `status`, `utilization`, `resets_at`) per snapshot, so the
  live log and SSE stream show changes as they happen.

**Pausing at a limit:**

- `SdkAgentRunner.run` raises `UsageLimitReached(kind="subscription" | "credits", snapshot,
  message)` when it sees a `rejected` `RateLimitEvent` (subscription, with the window and reset
  time) or an `AssistantMessage.error` of `rate_limit` or `billing_error`. It stops reading the
  stream; the cost reported so far (if any) is still added to the budget.
- `Orchestrator.run` turns it into a `usage_limit` decision point (§3.5) with an explanation
  such as: "Your Claude subscription's 5-hour usage limit was reached during the code stage. It
  resets at 14:00. Add extra usage to your plan, or wait for the reset, then press *Try again*.
  Or finish the run as it is: the work so far stays in the worktree, uncommitted." or "Your
  Anthropic Console credit balance ran out during the code stage. Add credits, then press *Try
  again*. Or finish the run as it is: ...".
- A foreground run in a terminal asks at once (§3.5): try again now, finish as is, or decide
  later.
- `allowed_warning` adds a note and a `usage` event ("approaching the 5-hour limit: 85% used,
  resets at 14:00"); the run carries on.
- **The dollar budget still applies with a subscription**, as a cap on API-equivalent cost.
  That keeps rule 5 (every run bounded) independent of the login. Labels change, the logic
  doesn't: "Cost" becomes "API-equivalent cost (not billed: subscription login)".
  `budget_exceeded` (advpipe's own cap) is unchanged: resume with `--budget`.

**Where usage is shown:**

- `cli._summary` and `status <id>`: the cost line becomes `$0.41 (API-equivalent)` for
  subscription runs, plus `5-hour limit: 62% used, resets 14:00` when known. Console runs are
  unchanged.
- `report.md`: under the cost table, "Login: Claude subscription", the latest known usage per
  window, and a note that the dollar figures are estimates.
- **Web UI:** the run page's cost meter, the New run dialog's budget line and the runs list
  header show the login kind and, for subscriptions, the last known usage per window as small
  meters ("5-hour: 62%, resets 14:00 · weekly: 30%, resets Mon"), from `RunState.rate_limits`
  (run page) or `.advpipe/usage.json` (elsewhere). "Not known yet: advpipe sees usage only when
  Claude reports a change" when there's nothing. The New run dialog warns when a window is at
  `allowed_warning` or `rejected`.
- `advpipe usage` (new, read-only): prints the login kind and the cached usage per window,
  with ages.

## 4. Invariants to test (all milestones)

- With no level anywhere and no new stop reached, a run is `full` with the scan on, and its
  agent calls, prompts, files, events and report are byte-for-byte what they are today (apart
  from the `Level: full` report line and the new `run_start` fields).
- At every level, `complete` requires green final gates; a failed gate is a blocking finding.
  The only exception is scanner hits below Full that a person chose to ignore, and that choice
  is recorded.
- At every level, blocking questions stop the run unless the person chose Bypass for that
  stop, chose *Begin work* at the cap, or chose Bypass up front. A Bypass never answers
  questions asked after it. No code path bypasses without a recorded person choice.
- Every decision point waits for a recorded person choice (or an up-front policy); no code path
  picks one. Fix passes never exceed `max_fix_passes`.
- At levels 3 to 5, a `red_checks` decision point is reached only after the stage's round cap
  and `_settle` have run; never earlier.
- At a decision point the worktree is left untouched, and resuming after a decision keeps it;
  `finish` commits nothing.
- A usage limit or credit error never ends a run `error` or `complete`: it always pauses at a
  `usage_limit` decision point.
- Between two person decisions, the number of agent calls never exceeds
  `max_agent_calls(level, limits)[1]`, and the happy path makes exactly `[0]`.
- Only roles in `roles_for(level)` are ever called (assert on `FakeAgentRunner`'s recorded
  requests).
- Wherever a critic or reviewer is called (levels 2 to 5), its prompt contains `task.md`, the
  diff and gate output, and no author reply and no bypass reply (extend
  `test_critics_get_artifacts_not_reasoning` to every level).
- At levels 3 to 5, the test author's prompt contains `task.md` and no implementation, and the
  test-file guard and red gate run.
- A solo pass that asks blocking questions leaves no file changed but `task.md`.
- Agent answers and ignored scanner hits are marked as such in `task.md` (answers),
  `report.md`, `status` and every UI page that shows them.
- The level and scan setting never change during a run, including across resume.
- No agent output can set the level, the scan setting or policy, Bypass, or a decision.
- Console-login runs record cost exactly as today and always show the Console warning; usage
  snapshots never change the budget logic.

## 5. Milestones

Each is a single Claude Code session. Each ends with `pytest`, `ruff check .`,
`ruff format --check .` and `mypy --strict src/` green, `README.md` and `docs/ARCHITECTURE.md`
updated for what changed, and a commit. Tests use `FakeAgentRunner` (`tests/fakes.py`): no API
calls.

**L0: levels as data, plumbing, the scan toggle, and Adversarial.** §3.1, §3.3, §3.9, §3.10
(without `decide`, `--bypass*`, `usage`, and with `scan_failures` accepted but only `block`
behaviour until L3; `ask` falls back to `block` with a note), the scan toggle from §3.7, the
`run_start` fields and report line from §3.12, and the one level that needs no new stage logic
beyond skipping review: **Adversarial**. `basic`, `reviewer` and `tests` are accepted by
the config but refused at run start with "level X isn't available yet" until L1/L3 land.
Tests, in a new `tests/test_levels.py`:
- `SHAPES`, `roles_for`, `steps_for`, `dropped_safeguards`, `parse_level` cover every level;
  `full` with the scan on drops nothing;
- config: `[pipeline]` by name and number, unknown names refused, defaults; `max_fix_passes`;
- precedence for `level` and `security_scan`: command line over front matter over config over
  the level default; front-matter errors with file and line;
- the scan: on at Full by default (today's behaviour), off at Adversarial by default; off at
  Full skips it in review and the report says so; not installed → skipped at every level;
- old `run.json` without the new fields loads as `full` with the scan on; resume keeps level
  and scan and refuses a config that changes them;
- an Adversarial run calls no reviewer, completes, and its report says no review ran;
- `advpipe levels` output; `_summary` shows `[adversarial]` and is unchanged for `full`.

**L1: the Tests level (no critics, single review, no arbiter).** §3.4's single review, §3.8. Red
gates at the end of `_settle` still end the run `needs_human` until L3. Tests:
- `adversarial_stage(..., critic=False)`: no critic calls; gate failures go back to the author
  up to the cap; red gate and test-file guard still apply; no `critic.json`;
- `_settle(..., arbiter=False)`: no arbiter call, every finding ruled `fix` with the level
  reason, one final author pass;
- `single_review_stage`: one `code-critic` call with the full diff, gates and test-file list
  and no author text; PASS completes; blocking → one coder pass → gates;
  `review/single.json` written; a `test-defect` finding takes the ordinary path;
- blocking spec questions and answering work at the Tests level exactly as at Full;
- the `max_agent_calls` worst-case test for Adversarial and Tests.

**L2: Bypass (levels 3 to 5).** §3.6 for the spec writer, the CLI part of it, and the report.
The solo levels don't exist yet; write the question/bypass helper so L3 can call it from the
solo stage. Tests in `tests/test_bypass.py`:
- `record_answers` with a bypass set: bypass all, mixed, an id both answered and bypassed
  (refused), unknown ids, the existing refusals;
- resume after Bypass: one bypass call to `spec-writer` (JSON, retried once on bad JSON, files
  it touched reverted), then the revision pass with the agent's answers; the committed
  `task.md` marks them; the run completes; `spec/pass-N/bypass.json` exists;
- unparseable twice → the fallback answer, and the run still goes on;
- Bypass covers one stop: after a Bypass, a revision's new blocking questions stop the run
  again and can be answered or bypassed again;
- at the cap the run still stops for good, as today, until L3 adds the *Begin work?* decision
  point;
- `--bypass-questions` / front matter / config: questions never stop the run, and each is
  recorded as bypassed; at the cap, the agent's answers go in without another revision and the
  run goes on;
- `advpipe answer --bypass`, `--bypass Q2`, interactive "Bypass Q2?", `Q2: BYPASS` in answers
  files; the `answers` event's `bypassed` field;
- later agents' prompts contain the Answers section and never the bypass reply;
- a `full` run with only person answers has today's Answers wording and report.

**L3: decision points, and Basic and Reviewer.** §3.5 (the `red_checks` and `scan_hits`
kinds; `usage_limit` comes in L6), §3.7's policy, §3.4's solo role and stage, §3.6 for the solo
agent, §3.11. Remove L0's "not available yet" check and the `ask`→`block` fallback. This is the
biggest milestone: if it runs long, stop after the decision points at levels 3 to 5 and the
scan policy, commit, and leave the solo levels for a follow-up session (say so in the summary
and in this file). Tests in `tests/test_decisions.py` and `tests/test_solo.py`:
- `red_checks` at levels 3 to 5: reached only after the round cap and `_settle` (and at red
  final gates), never earlier; the worktree untouched; `decide fix` (with and without a note:
  the note is in the author's prompt verbatim) → one pass by the right author (test-author for
  the tests stage, coder otherwise) → gates → carries on or asks again; the 4th `fix` refused;
- `decide takeover`: commits, removes the worktree, keeps the branch, ends the run; then
  `decide`, `resume` are refused; `clean` follows its usual rules;
- `scan_hits`: asked on the first scanner failure below Full with policy `ask`; `ignore` →
  minor findings marked, later hits not asked again; `block` → blocking, back to the author,
  `red_checks` if unresolved; up-front `ignore`/`block` never ask; Full never asks;
- `spec_cap` at every level that has a spec stage or solo stage: the pass at the cap with
  blocking questions stops with *Begin work?* (not "stops for good"); `begin` → one bypass
  call, the answers written without another revision, no later question stops the run, the run
  carries on; `stop` → ended, not resumable; answering at the cap is refused and names
  `advpipe decide <id> begin`;
- `decide` refusals: wrong state, a choice from another kind, live lock, branch gone, note too
  long or on the wrong choice; an interrupted fix pass reruns on `resume`; `resume` on a
  waiting run names `advpipe decide`;
- the terminal prompt in a foreground run (CliRunner with a fake TTY), and "decide later";
- the `solo` prompt and `.claude/agents/solo.md` in sync; `ROLE_TOOLS`/`ROLE_MODEL` entries;
- Basic happy path: one agent call, task.md + tests + code committed as one stage, final gates
  green, `complete`; `stage-solo/pass-1/*` and run-directory `task.md` exist;
- solo asks blocking questions: everything but `task.md` reverted, run awaits answers,
  `advpipe answer` works, the revision pass implements, completes; Bypass works too (the bypass
  call goes to `solo`);
- solo without `task.md` → blocking guard finding;
- red gates at Basic: no automatic fix pass; `red_checks` right after the solo pass;
- Reviewer: one `code-critic` call after the solo stage, an automatic fix pass by `solo` (not
  `coder`) for review findings, then red gates ask;
- resume mid-solo and mid-review at both levels; the worst-case call tests for both;
- `decide` CLI exit codes, `--detach` through the fake child, the summary lines;
- the invariants in §4 at every level (one parametrised test per invariant).
Existing tests that expect a run to *end* `needs_human` on red gates now see it stop at a
decision point (still `needs_human`); update their expectations about `decision` and the
report, and say which ones changed in the summary.

**L4: report, status and the Claude Code skill.** §3.12 in full, §3.14, and the docs. Tests:
- `report.md` lists the level, scan setting and dropped safeguards for every level; agent
  answers and ignored scanner hits are marked; the Decisions section for every kind and outcome
  (waiting, fix, takeover, ignore, block); `status <id>` shows all of it;
- the skill file mentions every level, Bypass, the scan, decision points and the `solo` agent
  (a light text check, as the sync test does for agents).
Update the README (a "Choosing a level" section near the top with the §3.2 table and when to
use each; Bypass; the scan toggle and its policy; decision points and `advpipe decide`;
`--level`, `advpipe levels`, the `[pipeline]` table, `max_fix_passes`, front-matter keys; the
"when not to use the pipeline" advice now points at Basic/Reviewer), `docs/ARCHITECTURE.md` (a
"Levels" section, a "Decision points" section, the §3.3 table, the module map, persistence and
events tables, resume rules), `BUILD_SPEC.md` §1 (a note under the design rules, as
`UI_PLAN.md` did for §9), `docs/ANSWERS_PLAN.md` (a status line noting Bypass and the solo
stage), and `CLAUDE.md` (the design-rules line from §3.3).

**L5: Web UI.** §3.13 (the usage line and the `paused` panel come with L6, or here if L6 is
already done). Tests in `tests/test_ui_levels.py` and `tests/test_ui_decisions.py` with
`TestClient` over real run directories from the orchestrator with fake agents:
- the New run form has the slider, the three-way scan choice, the scan policy and the bypass
  checkbox; the slider starts at the inherited level; the summary shows the label, agents,
  calls, scan setting and dropped safeguards for each value 1–5 and each scan choice;
- `POST /runs/start` passes `--level`, the scan flag only when not *Level default*,
  `--scan-failures` only when not *Ask me*, and `--bypass-questions` only when ticked
  (monkeypatch `actions.subprocess.run` and `control.child_command`, as `test_ui_actions.py`
  does); refuses a level or scan setting that doesn't match the confirmed one, out-of-range and
  non-number levels, with no subprocess call; CSRF and token required;
- the answer form's per-question Bypass boxes and *Bypass all* button produce the exact
  `advpipe answer` argument lists; at the cap the *Begin work?* panel replaces the form; agent answers show the
  badge and reason, escaped;
- the decision panel for each kind: its explanation, findings and two buttons; `POST
  /runs/{id}/decide` produces the exact argument list for each choice; a choice from another
  kind, a long note, or a note on the wrong choice is refused before any process; takeover and
  finish go through their confirmation pages; panels appear only for their statuses;
  `handed_over` and `finished` show what's where and no actions; all text escaped;
- the work-item editor round-trips the four front-matter keys (and "inherit" leaves them out);
- the run page timeline shows only the level's steps; solo, revision, fix-pass,
  single-review and decision panels render; level-3 rounds say "no critic at this level";
  every status renders at every level;
- the runs list shows the level chip; the context tab shows only the level's roles; Help has
  the Levels tables and covers `solo` and the new statuses;
- no inline script or style on the new pages (the existing polish checks).
Plus a manual browser check (as in U2 and A2): move the slider and watch the summary change;
start a Basic run on a copy of `tests/fixtures/sample_repo` with a fake agent that fails the
checks, ask for a fix pass from the page, then take over another; bypass a blocked run's
questions; ignore and block scan hits. Report what was checked by hand and what wasn't.
Re-make the screenshots (`scripts/ui_screenshots.py`) so the README shows the slider, and
update `docs/UI_PLAN.md` with a line noting this feature.

**L6: subscription usage, and pausing at a limit.** §3.15 and the `usage_limit` kind of §3.5
(`retry`, `finish`), CLI and, if L5 is done, the UI parts (`paused` and `finished` panels,
usage meters). It needs L3 (decision points); otherwise independent of the levels. Start by
checking the SDK (the `RateLimitEvent` fields, `AssistantMessage.error` values, whether the
init message exposes the login kind) and update §3.15 if it differs. Tests in
`tests/test_usage.py` with `FakeAgentRunner` scripting rate-limit snapshots, limit errors and
the login kind:
- snapshots are recorded per call, the latest per window lands in `run.json` and
  `.advpipe/usage.json`, and a `usage` event is written for each;
- `allowed_warning` adds a note and carries on;
- a `rejected` event, a `rate_limit` error and a `billing_error` each pause the run at a
  `usage_limit` decision point from any stage (spec, a critic call, review, the arbiter), with
  the right explanation (window and reset time, or credits); the cost so far is kept;
- `decide retry`: the interrupted stage restarts with its partial work kept and the "cut off by
  a usage limit" line in the author's prompt; a still-limited retry pauses again;
- `decide finish`: nothing committed, the worktree kept with its changes, the run final and not
  resumable; `clean` refuses the dirty worktree without `--force`;
- the terminal prompt on a foreground run;
- login detection: each fallback in order; "unknown" never treated as Console or
  subscription; subscription runs say "API-equivalent" everywhere; the budget still stops a run
  that crosses it;
- the Console warning: printed before the run when `ANTHROPIC_API_KEY` is set or the cached
  login is Console, printed after the first call when only that reveals it, never for
  subscription runs, once for `--parallel`; the report's login line; the UI warning boxes
  (if L5 is done);
- stale snapshots (past `resets_at`) are shown as unknown; `advpipe usage` output;
- UI (if L5 is done): the `paused` panel, *Try again* and *Finish as is* argument lists, the
  finish confirmation, the `finished` page, the usage meters, the warning and the empty state,
  escaping.
Plus one real check, **with Brendan's go-ahead** (it uses his subscription): a Basic run on a
copy of `tests/fixtures/sample_repo` while logged in with a subscription, to see which events
actually arrive. Report what arrived and update §3.15. Don't try to trigger a real limit.

Order: L0 → L1 → L2 → L3 → L4 → L5 → L6. L3 needs L2 (the solo stage reuses the bypass
helper). L5 needs L3 (all five levels and decision points must work before the UI offers them).
L6 needs L3; it can come before L4 and L5, in which case L5 builds its UI parts too.

## 6. Things that will bite you

- **`_run`'s resume logic is keyed by `state.commits`.** A solo run records `solo` (and maybe
  `review`), never `spec`/`tests`/`code`. Code that assumes `"spec" in commits` means "past
  the spec" (`answers._blocked_at_spec`, `migrate`, `not_waiting_message`, the UI's current
  stage logic in `ui/detail.py`, `check_resumable`) must go through `answers.spec_stage` /
  `steps_for(state.level)` / `COMMIT_KEYS` instead.
- **`Workspace.reopen` wipes uncommitted work.** Every resume after a decision must use
  `keep_changes=True`, or the work the person decided about is gone. Plain resumes (`running`,
  `error`, `budget_exceeded`) keep today's reset; don't change that. A solo revision reads the
  previous `task.md` from the run directory (as `_spec` does).
- **A retry restarts the interrupted stage at round 1** with the partial work in place, so
  that stage's round cap starts again after a usage-limit retry. That's acceptable (each retry
  is a person's choice, and the budget still applies); don't try to resume mid-round.
- **`UsageLimitReached` can come from anywhere**: inside `asyncio.gather` in `review_stage`
  (cancel the other reviewer cleanly), inside `call_json_agent`'s retry, inside `_settle`.
  Catch it only in `Orchestrator.run`, like `BudgetExceeded`, and test it from several places.
- **The stream may end without a `ResultMessage`** when a limit hits. `SdkAgentRunner.run`
  must raise `UsageLimitReached`, not the "no result" error it raises today.
- **Old tests that end on red gates.** At every level, red gates after `_settle` and at
  FINAL_GATES now stop at a decision point (still `needs_human`). L3 must update those tests
  deliberately, not by loosening assertions.
- **`ui/detail.py` hard-codes the six steps** (`TIMELINE`, the `stage is Stage.X` branches in
  the step-state function). Make it level-aware once, in one place.
- **`_settle` assumes an arbiter.** At levels without one, rulings are still recorded (so the
  report and the UI's ARBITER panel logic don't break); decide whether the panel shows "not
  used at this level" and test it.
- **The code critic's prompt mentions test-defects and earlier rounds.** The single-review
  paragraph must make clear there's one pass. Edit `src/advpipe/prompts/code-critic.md` first,
  then copy the body to `.claude/agents/code-critic.md` (the sync test enforces it).
- **Bypass calls use an author role with write tools.** The prompt says not to edit files, and
  code reverts anything changed anyway. Don't give the bypass call a new role just for that.
- **Two BLOCKING regexes**: `stages.blocking_open_questions` and `ui/render.py _BLOCKING` must
  keep accepting the same prefix forms; the solo prompt must use the same form.
- **The Answers section's wording changes only when an agent answered.** Tests from
  `ANSWERS_PLAN.md` check today's wording; keep it for person-only answers.
- **Scanner output is free text.** Pull rule, file and line out of semgrep's output where you
  can; when you can't, the hit is still shown (as the output tail). Never drop a hit because
  it didn't parse.
- **`clean` and dirty worktrees.** A finished run's worktree has uncommitted changes on
  purpose. `clean` must refuse it without `--force` and say what would be lost.
- **Rate-limit events are sparse.** They're sent on status changes, not per call. Design the
  UI around "last known", with its age, never a live gauge.
- **Login detection must not guess "console" from silence.** No event and no `apiKeySource`
  means "unknown", and the UI says "cost shown as API-equivalent if you're on a subscription".
- **Front matter is strict.** Adding keys to `KEYS` changes the error text that lists them;
  update the tests that check it.
- **The New run summary re-renders on every input.** Keep `levels.py` lookups cheap; read
  `.advpipe/usage.json` once per render, and don't scan run directories on every keystroke.
- **Budget is unchanged by level.** A Basic run under a $15 budget just spends far less. Don't
  add per-level budgets in this plan (§8).
- **Windows**: `takeover` removes a worktree; use `Workspace.remove` (it already handles the
  platform), never `shutil.rmtree` directly. Process checks go through `RunLog.lock_holder()`.
- **Prompts are the source of truth:** `src/advpipe/prompts/solo.md` first, then
  `.claude/agents/solo.md`.

## 7. Docs to update (by the milestone that changes the behaviour)

- `README.md`: "Choosing a level"; Bypass; the security scan toggle and policy; decision points
  and `advpipe decide`; `--level`, `--security-scan`, `--scan-failures`, `--bypass-questions`
  and `advpipe levels`; configuration (`[pipeline]`, `max_fix_passes`); work-item front-matter
  keys; the status table (the new display statuses) and summary lines; "when not to use the
  pipeline"; the UI section (slider, bypass, decision panels) and screenshots; the Claude Code
  section. For L6: a "Subscription or Console credits" paragraph in Cost expectations, pausing
  at a limit, `advpipe usage`.
- `docs/ARCHITECTURE.md`: big picture (levels), module map (`levels.py`, `decisions.py`,
  `usage.py`), a run step by step (SOLO, single review, the switches, Bypass, decision points),
  resume rules (`keep_changes`), persistence and events tables, Budget and Talking to Claude
  (usage, limits), the web UI section, testing strategy.
- `BUILD_SPEC.md` §1: a note under the design rules that rules 2, 4 and 6 apply at the levels
  that have those agents, default `full`; on the `[1] Spec writer` line, that blocking questions
  can be bypassed; and in §6, that red gates and usage limits stop at a decision point.
- `docs/ANSWERS_PLAN.md`: a status line noting Bypass and the solo stage.
- `CLAUDE.md`: the design-rules line (§3.3); Layout: `docs/LEVELS_PLAN.md` (done with this
  plan).
- `docs/UI_PLAN.md`: a line under the milestones noting the slider, Bypass and decision panels.

## 8. Open questions

None open: every question was settled on 2026-10-14 (see the decisions note at the top). If a
session finds something this plan doesn't answer, add it here with a proposed answer, use the
proposal, and say so in the summary.

## How to use this plan in a session

Start each session with something like:

> Read `docs/LEVELS_PLAN.md`, `CLAUDE.md`, `docs/ARCHITECTURE.md` and `docs/ANSWERS_PLAN.md`.
> Implement milestone **L0** only. Follow the principles and the invariants in §4. Stop at the
> end of the milestone and summarize what you built, how you verified it, and anything you
> couldn't check.

If something in the code no longer matches §2, trust the code, adjust the design minimally,
and say so in the summary and in this file.
