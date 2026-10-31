#!/usr/bin/env python3
"""Test-only CI gate for the SmartBot stability candidate.

Stages, no deployment, no production contact:

1. ``env``        -- prove the child interpreter resolves ``bot`` inside *this*
                     worktree, so a run can never silently certify another tree.
2. ``lint``       -- blocking ruff check (``E9,F63,F7,F82``) over the increment:
                     all of ``bot/`` plus this script and its own test module.
                     Any finding fails the gate. There is deliberately NO waiver
                     mechanism: a real defect is reported and must be fixed by
                     its owner, never parsed into a pass.
3. ``lint-diag``  -- the same rules over the historical ``tests/`` tree, recorded
                     as a *diagnostic* only. It is explicitly NOT part of the
                     batch-1 blocking scope and it never changes the exit code.
4. ``pytest``     -- the explicit risk-smoke list (business risk modules plus
                     this gate's own tests). Non-zero pytest fails the gate.

Design rules:

* stdlib only; importing this module must have no side effects (no subprocess,
  no filesystem writes, no ``main()`` call).
* every child process is an argument list with ``shell=False``.
* every path is resolved and required to stay inside the repo root.
* the gate does not edit source or configuration; explicit reports and test
  scratch files may be written. Children run with ``PYTHONDONTWRITEBYTECODE=1``
  and pytest uses ``-p no:cacheprovider``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_MISSING_TOOL = 3

# The repository that contains this script. Resolved without touching the
# filesystem beyond resolving the path itself; no I/O, no side effects.
DEFAULT_REPO_ROOT = Path(__file__).resolve().parent.parent

# Blocking lint rules only. These are unambiguous defects (syntax errors,
# undefined names, broken comparisons, forward references) rather than style
# opinions, so they are safe to block on for an existing baseline.
#
#   E9  -> E999 syntax error
#   F63 -> invalid `__future__` import / comparison
#   F7  -> F701..F707 / F722 / F811 / F821 / F823 broken syntax & scoping
#   F82 -> F821 undefined name, F822 undefined name in __all__, F823 local
#          used before assignment
#
# Progressive by design: style and import-order families are not enabled here.
LINT_RULES: tuple[str, ...] = ("E9", "F63", "F7", "F82")

# Batch-1 blocking lint scope: the increment, not a whole-repo reformat.
# ``bot/`` is included because the gate must be strict over the shipped code it
# guards; the two new files are included so the gate lints itself.
BLOCKING_LINT_TARGETS: tuple[str, ...] = (
    "bot",
    "scripts/ci_gate.py",
    "tests/test_ci_gate.py",
)

# Diagnostic-only lint scope. The historical test tree is scanned and reported
# so defects stay visible, but it is NOT in the batch-1 blocking scope: fixing
# those tests is outside this branch's mandate, and pretending the whole repo
# lints clean would be false.
DIAGNOSTIC_LINT_TARGETS: tuple[str, ...] = ("tests",)

# Fast, explicit smoke subset selected from this checkout's test inventory.
# Keep every entry present in tests/; add a new risk test here with its own
# change rather than silently expanding to every test pytest can discover.
# ``tests/test_ci_gate.py`` exercises this gate's own path, command, and exit
# code contract on every default run.
DEFAULT_PYTEST_TARGETS: tuple[str, ...] = (
    "tests/test_dm_search.py",
    "tests/test_context_gate.py",
    "tests/test_resource_health.py",
    "tests/test_background_health.py",
    "tests/test_p1_webentry_d3_17_health_disclosure.py",
    "tests/test_private_chat_history.py",
    "tests/test_ci_gate.py",
    "tests/test_telegram_cleanup.py",
    "tests/test_group_reply_targets.py",
    "tests/test_long_term_memory_privacy.py",
    "tests/test_self_check.py",
)

# Manual full-suite entry. Never run by CI, never claimed as run.
FULL_PYTEST_TARGET = "tests"

# Locale/path variables that can redirect imports to another worktree or venv.
ENV_VARS_TO_CLEAR: tuple[str, ...] = (
    "PYTHONPATH",
    "PYTHONHOME",
    "VIRTUAL_ENV",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
)

_PROBE_SOURCE = (
    "import json, sys, bot;"
    "print(json.dumps({'bot_file': getattr(bot, '__file__', None),"
    " 'executable': sys.executable, 'prefix': sys.prefix}))"
)


class GateUsageError(Exception):
    """Bad invocation or layout; maps to EXIT_USAGE."""


class GateToolError(Exception):
    """A required executable is missing; maps to EXIT_MISSING_TOOL."""


@dataclass
class LintFinding:
    code: str
    path: str
    row: int
    column: int
    message: str

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "path": self.path,
            "row": self.row,
            "column": self.column,
            "message": self.message,
        }

    def one_line(self) -> str:
        return f"{self.path}:{self.row}:{self.column}: {self.code} {self.message}"


@dataclass
class StageResult:
    name: str
    status: str
    exit_code: int | None
    command: list[str] | None = None
    stdout: str = ""
    stderr: str = ""
    extras: dict[str, object] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status == "FAIL"

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "status": self.status,
            "exit_code": self.exit_code,
            "command": self.command,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }
        payload.update(self.extras)
        return payload


def build_child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Child environment: no import redirection, no bytecode writes.

    The caller's environment is never mutated; a copy is returned.
    """
    env = dict(os.environ if base is None else base)
    for name in ENV_VARS_TO_CLEAR:
        env.pop(name, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def resolve_repo_root(candidate: Path | str) -> Path:
    """Resolve and validate a repository root using the layout markers."""
    root = Path(candidate).expanduser().resolve()
    if not root.is_dir():
        raise GateUsageError(f"repo root is not a directory: {root}")
    for marker in ("pyproject.toml", "bot/__init__.py"):
        if not (root / marker).exists():
            raise GateUsageError(f"repo root lacks {marker}: {root}")
    return root


def ensure_inside_repo(raw: str, repo_root: Path, *, must_exist: bool = True) -> Path:
    """Resolve ``raw`` against ``repo_root`` and refuse anything that escapes it."""
    candidate = Path(raw).expanduser()
    resolved = (
        candidate if candidate.is_absolute() else repo_root / candidate
    ).resolve()
    root = repo_root.resolve()
    if resolved != root and root not in resolved.parents:
        raise GateUsageError(
            f"path escapes the repository root ({root}): {raw} -> {resolved}"
        )
    if must_exist and not resolved.exists():
        raise GateUsageError(f"path does not exist: {raw} -> {resolved}")
    return resolved


def resolve_pytest_targets(
    repo_root: Path,
    extra: Sequence[str] = (),
    *,
    full: bool = False,
) -> list[str]:
    """Build the ordered, de-duplicated pytest target list.

    Defaults are the explicit risk set; ``extra`` appends (this is how a new
    selfcheck module joins); ``full`` selects the whole test tree instead.
    """
    if full:
        ensure_inside_repo(FULL_PYTEST_TARGET, repo_root)
        return [FULL_PYTEST_TARGET]
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in list(DEFAULT_PYTEST_TARGETS) + list(extra):
        resolved = ensure_inside_repo(raw, repo_root)
        relative = resolved.relative_to(repo_root).as_posix()
        if relative in seen:
            continue
        seen.add(relative)
        ordered.append(relative)
    return ordered


def resolve_tool(name: str, kind_label: str) -> str:
    """Locate an executable without following symlinks.

    ``Path.resolve()`` must NOT be used: a venv interpreter is usually a symlink
    to the base interpreter, and resolving it would silently drop that venv's
    site-packages.
    """
    candidate = Path(name).expanduser()
    if candidate.is_absolute() or os.sep in name:
        path = candidate.absolute()
        if not path.is_file():
            raise GateToolError(f"{kind_label} not found: {path}")
        return str(path)
    found = shutil.which(name)
    if not found:
        raise GateToolError(f"{kind_label} not found on PATH: {name}")
    return str(Path(found).absolute())


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float | None,
) -> tuple[int | None, str, str]:
    """Run one child process. Argument list only, never ``shell=True``."""
    try:
        completed = subprocess.run(
            list(command),
            cwd=str(cwd),
            env=env,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="replace")
        return None, out, f"timeout after {timeout}s: {exc}"
    except OSError as exc:  # pragma: no cover - environment dependent
        return None, "", f"failed to start {command[0]}: {exc}"
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def _stage_env_probe(
    python_cmd: str,
    repo_root: Path,
    env: dict[str, str],
    timeout: float | None,
) -> StageResult:
    """Prove ``bot`` resolves inside this worktree before anything else runs."""
    command = [python_cmd, "-c", _PROBE_SOURCE]
    code, out, err = _run(command, cwd=repo_root, env=env, timeout=timeout)
    extras: dict[str, object] = {"bot_file": None, "executable": None, "prefix": None}
    if code != 0:
        return StageResult("env", "FAIL", code, command, out, err, extras)
    try:
        payload = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        return StageResult(
            "env",
            "FAIL",
            code,
            command,
            out,
            f"{err}\nunparsable probe output: {exc}",
            extras,
        )
    bot_file = payload.get("bot_file")
    extras.update(
        {
            "bot_file": bot_file,
            "executable": payload.get("executable"),
            "prefix": payload.get("prefix"),
        }
    )
    if not bot_file:
        return StageResult(
            "env", "FAIL", code, command, out, "bot.__file__ is unset", extras
        )
    resolved = Path(bot_file).resolve()
    root = repo_root.resolve()
    if root not in resolved.parents:
        return StageResult(
            "env",
            "FAIL",
            code,
            command,
            out,
            f"bot resolves outside this worktree: {resolved} (expected under {root})",
            extras,
        )
    return StageResult("env", "PASS", code, command, out, err, extras)


def parse_ruff_findings(payload: str, repo_root: Path) -> list[LintFinding]:
    """Parse ``ruff check --output-format=json`` into repo-relative findings."""
    text = payload.strip()
    if not text:
        return []
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("ruff JSON payload is not a list")
    findings: list[LintFinding] = []
    root = repo_root.resolve()
    for item in data:
        location = item.get("location") or {}
        raw_path = str(item.get("filename") or "")
        path = Path(raw_path)
        absolute = (path if path.is_absolute() else root / path).resolve()
        try:
            rel = absolute.relative_to(root)
        except ValueError:
            rel = Path(raw_path)
        findings.append(
            LintFinding(
                code=str(item.get("code") or ""),
                path=rel.as_posix(),
                row=int(location.get("row") or 0),
                column=int(location.get("column") or 0),
                message=str(item.get("message") or ""),
            )
        )
    return findings


def _ruff_command(ruff_cmd: str, targets: Sequence[str]) -> list[str]:
    return [
        ruff_cmd,
        "check",
        "--isolated",  # never inherit a ruff config from this or a parent tree
        "--no-cache",
        "--ignore-noqa",
        "--select",
        ",".join(LINT_RULES),
        "--output-format=json",
        *targets,
    ]


def _stage_lint(
    ruff_cmd: str,
    repo_root: Path,
    env: dict[str, str],
    timeout: float | None,
) -> StageResult:
    """Blocking lint. Any finding at all fails: there is no waiver path."""
    command = _ruff_command(ruff_cmd, BLOCKING_LINT_TARGETS)
    code, out, err = _run(command, cwd=repo_root, env=env, timeout=timeout)
    extras: dict[str, object] = {"findings": [], "findings_total": 0}
    if code not in (0, 1):
        extras["tool_error"] = f"ruff exited {code}"
        return StageResult("lint", "FAIL", code, command, out, err, extras)
    try:
        findings = parse_ruff_findings(out, repo_root)
    except ValueError as exc:
        extras["tool_error"] = f"unparsable ruff output: {exc}"
        return StageResult("lint", "FAIL", code, command, out, err, extras)
    extras["findings"] = [item.as_dict() for item in findings]
    extras["findings_total"] = len(findings)
    if findings:
        details = "\n".join(item.one_line() for item in findings)
        return StageResult(
            "lint", "FAIL", code, command, out, f"{err}\n{details}".strip(), extras
        )
    if code != 0:
        extras["tool_error"] = "ruff exited non-zero without findings"
        return StageResult("lint", "FAIL", code, command, out, err, extras)
    return StageResult("lint", "PASS", code, command, out, err, extras)


def _stage_lint_diagnostic(
    ruff_cmd: str,
    repo_root: Path,
    env: dict[str, str],
    timeout: float | None,
    *,
    skip: bool = False,
) -> StageResult:
    """Non-blocking lint over the historical test tree; findings stay visible."""
    extras: dict[str, object] = {
        "blocking": False,
        "scope_note": (
            "diagnostic only: tests/ is NOT in the batch-1 blocking lint scope; "
            "fixing these files is outside this branch's mandate"
        ),
        "findings": [],
        "findings_total": 0,
    }
    if skip:
        extras["skipped"] = True
        return StageResult(
            "lint-diag", "DIAGNOSTIC_SKIPPED", None, None, "", "", extras
        )
    command = _ruff_command(ruff_cmd, DIAGNOSTIC_LINT_TARGETS)
    code, out, err = _run(command, cwd=repo_root, env=env, timeout=timeout)
    if code not in (0, 1):
        extras["tool_error"] = f"ruff exited {code}; diagnostic incomplete"
        return StageResult(
            "lint-diag", "DIAGNOSTIC_INCOMPLETE", code, command, out, err, extras
        )
    try:
        findings = parse_ruff_findings(out, repo_root)
    except ValueError as exc:
        extras["tool_error"] = f"unparsable ruff output: {exc}"
        return StageResult(
            "lint-diag", "DIAGNOSTIC_INCOMPLETE", code, command, out, err, extras
        )
    extras["findings"] = [item.as_dict() for item in findings]
    extras["findings_total"] = len(findings)
    if code == 1 and not findings:
        extras["tool_error"] = (
            "ruff exited non-zero without findings; diagnostic incomplete"
        )
        return StageResult(
            "lint-diag", "DIAGNOSTIC_INCOMPLETE", code, command, out, err, extras
        )
    status = "DIAGNOSTIC_CLEAN" if not findings else "DIAGNOSTIC_FINDINGS"
    return StageResult("lint-diag", status, code, command, out, err, extras)


def summarize_pytest_output(text: str) -> dict[str, object]:
    """Pull the per-test counters out of pytest's summary line verbatim."""
    summary: dict[str, object] = {"summary_line": "", "failed": 0, "passed": 0}
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.endswith("=") or stripped.startswith("="):
            continue
        if " in " in stripped and (
            "passed" in stripped or "failed" in stripped or "error" in stripped
        ):
            summary["summary_line"] = stripped
            for part in stripped.split(","):
                token = part.strip().split(" ")
                if len(token) >= 2 and token[0].isdigit():
                    summary[token[1]] = int(token[0])
            break
    return summary


def _stage_pytest(
    python_cmd: str,
    repo_root: Path,
    env: dict[str, str],
    timeout: float | None,
    targets: Sequence[str],
) -> StageResult:
    """Risk smoke. Non-zero pytest fails the gate; nothing is deselected."""
    command = [
        python_cmd,
        "-m",
        "pytest",
        "-q",
        "--tb=short",
        "-rf",
        "-p",
        "no:cacheprovider",
        *targets,
    ]
    code, out, err = _run(command, cwd=repo_root, env=env, timeout=timeout)
    summary = summarize_pytest_output(out + "\n" + err)
    extras: dict[str, object] = {"targets": list(targets)}
    extras.update(summary)
    status = "PASS" if code == 0 else "FAIL"
    return StageResult("pytest", status, code, command, out, err, extras)


def _print_summary(results: Sequence[StageResult]) -> None:
    print("\n=== gate summary ===")
    for result in results:
        code = "-" if result.exit_code is None else result.exit_code
        print(f"[{result.status:<20}] {result.name:<10} exit={code}")
        for key in (
            "findings_total",
            "summary_line",
            "bot_file",
            "scope_note",
            "tool_error",
        ):
            value = result.extras.get(key)
            if value not in (None, "", []):
                print(f"           {key}: {value}")
        for finding in result.extras.get("findings", []) or []:
            print(
                f"           finding: {finding['path']}:{finding['row']}:"
                f"{finding['column']} {finding['code']} {finding['message']}"
            )


def build_report(
    repo_root: Path,
    results: Sequence[StageResult],
    *,
    pytest_targets: Sequence[str] = (),
    partial: bool = False,
) -> dict[str, object]:
    """Assemble the machine-readable report (structure only, no verdict spin)."""
    blocking = [r for r in results if r.name != "lint-diag"]
    failed = [r.name for r in blocking if r.failed]
    diagnostic = next((r for r in results if r.name == "lint-diag"), None)
    if partial:
        status = "PARTIAL"
    elif failed:
        status = "FAIL"
    else:
        status = "PASS"
    return {
        "gate": "smartbot-stability-ci",
        "status": status,
        "partial": partial,
        "repo_root": str(repo_root),
        "pytest_targets": list(pytest_targets),
        "lint_rules": list(LINT_RULES),
        "blocking_lint_targets": list(BLOCKING_LINT_TARGETS),
        "diagnostic_lint_targets": list(DIAGNOSTIC_LINT_TARGETS),
        "failed_stages": failed,
        "stages": [r.as_dict() for r in results],
        "diagnostic_lint": diagnostic.as_dict() if diagnostic else None,
        "notes": [
            "lint-diag is diagnostic only and never affects the exit code",
            "no waiver, noqa or expected-failure list is accepted by this gate",
            "deployment is out of scope: this gate never publishes or deploys",
        ],
    }


def _write_report(path: Path, report: dict[str, object]) -> None:
    """Write the report atomically (temp file in the same directory, then rename)."""
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")
    os.replace(tmp, path)


def run_gate(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, run the stages in order, and return an exit code."""
    parser = argparse.ArgumentParser(
        prog="ci_gate.py",
        description="Test-only CI gate: env probe, blocking lint, risk-smoke pytest.",
    )
    parser.add_argument("--repo-root", default=str(DEFAULT_REPO_ROOT))
    parser.add_argument("--python", dest="python_cmd", default=sys.executable)
    parser.add_argument("--ruff", dest="ruff_cmd", default=None)
    parser.add_argument(
        "--pytest-target",
        action="append",
        default=[],
        metavar="PATH",
        help="append a test target (repeatable); must stay inside the repo root",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="run the whole tests/ tree instead of the risk-smoke list (manual entry)",
    )
    parser.add_argument("--timeout", type=float, default=None)
    parser.add_argument(
        "--report", default=None, help="write a JSON report to this path"
    )
    parser.add_argument(
        "--skip-pytest",
        action="store_true",
        help="run only the static stages; reported as PARTIAL and never as a pass",
    )
    parser.add_argument(
        "--skip-diagnostics",
        action="store_true",
        help="skip the non-blocking tests/ lint diagnostic",
    )
    args = parser.parse_args(None if argv is None else list(argv))
    try:
        repo_root = resolve_repo_root(args.repo_root)
        # Validate destinations and scopes before spawning anything at all.
        report_path = (
            ensure_inside_repo(args.report, repo_root, must_exist=False)
            if args.report
            else None
        )
        if report_path is not None and report_path.is_dir():
            raise GateUsageError(f"report path is a directory: {report_path}")
        if report_path is not None:
            ensure_inside_repo(str(report_path) + ".tmp", repo_root, must_exist=False)
        pytest_targets = resolve_pytest_targets(
            repo_root, args.pytest_target, full=args.full
        )
        for target in (*BLOCKING_LINT_TARGETS, *DIAGNOSTIC_LINT_TARGETS):
            ensure_inside_repo(target, repo_root)
        python_cmd = resolve_tool(args.python_cmd, "python interpreter")
        ruff_cmd = resolve_tool(args.ruff_cmd or "ruff", "ruff")
    except GateUsageError as exc:
        print(f"usage error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except GateToolError as exc:
        print(f"missing tool: {exc}", file=sys.stderr)
        return EXIT_MISSING_TOOL

    env = build_child_env()
    results: list[StageResult] = []
    print(f"repo root : {repo_root}")
    print(f"python    : {python_cmd}")
    print(f"ruff      : {ruff_cmd}")
    print(f"lint rules: {','.join(LINT_RULES)}")
    print(f"lint scope: {', '.join(BLOCKING_LINT_TARGETS)} (blocking)")
    print(
        f"            {', '.join(DIAGNOSTIC_LINT_TARGETS)} (diagnostic, non-blocking)"
    )
    print(f"pytest    : {' '.join(pytest_targets)}")
    print()

    env_result = _stage_env_probe(python_cmd, repo_root, env, args.timeout)
    results.append(env_result)
    print(f"--- env ({env_result.status}, exit={env_result.exit_code})")
    if env_result.stdout.strip():
        print(env_result.stdout.strip())
    if env_result.stderr.strip():
        print(env_result.stderr.strip(), file=sys.stderr)
    if env_result.failed:
        _print_summary(results)
        if report_path is not None:
            _write_report(
                report_path,
                build_report(repo_root, results, pytest_targets=pytest_targets),
            )
        return EXIT_FAILURE

    lint_result = _stage_lint(ruff_cmd, repo_root, env, args.timeout)
    results.append(lint_result)
    print(f"--- lint ({lint_result.status}, ruff exit={lint_result.exit_code})")
    for finding in lint_result.extras.get("findings", []) or []:
        print(
            f"    {finding['path']}:{finding['row']}:{finding['column']} "
            f"{finding['code']} {finding['message']}"
        )
    if lint_result.stderr.strip():
        print(lint_result.stderr.strip(), file=sys.stderr)

    diag_result = _stage_lint_diagnostic(
        ruff_cmd, repo_root, env, args.timeout, skip=args.skip_diagnostics
    )
    results.append(diag_result)
    print(
        f"--- lint-diag ({diag_result.status}, ruff exit={diag_result.exit_code}) "
        "-- diagnostic only, does not affect the exit code"
    )
    for finding in diag_result.extras.get("findings", []) or []:
        print(
            f"    {finding['path']}:{finding['row']}:{finding['column']} "
            f"{finding['code']} {finding['message']}"
        )

    if args.skip_pytest:
        results.append(
            StageResult(
                "pytest",
                "SKIPPED",
                None,
                None,
                "",
                "",
                {"targets": list(pytest_targets), "note": "skipped by --skip-pytest"},
            )
        )
        print("--- pytest (SKIPPED by --skip-pytest; result is PARTIAL, not a pass)")
        _print_summary(results)
        if report_path is not None:
            _write_report(
                report_path,
                build_report(
                    repo_root, results, pytest_targets=pytest_targets, partial=True
                ),
            )
        return EXIT_FAILURE

    pytest_result = _stage_pytest(
        python_cmd, repo_root, env, args.timeout, pytest_targets
    )
    results.append(pytest_result)
    print(f"--- pytest ({pytest_result.status}, exit={pytest_result.exit_code})")
    tail = [line for line in pytest_result.stdout.splitlines() if line.strip()][-25:]
    for line in tail:
        print(f"    {line}")
    if pytest_result.stderr.strip():
        print(pytest_result.stderr.strip(), file=sys.stderr)
    _print_summary(results)
    report = build_report(repo_root, results, pytest_targets=pytest_targets)
    if report_path is not None:
        _write_report(report_path, report)
    if lint_result.failed or pytest_result.failed:
        return EXIT_FAILURE
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    return run_gate(argv)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
