---
name: heats
description: Your tests decide when a fix is done. Heats sends you back when the check fails after your edits, and races several attempts in separate git worktrees when one attempt is not enough.
---

# Heats

## The referee (automatic)

After you edit files in a git repo and try to finish, Heats runs the repo's check (its tests). If the check
fails, you get a message starting "Heats referee:" with the exit code, the pass/fail counts and the last lines
of the output. Then:

- Read the failing output, fix the code, and run the check yourself before you finish again.
- Do not edit, delete or skip tests to make them pass.
- If the message says it is the last reminder and the check still fails, tell the user plainly that it fails
  and offer a race with `heats_run`. Do not claim the fix works.

## Races: `heats_run`

Use `heats_run` when the user asks for "heats", "best of N" or "several attempts", or when a bug fix or small
feature has a check that decides success and one attempt did not get it passing.

1. Make sure there is a check that FAILS now and should PASS after the fix. Heats runs it on the starting code
   first and stops the race if it cannot run or already passes. If there is no such test, ask the user, or
   write the failing test first in the main checkout. It does not need to be committed: attempts start from the
   working tree as it is, uncommitted edits included.
2. Write `task` so it stands alone: the attempts see nothing else from this chat. Include file names, the error
   message, what "done" means, and what has already been tried.
3. Call `heats_run(task=..., n=3, check="<command>")`. It waits up to about 6 minutes. If it returns
   `"status": "running"`, call `heats_wait(run_id=...)` and repeat until `"status": "done"`. Do not edit the
   repository yourself while a race runs.
4. Show the user the `scoreboard` as returned. Mention the winner's diff size and anything flagged (protected
   files edited, attempts that claimed a fix but changed nothing).
5. Apply only when the user agrees: `heats_apply(run_id=..., [heat=N])`. The patch lands unstaged, so the user
   reviews it with `git diff` like any other change. If there is no winner, offer `/heats show <run_id> <heat>`
   for the closest heat; use `force=true` only if the user asks for it.

`heats_run`, `heats_wait` and `heats_apply` are tools. If you found them with `tool_search`, invoke them with
`tool_call` (calls=[{"name": "heats_run", "arguments": {...}}]); never from Python code or the shell.

Do not start a race without a check command: with no objective judge it is just N guesses.
`/heats list`, `/heats show <run_id> [heat]` and `/heats apply <run_id> [heat]` work without the agent.
