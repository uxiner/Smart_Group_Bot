"""Tests for the offline CI gate (``scripts/ci_gate.py``).

These tests are meta-tests: they prove that the gate itself fails closed. They
do not run the risk-smoke test files (that is the gate's job, see
``docs/ci.md``); they prove the surrounding contract instead:

* importing the gate module has no side effects,
* the gate resolves paths against *this* worktree and refuses another one
  before it spawns anything,
* a non-zero lint/pytest result makes the gate exit non-zero,
* the new-test extension point actually extends the target list,
* commands are argument lists, never ``shell=True`` strings.

Every subprocess here is a tiny fake executable written to a temp directory, so
the suite is fast and never touches a real interpreter, network or database.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import json
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_PATH = REPO_ROOT / "scripts" / "ci_gate.py"
BOT_INIT = REPO_ROOT / "bot" / "__init__.py"

_load_counter = 0


def setUpModule() -> None:
    """Keep all self-test scratch files inside the checkout, including on CI."""
    (REPO_ROOT / "data").mkdir(exist_ok=True)


def load_gate() -> object:
    """Import scripts/ci_gate.py under a fresh module object."""
    global _load_counter
    _load_counter += 1
    name = f"ci_gate_under_test_{_load_counter}"
    spec = importlib.util.spec_from_file_location(name, GATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _write_executable(directory: Path, name: str, body: str) -> str:
    path = directory / name
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def _fake_ruff(
    directory: Path,
    *,
    findings: list[dict[str, object]] | None = None,
    exit_code: int | None = None,
    diagnostic_findings: list[dict[str, object]] | None = None,
) -> str:
    payload = json.dumps(findings if findings is not None else [])
    diagnostic_payload = json.dumps(diagnostic_findings or [])
    code = exit_code if exit_code is not None else (1 if findings else 0)
    return _write_executable(
        directory,
        "fake_ruff",
        f"""
        import sys
        if sys.argv[-1] == "tests":
            print({diagnostic_payload!r})
            sys.exit({1 if diagnostic_findings else 0})
        print({payload!r})
        sys.exit({code})
        """,
    )


def _fake_python(
    directory: Path,
    *,
    pytest_exit: int = 0,
    marker: Path | None = None,
) -> str:
    marker_line = f"open({str(marker)!r}, 'w').write('ran')\n" if marker else ""
    return _write_executable(
        directory,
        "fake_python",
        f"""
        import json
        import sys
        {marker_line}
        args = sys.argv[1:]
        if "-m" in args and "pytest" in args:
            print({
            ("1 failed, 0 passed in 0.01s" if pytest_exit else "1 passed in 0.01s")!r
        })
            sys.exit({pytest_exit})
        print(json.dumps({{
            "python_version": "3.12.0",
            "executable": sys.executable,
            "bot_file": {str(BOT_INIT)!r},
        }}))
        sys.exit(0)
        """,
    )


def _run_main(gate: object, argv: list[str]) -> tuple[int, str]:
    """Run the gate in-process, capturing both streams (usage errors go to stderr)."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = gate.main(argv)
    return code, stdout.getvalue() + stderr.getvalue()


class GateImportTests(unittest.TestCase):
    def test_import_has_no_side_effects(self) -> None:
        """Importing the module must not spawn processes or call main()."""
        with mock.patch("subprocess.run") as run_mock:
            with mock.patch("subprocess.Popen") as popen_mock:
                with mock.patch("os.system") as system_mock:
                    gate = load_gate()
        run_mock.assert_not_called()
        popen_mock.assert_not_called()
        system_mock.assert_not_called()
        self.assertTrue(callable(gate.main))

    def test_default_repo_root_is_this_worktree(self) -> None:
        gate = load_gate()
        self.assertEqual(gate.DEFAULT_REPO_ROOT, REPO_ROOT)

    def test_default_targets_are_the_declared_risk_smoke_set(self) -> None:
        gate = load_gate()
        self.assertEqual(
            list(gate.DEFAULT_PYTEST_TARGETS),
            [
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
            ],
        )
        self.assertEqual(len(gate.DEFAULT_PYTEST_TARGETS), 11)
        self.assertEqual(len(set(gate.DEFAULT_PYTEST_TARGETS)), 11)
        for target in gate.DEFAULT_PYTEST_TARGETS:
            self.assertTrue((REPO_ROOT / target).is_file(), target)


class GatePathSafetyTests(unittest.TestCase):
    def test_relative_escape_is_rejected(self) -> None:
        gate = load_gate()
        with self.assertRaises(gate.GateUsageError):
            gate.ensure_inside_repo(
                "../../Smart_Bot_FullAudit/tests/test_dm_search.py", REPO_ROOT
            )

    def test_absolute_foreign_worktree_is_rejected(self) -> None:
        gate = load_gate()
        foreign = (
            REPO_ROOT.parent.parent
            / "Smart_Bot_FullAudit"
            / "tests"
            / "test_dm_search.py"
        )
        with self.assertRaises(gate.GateUsageError):
            gate.ensure_inside_repo(str(foreign), REPO_ROOT)

    def test_foreign_target_is_refused_before_anything_runs(self) -> None:
        """A target outside this worktree must be rejected without spawning a child."""
        gate = load_gate()
        foreign = (
            REPO_ROOT.parent.parent
            / "Smart_Bot_FullAudit"
            / "tests"
            / "test_dm_search.py"
        )
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / "data") as tmp:
            tmpdir = Path(tmp)
            marker = tmpdir / "spawned.marker"
            python_exe = _fake_python(tmpdir, marker=marker)
            ruff_exe = _fake_ruff(tmpdir)
            code, output = _run_main(
                gate,
                [
                    "--python",
                    python_exe,
                    "--ruff",
                    ruff_exe,
                    "--pytest-target",
                    str(foreign),
                ],
            )
            # Check before cleanup can remove a mistakenly spawned marker.
            self.assertFalse(
                marker.exists(), "a child process was spawned for a foreign path"
            )
        self.assertEqual(code, gate.EXIT_USAGE, output)
        self.assertIn("path escapes the repository root", output)

    def test_symlink_escape_is_refused_before_anything_runs(self) -> None:
        gate = load_gate()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / "data") as tmp:
            link = Path(tmp) / "foreign-target.py"
            link.symlink_to(REPO_ROOT.parent / "foreign-test.py")
            with mock.patch("subprocess.run") as run_mock:
                code, output = _run_main(gate, ["--pytest-target", str(link)])
            self.assertEqual(code, gate.EXIT_USAGE, output)
            self.assertIn("path escapes the repository root", output)
            run_mock.assert_not_called()

    def test_extension_target_appends_and_dedupes(self) -> None:
        gate = load_gate()
        extra = "tests/test_tts.py"
        targets = gate.resolve_pytest_targets(REPO_ROOT, [extra])
        relative = targets
        self.assertEqual(
            relative[: len(gate.DEFAULT_PYTEST_TARGETS)],
            list(gate.DEFAULT_PYTEST_TARGETS),
        )
        self.assertEqual(relative[-1], extra)
        self.assertEqual(len(relative), len(gate.DEFAULT_PYTEST_TARGETS) + 1)
        # Requesting an already-present target must not duplicate it.
        duplicated = gate.resolve_pytest_targets(REPO_ROOT, [extra, extra, extra])
        self.assertEqual(len(duplicated), len(targets))

    def test_selfcheck_default_and_explicit_additions_run_exactly_once(self) -> None:
        gate = load_gate()
        sibling = "tests/test_self_check.py"
        self.assertEqual(gate.DEFAULT_PYTEST_TARGETS.count(sibling), 1)
        targets = gate.resolve_pytest_targets(
            REPO_ROOT,
            [sibling, sibling, "./tests/test_self_check.py", str(REPO_ROOT / sibling)],
        )
        self.assertEqual(targets, list(gate.DEFAULT_PYTEST_TARGETS))
        self.assertEqual(targets.count(sibling), 1)

    def test_smoke_defaults_need_no_extra_args_and_dedupe_aliases(self) -> None:
        gate = load_gate()
        targets = gate.resolve_pytest_targets(REPO_ROOT)
        self.assertEqual(targets, list(gate.DEFAULT_PYTEST_TARGETS))
        self.assertEqual(len(targets), 11)
        extras: list[str] = []
        for target in (
            "tests/test_telegram_cleanup.py",
            "tests/test_group_reply_targets.py",
            "tests/test_long_term_memory_privacy.py",
            "tests/test_self_check.py",
        ):
            self.assertEqual(targets.count(target), 1)
            extras.extend([target, target, f"./{target}", str(REPO_ROOT / target)])
        self.assertEqual(gate.resolve_pytest_targets(REPO_ROOT, extras), targets)

    def test_full_mode_selects_the_tests_tree(self) -> None:
        gate = load_gate()
        targets = gate.resolve_pytest_targets(REPO_ROOT, full=True)
        self.assertEqual(targets, ["tests"])

    def test_missing_target_is_rejected(self) -> None:
        gate = load_gate()
        with self.assertRaises(gate.GateUsageError):
            gate.resolve_pytest_targets(REPO_ROOT, ["tests/test_does_not_exist.py"])

    def test_repo_root_without_bot_package_is_rejected(self) -> None:
        gate = load_gate()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / "data") as tmp:
            with self.assertRaises(gate.GateUsageError):
                gate.resolve_repo_root(Path(tmp))


class GateEnvironmentTests(unittest.TestCase):
    def test_child_env_clears_interpreter_leaks(self) -> None:
        gate = load_gate()
        polluted = {
            "PYTHONPATH": "/somewhere/else",
            "PYTHONHOME": "/opt/other",
            "VIRTUAL_ENV": "/opt/other/venv",
            "PYTHONSTARTUP": "/tmp/x.py",
            "PYTHONUSERBASE": "/tmp/userbase",
            "PATH": "/usr/bin",
        }
        env = gate.build_child_env(polluted)
        for leaked in (
            "PYTHONPATH",
            "PYTHONHOME",
            "VIRTUAL_ENV",
            "PYTHONSTARTUP",
            "PYTHONUSERBASE",
        ):
            self.assertNotIn(leaked, env)
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_child_env_does_not_mutate_the_caller_environment(self) -> None:
        gate = load_gate()
        polluted = {"PYTHONPATH": "/somewhere/else"}
        gate.build_child_env(polluted)
        self.assertEqual(polluted, {"PYTHONPATH": "/somewhere/else"})


class GateLintTests(unittest.TestCase):
    def _payload(
        self, code: str, filename: Path, row: int, message: str = "boom"
    ) -> str:
        return json.dumps(
            [
                {
                    "code": code,
                    "filename": str(filename),
                    "location": {"row": row, "column": 1},
                    "message": message,
                }
            ]
        )

    def test_parse_findings_keeps_real_location_and_message(self) -> None:
        gate = load_gate()
        findings = gate.parse_ruff_findings(
            self._payload("F821", GATE_PATH, 2), REPO_ROOT
        )
        self.assertEqual(
            [item.as_dict() for item in findings],
            [
                {
                    "code": "F821",
                    "path": "scripts/ci_gate.py",
                    "row": 2,
                    "column": 1,
                    "message": "boom",
                }
            ],
        )

    def test_blocking_scope_and_rules_are_exact(self) -> None:
        gate = load_gate()
        self.assertEqual(
            gate.BLOCKING_LINT_TARGETS,
            (
                "bot",
                "scripts/ci_gate.py",
                "tests/test_ci_gate.py",
            ),
        )
        self.assertEqual(gate.DIAGNOSTIC_LINT_TARGETS, ("tests",))
        self.assertEqual(gate.LINT_RULES, ("E9", "F63", "F7", "F82"))
        self.assertFalse(hasattr(gate, "KNOWN_LINT_EXCEPTIONS"))
        self.assertFalse(hasattr(gate, "partition_lint_findings"))

    def test_any_blocking_finding_fails_even_when_ruff_returns_zero(self) -> None:
        gate = load_gate()
        for rule in ("F821", "F822", "F823", "invalid-syntax"):
            with (
                self.subTest(rule=rule),
                mock.patch.object(
                    gate,
                    "_run",
                    return_value=(0, self._payload(rule, GATE_PATH, 1), ""),
                ),
            ):
                result = gate._stage_lint("ruff", REPO_ROOT, {}, None)
                self.assertTrue(result.failed)
                self.assertEqual(result.extras["findings_total"], 1)
                self.assertIn(rule, result.stderr)

    def test_malformed_ruff_output_blocks(self) -> None:
        gate = load_gate()
        for payload in ("not JSON", "{}"):
            with (
                self.subTest(payload=payload),
                mock.patch.object(gate, "_run", return_value=(0, payload, "")),
            ):
                self.assertTrue(gate._stage_lint("ruff", REPO_ROOT, {}, None).failed)

    def test_nonzero_ruff_without_findings_blocks(self) -> None:
        gate = load_gate()
        with mock.patch.object(gate, "_run", return_value=(1, "[]", "")):
            self.assertTrue(gate._stage_lint("ruff", REPO_ROOT, {}, None).failed)

    def test_historical_findings_are_reported_not_waived(self) -> None:
        gate = load_gate()
        payload = self._payload("F821", REPO_ROOT / "tests/test_skill_service.py", 1570)
        with mock.patch.object(gate, "_run", return_value=(1, payload, "")):
            result = gate._stage_lint_diagnostic("ruff", REPO_ROOT, {}, None)
        self.assertEqual(result.status, "DIAGNOSTIC_FINDINGS")
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(result.extras["blocking"])
        self.assertEqual(result.extras["findings"][0]["code"], "F821")

    def test_diagnostic_tool_failure_is_explicit_not_clean(self) -> None:
        gate = load_gate()
        for code, payload in ((2, ""), (1, "[]"), (0, "not JSON")):
            with (
                self.subTest(code=code, payload=payload),
                mock.patch.object(
                    gate, "_run", return_value=(code, payload, "tool stderr")
                ),
            ):
                result = gate._stage_lint_diagnostic("ruff", REPO_ROOT, {}, None)
                self.assertEqual(result.status, "DIAGNOSTIC_INCOMPLETE")
                self.assertIn("tool_error", result.extras)


class GateExitCodeTests(unittest.TestCase):
    def test_passing_run_returns_zero(self) -> None:
        gate = load_gate()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / "data") as tmp:
            tmpdir = Path(tmp)
            code, output = _run_main(
                gate,
                ["--python", _fake_python(tmpdir), "--ruff", _fake_ruff(tmpdir)],
            )
        self.assertEqual(code, gate.EXIT_OK, output)
        self.assertIn("--- lint (PASS, ruff exit=0)", output)
        self.assertIn("--- pytest (PASS, exit=0)", output)

    def test_gate_runs_without_a_workflow_file(self) -> None:
        """The standalone gate needs the repo layout, not CI YAML or PyYAML."""
        gate = load_gate()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / "data") as tmp:
            root = Path(tmp)
            for target in (
                "pyproject.toml",
                "bot/__init__.py",
                *gate.BLOCKING_LINT_TARGETS[1:],
                *gate.DEFAULT_PYTEST_TARGETS,
            ):
                path = root / target
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            workflow = root / ".github/workflows/ci.yml"
            self.assertFalse(workflow.exists())
            with (
                mock.patch.dict(sys.modules, {"yaml": None}),
                mock.patch.object(
                    gate,
                    "_run",
                    side_effect=[
                        (0, json.dumps({"bot_file": str(root / "bot/__init__.py")}), ""),
                        (0, "[]", ""),
                        (0, "[]", ""),
                        (0, "1 passed in 0.01s", ""),
                    ],
                ) as run_mock,
            ):
                code, output = _run_main(
                    gate,
                    ["--repo-root", str(root), "--python", sys.executable,
                     "--ruff", sys.executable],
                )
            self.assertEqual(code, gate.EXIT_OK, output)
            self.assertEqual(run_mock.call_count, 4)
            self.assertIn("--- pytest (PASS, exit=0)", output)
            self.assertFalse(workflow.exists(), "the gate must not create CI YAML")

    def test_missing_ruff_fails_before_any_spawn(self) -> None:
        gate = load_gate()
        with mock.patch("subprocess.run") as run_mock:
            code, output = _run_main(
                gate, ["--python", sys.executable,
                       "--ruff", str(REPO_ROOT / "data/no-ruff")]
            )
        self.assertEqual(code, gate.EXIT_MISSING_TOOL, output)
        self.assertIn("missing tool: ruff", output)
        run_mock.assert_not_called()

    def test_lint_failure_blocks(self) -> None:
        gate = load_gate()
        finding = {
            "code": "F821",
            "filename": str(GATE_PATH),
            "location": {"row": 1, "column": 1},
            "message": "Undefined name `nope`",
        }
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / "data") as tmp:
            tmpdir = Path(tmp)
            code, output = _run_main(
                gate,
                [
                    "--python",
                    _fake_python(tmpdir),
                    "--ruff",
                    _fake_ruff(tmpdir, findings=[finding]),
                ],
            )
        self.assertEqual(code, gate.EXIT_FAILURE, output)
        self.assertIn("--- lint (FAIL, ruff exit=", output)
        self.assertIn("Undefined name", output)

    def test_lint_tool_error_blocks(self) -> None:
        """ruff exiting non-zero without any parsable finding is a tool failure."""
        gate = load_gate()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / "data") as tmp:
            tmpdir = Path(tmp)
            code, output = _run_main(
                gate,
                [
                    "--python",
                    _fake_python(tmpdir),
                    "--ruff",
                    _fake_ruff(tmpdir, exit_code=2),
                ],
            )
        self.assertEqual(code, gate.EXIT_FAILURE, output)
        self.assertIn("--- lint (FAIL, ruff exit=", output)

    def test_diagnostic_findings_do_not_change_gate_exit(self) -> None:
        """A separate historical diagnostic stays visible without waiving a finding."""
        gate = load_gate()
        finding = {
            "code": "F821",
            "filename": str(REPO_ROOT / "tests/test_skill_service.py"),
            "location": {"row": 1570, "column": 17},
            "message": "Undefined name `context`",
        }
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / "data") as tmp:
            tmpdir = Path(tmp)
            report_path = tmpdir / "report.json"
            code, output = _run_main(
                gate,
                [
                    "--python",
                    _fake_python(tmpdir),
                    "--ruff",
                    _fake_ruff(tmpdir, diagnostic_findings=[finding]),
                    "--report",
                    str(report_path),
                ],
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(code, gate.EXIT_OK, output)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["failed_stages"], [])
        self.assertEqual(report["diagnostic_lint"]["status"], "DIAGNOSTIC_FINDINGS")
        self.assertEqual(report["diagnostic_lint"]["exit_code"], 1)
        self.assertIn("Undefined name `context`", output)
        self.assertNotIn("WAIVED", output)

    def test_pytest_failure_blocks(self) -> None:
        gate = load_gate()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / "data") as tmp:
            tmpdir = Path(tmp)
            code, output = _run_main(
                gate,
                [
                    "--python",
                    _fake_python(tmpdir, pytest_exit=1),
                    "--ruff",
                    _fake_ruff(tmpdir),
                ],
            )
        self.assertEqual(code, gate.EXIT_FAILURE, output)
        self.assertIn("--- pytest (FAIL, exit=1)", output)

    def test_missing_tool_fails_before_any_spawn(self) -> None:
        gate = load_gate()
        with mock.patch("subprocess.run") as run_mock:
            code, output = _run_main(
                gate, ["--python", str(REPO_ROOT / "data/no-python")]
            )
        self.assertEqual(code, gate.EXIT_MISSING_TOOL, output)
        run_mock.assert_not_called()

    def test_foreign_bot_import_stops_before_lint_or_pytest(self) -> None:
        gate = load_gate()
        foreign = str(REPO_ROOT.parent / "foreign/bot/__init__.py")
        with mock.patch.object(
            gate,
            "_run",
            return_value=(
                0,
                json.dumps(
                    {
                        "bot_file": foreign,
                        "executable": sys.executable,
                    }
                ),
                "",
            ),
        ) as run_mock:
            code, output = _run_main(
                gate, ["--python", sys.executable, "--ruff", sys.executable]
            )
        self.assertEqual(code, gate.EXIT_FAILURE, output)
        self.assertEqual(run_mock.call_count, 1)
        self.assertIn("outside this worktree", output)

    def test_skip_pytest_is_never_a_pass(self) -> None:
        gate = load_gate()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / "data") as tmp:
            tmpdir = Path(tmp)
            code, output = _run_main(
                gate,
                [
                    "--python",
                    _fake_python(tmpdir),
                    "--ruff",
                    _fake_ruff(tmpdir),
                    "--skip-pytest",
                ],
            )
        self.assertEqual(code, gate.EXIT_FAILURE, output)
        self.assertIn("PARTIAL, not a pass", output)

    def test_report_records_stage_results(self) -> None:
        gate = load_gate()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / "data") as tmp:
            tmpdir = Path(tmp)
            report_path = tmpdir / "report.json"
            try:
                code, output = _run_main(
                    gate,
                    [
                        "--python",
                        _fake_python(tmpdir, pytest_exit=1),
                        "--ruff",
                        _fake_ruff(tmpdir),
                        "--report",
                        str(report_path),
                    ],
                )
                self.assertEqual(code, gate.EXIT_FAILURE, output)
                report = json.loads(report_path.read_text(encoding="utf-8"))
            finally:
                report_path.unlink(missing_ok=True)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["pytest_targets"], list(gate.DEFAULT_PYTEST_TARGETS))
        self.assertEqual(report["stages"][-1]["targets"], report["pytest_targets"])
        self.assertEqual(report["stages"][-1]["command"][8:], report["pytest_targets"])
        self.assertEqual(report["failed_stages"], ["pytest"])
        names = [stage["name"] for stage in report["stages"]]
        self.assertEqual(names, ["env", "lint", "lint-diag", "pytest"])
        self.assertEqual(report["stages"][-1]["failed"], 1)
        self.assertEqual(report["stages"][-1]["passed"], 0)
        for stage in report["stages"]:
            self.assertIsInstance(stage["command"], list)
            self.assertIn("exit_code", stage)

    def test_report_outside_the_worktree_is_refused(self) -> None:
        gate = load_gate()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / "data") as tmp:
            tmpdir = Path(tmp)
            code, output = _run_main(
                gate,
                [
                    "--python",
                    _fake_python(tmpdir),
                    "--ruff",
                    _fake_ruff(tmpdir),
                    "--report",
                    str(REPO_ROOT.parent / "ci-outside-report.json"),
                ],
            )
        self.assertEqual(code, gate.EXIT_USAGE, output)
        self.assertIn("path escapes the repository root", output)


class GateCommandShapeTests(unittest.TestCase):
    def test_source_never_uses_a_shell(self) -> None:
        tree = ast.parse(GATE_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == "shell":
                        self.assertFalse(
                            isinstance(keyword.value, ast.Constant)
                            and keyword.value.value is True,
                            f"shell=True at line {node.lineno}",
                        )
        source = GATE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("os.system(", source)
        self.assertNotIn("os.popen(", source)

    def test_run_passes_an_argument_list_and_no_shell(self) -> None:
        gate = load_gate()
        with mock.patch("subprocess.run") as run_mock:
            run_mock.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            gate._run(
                ["echo", "hello"], cwd=REPO_ROOT, env={"PATH": "/usr/bin"}, timeout=5
            )
        self.assertEqual(run_mock.call_count, 1)
        args, kwargs = run_mock.call_args
        self.assertIsInstance(args[0], list)
        self.assertEqual(args[0], ["echo", "hello"])
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["cwd"], str(REPO_ROOT))

    def test_timeout_output_remains_reportable_and_fails(self) -> None:
        gate = load_gate()
        import subprocess

        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(
                cmd=["python"], timeout=1, output=b"partial output"
            ),
        ):
            result = gate._stage_pytest(
                "python", REPO_ROOT, {}, 1, ["tests/test_ci_gate.py"]
            )
        self.assertTrue(result.failed)
        self.assertIsNone(result.exit_code)
        self.assertEqual(result.stdout, "partial output")
        self.assertIn("timeout", result.stderr)
        json.dumps(result.as_dict())

    def test_ruff_command_uses_exact_scope_without_inline_suppression(self) -> None:
        gate = load_gate()
        with mock.patch("subprocess.run") as run_mock:
            run_mock.return_value = mock.Mock(returncode=0, stdout="[]", stderr="")
            gate._stage_lint("ruff", REPO_ROOT, {}, None)
        command = run_mock.call_args.args[0]
        self.assertEqual(command[-3:], list(gate.BLOCKING_LINT_TARGETS))
        self.assertIn("--isolated", command)
        self.assertIn("--no-cache", command)
        self.assertIn("--ignore-noqa", command)
        self.assertEqual(command[command.index("--select") + 1], "E9,F63,F7,F82")

    def test_pytest_command_uses_this_interpreter_and_relative_targets(self) -> None:
        gate = load_gate()
        targets = gate.resolve_pytest_targets(REPO_ROOT, [])
        with mock.patch("subprocess.run") as run_mock:
            run_mock.return_value = mock.Mock(
                returncode=0, stdout="1 passed", stderr=""
            )
            with contextlib.redirect_stdout(io.StringIO()):
                gate._stage_pytest("/usr/bin/python3.12", REPO_ROOT, {}, None, targets)
        args, kwargs = run_mock.call_args
        command = args[0]
        self.assertIsInstance(command, list)
        self.assertEqual(command[:3], ["/usr/bin/python3.12", "-m", "pytest"])
        self.assertIn("-p", command)
        self.assertIn("no:cacheprovider", command)
        for target in command[6:]:
            self.assertFalse(Path(target).is_absolute(), target)
        self.assertEqual(kwargs["cwd"], str(REPO_ROOT))
        self.assertNotIn("PYTHONPATH", kwargs["env"])


@unittest.skipUnless(
    (REPO_ROOT / ".github/workflows/ci.yml").is_file(),
    "This checkout has no .github/workflows/ci.yml; standalone gate coverage is separate",
)
class GateWorkflowTests(unittest.TestCase):
    WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

    @staticmethod
    def _workflow_code() -> str:
        """Workflow text with comment lines removed (we assert on configuration)."""
        lines = GateWorkflowTests.WORKFLOW.read_text(encoding="utf-8").splitlines()
        return "\n".join(line for line in lines if not line.lstrip().startswith("#"))

    def test_workflow_is_test_only_and_read_only(self) -> None:
        text = self._workflow_code()
        self.assertIn("contents: read", text)
        self.assertIn("pull_request:", text)
        self.assertIn("workflow_dispatch:", text)
        # No privileged fork trigger, no secrets, no write scope, no publish step.
        self.assertNotIn("pull_request_target", text)
        self.assertNotIn("${{ secrets.", text)
        self.assertNotIn("contents: write", text)
        for forbidden in (
            "docker/build-push-action",
            "docker/login-action",
            "actions/upload-pages-artifact",
            "appleboy/ssh-action",
        ):
            self.assertNotIn(
                forbidden, text, f"workflow must not contain {forbidden!r}"
            )

    def test_workflow_is_valid_yaml(self) -> None:
        import yaml  # Required runtime dependency: absence must fail, never skip.

        document = yaml.safe_load(self.WORKFLOW.read_text(encoding="utf-8"))
        self.assertIsInstance(document, dict)
        self.assertEqual(sorted(document["jobs"]), ["gate"])
        self.assertEqual(document["permissions"], {"contents": "read"})

    def test_missing_yaml_dependency_fails_instead_of_skipping(self) -> None:
        with mock.patch.dict(sys.modules, {"yaml": None}):
            with self.assertRaises(ModuleNotFoundError):
                self.test_workflow_is_valid_yaml()

    def test_workflow_pins_the_ci_tooling_file(self) -> None:
        text = self.WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("requirements-ci.txt", text)
        self.assertIn("requirements.lock", text)
        ci_requirements = (REPO_ROOT / "requirements-ci.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("pytest==9.1.1", ci_requirements)
        self.assertIn("ruff==0.17.3", ci_requirements)
        runtime_pins = (REPO_ROOT / "requirements.lock").read_text(encoding="utf-8")
        self.assertIn("pyyaml==6.0.3", runtime_pins)

    def test_workflow_invokes_formal_entry_without_skipping_tests(self) -> None:
        import yaml

        document = yaml.safe_load(self.WORKFLOW.read_text(encoding="utf-8"))
        commands = [step.get("run", "") for step in document["jobs"]["gate"]["steps"]]
        gate_commands = [
            command for command in commands if "python scripts/ci_gate.py" in command
        ]
        self.assertEqual(len(gate_commands), 1)
        self.assertNotIn("--skip-pytest", gate_commands[0])
        self.assertNotIn("--full", gate_commands[0])


class GateDefaultSenderCoverageTests(unittest.TestCase):
    """Default smoke covers durable cleanup and group reply-target resolution.

    This is not certification of the complete sender suite: ambiguous delivery
    and contact redaction need the wider, explicitly selected regression tests.
    """

    SENDER_RISK_MODULES = (
        "tests/test_telegram_cleanup.py",
        "tests/test_group_reply_targets.py",
    )

    def test_default_invocation_launches_each_sender_module_exactly_once(self) -> None:
        gate = load_gate()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / "data") as tmp:
            tmpdir = Path(tmp)
            recorder = tmpdir / "pytest_argv.json"
            python_cmd = _write_executable(
                tmpdir,
                "recording_python",
                f"""
                import json
                import sys
                args = sys.argv[1:]
                if "-m" in args and "pytest" in args:
                    path = {str(recorder)!r}
                    open(path, "w", encoding="utf-8").write(json.dumps(args))
                    print("1 passed in 0.01s")
                    sys.exit(0)
                print(json.dumps({{
                    "python_version": "3.12.0",
                    "executable": sys.executable,
                    "bot_file": {str(BOT_INIT)!r},
                }}))
                sys.exit(0)
                """,
            )
            report_path = tmpdir / "report.json"
            try:
                code, output = _run_main(
                    gate,
                    [
                        "--python",
                        python_cmd,
                        "--ruff",
                        _fake_ruff(tmpdir),
                        "--report",
                        str(report_path),
                    ],
                )
                self.assertEqual(code, gate.EXIT_OK, output)
                child_argv = json.loads(recorder.read_text(encoding="utf-8"))
                report = json.loads(report_path.read_text(encoding="utf-8"))
            finally:
                report_path.unlink(missing_ok=True)

        # The default run is exactly the declared risk set: no extra targets.
        self.assertNotIn("--pytest-target", child_argv)
        self.assertEqual(report["pytest_targets"], list(gate.DEFAULT_PYTEST_TARGETS))
        self.assertNotIn(
            "tests",
            report["pytest_targets"],
            "the default run must not be a full-tests-tree run",
        )
        runtime_targets = child_argv[child_argv.index("no:cacheprovider") + 1 :]
        self.assertEqual(runtime_targets, list(gate.DEFAULT_PYTEST_TARGETS))
        for module in self.SENDER_RISK_MODULES:
            self.assertEqual(gate.DEFAULT_PYTEST_TARGETS.count(module), 1, module)
            self.assertEqual(runtime_targets.count(module), 1, module)
            self.assertTrue((REPO_ROOT / module).is_file(), module)
            # Selected by default, not sunk into the non-blocking diagnostic scope.
            self.assertNotIn(module, gate.DIAGNOSTIC_LINT_TARGETS)
        # No CLI flag is needed for that coverage: the bare default resolution
        # already carries every sender module, once each.
        default_targets = gate.resolve_pytest_targets(REPO_ROOT)
        for module in self.SENDER_RISK_MODULES:
            self.assertEqual(default_targets.count(module), 1, module)


class GateDefaultMemoryCoverageTests(unittest.TestCase):
    """Focused memory privacy coverage runs by default, not by an opt-in flag."""

    # Focused cross-scope privacy coverage; the wider memory suite is opt-in.
    MEMORY_RISK_MODULES: tuple[str, ...] = (
        "tests/test_long_term_memory_privacy.py",
    )

    def test_default_invocation_runs_each_memory_module_exactly_once(self) -> None:
        """The default gate must launch every memory risk module by itself.

        This is deliberately causal rather than static: it drives the real
        ``main()`` on the default path and inspects the child pytest argv, so a
        gate whose default list merely mentions these modules elsewhere cannot
        satisfy it.
        """
        gate = load_gate()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / "data") as tmp:
            tmpdir = Path(tmp)
            recorder = tmpdir / "pytest_argv.json"
            python_cmd = _write_executable(
                tmpdir,
                "recording_python",
                f"""
                import json
                import sys
                args = sys.argv[1:]
                if "-m" in args and "pytest" in args:
                    open({str(recorder)!r}, "w", encoding="utf-8").write(
                        json.dumps(args)
                    )
                    print("1 passed in 0.01s")
                    sys.exit(0)
                print(json.dumps({{
                    "python_version": "3.12.0",
                    "executable": sys.executable,
                    "bot_file": {str(BOT_INIT)!r},
                }}))
                sys.exit(0)
                """,
            )
            report_path = tmpdir / "report.json"
            try:
                code, output = _run_main(
                    gate,
                    [
                        "--python",
                        python_cmd,
                        "--ruff",
                        _fake_ruff(tmpdir),
                        "--report",
                        str(report_path),
                    ],
                )
                self.assertEqual(code, gate.EXIT_OK, output)
                child_argv = json.loads(recorder.read_text(encoding="utf-8"))
                report = json.loads(report_path.read_text(encoding="utf-8"))
            finally:
                report_path.unlink(missing_ok=True)

        # Nothing but the declared default was ever requested.
        for forbidden in ("--pytest-target", "--full", "--skip-pytest", "--timeout"):
            self.assertNotIn(forbidden, child_argv)
        self.assertEqual(report["pytest_targets"], list(gate.DEFAULT_PYTEST_TARGETS))
        self.assertNotIn("tests", report["pytest_targets"])

        runtime_targets = child_argv[child_argv.index("no:cacheprovider") + 1 :]
        self.assertEqual(runtime_targets, list(gate.DEFAULT_PYTEST_TARGETS))
        for module in self.MEMORY_RISK_MODULES:
            self.assertEqual(gate.DEFAULT_PYTEST_TARGETS.count(module), 1, module)
            self.assertEqual(runtime_targets.count(module), 1, module)
            self.assertTrue((REPO_ROOT / module).is_file(), module)
            # Selected by default, not sunk into the non-blocking diagnostic scope.
            self.assertNotIn(module, gate.DIAGNOSTIC_LINT_TARGETS)
        default_targets = gate.resolve_pytest_targets(REPO_ROOT)
        for module in self.MEMORY_RISK_MODULES:
            self.assertEqual(default_targets.count(module), 1, module)

    def test_explicit_modes_keep_their_existing_semantics(self) -> None:
        gate = load_gate()
        defaults = list(gate.DEFAULT_PYTEST_TARGETS)
        # Custom selection still appends to the defaults and de-duplicates.
        self.assertEqual(
            gate.resolve_pytest_targets(REPO_ROOT, ["tests/test_private_chat_history.py"]),
            defaults,
        )
        self.assertEqual(
            gate.resolve_pytest_targets(REPO_ROOT, ["tests/test_telegram_cleanup.py"]),
            defaults,
        )
        # Full mode is still the manual whole-tree entry.
        self.assertEqual(gate.resolve_pytest_targets(REPO_ROOT, full=True), ["tests"])
        # Skip mode still refuses to look like a pass.
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / "data") as tmp:
            tmpdir = Path(tmp)
            report_path = tmpdir / "skip-report.json"
            try:
                code, output = _run_main(
                    gate,
                    [
                        "--python",
                        _fake_python(tmpdir),
                        "--ruff",
                        _fake_ruff(tmpdir),
                        "--skip-pytest",
                        "--report",
                        str(report_path),
                    ],
                )
                report = json.loads(report_path.read_text(encoding="utf-8"))
            finally:
                report_path.unlink(missing_ok=True)
        self.assertEqual(code, gate.EXIT_FAILURE, output)
        self.assertEqual(report["status"], "PARTIAL")
        self.assertTrue(report["partial"])
        pytest_stage = next(s for s in report["stages"] if s["name"] == "pytest")
        self.assertEqual(pytest_stage["status"], "SKIPPED")
        self.assertEqual(report["pytest_targets"], defaults)


if __name__ == "__main__":
    unittest.main()
