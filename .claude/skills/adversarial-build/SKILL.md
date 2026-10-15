---
name: adversarial-build
description: Drive one work item (feature or bug fix) through an adversarial author/critic pipeline of subagents (spec, tests, code, review, arbiter) with deterministic gates and round caps, leaving a branch ready for human review. Use when the user asks for an adversarial build, or invokes /adversarial-build with a work item.
---

# Adversarial build

Drive **one work item** through the pipeline below using the subagents in `.claude/agents/`.
You are the orchestrator. Follow the procedure exactly. You don't skip stages, you don't write
code or tests yourself, and you don't let an agent decide whether a stage runs. Which stages run
depends on the **level** (next section); the default is `full`.

```
[1] spec-writer ──► task.md                max 3 passes   BLOCKING questions: ask the user (or Bypass), then revise
[2] test-author ⇄ test-critic              max 2 rounds   gate: new tests exist and FAIL for the right reason
[3] coder ⇄ code-critic                    max 3 rounds   gate: tests + types + lint green, no blocking findings
[4] standards-reviewer ∥ security-reviewer one pass each, launched in parallel
[5] arbiter                                only if blocking findings remain or are disputed
[6] done → branch ready for human review, or needs_human with open findings
```

## Level

The user picks how much checking the run gets. **You never pick or change the level**, and no
agent can.

- The user may name a level ("`/adversarial-build --level basic fix the typo in ...`"), the
  security scan ("with the security scan" / "no scan"), and "don't ask me questions" (Bypass up
  front, below). Without those: level `full`, with the security scan on.
- If the work item looks bigger than the chosen level, say so once and continue at the chosen
  level.

| # | Level | Stages that run | Agents |
|---|---|---|---|
| 1 | `basic` | Solo → Final gates | `solo` |
| 2 | `reviewer` | Solo → Single review → Final gates | `solo`, `code-critic` (one pass) |
| 3 | `tests` | Spec → Tests → Code → Single review → Final gates | `spec-writer`, `test-author`, `coder`, `code-critic` (one pass) |
| 4 | `adversarial` | Spec → Tests ⇄ → Code ⇄ → Arbiter → Final gates | `spec-writer`, `test-author`, `test-critic`, `coder`, `code-critic`, `arbiter` |
| 5 | `full` | Stages 1 to 6 below | all of them |

What a level leaves out:

- **Basic, Reviewer:** one agent (`solo`) writes `task.md`, the tests and the code, so the tests
  aren't written before the code and nobody checks them independently. No test-file guard and
  no red gate. Basic has no review at all.
- **Tests:** no critics. The authors loop on the gates alone (up to the round caps), then
  `code-critic` reviews the whole change once. Skip the `test-critic` and `code-critic` steps
  in Stages 2 and 3; the gates, the red gate and the test-file guard still apply.
- **Adversarial:** everything except Stage 4 (no standards or security review).
- **No arbiter below Adversarial:** where the procedure says "go to the Arbiter", instead rule
  every open finding `fix` yourself (reason: "no arbiter at this level") and give the author
  the one final pass.
- **The security scan** is its own switch: on at `full`, off below it unless the user asks. At
  `full` it feeds the security reviewer. Below `full`, if it's on, run it with the other
  gates; see "Decision points" for what a failure means.
- **Gates are never dropped.** Tests, types and lint must be green for `complete` at every
  level, and a failed gate is a blocking finding at every level.

State the level, the scan setting and what it leaves out when you start.

### The solo procedure (Basic and Reviewer)

1. Launch `solo` with the work item and the gate commands. It writes `task.md` first, then the
   tests and the code, and runs the gates.
2. If `task.md` has `BLOCKING:` questions, the agent was told to stop before writing code.
   **Revert every changed file except `task.md`**, then ask the user (see "Blocking questions
   and Bypass"). With answers, launch `solo` again with its previous `task.md` and the Q&A to
   revise `task.md` and do the work, and write the `## Answers` section yourself.
3. Run all gates. No automatic retry: if they're red, go to "Decision points".
4. **Basic:** go to the final gates. **Reviewer:** launch `code-critic` **once** with `task.md`,
   the whole diff from the base commit (tests included, `task.md` excluded), the gate output and
   the list of changed test files, and say "This is a single review pass." It has no earlier
   rounds and won't get another. Treat a `test-defect` finding like any other blocking finding.
   Send blocking findings to `solo` for **one** fix pass, then run the gates again; still red
   goes to "Decision points".

## Blocking questions and Bypass

Blocking questions **always stop the run**, at every level (the spec writer's in Stage 1, the
solo agent's above). Ask the user all of them at once, numbered `Q1`, `Q2`, ...; the user can
answer each or say **bypass** ("or say *bypass* and I'll have the agent decide").

- **Bypass** covers only the questions asked at that stop. Launch the agent that asked
  (`spec-writer` or `solo`) once, tell it to answer each question with its best judgement and a
  one-paragraph reason, and not to edit files. Revert anything it changed. Then revise as with
  any answers. Any later question stops the run again and offers the same choice.
- **Mark agent answers** in the `## Answers` section, so a later agent and the human reviewer can
  tell them apart: the intro line gains "Answers marked "agent's best judgement" were chosen by
  the agent because the person chose Bypass.", and each such answer reads
  `**Answer (agent's best judgement):** <answer> Reason: <reason>`. Answers the person wrote keep
  the usual wording. Later agents see the Answers section, never the bypass reply itself.
- **Bypass up front** ("don't ask me questions"): bypass every blocking question as it comes up,
  and say so in the summary.
- **After the third pass** with blocking questions still open, don't ask again. Ask **Begin
  work?**: "I'll have the agent decide the remaining questions, and any later ones." If the user
  says yes, run one Bypass call for the open questions, write the answers into `task.md`
  without another revision, and carry on, bypassing any later question. If no, stop with
  `needs_human`.

## Decision points

Some stops need a person. Ask the user, wait, and never choose for them:

- **Failing checks.** The gates are still red after everything the level does by itself (the
  final pass after the arbiter or the single review at levels 3 to 5; right after the solo pass
  at Basic; after Reviewer's fix pass; and at the final gates). Ask: "another fix pass (with an
  optional note), or take over?" A fix pass is one pass by the stage's author (`test-author` for
  tests, `coder` for code and review, `solo` at Basic and Reviewer) given the failing output and
  the user's note verbatim, then the gates; no critic round. At most **3** fix passes per run.
  *Take over* means: commit the work as it is ("advpipe: work in progress, handed over (checks
  failing)"), tell the user the branch and the gate commands, and stop.
- **Security scan hits below `full`** (scan on). No security reviewer checks them. Ask once:
  "ignore or block?" *Ignore*: record the hits as a minor finding "ignored by you" and carry on.
  *Block*: treat the hits like a failing test (the author fixes them and you rerun the scan; if
  they can't be cleared, ask the failing-checks question). The choice sticks for the rest of the
  run.
- **Begin work?** after the third spec pass (above).

A usage limit hit inside a Claude Code session is Claude Code's own business; this skill doesn't
handle it.

## Setup

1. Identify the **target repo** (ask if unclear) and the **work item**.
2. Work on a new branch `advpipe/<short-slug>` off the current HEAD. If the working tree is dirty,
   stop and ask the user. Never push, merge, or open a PR.
3. Determine the **gate commands** from the target repo's `pipeline.toml` `[gates]` if present,
   otherwise from its `CLAUDE.md` / `pyproject.toml`. Defaults: `pytest -q`, `mypy .`,
   `ruff check .`, and `semgrep --error --config auto` only if installed. Note the test paths
   (default `tests/`).
4. Keep a short run log as you go: stage, round, verdicts, gate results.

## Gates are ground truth

You run the gates yourself with Bash after each author pass. A failed gate is automatically a
**blocking finding** (`category: correctness`, evidence = tail of the output). Agents never argue
with a gate result.

## Context rules

- The test author gets `task.md` only, never an implementation.
- Critics and reviewers get **artifacts, not reasoning**: `task.md`, the diff
  (`git diff <base>` for the stage), and gate output. Never pass them the author's transcript or
  summary.
- Critics, reviewers, and the arbiter are read-only.

## Verdict format

Every critic and reviewer replies with JSON only:

```json
{
  "verdict": "PASS | FAIL",
  "findings": [
    {
      "id": "F1",
      "severity": "blocking | minor",
      "category": "correctness | coverage | standards | security | scope",
      "file": "path/to/file.py",
      "line": 42,
      "criterion": "AC2",
      "claim": "...",
      "evidence": "...",
      "suggested_check": "..."
    }
  ]
}
```

Validate each verdict:
- `PASS` with any `blocking` finding counts as `FAIL`.
- Drop findings that have neither (`file` and `line`) nor `criterion`, and log them as noise.
- Only `blocking` findings reopen a loop. `minor` findings go into the final summary, unfixed.
- If the output is not valid JSON, re-ask that agent once with the parse error. If it fails again,
  treat it as `FAIL` with one blocking finding: "critic output unparseable".

Authors reply to findings with an AuthorResponse:

```json
{ "responses": [ { "finding_id": "F1", "action": "fixed | disputed", "note": "...", "reason": "..." } ] }
```

A **disputed** finding is not re-debated. If the critic flags it again next round, it goes
straight to the arbiter.

## Procedure

### Stage 1: Spec (max 3 passes)
Launch `spec-writer` with the work item. It writes `task.md` at the repo root.

Levels 1 and 2 have no Stage 1: use the solo procedure above. If **Open questions** contains
any `BLOCKING:` item, no tests or code get written until it's answered:

1. **Ask the user** the blocking questions, all at once, numbered `Q1`, `Q2`, ... (keep
   numbering across passes: a later pass's new questions continue from the last number). Offer
   Bypass, as in "Blocking questions and Bypass".
2. If the user declines or doesn't answer, **stop**: report status `needs_human` with the
   questions.
3. With answers, launch `spec-writer` again for a revision. Give it the work item, its previous
   `task.md`, and each question with its answer (`Q1: <question>` / `Answer: <answer>`), and
   tell it to follow "Revising after answers" in its instructions.
4. Then **you** rewrite the `## Answers` section at the end of `task.md` (replace it if it
   exists), listing every answered question so far with the answer word for word, each answer
   line indented by two spaces:

   ```markdown
   ## Answers
   Answers from the person who started this run. They override anything above that disagrees.

   - **Q1:** <question>
     **Answer:** <answer, verbatim>
   ```

5. Check **Open questions** again. A question that repeats an answered one doesn't count: its
   answer stands. If new `BLOCKING:` items remain, go back to step 1, but after the third spec
   pass ask **Begin work?** as described above instead of asking the questions again.

### Stage 2: Tests (max 2 rounds)
For round = 1..2:
1. Launch `test-author` (round 1: `task.md` only; later rounds: the blocking findings).
2. **Inverted gate:** run the test command. New tests must exist and **fail**, for the right reason
   (missing feature, not a broken test). If they pass, add a blocking finding: "tests don't
   exercise the change". An `ImportError`/`AttributeError` for the not-yet-written symbol is the
   right reason. Any other collection or syntax error is a blocking finding with the output.
   Existing tests must still pass.
3. Launch `test-critic` with `task.md`, the test diff, and the gate output.
4. If there are no blocking findings (critic or gate), the stage passes. Otherwise continue.

If the cap is hit with blocking findings still open, go to the **Arbiter** for them, then continue.

### Stage 3: Code (max 3 rounds)
For round = 1..3:
1. Launch `coder` (round 1: `task.md` + tests; later rounds: blocking findings + gate output).
2. **Test-file guard:** run `git diff --name-only` on the test paths against the end of Stage 2.
   If any test file changed, revert those files (`git checkout <stage-2-commit> -- <paths>`) and
   add a blocking finding: "coder modified test files".
3. Run all gates (tests, types, lint). Each failure is a blocking finding.
4. Launch `code-critic` with `task.md`, the implementation diff, and gate output.
   If the test gate failed and the critic reports a `test-defect` finding (the test itself is
   wrong), give `test-author` **one** fix pass per stage for exactly those tests. Revert any
   non-test changes it makes, commit the fixed tests (this is the new base for the test-file
   guard), and re-run the gates before deciding the round. Never send test-defect findings to
   the coder.
5. If all gates are green and there are no blocking findings, the stage passes. Otherwise continue.

If the cap is hit with blocking findings still open, go to the **Arbiter**, then continue.

Commit at the end of Stages 2 and 3 on the run branch, so later diffs and the test guard have a
fixed base.

### Stage 4: Review (parallel, one pass; level `full` only)
At `tests`, do the single review instead: launch `code-critic` once, as in the Reviewer step,
and send blocking findings to `coder` for one pass, then the gates. At `adversarial` there is no
review. At `full`: run the security scanner if the scan is on, configured and installed. Then launch `standards-reviewer` and
`security-reviewer` **in the same message**, so they run in parallel. Give both `task.md`, the full
diff from the base commit, and gate output; also give the standards reviewer the standards doc
and the security reviewer the scanner output (or "no scanner installed").

### Stage 5: Arbiter (only if needed; levels `adversarial` and `full`)
Run the arbiter if any blocking findings remain open after a capped stage or Stage 4, or if any
finding was disputed and re-flagged. Give it `task.md`, the current diff, gate output, the open
findings, and the author's responses. It returns:

```json
{ "rulings": [ { "finding_id": "F2", "decision": "dismiss | fix", "reason": "..." } ] }
```

For `fix` rulings, give the relevant author **one final pass** with those findings. There's no
further critic round. Re-run the gates (and the test-file guard for the coder).

### Stage 6: Final gates and summary
Run all gates one last time.
- **complete:** all gates green, no open blocking findings. Commit; the branch is ready for human
  review.
- **needs_human:** anything else. Leave the branch as is. Red gates are a decision point: ask
  the user for a fix pass or a takeover (above).

## Stop and summarize

Whenever you stop (blocking open questions the user didn't answer, a cap hit that the arbiter
couldn't settle, or the end of the run), **stop and summarize for the user**:

- Status (`complete` / `needs_human`) and the branch name
- The **level**, the security scan setting, and what the level didn't do (as listed above)
- Acceptance criteria and whether each is covered and met
- Rounds used per stage, and whether the arbiter ran (with its rulings)
- **Open blocking findings**, in full, if any
- Minor findings (not fixed)
- Final gate results, and whether the security scanner ran
- Any blocking questions and the user's answers, marking every question **bypassed** (the
  agent's own answers and reasons)
- Every **decision** the user made (fix pass with its note, takeover, scan hits ignored or
  blocked, Begin work?), and any scan hits ignored
