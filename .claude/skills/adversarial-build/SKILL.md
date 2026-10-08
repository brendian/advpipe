---
name: adversarial-build
description: Drive one work item (feature or bug fix) through an adversarial author/critic pipeline of subagents (spec, tests, code, review, arbiter) with deterministic gates and round caps, leaving a branch ready for human review. Use when the user asks for an adversarial build, or invokes /adversarial-build with a work item.
---

# Adversarial build

Drive **one work item** through the pipeline below using the subagents in `.claude/agents/`.
You are the orchestrator. Follow the procedure exactly. You don't skip stages, you don't write
code or tests yourself, and you don't let an agent decide whether a stage runs.

```
[1] spec-writer ──► task.md
[2] test-author ⇄ test-critic              max 2 rounds   gate: new tests exist and FAIL for the right reason
[3] coder ⇄ code-critic                    max 3 rounds   gate: tests + types + lint green, no blocking findings
[4] standards-reviewer ∥ security-reviewer one pass each, launched in parallel
[5] arbiter                                only if blocking findings remain or are disputed
[6] done → branch ready for human review, or needs_human with open findings
```

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

### Stage 1: Spec
Launch `spec-writer` with the work item. It writes `task.md` at the repo root.
If **Open questions** contains any `BLOCKING:` item, **stop**: report status `needs_human` with
the questions. No tests or code get written.

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

### Stage 4: Review (parallel, one pass)
Run the security scanner if configured and installed. Then launch `standards-reviewer` and
`security-reviewer` **in the same message**, so they run in parallel. Give both `task.md`, the full
diff from the base commit, and gate output; also give the standards reviewer the standards doc
and the security reviewer the scanner output (or "no scanner installed").

### Stage 5: Arbiter (only if needed)
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
- **needs_human:** anything else. Leave the branch as is.

## Stop and summarize

Whenever you stop (blocking open questions, a cap hit that the arbiter couldn't settle, or the end
of the run), **stop and summarize for the user**:

- Status (`complete` / `needs_human`) and the branch name
- Acceptance criteria and whether each is covered and met
- Rounds used per stage, and whether the arbiter ran (with its rulings)
- **Open blocking findings**, in full, if any
- Minor findings (not fixed)
- Final gate results, and whether the security scanner ran
