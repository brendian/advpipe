# Windows: remaining fixes

Runs, `--detach` and the web UI work on Windows since `pid_alive` replaced `os.kill(pid, 0)`
(signal 0 is `CTRL_C_EVENT` there, which failed with WinError 87 and crashed every detached
run before it wrote `run.json`). Two gaps are left.

## 1. Cancel stops at once, not at a safe point

`cancel_run` sends `SIGINT`. On Windows `os.kill(pid, SIGINT)` is `TerminateProcess`: no
"Interrupted during <stage>" note, `run.lock` left behind (harmless, the pid is dead), and the
Claude CLI subprocess the SDK started may be orphaned.

Fix:
- Ask the run to stop through a file instead of a signal: `cancel_run` writes
  `.advpipe/runs/<id>/cancel`; the orchestrator checks for it between agent calls and gates and
  raises the same cancellation it gets from SIGINT today. Works the same on every OS; keep
  SIGINT on POSIX for an in-flight agent call.
- On Windows, if the run hasn't stopped after a grace period, end the whole tree
  (`taskkill /T /F /PID <pid>`) so no Claude process is left behind.
- Test: the fake child honours the cancel file; `cancel` → status stays `running`, note
  recorded, lock released, `resume` continues.

## 2. The UI hides why a run can't be shown

`run_detail._starting` swallows `OSError`/`ValueError` from `load_starting` and falls through
to "There's no run <id> in this repo", which is how the WinError 87 bug looked like a missing
run.

Fix:
- Let those errors reach `_load`'s "Can't read this run" page (500, with the message) instead
  of returning None.
- Only a missing run directory gives "No such run".
- Test: a run directory whose `run.lock` read raises shows the error, not "No such run".

## 3. CI

There's no CI yet. Add a GitHub Actions workflow with `ubuntu-latest` and `windows-latest`
jobs running `pytest` and `mypy --strict --platform win32 src/`, so
POSIX-only calls are caught before release.
