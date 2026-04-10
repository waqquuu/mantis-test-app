"""Format and display Mantis results using Rich."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree
from rich.columns import Columns
from rich.rule import Rule

from mantis.blast_radius import BlastRadiusItem
from mantis.diff_parser import DiffResult
from mantis.runner import RunResult

console = Console()


def print_banner() -> None:
    console.print()
    console.print(
        Panel(
            "[bold white]MANTIS[/]  [dim]—  Regression Detection Engine[/]",
            border_style="bright_blue",
            padding=(0, 2),
        )
    )


def print_analysis_header(diff_result: DiffResult) -> None:
    n_files = len(diff_result.changed_files)
    n_funcs = len(diff_result.changed_functions)

    console.print()
    console.print(
        f"[bold blue]📂 Changed functions:[/]  "
        f"[white]{n_files} file{'s' if n_files != 1 else ''}[/], "
        f"[white]{n_funcs} function{'s' if n_funcs != 1 else ''}[/] modified"
    )

    if diff_result.changed_functions:
        for cf in diff_result.changed_functions:
            console.print(
                f"   [dim]└──[/] [dim]{cf.file_path}[/] → [bold cyan]{cf.qualified_name}[/]"
            )


def print_blast_radius(blast_radius: list[BlastRadiusItem]) -> None:
    if not blast_radius:
        console.print("\n[yellow]💥 Blast radius: 0 connected functions found[/]")
        console.print("[dim]   No other functions depend on the changed code.[/]")
        return

    console.print()
    console.print(
        f"[bold yellow]💥 Blast Radius:[/]  "
        f"[bold white]{len(blast_radius)}[/] connected "
        f"function{'s' if len(blast_radius) != 1 else ''} found"
    )
    console.print()

    tree = Tree("[bold yellow]Affected functions[/]", guide_style="dim")

    by_file: dict[str, list[BlastRadiusItem]] = {}
    for item in blast_radius:
        by_file.setdefault(item.file_path, []).append(item)

    for file_path, items in by_file.items():
        file_branch = tree.add(f"[bold]{file_path}[/]")
        for item in items:
            depth_badge = (
                f"[green]depth {item.depth}[/]"
                if item.depth == 1
                else f"[yellow]depth {item.depth}[/]"
            )
            func_node = file_branch.add(
                f"[cyan]{item.qualified_name}[/]  {depth_badge}"
            )
            func_node.add(f"[dim]{item.reason}[/]")

    console.print(tree)


def print_generating() -> None:
    console.print()
    console.print("[bold magenta]🧪 Generating regression tests...[/]")


def print_running() -> None:
    console.print("[bold magenta]🏃 Running tests...[/]")
    console.print()


def print_results(run_result: RunResult) -> None:
    if not run_result.tests:
        console.print("[yellow]No tests were generated or collected.[/]")
        return

    import re

    passed = [t for t in run_result.tests if t.status == "passed"]
    failed = [t for t in run_result.tests if t.status == "failed"]
    errors = [t for t in run_result.tests if t.status == "error"]

    if passed:
        console.print(f"  [green]✅ {len(passed)} passed[/]")
        for t in passed:
            console.print(f"     [dim green]{t.name}[/]")
        console.print()

    if failed:
        error_groups: dict[str, list] = {}
        file_groups: dict[str, list] = {}

        for t in failed:
            root_cause = _extract_root_cause(t.message)
            error_groups.setdefault(root_cause, []).append(t)

            affected_file = _extract_affected_file(t.message)
            file_groups.setdefault(affected_file, []).append(t)

        console.print(f"  [bold red]❌ {len(failed)} failed[/]\n")

        console.print("  [bold]Root cause:[/]")
        for cause, tests in error_groups.items():
            console.print(f"     [red]{cause}[/]  [dim]({len(tests)} tests)[/]")
        console.print()

        console.print("  [bold]Broken subsystems:[/]")
        for file_path, tests in file_groups.items():
            test_names = [t.name for t in tests]
            short_names = _summarize_test_names(test_names)
            console.print(f"     [cyan]{file_path}[/]  [dim]— {short_names}[/]")
        console.print()

    if errors:
        console.print(f"  [yellow]⚠️  {len(errors)} errors[/]")
        for t in errors:
            if t.message:
                first_line = t.message.strip().splitlines()[0].strip()
                console.print(f"     [dim yellow]{first_line}[/]")
        console.print()


def _extract_root_cause(message: str) -> str:
    """Pull the actual error type + message from a test failure."""
    if not message:
        return "Unknown error"

    import re
    for line in reversed(message.splitlines()):
        line = line.strip()
        err_match = re.match(r"^E\s+(\w+(?:Error|Exception|Warning):\s*.+)", line)
        if err_match:
            return err_match.group(1)
        if line.startswith("E   "):
            cleaned = line[4:].strip()
            if cleaned:
                return cleaned

    for line in message.splitlines():
        line = line.strip()
        if "Error:" in line or "Exception:" in line:
            return line
    return "Test assertion failed"


def _extract_affected_file(message: str) -> str:
    """Extract the source file where the failure actually occurred."""
    if not message:
        return "unknown"

    import re
    matches = re.findall(r"(\S+\.py):\d+: in ", message)
    for m in reversed(matches):
        if "mantis_regression_test" not in m:
            return m
    return matches[0] if matches else "unknown"


def _summarize_test_names(names: list[str]) -> str:
    """Create a short summary of what the tests cover."""
    keywords = set()
    for name in names:
        parts = name.replace("test_", "").split("_preserves_")[0].split("_")
        key = " ".join(parts[:4])
        keywords.add(key)

    items = sorted(keywords)
    if len(items) <= 3:
        return ", ".join(items)
    return f"{', '.join(items[:3])}, +{len(items) - 3} more"


def print_summary(run_result: RunResult) -> None:
    console.print()

    parts = []
    if run_result.passed:
        parts.append(f"[green]{run_result.passed} passed[/]")
    if run_result.failed:
        parts.append(f"[red]{run_result.failed} failed[/]")
    if run_result.errors:
        parts.append(f"[yellow]{run_result.errors} errors[/]")

    summary_line = "  ·  ".join(parts) if parts else "no tests"

    if run_result.success and run_result.total > 0:
        panel = Panel(
            f"[bold green]✅  All regression tests passed.[/]\n\n"
            f"   {summary_line}\n\n"
            f"[green]Safe to merge.[/]",
            title="[bold green] PASS [/]",
            border_style="green",
            padding=(1, 3),
        )
    elif run_result.failed > 0:
        panel = Panel(
            f"[bold red]❌  {run_result.failed} regression{'s' if run_result.failed != 1 else ''} detected.[/]\n\n"
            f"   {summary_line}\n\n"
            f"[bold red]Not safe to merge.[/]  [dim]Review failures above.[/]",
            title="[bold red] FAIL [/]",
            border_style="red",
            padding=(1, 3),
        )
    elif run_result.errors > 0:
        panel = Panel(
            f"[bold yellow]⚠️  {run_result.errors} test{'s' if run_result.errors != 1 else ''} could not run.[/]\n\n"
            f"   {summary_line}\n\n"
            f"[yellow]Generated tests had errors. Results inconclusive.[/]",
            title="[bold yellow] INCONCLUSIVE [/]",
            border_style="yellow",
            padding=(1, 3),
        )
    else:
        panel = Panel(
            "[dim]No regression tests were generated.[/]",
            title="[dim] NO TESTS [/]",
            border_style="dim",
            padding=(1, 3),
        )

    console.print(panel)


def print_differential_results(diff_result) -> None:
    """Display results from differential testing."""
    from mantis.differential import DifferentialResult

    if not diff_result.diffs:
        console.print("[green]✅ No behavioral differences detected.[/]")
        console.print("[dim]   All blast radius functions produce identical outputs on both branches.[/]")
        return

    behavioral = diff_result.behavioral_changes
    crashes = diff_result.new_crashes

    if crashes:
        console.print(f"  [bold red]💥 {len(crashes)} new crash{'es' if len(crashes) != 1 else ''}[/]\n")
        for item in crashes:
            console.print(f"     [red]{item.probe_name}[/]")
            console.print(f"       [dim]base:[/] [green]returned {_shorten(item.base_result.value, 60)}[/]")
            console.print(f"       [dim]head:[/] [red]{item.head_result.error_type}: {item.head_result.error_msg}[/]")
            console.print()

    if behavioral:
        console.print(f"  [bold yellow]🔄 {len(behavioral)} behavioral change{'s' if len(behavioral) != 1 else ''}[/]\n")
        for item in behavioral:
            console.print(f"     [yellow]{item.probe_name}[/]")
            console.print(f"       [dim]base:[/] [green]{_shorten(item.base_result.value, 60)}[/]")
            console.print(f"       [dim]head:[/] [red]{_shorten(item.head_result.value, 60)}[/]")
            console.print()

    fixes = [d for d in diff_result.diffs if d.category == "new_fix"]
    if fixes:
        console.print(f"  [bold green]🔧 {len(fixes)} fix{'es' if len(fixes) != 1 else ''} (previously broken, now works)[/]")
        for item in fixes:
            console.print(f"     [dim]{item.probe_name}[/]")
        console.print()


def print_differential_summary(diff_result) -> None:
    """Print the final verdict for differential testing."""
    regressions = diff_result.regressions
    behavioral = diff_result.behavioral_changes
    crashes = diff_result.new_crashes

    console.print()

    if not diff_result.diffs:
        panel = Panel(
            "[bold green]✅  No behavioral regressions.[/]\n\n"
            "   All blast radius functions produce identical outputs.\n\n"
            "[green]Safe to merge.[/]",
            title="[bold green] PASS [/]",
            border_style="green",
            padding=(1, 3),
        )
    elif regressions:
        parts = []
        if crashes:
            parts.append(f"[red]{len(crashes)} new crash{'es' if len(crashes) != 1 else ''}[/]")
        if behavioral:
            parts.append(f"[yellow]{len(behavioral)} behavioral change{'s' if len(behavioral) != 1 else ''}[/]")
        summary_line = "  ·  ".join(parts)

        panel = Panel(
            f"[bold red]❌  {len(regressions)} regression{'s' if len(regressions) != 1 else ''} detected.[/]\n\n"
            f"   {summary_line}\n\n"
            f"[bold red]Not safe to merge.[/]  [dim]Review differences above.[/]",
            title="[bold red] FAIL [/]",
            border_style="red",
            padding=(1, 3),
        )
    else:
        panel = Panel(
            "[bold green]✅  Only improvements detected.[/]\n\n"
            "   Previously broken functions now work.\n\n"
            "[green]Safe to merge.[/]",
            title="[bold green] PASS [/]",
            border_style="green",
            padding=(1, 3),
        )

    console.print(panel)


def _shorten(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


def print_no_changes() -> None:
    console.print("\n[dim]No changes detected. Nothing to analyze.[/]")


def print_no_blast_radius() -> None:
    console.print(
        "\n[green]✅ No blast radius detected.[/] "
        "The changes don't affect any other functions in the codebase."
    )


def print_error(message: str) -> None:
    console.print(f"\n[bold red]Error:[/] {message}")


def format_github_comment(
    diff_result: DiffResult,
    blast_radius: list[BlastRadiusItem],
    run_result: RunResult | None,
    pr_title: str = "",
) -> str:
    """Format results as a GitHub-flavored markdown PR comment."""
    lines: list[str] = []
    lines.append("## 🦗 Mantis Regression Report")
    lines.append("")

    if pr_title:
        lines.append(f"> **{pr_title}**")
        lines.append("")

    n_files = len(diff_result.changed_files)
    n_funcs = len(diff_result.changed_functions)
    n_blast = len(blast_radius)
    lines.append(
        f"**{n_files} file{'s' if n_files != 1 else ''} changed, "
        f"{n_funcs} function{'s' if n_funcs != 1 else ''} modified** · "
        f"**{n_blast} blast radius function{'s' if n_blast != 1 else ''}**"
    )
    lines.append("")

    lines.append("### Changed Functions")
    for cf in diff_result.changed_functions:
        lines.append(f"- `{cf.file_path}` → `{cf.qualified_name}`")
    lines.append("")

    if blast_radius:
        lines.append("### Blast Radius")
        lines.append("| Function | File | Reason | Depth |")
        lines.append("|----------|------|--------|:-----:|")
        for item in blast_radius:
            lines.append(
                f"| `{item.qualified_name}` | `{item.file_path}` "
                f"| {item.reason} | {item.depth} |"
            )
        lines.append("")

    if run_result and run_result.tests:
        lines.append("### Test Results")
        for test in run_result.tests:
            if test.status == "passed":
                lines.append(f"- ✅ **PASSED** `{test.name}`")
            elif test.status == "failed":
                msg = ""
                if test.message:
                    first_line = test.message.strip().splitlines()[0]
                    msg = f" — {first_line}"
                lines.append(f"- ❌ **FAILED** `{test.name}`{msg}")
            elif test.status == "error":
                msg = ""
                if test.message:
                    first_line = test.message.strip().splitlines()[0]
                    msg = f" — {first_line}"
                lines.append(f"- ⚠️ **ERROR** `{test.name}`{msg}")
        lines.append("")

    if run_result and run_result.tests:
        if run_result.success:
            lines.append(
                f"### ✅ Verdict: All {run_result.passed} regression tests passed. "
                "Safe to merge."
            )
        elif run_result.failed > 0:
            lines.append(
                f"### ❌ Verdict: {run_result.failed} "
                f"regression{'s' if run_result.failed != 1 else ''} detected. "
                "Not safe to merge."
            )
        elif run_result.errors > 0:
            lines.append(
                f"### ⚠️ Verdict: {run_result.errors} "
                f"test{'s' if run_result.errors != 1 else ''} errored. "
                "Results inconclusive."
            )
    elif not blast_radius:
        lines.append(
            "### ✅ Verdict: No blast radius. Changes don't affect other functions."
        )
    else:
        lines.append("### Verdict: Dry run — tests were not executed.")

    lines.append("")
    lines.append("---")
    lines.append("*Generated by [Mantis](https://github.com) — regression detection for pull requests*")

    return "\n".join(lines)
