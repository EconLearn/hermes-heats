"""Find and run a repository's check command (its tests), and read pass/fail counts."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

OUTPUT_TAIL_CHARS = 2_000


@dataclass
class CheckResult:
    command: str
    exit_code: Optional[int]          # None = could not run / timed out
    duration_s: float
    passed: Optional[int] = None
    failed: Optional[int] = None
    timed_out: bool = False
    tail: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "command", "exit_code", "duration_s", "passed", "failed", "timed_out", "tail", "error")} | {"ok": self.ok}


def detect_check(root: str) -> Optional[str]:
    """Best guess at the project's test command, or None."""
    p = Path(root)
    pkg = p / "package.json"
    if pkg.is_file():
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts") or {}
            test = scripts.get("test", "")
            if test and "no test specified" not in test:
                runner = "pnpm" if (p / "pnpm-lock.yaml").exists() else "yarn" if (p / "yarn.lock").exists() else "npm"
                return f"{runner} test"
        except (OSError, ValueError):
            pass
    if (p / "Cargo.toml").is_file():
        return "cargo test"
    if (p / "go.mod").is_file():
        return "go test ./..."
    py_markers = ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "conftest.py")
    has_py_tests = (p / "tests").is_dir() or (p / "test").is_dir() or any(p.glob("test_*.py"))
    if has_py_tests or any((p / m).is_file() for m in py_markers[:1]):
        return "python -m pytest -q"
    mk = p / "Makefile"
    if mk.is_file():
        try:
            if re.search(r"^test:", mk.read_text(encoding="utf-8", errors="replace"), re.M):
                return "make test"
        except OSError:
            pass
    return None


_PYTEST = re.compile(r"(\d+) (passed|failed|error|errors)\b")
_JEST = re.compile(r"Tests:\s+(?:(\d+) failed,\s+)?(?:\d+ skipped,\s+)?(?:(\d+) passed,\s+)?(\d+) total")
_CARGO = re.compile(r"test result: \w+\. (\d+) passed; (\d+) failed")
_GO_FAIL = re.compile(r"^--- FAIL", re.M)
_GO_PASS = re.compile(r"^--- PASS", re.M)


def parse_counts(output: str) -> tuple[Optional[int], Optional[int]]:
    """(passed, failed) from common test-runner summaries; (None, None) when unknown."""
    passed = failed = None
    m = _JEST.search(output)
    if m:
        failed = int(m.group(1) or 0)
        passed = int(m.group(2) or 0)
        return passed, failed
    cargo = _CARGO.findall(output)
    if cargo:
        return sum(int(a) for a, _ in cargo), sum(int(b) for _, b in cargo)
    hits = _PYTEST.findall(output)
    if hits:
        # pytest prints its summary last; read the final summary line's counts.
        last_line = [ln for ln in output.splitlines() if _PYTEST.search(ln)][-1]
        counts = {k: int(v) for v, k in _PYTEST.findall(last_line)}
        passed = counts.get("passed", 0)
        failed = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
        return passed, failed
    gp, gf = len(_GO_PASS.findall(output)), len(_GO_FAIL.findall(output))
    if gp or gf:
        return gp, gf
    return None, None


def run_check(command: str, cwd: str, timeout_s: float = 600, env_extra: Optional[dict] = None) -> CheckResult:
    """Run *command* in *cwd* through the shell; kill the whole process group on timeout."""
    env = dict(os.environ)
    env.update({"CI": "1", "PYTHONDONTWRITEBYTECODE": "1", "NO_COLOR": "1", "FORCE_COLOR": "0"})
    env.pop("PYTEST_ADDOPTS", None)
    if env_extra:
        env.update(env_extra)
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            command, shell=True, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            start_new_session=True,
        )
    except OSError as exc:
        return CheckResult(command, None, 0.0, error=f"could not start the check: {exc}")
    try:
        out, _ = proc.communicate(timeout=timeout_s)
        timed_out = False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        out, _ = proc.communicate()
        timed_out = True
    dur = round(time.monotonic() - start, 2)
    out = out or ""
    passed, failed = parse_counts(out)
    return CheckResult(
        command=command, exit_code=None if timed_out else proc.returncode, duration_s=dur,
        passed=passed, failed=failed, timed_out=timed_out, tail=out[-OUTPUT_TAIL_CHARS:],
        error=f"timed out after {timeout_s:.0f}s" if timed_out else "",
    )


_TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs)(/|$)|(^|/)test_[^/]*\.py$|_test\.(py|go)$|\.(test|spec)\.[cm]?[jt]sx?$|(^|/)conftest\.py$"
)


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH.search(path.replace("\\", "/")))


@dataclass
class Guard:
    """Files a heat may not change: by default every test file (so a heat cannot 'win' by
    deleting or weakening the test that judges it)."""
    protect_tests: bool = True
    extra: list[str] = field(default_factory=list)

    def violations(self, files: list[str]) -> list[str]:
        out = []
        for f in files:
            if (self.protect_tests and is_test_path(f)) or any(f == e or f.startswith(e.rstrip("/") + "/") for e in self.extra):
                out.append(f)
        return out
