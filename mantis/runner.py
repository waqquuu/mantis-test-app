"""Run generated regression tests using pytest and collect results."""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TestResult:
    name: str
    status: str  # "passed", "failed", "error"
    message: str = ""
    duration: float = 0.0


@dataclass
class RunResult:
    tests: list[TestResult] = field(default_factory=list)
    passed: int = 0
    failed: int = 0
    errors: int = 0
    raw_output: str = ""
    test_file_path: str = ""
    generated_code: str = ""

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors

    @property
    def success(self) -> bool:
        return self.failed == 0 and self.errors == 0


def run_tests(
    test_code: str,
    repo_path: str,
    timeout: int = 120,
) -> RunResult:
    """Write generated tests to a temp file, run pytest, and collect results."""
    repo = Path(repo_path).resolve()

    test_file = _write_test_file(test_code, repo)

    try:
        raw_output = _run_pytest(test_file, repo, timeout)

        if "collected 0 items / 1 error" in raw_output and "ImportError" in raw_output:
            fixed_code = _try_fix_imports(test_code, raw_output)
            if fixed_code != test_code:
                test_file.write_text(fixed_code, encoding="utf-8")
                test_code = fixed_code
                raw_output = _run_pytest(test_file, repo, timeout)

        tests = _parse_pytest_output(raw_output)

        passed = sum(1 for t in tests if t.status == "passed")
        failed = sum(1 for t in tests if t.status == "failed")
        errors = sum(1 for t in tests if t.status == "error")

        return RunResult(
            tests=tests,
            passed=passed,
            failed=failed,
            errors=errors,
            raw_output=raw_output,
            test_file_path=str(test_file),
            generated_code=test_code,
        )
    finally:
        _cleanup(test_file)


def _write_test_file(test_code: str, target_dir: Path) -> Path:
    fd, path = tempfile.mkstemp(
        prefix="mantis_regression_test_",
        suffix=".py",
        dir=str(target_dir),
    )
    with open(fd, "w", encoding="utf-8") as f:
        f.write(test_code)
    return Path(path)


def _run_pytest(test_file: Path, repo: Path, timeout: int) -> str:
    cmd = [
        sys.executable, "-m", "pytest",
        str(test_file),
        "-v",
        "--tb=short",
        "--no-header",
        "-o", "filterwarnings=",
        "--override-ini=addopts=",
        "--noconftest",
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(repo),
            timeout=timeout,
            env=_build_env(repo),
        )
        return result.stdout + "\n" + result.stderr
    except subprocess.TimeoutExpired:
        return f"TIMEOUT: Tests exceeded {timeout}s time limit"


def _build_env(repo: Path) -> dict[str, str]:
    import os
    env = os.environ.copy()
    python_path = env.get("PYTHONPATH", "")
    repo_str = str(repo)
    if repo_str not in python_path:
        env["PYTHONPATH"] = f"{repo_str}:{python_path}" if python_path else repo_str
    return env


def _parse_pytest_output(output: str) -> list[TestResult]:
    """Parse pytest verbose output to extract individual test results."""
    tests: list[TestResult] = []

    # Match lines like: test_file.py::test_name PASSED
    result_pattern = re.compile(
        r"^.*?::(\S+)\s+(PASSED|FAILED|ERROR)", re.MULTILINE
    )

    for match in result_pattern.finditer(output):
        name = match.group(1)
        status = match.group(2).lower()
        tests.append(TestResult(name=name, status=status))

    # Extract failure details from the FAILURES section
    failure_blocks = _extract_failure_blocks(output)
    for test in tests:
        if test.status in ("failed", "error") and test.name in failure_blocks:
            test.message = failure_blocks[test.name]

    # If no tests were parsed but output indicates a collection error,
    # report it as a single error result
    if not tests and ("ERROR" in output or "ImportError" in output or "SyntaxError" in output):
        tests.append(
            TestResult(
                name="(collection)",
                status="error",
                message=_extract_collection_error(output),
            )
        )

    return tests


def _extract_failure_blocks(output: str) -> dict[str, str]:
    """Extract failure messages keyed by test name."""
    blocks: dict[str, str] = {}
    failure_section = False
    current_test: str | None = None
    current_lines: list[str] = []

    for line in output.splitlines():
        if line.startswith("FAILED") or "FAILURES" in line:
            failure_section = True
            continue
        if failure_section:
            test_match = re.match(r"_{3,}\s+(\S+)\s+_{3,}", line)
            if test_match:
                if current_test:
                    blocks[current_test] = "\n".join(current_lines).strip()
                current_test = test_match.group(1)
                current_lines = []
            elif line.startswith("=") and "short test summary" in line:
                if current_test:
                    blocks[current_test] = "\n".join(current_lines).strip()
                break
            elif current_test is not None:
                current_lines.append(line)

    if current_test and current_test not in blocks:
        blocks[current_test] = "\n".join(current_lines).strip()

    # Also parse the short test summary
    for line in output.splitlines():
        match = re.match(r"FAILED\s+\S+::(\S+)\s*-\s*(.*)", line)
        if match:
            name, msg = match.group(1), match.group(2)
            if name not in blocks or not blocks[name]:
                blocks[name] = msg

    return blocks


def _extract_collection_error(output: str) -> str:
    lines = output.splitlines()
    error_lines = [l for l in lines if "Error" in l or "error" in l.lower()]
    return "\n".join(error_lines[:5]) if error_lines else "Test collection failed"


def _try_fix_imports(test_code: str, error_output: str) -> str:
    """Attempt to fix ImportError by removing the bad symbol from the
    top-level import. The symbol likely needs a submodule import which
    may already exist elsewhere in the file."""
    fixed = test_code
    for line in error_output.splitlines():
        if line.strip().startswith("E   ImportError: cannot import name"):
            m = re.search(r"cannot import name '(\w+)' from '([\w.]+)'", line)
            if not m:
                continue
            symbol, pkg = m.group(1), m.group(2)
            bad_import = f"from {pkg} import"
            for code_line in test_code.splitlines():
                if bad_import in code_line and symbol in code_line:
                    symbols_in_line = [
                        s.strip() for s in
                        code_line.split("import", 1)[1].split(",")
                    ]
                    remaining = [s for s in symbols_in_line if s != symbol]
                    new_lines = []
                    if remaining:
                        new_lines.append(
                            f"from {pkg} import {', '.join(remaining)}"
                        )
                    fixed = fixed.replace(code_line, "\n".join(new_lines))
                    break
    return fixed


def _cleanup(test_file: Path) -> None:
    try:
        test_file.unlink(missing_ok=True)
    except OSError:
        pass
