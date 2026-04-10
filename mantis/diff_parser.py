"""Parse git diffs and map changed lines to function definitions using tree-sitter."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_python as tspython
from tree_sitter import Language, Parser

from mantis.git_utils import run_git as _run_git

PY_LANGUAGE = Language(tspython.language())


@dataclass
class ChangedFunction:
    file_path: str
    function_name: str
    start_line: int
    end_line: int
    source: str
    class_name: str | None = None

    @property
    def qualified_name(self) -> str:
        if self.class_name:
            return f"{self.class_name}.{self.function_name}"
        return self.function_name


@dataclass
class DiffResult:
    changed_functions: list[ChangedFunction] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    raw_diff: str = ""


def parse_diff(repo_path: str, base: str | None = None, head: str | None = None) -> DiffResult:
    """Parse a git diff and extract changed functions.

    If base and head are provided, diffs base..head.
    If only base is provided, diffs base against working tree.
    If neither is provided, diffs HEAD against working tree (uncommitted changes).
    """
    repo = Path(repo_path).resolve()

    diff_cmd = ["git", "-C", str(repo), "diff", "--unified=0"]
    if base and head:
        diff_cmd.append(f"{base}..{head}")
    elif base:
        diff_cmd.append(base)

    raw_diff = _run_git(diff_cmd)
    if not raw_diff.strip():
        return DiffResult(raw_diff=raw_diff)

    file_hunks = _parse_unified_diff(raw_diff)
    changed_files = list(file_hunks.keys())

    changed_functions: list[ChangedFunction] = []
    for file_path, line_ranges in file_hunks.items():
        if not file_path.endswith(".py"):
            continue
        full_path = repo / file_path
        if not full_path.exists():
            continue
        source = full_path.read_text(encoding="utf-8", errors="replace")
        funcs = _map_lines_to_functions(source, file_path, line_ranges)
        changed_functions.extend(funcs)

    return DiffResult(
        changed_functions=changed_functions,
        changed_files=changed_files,
        raw_diff=raw_diff,
    )



def _parse_unified_diff(diff_text: str) -> dict[str, list[tuple[int, int]]]:
    """Parse unified diff output into {file_path: [(start_line, end_line), ...]}."""
    file_hunks: dict[str, list[tuple[int, int]]] = {}
    current_file: str | None = None

    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
            if current_file not in file_hunks:
                file_hunks[current_file] = []
        elif line.startswith("@@ ") and current_file:
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                start = int(match.group(1))
                count = int(match.group(2)) if match.group(2) else 1
                if count > 0:
                    file_hunks[current_file].append((start, start + count - 1))

    return file_hunks


def _map_lines_to_functions(
    source: str,
    file_path: str,
    line_ranges: list[tuple[int, int]],
) -> list[ChangedFunction]:
    """Use tree-sitter to find which functions contain the changed lines."""
    parser = Parser(PY_LANGUAGE)
    tree = parser.parse(source.encode("utf-8"))

    functions = _extract_functions(tree.root_node, source)
    changed: list[ChangedFunction] = []
    seen: set[str] = set()

    for func_name, class_name, start_line, end_line, func_source in functions:
        key = f"{file_path}:{class_name}.{func_name}" if class_name else f"{file_path}:{func_name}"
        if key in seen:
            continue
        for hunk_start, hunk_end in line_ranges:
            if _ranges_overlap(start_line, end_line, hunk_start, hunk_end):
                seen.add(key)
                changed.append(
                    ChangedFunction(
                        file_path=file_path,
                        function_name=func_name,
                        start_line=start_line,
                        end_line=end_line,
                        source=func_source,
                        class_name=class_name,
                    )
                )
                break

    return changed


def _extract_functions(
    root_node,
    source: str,
) -> list[tuple[str, str | None, int, int, str]]:
    """Extract all function definitions from a tree-sitter AST.

    Returns list of (name, class_name, start_line, end_line, source).
    Lines are 1-indexed to match diff output.
    """
    results: list[tuple[str, str | None, int, int, str]] = []

    def _walk(node, class_name: str | None = None):
        if node.type == "class_definition":
            name_node = node.child_by_field_name("name")
            cls_name = name_node.text.decode("utf-8") if name_node else None
            for child in node.children:
                _walk(child, class_name=cls_name)
        elif node.type == "function_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                func_name = name_node.text.decode("utf-8")
                start = node.start_point[0] + 1
                end = node.end_point[0] + 1
                func_source = source.splitlines()[start - 1 : end]
                results.append((func_name, class_name, start, end, "\n".join(func_source)))
            for child in node.children:
                _walk(child, class_name=class_name)
        else:
            for child in node.children:
                _walk(child, class_name=class_name)

    _walk(root_node)
    return results


def _ranges_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start <= b_end and b_start <= a_end
