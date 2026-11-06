"""Rank heats and render the scoreboard."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .checks import CheckResult


@dataclass
class HeatResult:
    index: int                       # 1-based
    strategy: str
    model: Optional[str] = None
    agent_state: str = "PENDING"
    agent_summary: str = ""
    agent_error: str = ""
    duration_s: float = 0.0
    files: list[str] = field(default_factory=list)
    insertions: int = 0
    deletions: int = 0
    guarded: list[str] = field(default_factory=list)   # protected files the heat changed
    escaped: int = 0                                    # tool calls Heats redirected into the worktree
    check: Optional[CheckResult] = None
    patch_file: str = ""
    tokens: Optional[int] = None
    laps: int = 1                    # child launches used (a failing heat may be relaunched with the check output)

    @property
    def changed(self) -> bool:
        return bool(self.files)

    @property
    def qualifies(self) -> bool:
        """Can win: passed the check, changed something, did not touch protected files."""
        return bool(self.check and self.check.ok and self.changed and not self.guarded)

    def sort_key(self):
        c = self.check
        passed = (c.passed or 0) if c else -1
        failed = (c.failed if c and c.failed is not None else 10**6)
        return (
            0 if self.qualifies else 1,
            0 if (c and c.ok) else 1,
            0 if not self.guarded else 1,
            -passed,
            failed,
            self.insertions + self.deletions if self.changed else 10**9,
            self.duration_s,
            self.index,
        )

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in (
            "index", "strategy", "model", "agent_state", "agent_summary", "agent_error", "duration_s", "files",
            "insertions", "deletions", "guarded", "escaped", "patch_file", "tokens", "laps")}
        d["check"] = self.check.to_dict() if self.check else None
        d["qualifies"] = self.qualifies
        return d


def rank(results: list[HeatResult]) -> list[HeatResult]:
    return sorted(results, key=lambda r: r.sort_key())


def winner(results: list[HeatResult]) -> Optional[HeatResult]:
    ranked = rank(results)
    return ranked[0] if ranked and ranked[0].qualifies else None


def _fmt_time(s: float) -> str:
    s = int(round(s))
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def _fmt_tests(c: Optional[CheckResult]) -> str:
    if c is None:
        return "not run"
    if c.passed is None and c.failed is None:
        return "-"
    total = (c.passed or 0) + (c.failed or 0)
    return f"{c.passed or 0}/{total}"


def _fmt_check(c: Optional[CheckResult]) -> str:
    if c is None:
        return "not run"
    if c.timed_out:
        return "timeout"
    if c.exit_code is None:
        return "error"
    return "PASS" if c.ok else f"FAIL ({c.exit_code})"


def scoreboard(run_id: str, results: list[HeatResult], baseline: Optional[CheckResult], check_cmd: str,
               base_label: str) -> str:
    ranked = rank(results)
    win = winner(results)
    lines = [f"**Heats `{run_id}`**: {len(results)} heats, check `{check_cmd}`, base {base_label}"]
    if baseline is not None:
        lines.append(f"Before: {_fmt_check(baseline)}, tests {_fmt_tests(baseline)}")
    lines += ["", "| Rank | Heat | Model | Strategy | Check | Tests | Diff | Time | Notes |",
              "|---|---|---|---|---|---|---|---|---|"]
    for pos, r in enumerate(ranked, 1):
        notes = []
        if r.guarded:
            notes.append(f"edited protected files: {', '.join(r.guarded[:3])}")
        if not r.changed:
            claimed = bool(re.search(r"\b(pass(es|ed)?|fixed|updated|changed|resolved)\b", r.agent_summary or "", re.I))
            notes.append("claimed a fix but changed nothing" if claimed else "no changes")
        if r.agent_state not in ("SUCCEEDED",):
            notes.append(f"agent {r.agent_state.lower()}")
        if r.laps > 1:
            notes.append(f"{r.laps} laps")
        if r.escaped:
            notes.append(f"{r.escaped} path(s) redirected into its worktree")
        diff = f"+{r.insertions} -{r.deletions} ({len(r.files)} files)" if r.changed else "-"
        mark = "WIN " if win is r else ""
        lines.append(
            f"| {mark}{pos} | {r.index} | {r.model or 'default'} | {r.strategy} | {_fmt_check(r.check)} | "
            f"{_fmt_tests(r.check)} | {diff} | {_fmt_time(r.duration_s)} | {'; '.join(notes)} |")
    lines.append("")
    if win:
        lines.append(f"Winner: heat {win.index}. Nothing in your repo has changed yet. "
                     f"Apply it with `heats_apply` (run_id `{run_id}`, heat {win.index}) or `/heats apply {run_id} {win.index}`.")
    else:
        lines.append("No heat passed the check without touching protected files, so there is no winner. "
                     "Every patch is saved; inspect one with `/heats show " + run_id + " <heat>`.")
    return "\n".join(lines)
