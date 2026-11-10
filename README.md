# Heats

**Your tests, not the model, decide when a fix is done.**

When the Hermes agent edits code and is about to say "done", Heats runs your tests. If they fail, the agent
goes back to work with the real output. If one attempt still can't get them green, Heats races several
attempts in separate git worktrees and you apply the one that passes. Each attempt is called a *heat*.

![A real run: the 4B model says its fix was "verified ... all 4 tests passing"; the Referee card shows 0 passed, 4 failed, sent back](docs/heats_claim_vs_tests.gif)

*Real Hermes Webapp run, free local model (qwen3-4b Q4 via Ollama). The model claims all 4 tests pass; Heats ran
them before it could finish: 0 passed, 4 failed.*

![A real race: three heats, heat 2 (root-cause) wins with 4/4 tests and is applied](docs/race_winner.png)

```text
you>     fix the accent bug in app/slug.py
agent>   Fixed: slugify now strips accents. Done.
[Heats referee, to the agent] you edited app/slug.py and are about to finish, but the check
         `python -m pytest -q` exited 1, 3 passed / 2 failed. Your work is not done yet. [output tail]
agent>   (reads the failure, fixes app/slug.py, runs the tests: 5 passed)
you>     use heats (3) on the date parser bug
| Rank  | Heat | Strategy   | Check         | Tests | Diff            | Notes                                      |
| WIN 1 | 2    | root-cause | PASS          | 5/5   | +3 -1 (1 file)  | 2 laps                                     |
| 2     | 3    | minimal    | PASS          | 5/5   | +2 -6 (2 files) | edited protected files: tests/test_date.py |
| 3     | 1    | standard   | FAIL (exit 1) | 3/5   | +1 -1 (1 file)  | agent stopped                              |
Winner: heat 2. Nothing in your repo has changed yet.
```
*Example, shortened: the real referee message also names the folder the check ran in, and the real scoreboard
also has Model and Time columns.*

## What it does

**1. The referee (on by default, no setup).** Hermes fires a `pre_verify` hook when the agent has edited files
in this turn and is about to finish. Heats then runs your check command in your checkout. If the check fails,
the agent is sent back with the exit code, the pass/fail counts and the tail of the test output. The message goes
to the agent only: Hermes keeps it out of the chat history, so in the chat you see the agent carrying on; the
Desktop pane's Referee card shows each verdict (`tests red (exit 1) · 2 passed, 2 failed · sent back (1/3)`,
then `tests green · 4 passed, 0 failed · done`). Each referee call is logged to `referee.jsonl` (see Disclosure).
Hermes allows at most 3 of these per turn (`agent.max_verify_nudges`). On the last one, the agent is told to say
plainly that the check still fails and to offer a race. If the check passes, the referee stays quiet, unless
the agent changed test files in this turn: a green check after editing the tests is sent back too (amber dot:
`tests green, but tests edited (tests/test_slug.py)`), with the instruction to restore the tests and fix the code
unless you asked for test changes. Same rule as a race: editing the tests cannot turn the result green. It also stays
quiet, and the card says `skipped: ...` with the reason, when the agent is not to blame or the check cannot be
trusted: the check could not run (no test runner, pytest usage error), it ran past the referee's time limit, or the
agent edited a file that defines the check (`conftest.py`, `pyproject.toml`, `package.json`, a Makefile, ...) or a
script the check command names. In that last case Heats will not execute a command the agent just wrote.

**2. Races, when one attempt is not enough.** `heats_run` starts N attempts at the same task. Each works in its
own git worktree made from your working tree as it is, uncommitted and untracked files included.

- Each attempt gets Hermes's coding brief and is told to run the check first.
- Heats runs the check once on the starting code. If it cannot run, or already passes, the race stops before
  any attempt starts and tells you why.
- When an attempt finishes, Heats runs the check in its worktree. If it fails, the attempt starts again (a
  *lap*) with what Heats measured: files changed, exit code, test output and the previous lap's diff. A lap
  that left fewer tests passing than the starting code is undone first. Up to 3 laps per attempt.
- The first attempt that passes cleanly ends the race. The others are stopped and judged as they stand.
- Ranking: passes the check, then more tests passing, then smaller diff, then faster.

**3. Your checkout is safe.**

- Attempts cannot write outside their worktree. File paths are moved into it; writes elsewhere are refused.
  Git commands that would change state shared with your checkout (stash, branches, config, push, other
  worktrees) are refused, checked segment by segment through `;`, `&&`, pipes and subshells.
- An attempt that changes tests or test config (`conftest.py`, `pytest.ini`, the pytest section of `pyproject.toml`,
  jest/vitest configs, the test script in `package.json`, a Makefile test target; renames and non-ASCII names
  included) is flagged and cannot win.
- Files a test run creates (`__pycache__`, `.pytest_cache`, coverage reports, ...) are left out of patches.
- Apply is all or nothing. The winner lands as a normal unstaged edit. If the patch no longer applies cleanly
  and a file it touches has local changes, Heats refuses and changes nothing.
- Heats fingerprints your checkout (files, branches, stash, git config) before and after and warns if anything
  changed. Worktrees and branches are removed at the end; worktrees left by a killed Hermes are cleaned up by a
  later race in the same repo once they are 6 hours old.

## Desktop pane

In Hermes Desktop, a Heats pane shows each heat as a lane: `running`, `running tests`, `lap 2`, `PASS`,
`FAIL`, or `passed, but edited tests`. The winning lane is outlined and an **Apply winner** button applies it.
A status-bar chip shows `heats 1/3` while a race runs. Above the lanes, a Referee card lists the last verdicts
(red dot: `tests red (exit 1) · 2 passed, 2 failed · sent back (1/3)`; green dot: `tests green · done`; grey dot:
`skipped: conftest.py edited (it can change what the check runs) · not sent back`), each with how long ago it
happened. The referee's message to the agent is not in the chat, so this card is where you see it work.

## Measured

Same free local model throughout (qwen3-4b-instruct-2507, Q4, via Ollama), 10 small Python bug-fix tasks, each a
tiny repo with failing tests. Solved = the tests pass in your checkout afterwards and the test files are unchanged.
Small samples, one weak model: read these as evidence about the mechanism, not a general benchmark.

**Bench v6 + v7 (9-10 Nov):** three arms run back to back on every task. v6 ran Heats v0.3.0 twice per task;
v7 repeated it with v0.3.1 on the same tasks, model and machine (two more runs per task).

| Arm | v6 | v7 (replication) | Both |
|---|---|---|---|
| Plain Hermes | 8/20 | 14/20 | 22/40 |
| Hermes + Heats referee | 12/20 | 12/20 | 24/40 |
| Heats race, one attempt (`heats_run n=1`) | 16/20 | 12/20 | 28/40 |

- **The v6 gain did not replicate.** In v6, on the same task and run, the race solved 8 that plain Hermes did not, and plain Hermes 0 that it did not
  (exact sign test p = 0.01). In v7 it was 1 vs 3. Both benches together: 9 vs 3 (p = 0.15), referee 8 vs 6.
  Plain Hermes itself moved from 8/20 to 14/20 between the two benches, so run-to-run noise on this model is
  as large as the effect we were looking for. We do not claim that Heats raises the solve rate.
- What held in every run: **none of the 74 solved runs (of 120) had edited tests (the model rarely tried: 1 tampered run in 120, unsolved)**; the referee
  ran 66 checks and sent the agent back 39 times in 16 of 40 runs, each time with the real failing output (3 of those 16 runs then ended solved).
  Each send-back is a moment the 4B model was about to say "done" with the tests red.
- Raw results, scripts and the summary: `bench/` (`RESULTS_v7.md`, `summarize_v7.py`).

**Before v0.3.0 the race did not help** (v0.2.x, one RTX 4060): one attempt solved 7/20 against 11/20 for plain
Hermes. The cause was in Heats: attempts ran without Hermes's coding brief, were told to edit before testing, and a
lap restarted on code it had made worse. v0.3.0 fixed those three things; v6 is the first bench after the fix.

Across the v0.2.x races, 50 of 89 attempts that said they were done failed the tests when Heats ran them, and in
15 of 39 races with a winner, the winner had failed its first lap and passed after a restart with the real output.

The bench scripts and every raw result file are in `bench/`.

## Install

Requires Hermes >= 0.23.0, `git` on PATH and the local terminal backend. No API keys.

```bash
hermes plugins install heats && hermes plugins enable heats      # once listed in the catalog
git clone https://github.com/EconLearn/hermes-heats ~/.hermes/plugins/heats && hermes plugins enable heats
```

## Use

The referee needs nothing: it works on every turn where the agent edits code in a git repo.

For a race, ask in plain words: *"use heats (3) to fix the failing parser test"*.

| Tool | What it does |
|---|---|
| `heats_run(task, n=3, check=?, models=?, timeout_minutes=?, laps=?, repo_path=?)` | Starts a race and returns the scoreboard. `check` is auto-detected (pytest, npm/pnpm/yarn test, cargo test, go test, make test) when omitted. |
| `heats_wait(run_id)` | A race that takes longer than about 6 minutes keeps running in the background. `heats_run` returns its `run_id` and progress, and the agent calls `heats_wait` until it is done. Hermes stops waiting for any one tool call after 420 s. |
| `heats_apply(run_id, heat=?, force=?)` | Applies the winner's (or a named heat's) patch to your working tree, unstaged. |

Without the agent:

```
/heats list                              # recent runs
/heats show <run_id> [heat]              # scoreboard, or one heat's diff and test output
/heats apply <run_id> [heat] [--force]   # apply a patch
hermes heats list|show|apply ...         # the same from the shell
```

## Settings

`hermes config set plugins.entries.heats.settings.<key> <value>`, or the Desktop plugin settings page.

| Key | Default | Meaning |
|---|---|---|
| `referee` | true | Run the check when the agent edited code and is about to finish; send it back if it fails |
| `referee_timeout_seconds` | 25 | Time limit for the referee's check, capped 5 s below Hermes's `plugins.hook_callback_timeout` (30 s by default). A check that runs longer is skipped, never blamed on the agent |
| `check` | (empty) = auto-detect | Command that exits 0 on success. Used by the referee and by races |
| `check_timeout_seconds` | 600 | Time limit for one run of the check in a race |
| `default_heats` | 3 | Attempts per race (1-8) |
| `timeout_minutes` | 20 | Unfinished attempts are stopped and judged as they stand. Capped at 4 hours, so a forgotten race always ends |
| `laps` | 3 | Launches per attempt; a failing attempt is restarted with the measured check output |
| `finish_early` | true | The first attempt that passes cleanly ends the race |
| `models` | [] | Models to race; attempts cycle through them. Empty = the delegation model |
| `toolsets` | [terminal, file] | What each attempt may use (never broader than the parent's) |
| `protect_tests` | true | Attempts that edit tests or test config cannot win |
| `protected_paths` | [] | More files or folders an attempt may not change if it is to win |
| `wait_seconds` | 360 | How long one `heats_run` / `heats_wait` call waits before returning progress. Keep it below Hermes's 420 s tool limit |
| `run_baseline` | true | Run the check on the starting code first: shows before/after, and stops a race whose check cannot run or already passes |

Turn the referee off: `hermes config set plugins.entries.heats.settings.referee false`.

## What it does on your machine (disclosure)

- **Referee:** after the agent edits files with Hermes's file tools in a git repo, runs your check command (the
  `check` setting, or the auto-detected test command) in your checkout, with your permissions, up to 3 times
  per turn, each run limited to `referee_timeout_seconds` (25 s). It does not run the check in a turn where the
  agent edited a file that can change what the check runs (`package.json`, Makefiles, `pyproject.toml`,
  `setup.cfg`, `setup.py`, `conftest.py`, pytest/tox/nox/jest/vitest configs, `Cargo.toml`, `go.mod`, ...) or a
  script named in the check command; the pane shows `skipped` with the reason instead. A check that times out
  or cannot run is also skipped and never blamed on the agent. Appends one line per call to
  `<HERMES_HOME>/plugin-data/heats/referee.jsonl` (time, session, attempt, whether it sent the agent back,
  edited paths, repo name, exit code, pass/fail counts, timeout or skip reason) and keeps the last 6 verdicts in
  `referee_live.json` for the Desktop pane.
- **Races:** creates git worktrees under `<repo>/.heats/<run_id>/` (added to `.git/info/exclude`, never your
  `.gitignore`) and branches `heats/<run_id>/h<N>`, and removes both when the race ends. Writes one dangling
  snapshot commit (no ref; git's normal garbage collection removes it). Runs your check command in each
  worktree. Launches N subagents through Hermes's subagent lifecycle API on your delegation model and normal
  approval rules; cost scales with N and laps. A race runs on a background thread while it is live.
- Saves reports, patches and the live snapshot under `<HERMES_HOME>/plugin-data/heats/`.
- Desktop pane backend routes under `/api/plugins/heats/`: `GET /live`, `GET /referee`, `GET /runs`, `POST /apply` (applies a
  heat that passed cleanly; never forced).
- No network calls of its own, no telemetry, no credentials.

## Limits

- The referee only fires after edits made with Hermes's `write_file` or `patch` tools. Edits made through shell
  commands do not trigger it.
- The referee runs your whole check. If the repo already has unrelated failing tests, the agent is sent back
  for those too. Set `check` to a narrower command, or turn the referee off.
- Hermes gives a hook 30 s by default (`plugins.hook_callback_timeout`), so the referee's check gets
  `referee_timeout_seconds` (25 s) or 5 s less than the hook limit, whichever is smaller. A check that runs
  longer is skipped, not blamed on the agent. For a slower suite, set a narrower `check` or raise both settings.
- The referee does not compare against failures that existed before the turn; it tells the agent it may say so
  and finish if failures are unrelated. A check that cannot run (command not found or not executable, exit
  126/127; pytest usage error or no tests collected, exit 4/5) is skipped.
- The referee skips the check when the agent edited a file that defines it or a script it names (see
  Disclosure). The agent is not told; the pane is. Run the check yourself in that case.
- Races need a git repository with at least one commit, and the local terminal backend.
- A race ends after `timeout_minutes`, and after 4 hours at the latest whatever the setting.
- Each lap is a new subagent. It gets the task and what Heats measured, not the previous lap's conversation.
- Several attempts on one local GPU share it, so each runs slower and may hit the time limit. Locally, use
  fewer heats or a hosted model.
- Isolation covers Hermes's file and terminal tools. A command that computes a path into your checkout at run
  time is not rewritten; the before/after fingerprint reports it if it happens.
- Dependency folders (`node_modules`, `.venv`, ...) are linked into each worktree, not copied. An attempt that
  runs `npm install` or `pip install` changes your real ones. Editable Python installs are pointed at the
  worktree's code when the check runs.
- Untracked files over 10 MB are left out of the attempts' copy of your tree.
- Protected-test checks stop an attempt from winning by editing tests. They do not stop code that special-cases
  the test inputs; review the winner's diff.

## How it works (Hermes surfaces)

- `pre_verify` hook: the referee.
- `ctx.subagent_lifecycle`: launches, waits for and cancels attempts.
- `tool_request` middleware: moves each attempt's file and terminal calls into its worktree (the lifecycle API
  does not take a working directory yet).
- `pre_tool_call` hook: refuses writes outside the worktree, shared-state git commands, and every call from an
  attempt after its race ended. It also catches a model trying to run `heats_run(...)` from `execute_code` or
  the shell and tells it the correct tool call.
- `post_tool_call` hook: follows each attempt's terminal directory.
- Three tools, `/heats`, `hermes heats`, a bundled skill, and a Desktop pane with a status-bar chip.

## Development

```bash
python -m pytest -q tests          # unit tests (no Hermes needed, needs git)
hermes plugins validate .          # the catalog CI check
```

MIT licence.
