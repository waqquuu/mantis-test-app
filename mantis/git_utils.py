"""Shared git helper utilities."""

from __future__ import annotations

import subprocess


def run_git(cmd: list[str]) -> str:
    """Run a git command and return stdout. Raises RuntimeError on failure."""
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0 and result.stderr:
        raise RuntimeError(f"Git command failed: {' '.join(cmd)}\n{result.stderr}")
    return result.stdout
