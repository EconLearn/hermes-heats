# Heats

**Your tests, not the model, decide when a fix is done.**

When the Hermes agent edits code and is about to say "done", Heats runs your tests. If they fail, the agent
goes back to work with the real output. If one attempt still can't get them green, Heats races several
attempts in separate git worktrees and you apply the one that passes. Each attempt is called a *heat*.

<!-- GIF: real Desktop run, added by lead -->

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
*Example, shortened (the real scoreboard also has Model and Time columns).*

## What it does

**1. The referee (on by default, no setup).** Hermes fires a `pre_verify` hook when the agent has edited files
in this turn and is about to finish. Heats then runs your check command in your checkout. If the check fails,
the agent is sent back with the exit code, the pass/fail counts and the tail of the test output. The message goes
to the agent only: Hermes keeps it out of the chat history, so in the chat you see the agent carrying on; the
Desktop pane's Referee card shows each verdict (tests red, sent back 1/3; tests green). Each
referee call is logged to `referee.jsonl` (see Disclosure). Hermes allows at most 3 of these per turn
(`agent.max_verify_nudges`). On the last one, the agent is told to say plainly that
the check still fails and to offer a race. If the check passes, or cannot start at all, the referee stays quiet.

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
A status-bar chip shows `heats 1/3` while a race runs.

## Measured

Setup: qwen3-4b-instruct on one RTX 4060, 10 small Python bug-fix tasks, 2 runs each. Small samples, single
model.

- **Races did not solve more tasks than a plain single attempt** on this setup (plugin v0.2.x). We do not
  claim a solve-rate gain.
- **False "done" claims are common.** Across the bench races, 22 of 45 attempts that said they were finished
  failed the tests when Heats ran them.
- **Laps rescue attempts.** In 9 of 18 races that had a winner, the winning attempt had failed its first lap
  and passed after a restart with the real test output.
- **No tampered wins.** 0 solved runs had edited tests.

<!-- MEASURED v6: filled by lead -->

The bench scripts and raw results are in the source repository's `bench/` folder.

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
| `referee_timeout_seconds` | 120 | Time limit for the referee's check. A check that times out counts as failing |
| `check` | (empty) = auto-detect | Command that exits 0 on success. Used by the referee and by races |
| `check_timeout_seconds` | 600 | Time limit for one run of the check in a race |
| `default_heats` | 3 | Attempts per race (1-8) |
| `timeout_minutes` | 20 | Unfinished attempts are stopped and judged as they stand |
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
  per turn. Appends one line per call to `<HERMES_HOME>/plugin-data/heats/referee.jsonl` (time, session,
  attempt, whether it sent the agent back, edited paths).
- **Races:** creates git worktrees under `<repo>/.heats/<run_id>/` (added to `.git/info/exclude`, never your
  `.gitignore`) and branches `heats/<run_id>/h<N>`, and removes both when the race ends. Writes one dangling
  snapshot commit (no ref; git's normal garbage collection removes it). Runs your check command in each
  worktree. Launches N subagents through Hermes's subagent lifecycle API on your delegation model and normal
  approval rules; cost scales with N and laps. A race runs on a background thread while it is live.
- Saves reports, patches and the live snapshot under `<HERMES_HOME>/plugin-data/heats/`.
- Desktop pane backend routes under `/api/plugins/heats/`: `GET /live`, `GET /runs`, `POST /apply` (applies a
  heat that passed cleanly; never forced).
- No network calls of its own, no telemetry, no credentials.

## Limits

- The referee only fires after edits made with Hermes's `write_file` or `patch` tools. Edits made through shell
  commands do not trigger it.
- The referee runs your whole check. If the repo already has unrelated failing tests, the agent is sent back
  for those too. Set `check` to a narrower command, or turn the referee off.
- A slow suite can hit `referee_timeout_seconds` (120 s) and counts as failing. Raise it or narrow `check`.
- Races need a git repository with at least one commit, and the local terminal backend.
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
