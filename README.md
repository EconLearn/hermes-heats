# Heats

**Race several attempts at a coding task in parallel git worktrees. Your own tests pick the winner.**

One agent attempt at a bug fix sometimes works and sometimes doesn't, especially with a small or local
model. Heats runs N attempts at the same task at once, each in its own git worktree, then runs your
check command (your tests) in every worktree and ranks the results. Your checkout is not touched until
you apply the winner.

```
you> use heats (3) to fix the failing date tests, check is `pytest -q tests/test_dates.py`

🏁 heats_run ...

Heats 1106-153012-a1f2: 3 heats, check `pytest -q tests/test_dates.py`, base 4c1e9a20 + your uncommitted changes
Before: FAIL (1), tests 2/4

| Rank  | Heat | Model    | Strategy   | Check    | Tests | Diff            | Time  | Notes                                 |
|-------|------|----------|------------|----------|-------|-----------------|-------|---------------------------------------|
| WIN 1 | 2    | default  | root-cause | PASS     | 4/4   | +4 -2 (1 files) | 1m12s |                                       |
| 2     | 3    | default  | test-first | PASS     | 4/4   | +9 -1 (2 files) | 1m40s | edited protected files: tests/test_dates.py |
| 3     | 1    | default  | minimal    | FAIL (1) | 3/4   | +1 -1 (1 files) | 0m48s |                                       |

Winner: heat 2. Nothing in your repo has changed yet.

you> apply it
🏁 heats_apply: applied heat 2 to ~/code/app. Review with `git diff`.
```

## Why it is more than a prompt

- **Real isolation.** Each attempt works in its own git worktree, created from your current tree
  *including uncommitted edits and untracked files*. A `tool_request` middleware moves every file and
  terminal call an attempt makes into that attempt's worktree, so attempts cannot overwrite each
  other or your checkout. A `pre_tool_call` hook refuses any write that would still land outside it.
- **An objective judge.** Every attempt is scored by running your check command in its worktree. Ranking:
  passes the check → more tests passing → smaller diff → faster.
- **Cheating cannot win.** An attempt that edits test files (or any path you protect) is flagged and
  cannot be the winner, even if the check passes.
- **Nothing is lost or left behind.** Worktrees and branches are removed at the end; every attempt's
  patch is saved. Heats fingerprints your checkout before and after and warns if anything changed.
- **Diversity built in.** Attempts get different approaches (minimal fix, root cause, test-first,
  fresh eyes) and can race different models (`models: [qwen3:4b, your-cloud-model]`).

## Measured

<!-- filled from bench/RESULTS.md -->

## Install

```bash
hermes plugins install heats      # once listed in the catalog
hermes plugins enable heats
```

From source: clone this repo into `~/.hermes/plugins/heats` and `hermes plugins enable heats`.
Requires `git` on PATH and the local terminal backend. No API keys, no network calls of its own.

## Use

Ask the agent in plain words: *"use heats (3 attempts) to fix the failing parser test"*. The agent calls:

| Tool | What it does |
|---|---|
| `heats_run(task, n=3, check=?, models=?, timeout_minutes=?, repo_path=?)` | Runs the race and returns the scoreboard. `check` is auto-detected (pytest, npm/pnpm/yarn test, cargo test, go test, make test) when omitted. |
| `heats_apply(run_id, heat=?, force=?)` | Applies the winner's (or a named heat's) patch to your working tree, unstaged. |

Without the agent:

```
/heats list                      # recent runs
/heats show <run_id> [heat]      # scoreboard, or one heat's diff and test output
/heats apply <run_id> [heat]     # apply a patch
hermes heats list|show|apply ... # same from the shell
```

## Settings

`hermes config set plugins.entries.heats.settings.<key> <value>`, or the Desktop plugin settings page.

| Key | Default | Meaning |
|---|---|---|
| `default_heats` | 3 | Attempts per run (1-8) |
| `timeout_minutes` | 15 | Unfinished attempts are stopped and judged as they stand |
| `check` | auto | Command that exits 0 on success |
| `models` | [] | Models to race; attempts cycle through them |
| `toolsets` | [terminal, file] | What each attempt may use (never broader than the parent) |
| `protect_tests` | true | Attempts that edit tests cannot win |
| `protected_paths` | [] | More files or folders an attempt may not change |
| `run_baseline` | true | Run the check on the starting code too, for a before/after |

## What it does on your machine (disclosure)

- Creates git worktrees under `<repo>/.heats/<run_id>/` (added to `.git/info/exclude`, never your
  `.gitignore`) and branches `heats/<run_id>/h<N>`; removes both when the run ends.
- Runs your check command in each worktree (shell commands you chose, with your permissions).
- Launches N subagents through Hermes's subagent lifecycle API; they use your delegation model and the
  normal approval rules. Cost scales with N.
- Saves reports and patches under `<HERMES_HOME>/plugin-data/heats/runs/`.
- No network calls of its own, no telemetry, no credentials.

## Limits

- Needs a git repository with at least one commit, and the local terminal backend.
- Isolation is enforced for Hermes's file and terminal tools. A command that computes a path into your
  checkout at run time is not rewritten; the before/after fingerprint reports it if it happens.
- Parallel attempts on one local GPU share it; the race is still useful, it just takes longer.

## Development

```bash
python -m pytest -q tests          # unit tests (no Hermes needed, needs git)
hermes plugins validate .          # the catalog CI check
```

MIT licence.
