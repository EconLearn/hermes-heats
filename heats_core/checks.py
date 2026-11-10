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


def _python_for(p: Path) -> str:
    """The project's own interpreter when it has a venv (relative, so it also works in a worktree where
    the venv is linked), else ``python``, else ``python3`` (macOS has no ``python``)."""
    import shutil
    for v in (".venv", "venv"):
        if (p / v / "bin" / "python").exists():
            return f"{v}/bin/python"
    return "python" if shutil.which("python") else "python3"


def editable_pythonpath(root: str, wt: str) -> list[str]:
    """Worktree equivalents of the main-checkout folders an editable install points at.

    A venv linked into a worktree still imports the user's checkout through its ``.pth`` files and
    ``__editable__`` finders (``pip install -e .`` with a src layout). Putting the worktree's copies on
    PYTHONPATH makes them win (PYTHONPATH comes before site-packages; editable finders are appended to
    sys.meta_path, after the path finder)."""
    root, wt = os.path.realpath(root), os.path.realpath(wt)
    out: list[str] = []

    def add(path: str) -> None:
        path = os.path.normpath(path)
        if path == root or path.startswith(root + os.sep):
            mapped = os.path.join(wt, os.path.relpath(path, root)) if path != root else wt
            if mapped not in out and os.path.isdir(mapped):
                out.append(mapped)

    for v in (".venv", "venv", "env"):
        venv = Path(wt) / v
        if not (venv / "pyvenv.cfg").exists():
            continue
        for sp in list(venv.glob("lib/python*/site-packages")) + list(venv.glob("Lib/site-packages")):
            try:
                entries = list(sp.iterdir())
            except OSError:
                continue
            for f in entries:
                try:
                    if f.suffix == ".pth":
                        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                            line = line.strip()
                            if line and not line.startswith(("#", "import ")) and os.path.isabs(line):
                                add(line)
                    elif f.name.startswith("__editable__") and f.suffix == ".py":
                        for q in re.findall(r"""['"](/[^'"]+)['"]""", f.read_text(encoding="utf-8", errors="replace")):
                            add(os.path.dirname(os.path.normpath(q)))  # MAPPING values are package dirs / module files
                except OSError:
                    continue
    return out


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
    py_evidence = any((p / m).is_file() for m in py_markers) or any(p.glob("test_*.py")) or any(
        next(iter((p / d).rglob("*.py")), None) is not None for d in ("tests", "test") if (p / d).is_dir())
    has_test_dir = (p / "tests").is_dir() or (p / "test").is_dir()
    # A JS project (package.json, e.g. `npm init` with no test script) with a test/ folder is not a
    # pytest project: only choose pytest there when there is Python evidence.
    wants_pytest = has_test_dir or any(p.glob("test_*.py")) or (p / "pytest.ini").is_file()
    if wants_pytest and (py_evidence or not pkg.is_file()):
        return f"{_python_for(p)} -m pytest -q"
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


def _heat_main_root(cwd: str) -> Optional[str]:
    """The user's main checkout when *cwd* is one of Heats' worktrees (``<root>/.heats/<run>/h<N>``)."""
    real = os.path.realpath(cwd)
    marker = os.sep + ".heats" + os.sep
    if marker not in real + os.sep:
        return None
    try:
        cp = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=real, capture_output=True, text=True,
                            timeout=30, stdin=subprocess.DEVNULL, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.SubprocessError):
        return None
    common = cp.stdout.strip()
    if cp.returncode != 0 or not common:
        return None
    common = os.path.realpath(os.path.join(real, common))
    if os.path.basename(common) != ".git":
        return None
    root = os.path.dirname(common)
    return root if real.startswith(root + marker) else None


def point_at_worktree(command: str, cwd: str, repo_root: Optional[str] = None) -> str:
    """*command* with references to the user's main checkout (``cd /abs/repo && pytest``) pointed at
    the worktree *cwd*, so a heat is judged on its own code, not on the user's checkout."""
    root = repo_root or _heat_main_root(cwd)
    if not root or not isinstance(command, str):
        return command
    from .isolation import Lane, _rewrite_command
    root, wt = os.path.realpath(root), os.path.realpath(cwd)
    if root == wt:
        return command
    return _rewrite_command(command, Lane(root=root, worktree=wt, heat=0))


# The check runs repository code (test scripts, conftest.py, Makefiles). It gets only what a test run needs,
# never the rest of Hermes's environment: provider keys and tokens must not reach repo code.
_ENV_KEEP = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TERM", "TZ", "TMPDIR", "TMP", "TEMP",
             "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX", "CONDA_DEFAULT_ENV", "PYENV_VERSION", "NODE_PATH",
             "NVM_DIR", "NVM_BIN", "GOPATH", "GOROOT", "GOCACHE", "GOMODCACHE", "GOFLAGS", "CARGO_HOME",
             "RUSTUP_HOME", "RUSTUP_TOOLCHAIN", "JAVA_HOME", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
             "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA")


EXTRA_ENV: list = []  # names the owner listed in the plugin's ``check_env`` setting (the plugin sets this)


def check_env(extra_names: Optional[list] = None) -> dict:
    """A minimal environment for the check: the names above, LC_*, the owner's EXTRA_ENV names, and the
    CI/colour settings. Nothing else from os.environ."""
    names = EXTRA_ENV if extra_names is None else extra_names
    keep = set(_ENV_KEEP) | {n for n in (names or []) if isinstance(n, str) and n}
    env = {k: v for k, v in os.environ.items() if k in keep or k.startswith("LC_")}
    env.update({"CI": "1", "PYTHONDONTWRITEBYTECODE": "1", "NO_COLOR": "1", "FORCE_COLOR": "0"})
    return env


def run_check(command: str, cwd: str, timeout_s: float = 600, env_extra: Optional[dict] = None,
              max_output_bytes: int = 8_000_000, repo_root: Optional[str] = None) -> CheckResult:
    """Run *command* in *cwd* through the shell. Output goes to a temp file (memory stays flat), the
    whole process group is killed afterwards (so servers a test suite started do not linger), and
    only the tail is kept. In a Heats worktree, absolute references to the main checkout in the
    command are pointed at the worktree first (*repo_root*, or found from git)."""
    import tempfile
    shell_command = point_at_worktree(command, cwd, repo_root)
    main_root = repo_root or _heat_main_root(cwd)
    if main_root and os.path.realpath(main_root) != os.path.realpath(cwd):
        extra_path = editable_pythonpath(main_root, cwd)
        if extra_path:
            env_extra = dict(env_extra or {})
            prev = env_extra.get("PYTHONPATH", os.environ.get("PYTHONPATH", ""))
            env_extra["PYTHONPATH"] = os.pathsep.join(extra_path + ([prev] if prev else []))
    env = check_env()
    if env_extra:
        env.update(env_extra)
    start = time.monotonic()
    with tempfile.TemporaryFile() as out:
        try:
            proc = subprocess.Popen(shell_command, shell=True, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            return CheckResult(command, None, 0.0, error=f"could not start the check: {exc}")
        timed_out = False
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # the group: also children that outlived the shell
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        dur = round(time.monotonic() - start, 2)
        size = out.seek(0, os.SEEK_END)
        out.seek(max(0, size - min(size, max_output_bytes)))
        text = out.read().decode("utf-8", "replace")
    passed, failed = parse_counts(text[-200_000:])
    return CheckResult(
        command=command, exit_code=None if timed_out else proc.returncode, duration_s=dur,
        passed=passed, failed=failed, timed_out=timed_out, tail=text[-OUTPUT_TAIL_CHARS:],
        error=f"timed out after {timeout_s:.0f}s" if timed_out else "",
    )


_TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs)(/|$)|(^|/)test_[^/]*\.py$|_test\.(py|go)$|\.(test|spec)\.[cm]?[jt]sx?$|(^|/)conftest\.py$"
)


_NO_RUNNER = re.compile(r"No module named '?(pytest|py\.test|unittest2|nose)'?|command not found|: not found\b|"
                        r"is not recognized as an internal or external command|ModuleNotFoundError: No module named '?pytest")


def cannot_run(res: "CheckResult", command: str = "") -> bool:
    """True when the check never really ran (missing command or test runner, usage error): no one's code was
    judged, so neither an agent nor an attempt should be blamed for it."""
    if res.timed_out:
        return False
    if res.exit_code is None or res.exit_code in (126, 127):
        return True
    if "pytest" in (command or res.command or "") and res.exit_code in (4, 5):
        return True  # pytest: usage error / no tests collected
    return bool(res.exit_code and _NO_RUNNER.search((res.tail or "")[-4000:]))


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH.search(path.replace("\\", "/")))


# Files that define how the tests run: changing them can skip the failing test without fixing anything.
_TEST_CONFIG_FILE = re.compile(
    r"(^|/)(pytest\.ini|tox\.ini|noxfile\.py|\.mocharc(\.[a-z]+)?|phpunit\.xml(\.dist)?|"
    r"(jest|vitest|karma|playwright|cypress|ava|mocha)\.config\.[cm]?[jt]s(on)?|"
    r"jest\.setup\.[cm]?[jt]s|vitest\.workspace\.[cm]?[jt]s)$")
_MAKEFILES = {"Makefile", "makefile", "GNUmakefile"}
_PKG_TEST_KEY = re.compile(r'"(?:pre|post)?test(?::[^"]*)?"\s*:')
_PYTEST_KEYS = re.compile(r"^\s*(addopts|testpaths|python_files|python_classes|python_functions|norecursedirs|"
                          r"required_plugins|minversion|collect_ignore|usefixtures)\s*=|\[\s*(tool[.:])?pytest")
_SECTION = re.compile(r"^\s*\[+\s*([^\]]+?)\s*\]+")
_MAKE_TARGET = re.compile(r"^([A-Za-z0-9_.%/$(){}-]+(?:\s+[A-Za-z0-9_.%/$(){}-]+)*)\s*::?(?!=)")


def _is_test_target(names: str) -> bool:
    return any(n == "check" or n.startswith("test") or n.endswith("test") or n.endswith("tests")
               for n in names.split())


def harness_changes(patch: Optional[bytes]) -> list[str]:
    """Files in *patch* whose change touches the definition of the test command: the ``test`` script in
    package.json, the pytest section of pyproject.toml / setup.cfg, a Makefile's test target."""
    if not patch:
        return []
    text = patch.decode("utf-8", "replace") if isinstance(patch, bytes) else str(patch)
    out: list[str] = []
    for section in re.split(r"(?m)^(?=diff --git )", text):
        m = re.search(r"(?m)^\+\+\+ (?:b/)?(.+)$", section) or re.search(r"(?m)^--- (?:a/)?(.+)$", section)
        if not m:
            continue
        path = m.group(1).strip().strip('"')
        if path == "/dev/null":
            m2 = re.search(r"(?m)^--- (?:a/)?(.+)$", section)
            path = m2.group(1).strip().strip('"') if m2 else path
        name = path.rsplit("/", 1)[-1]
        if name not in ("package.json", "pyproject.toml", "setup.cfg") and name not in _MAKEFILES:
            continue
        hit = False
        current = ""
        for line in section.splitlines():
            if line.startswith(("+++", "---", "diff --git", "index ")):
                continue
            if line.startswith("@@"):
                ctx = line.split("@@", 2)[-1].strip()
                current = ""
                if name in _MAKEFILES:
                    tm = _MAKE_TARGET.match(ctx)
                    current = tm.group(1) if tm else ""
                else:
                    sm = _SECTION.match(ctx)
                    current = sm.group(1) if sm else ""
                continue
            if not line or line[0] not in "+- ":
                continue
            body, changed = line[1:], line[0] in "+-"
            if name in _MAKEFILES:
                tm = _MAKE_TARGET.match(body)
                if tm:
                    current = tm.group(1)
                if changed and (_is_test_target(current) or (tm and _is_test_target(tm.group(1)))):
                    hit = True
            elif name == "package.json":
                if changed and _PKG_TEST_KEY.search(body):
                    hit = True
            else:
                sm = _SECTION.match(body)
                if sm:
                    current = sm.group(1)
                if changed and ("pytest" in current or _PYTEST_KEYS.search(body)):
                    hit = True
            if hit:
                break
        if hit and path not in out:
            out.append(path)
    return out


@dataclass
class Guard:
    """Files a heat may not change: by default every test file and the files that define how the tests
    run (so a heat cannot 'win' by deleting or weakening the test that judges it, or by skipping it)."""
    protect_tests: bool = True
    extra: list[str] = field(default_factory=list)

    def violations(self, files: list[str], patch: Optional[bytes] = None) -> list[str]:
        out = []
        for f in files:
            if (self.protect_tests and (is_test_path(f) or _TEST_CONFIG_FILE.search(f.replace("\\", "/")))) \
                    or any(f == e or f.startswith(e.rstrip("/") + "/") for e in self.extra):
                out.append(f)
        if self.protect_tests:
            data = patch if patch is not None else getattr(files, "patch_data", None)
            for f in harness_changes(data):
                if f not in out:
                    out.append(f)
        return out
