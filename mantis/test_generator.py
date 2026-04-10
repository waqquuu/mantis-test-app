"""Generate regression tests using Claude API.

Sends the changed code, diff, and blast radius context to Claude and gets
back pytest regression tests targeting the affected functions.
"""

from __future__ import annotations

import os
import textwrap

import anthropic

from mantis.blast_radius import BlastRadiusItem
from mantis.diff_parser import ChangedFunction

DEFAULT_MODEL = "claude-sonnet-4-20250514"


def generate_tests(
    changed_functions: list[ChangedFunction],
    blast_radius: list[BlastRadiusItem],
    raw_diff: str,
    repo_path: str,
    model: str = DEFAULT_MODEL,
) -> str:
    """Generate pytest regression tests for the blast radius functions.

    Returns the generated test code as a string ready to be written to a file.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY environment variable is not set. "
            "Set it to your Anthropic API key to use test generation."
        )

    prompt = _build_prompt(changed_functions, blast_radius, raw_diff, repo_path)
    client = anthropic.Anthropic(api_key=api_key)

    message = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = message.content[0].text
    return _extract_python_code(response_text)


def _build_prompt(
    changed_functions: list[ChangedFunction],
    blast_radius: list[BlastRadiusItem],
    raw_diff: str,
    repo_path: str,
) -> str:
    changed_section = _format_changed_functions(changed_functions)
    blast_section = _format_blast_radius(blast_radius)
    diff_section = _truncate(raw_diff, max_lines=150)

    return textwrap.dedent(f"""\
        You are a regression test engineer. Your job is to generate pytest tests
        that verify EXISTING behavior is preserved after a code change.

        IMPORTANT RULES:
        - You are NOT testing the changed code itself. The developer handles that.
        - You ARE testing the BLAST RADIUS: functions that DEPEND on the changed code.
        - Generate tests that would PASS before the change. If they FAIL after the
          change, that indicates a regression.
        - Use pytest style (functions, not classes unless necessary).
        - Use unittest.mock for mocking external dependencies (databases, APIs, files).
        - Each test should have a clear, descriptive name indicating what behavior
          it verifies.
        - Include appropriate imports at the top of the file.
        - Add a brief docstring to each test explaining what regression it guards against.
        - Do NOT use any fixtures or conftest features -- keep tests self-contained.
        - ONLY import modules and symbols that actually exist in the source code shown
          below. Do NOT guess or hallucinate import paths. If you are unsure whether a
          module path exists, import from the top-level package instead.
        - Always import symbols from their DEFINING module, not from a package's
          __init__.py. For example, use "from fastapi.routing import APIRoute"
          instead of "from fastapi import APIRoute".
        - The tests will be run from the repository root: {repo_path}

        CRITICAL — REGRESSION DETECTION STRATEGY:
        - Your tests verify that blast radius functions STILL WORK after the change.
          If the changed code introduces a new type conversion or constraint that
          breaks existing callers, that is a REGRESSION you must catch.
        - If the CALL SITE context shows that callers pass certain values (e.g. string
          status codes like "5XX", "4XX", "default"), your test must call the changed
          function with THOSE EXACT VALUES and assert it does NOT crash. Do NOT use
          pytest.raises for these — the point is to prove the function CANNOT handle
          inputs that real callers send, which means the change broke them.
        - Pay close attention to type annotations and parameter types. If a function
          accepts Union types (e.g. Union[int, str, None]), test ALL accepted types.
        - If a function does type conversion (int(), str(), float()), pass inputs
          that callers actually use but that would FAIL the conversion. The test
          should call the function normally (no try/except, no pytest.raises) so
          the crash surfaces as a test FAILURE.
        - For web frameworks: test with wildcard patterns, default values, status code
          ranges, and format strings that the spec allows but developers often forget.
        - Test boundary values, None, empty strings, zero, negative numbers.
        - Think adversarially: what inputs would a real user pass that the developer
          might not have considered?

        === GIT DIFF (what changed) ===
        {diff_section}

        === CHANGED FUNCTIONS ===
        {changed_section}

        === BLAST RADIUS (functions that depend on the changes) ===
        {blast_section}

        Generate a single Python file with all the regression tests.
        Output ONLY the Python code, wrapped in ```python ... ``` markers.
        No explanations before or after the code block.
    """)


def _format_changed_functions(changed_functions: list[ChangedFunction]) -> str:
    if not changed_functions:
        return "(no functions identified)"

    parts: list[str] = []
    for cf in changed_functions:
        header = f"# {cf.file_path} :: {cf.qualified_name} (lines {cf.start_line}-{cf.end_line})"
        parts.append(f"{header}\n{cf.source}")
    return "\n\n".join(parts)


def _format_blast_radius(blast_radius: list[BlastRadiusItem]) -> str:
    if not blast_radius:
        return "(no blast radius detected)"

    parts: list[str] = []
    for item in blast_radius:
        header = (
            f"# {item.file_path} :: {item.qualified_name}\n"
            f"# Reason: {item.reason} (depth={item.depth})"
        )
        section = f"{header}\n{item.source}"
        if item.call_site_context:
            section += (
                f"\n\n# CALL SITE — how this function calls the changed code:\n"
                f"{item.call_site_context}"
            )
        parts.append(section)
    return "\n\n".join(parts)


def generate_probe_script(
    changed_functions: list[ChangedFunction],
    blast_radius: list[BlastRadiusItem],
    raw_diff: str,
    repo_path: str,
    model: str = DEFAULT_MODEL,
) -> str:
    """Generate a probe script that exercises blast radius functions and
    captures their outputs as JSON, for differential testing."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY environment variable is not set."
        )

    prompt = _build_probe_prompt(changed_functions, blast_radius, raw_diff, repo_path)
    client = anthropic.Anthropic(api_key=api_key)

    message = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    return _extract_python_code(message.content[0].text)


def _build_probe_prompt(
    changed_functions: list[ChangedFunction],
    blast_radius: list[BlastRadiusItem],
    raw_diff: str,
    repo_path: str,
) -> str:
    changed_section = _format_changed_functions(changed_functions)
    blast_section = _format_blast_radius(blast_radius)
    diff_section = _truncate(raw_diff, max_lines=150)

    return textwrap.dedent(f"""\
        You are a differential testing engineer. Your job is to generate a Python
        probe script that exercises blast radius functions with realistic inputs
        and captures their outputs as structured JSON.

        The script will be run on TWO branches (before and after a code change)
        to detect behavioral differences — cases where the output changes
        silently without crashing.

        CRITICAL RULES:
        - The script must be COMPLETELY SELF-CONTAINED. No pytest, no unittest.
        - Import only from the project's own modules (shown below) and the
          standard library. Always import from the DEFINING submodule, not
          from a package's __init__.py.
        - For each blast radius function, construct realistic inputs based on
          the CALL SITE context and the function's signature.
        - If a function needs complex setup (class instantiation, mocks), do
          the minimal setup needed. Use simple stubs, not unittest.mock.
        - Wrap EVERY function call in the capture() helper shown below.
        - The script MUST print exactly ONE line of JSON to stdout.
        - Do NOT print anything else to stdout (use stderr for debug output).
        - Do NOT wrap code in try/except blocks. The runner handles isolation
          automatically. Each top-level statement runs independently — if one
          fails, the rest still execute. Just write flat top-level code.

        USE THIS EXACT STRUCTURE (the runner provides capture() and results):

        ```python
        from some_module import SomeFunction
        from pydantic import BaseModel

        class TestModel(BaseModel):
            detail: str

        def dummy():
            return {{}}

        # Each capture() call is isolated. If one fails, the rest still run.
        capture("SomeFunction:input_a", SomeFunction, arg1, arg2)
        capture("SomeFunction:input_b", SomeFunction, other_arg)
        capture("SomeFunction:edge_case", SomeFunction, edge_value)
        ```

        CRITICAL STRUCTURE RULES:
        - Do NOT write try/except blocks. The runner isolates each statement.
        - Do NOT write results = {{}} or print(json.dumps(results)).
        - Do NOT define capture(). It is provided by the runner.
        - Just write imports, helper definitions, and capture() calls.
        - Pass the function and its arguments directly to capture().
          capture("name", function, arg1, arg2, kwarg=value)
          NOT: result = function(arg1); capture("name", lambda: result)

        IMPORTANT FOR INPUT SELECTION:
        - Look at the CALL SITE context to see what values callers actually pass.
          If a function iterates over dict keys (e.g. `for status_code in
          route.responses:`), probe with dicts containing edge-case keys like
          string wildcards ("4XX", "5XX", "default"), None, empty strings, etc.
        - Test with the values that appear in the codebase, not just trivial ones.
        - For OpenAPI/web frameworks: always include wildcard patterns like
          "4XX", "5XX", "2XX", "default" as dict keys in responses.
        - Include edge cases: None, empty strings, boundary values.
        - For functions that need complex objects, create minimal stubs with
          only the attributes the function actually accesses.
        - CRITICAL: for functions that take a route/request/model with a
          "responses" dict, include probes where responses dict has string
          keys that are NOT pure integers. This catches type coercion bugs.
        - When probing with response dicts, ALWAYS include a "model" key
          pointing to a Pydantic BaseModel subclass. Many code paths are
          only triggered when a response model is present. Without it,
          body-checking logic is skipped entirely and bugs go undetected.
        - You MUST include these exact probes for any web framework with routes:
          1. APIRoute with responses={{"5XX": {{"model": SomeModel, "description": "err"}}}}
          2. APIRoute with responses={{"4XX": {{"model": SomeModel, "description": "err"}}}}
          3. APIRoute with responses={{"default": {{"model": SomeModel, "description": "err"}}}}
          These catch wildcard status code handling bugs. Define SomeModel as
          a simple Pydantic BaseModel with a "detail: str" field.

        === GIT DIFF (what changed) ===
        {diff_section}

        === CHANGED FUNCTIONS (the code that was modified) ===
        {changed_section}

        === BLAST RADIUS (functions to probe — they depend on the changes) ===
        {blast_section}

        Generate the probe script. Output ONLY Python code in ```python ... ```
        markers. No explanations.
    """)


def _extract_python_code(response: str) -> str:
    """Extract Python code from Claude's response, handling markdown fences."""
    import re

    pattern = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)
    match = pattern.search(response)
    if match:
        return match.group(1).strip()

    if response.strip().startswith("import ") or response.strip().startswith("from "):
        return response.strip()

    return response.strip()


def _truncate(text: str, max_lines: int) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} more lines truncated)"
