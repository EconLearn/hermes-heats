---
name: heats
description: Race several attempts at a coding task in parallel git worktrees and let the tests pick the winner.
---

# Heats

Use `heats_run` when the user asks for "heats", "best of N", "several attempts", or when a bug fix /
small feature has a test command that decides success and a single attempt is uncertain (a small or
local model, a flaky area, a fix you got wrong once already).

1. Make sure there is an objective check: a test file or command that FAILS now and should PASS after the fix.
   If none exists, ask the user, or write the failing test first in the main checkout and commit it.
2. Write `task` so it stands alone: the attempts see nothing else from this chat. Include file names,
   the error message and what "done" means.
3. Call `heats_run(task=..., n=3, check="<command>")`. It waits up to ~6 minutes. If it returns
   `"status": "running"`, call `heats_wait(run_id=...)` and repeat until `"status": "done"`. Do not edit
   the repository yourself while a race runs.
4. Show the user the `scoreboard` as returned. Mention the winner's diff size and anything flagged
   (protected files edited, attempts that tried to write into the main checkout).
5. Apply only when the user agrees: `heats_apply(run_id=..., [heat=N])`. The patch lands unstaged, so
   the user reviews it with `git diff` like any other change.

`heats_run`, `heats_wait` and `heats_apply` are tools. If you found them with `tool_search`, invoke them with
`tool_call` (calls=[{"name": "heats_run", "arguments": {...}}]); never from Python code or the shell.

Do not use Heats without a check command: with no objective judge it is just N guesses.
`/heats list`, `/heats show <run_id> [heat]` and `/heats apply <run_id> [heat]` work without the agent.
