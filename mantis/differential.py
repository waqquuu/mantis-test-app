"""Differential testing engine.

Runs blast radius functions on both the base and head branches with the same
inputs, then compares outputs to detect behavioral regressions — cases where
the code still runs but produces different results.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ProbeResult:
    name: str
    status: str  # "ok" or "error"
    value: str = ""
    value_type: str = ""
    error_type: str = ""
    error_msg: str = ""


@dataclass
class DiffItem:
    probe_name: str
    category: str  # "behavioral_change", "new_crash", "new_fix"
    base_result: ProbeResult
    head_result: ProbeResult

    @property
    def is_regression(self) -> bool:
        return self.category in ("behavioral_change", "new_crash")


@dataclass
class DifferentialResult:
    diffs: list[DiffItem] = field(default_factory=list)
    base_raw: str = ""
    head_raw: str = ""
    probe_code: str = ""

    @property
    def regressions(self) -> list[DiffItem]:
        return [d for d in self.diffs if d.is_regression]

    @property
    def behavioral_changes(self) -> list[DiffItem]:
        return [d for d in self.diffs if d.category == "behavioral_change"]

    @property
    def new_crashes(self) -> list[DiffItem]:
        return [d for d in self.diffs if d.category == "new_crash"]


def run_differential(
    probe_code: str,
    repo_path: str,
    base_ref: str,
    head_ref: str,
    timeout: int = 60,
) -> DifferentialResult:
    """Run a probe script on both branches and compare outputs.

    Uses git worktrees so the original working tree is never modified.
    """
    repo = Path(repo_path).resolve()

    base_worktree = None
    head_worktree = None

    try:
        base_worktree = _create_worktree(repo, base_ref, "base")
        head_worktree = _create_worktree(repo, head_ref, "head")

        base_output = _run_in_worktree(probe_code, base_worktree, timeout)
        head_output = _run_in_worktree(probe_code, head_worktree, timeout)

        base_results = _parse_probe_output(base_output)
        head_results = _parse_probe_output(head_output)

        diffs = _compare(base_results, head_results)

        return DifferentialResult(
            diffs=diffs,
            base_raw=base_output,
            head_raw=head_output,
            probe_code=probe_code,
        )
    finally:
        if base_worktree:
            _remove_worktree(repo, base_worktree)
        if head_worktree:
            _remove_worktree(repo, head_worktree)


PROBE_RUNNER = '''\
import json
import sys
import ast
import types

results = {}

def capture(name, fn, *args, **kwargs):
    try:
        result = fn(*args, **kwargs)
        results[name] = {
            "status": "ok",
            "value": repr(result),
            "type": type(result).__name__,
        }
    except Exception as e:
        results[name] = {
            "status": "error",
            "error_type": type(e).__name__,
            "error_msg": str(e),
        }

_probe_code = ""\"
{probe_body}
""\"

_globals = {"capture": capture, "results": results, "__name__": "__main__"}

try:
    _tree = ast.parse(_probe_code)
    for _node in _tree.body:
        try:
            _mod = ast.Module(body=[_node], type_ignores=[])
            exec(compile(_mod, "<probe>", "exec"), _globals)
        except Exception:
            pass
except SyntaxError as e:
    results["__syntax_error__"] = {
        "status": "error",
        "error_type": "SyntaxError",
        "error_msg": str(e),
    }

print(json.dumps(results))
'''


def wrap_probe_code(raw_code: str) -> str:
    """Strip boilerplate from Claude's probe code and wrap it in our
    resilient runner that executes each top-level statement independently."""
    lines = raw_code.splitlines()
    filtered: list[str] = []
    skip_block = False

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("results = {}") or stripped.startswith("results={}"):
            continue
        if stripped == "print(json.dumps(results))":
            continue
        if stripped.startswith("import json") or stripped.startswith("import traceback"):
            continue
        if stripped.startswith("import atexit"):
            continue
        if stripped.startswith("import sys") and not stripped.startswith("import sys,"):
            continue
        if stripped.startswith("def _flush_results"):
            skip_block = True
            continue
        if stripped.startswith("atexit.register"):
            continue

        if stripped.startswith("def capture("):
            skip_block = True
            continue
        if skip_block:
            if line and not line[0].isspace() and not stripped == "":
                skip_block = False
            else:
                continue

        filtered.append(line)

    body = "\n".join(filtered).strip()
    escaped_body = body.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    return PROBE_RUNNER.replace("{probe_body}", escaped_body)


def _create_worktree(repo: Path, ref: str, label: str) -> Path:
    """Create a temporary git worktree checked out at the given ref."""
    worktree_dir = Path(tempfile.mkdtemp(prefix=f"mantis_wt_{label}_"))
    result = subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "--detach",
         str(worktree_dir), ref],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        shutil.rmtree(worktree_dir, ignore_errors=True)
        raise RuntimeError(
            f"Failed to create worktree for '{ref}': {result.stderr.strip()}"
        )
    return worktree_dir


def _remove_worktree(repo: Path, worktree_dir: Path) -> None:
    """Remove a git worktree and clean up its directory."""
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", "--force",
         str(worktree_dir)],
        capture_output=True, check=False,
    )
    if worktree_dir.exists():
        shutil.rmtree(worktree_dir, ignore_errors=True)


def _run_in_worktree(
    probe_code: str, worktree_dir: Path, timeout: int,
) -> str:
    """Write and execute a probe script inside a worktree."""
    wrapped = wrap_probe_code(probe_code)
    probe_file = worktree_dir / "mantis_probe_tmp.py"
    probe_file.write_text(wrapped, encoding="utf-8")

    env = os.environ.copy()
    wt_str = str(worktree_dir)
    python_path = env.get("PYTHONPATH", "")
    if wt_str not in python_path:
        env["PYTHONPATH"] = f"{wt_str}:{python_path}" if python_path else wt_str

    try:
        result = subprocess.run(
            [sys.executable, str(probe_file)],
            capture_output=True, text=True,
            cwd=wt_str, timeout=timeout, env=env,
        )
        return result.stdout + "\n" + result.stderr
    except subprocess.TimeoutExpired:
        return '{"__error__": "probe timed out"}'
    finally:
        probe_file.unlink(missing_ok=True)


def _parse_probe_output(raw: str) -> dict[str, ProbeResult]:
    results: dict[str, ProbeResult] = {}

    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    else:
        return results

    for name, info in data.items():
        if name.startswith("__"):
            continue
        if isinstance(info, dict):
            results[name] = ProbeResult(
                name=name,
                status=info.get("status", "unknown"),
                value=info.get("value", ""),
                value_type=info.get("type", ""),
                error_type=info.get("error_type", ""),
                error_msg=info.get("error_msg", ""),
            )

    return results


def _normalize_value(value: str) -> str:
    """Strip non-deterministic parts from repr() output so comparisons
    are stable across runs (memory addresses, temp paths, etc.)."""
    normalized = re.sub(r" at 0x[0-9a-fA-F]+", " at 0x...", value)
    normalized = re.sub(r"object at 0x[0-9a-fA-F]+", "object at 0x...", normalized)
    normalized = re.sub(r"/tmp/[^\s'\"]+", "/tmp/...", normalized)
    return normalized


def _compare(
    base: dict[str, ProbeResult],
    head: dict[str, ProbeResult],
) -> list[DiffItem]:
    diffs: list[DiffItem] = []

    all_names = sorted(set(base.keys()) | set(head.keys()))

    for name in all_names:
        b = base.get(name)
        h = head.get(name)

        if b is not None and h is None and b.status == "ok":
            diffs.append(DiffItem(
                probe_name=name,
                category="new_crash",
                base_result=b,
                head_result=ProbeResult(
                    name=name, status="error",
                    error_type="ScriptCrash",
                    error_msg="Probe could not run on head branch",
                ),
            ))
            continue

        if b is None or h is None:
            continue

        if b.status == "ok" and h.status == "error":
            diffs.append(DiffItem(
                probe_name=name,
                category="new_crash",
                base_result=b,
                head_result=h,
            ))
        elif b.status == "ok" and h.status == "ok":
            b_norm = _normalize_value(b.value)
            h_norm = _normalize_value(h.value)
            if b_norm != h_norm:
                diffs.append(DiffItem(
                    probe_name=name,
                    category="behavioral_change",
                    base_result=b,
                    head_result=h,
                ))
        elif b.status == "error" and h.status == "ok":
            diffs.append(DiffItem(
                probe_name=name,
                category="new_fix",
                base_result=b,
                head_result=h,
            ))

    return diffs
