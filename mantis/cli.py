"""Mantis CLI -- catch regressions in pull requests."""

from __future__ import annotations

import sys
from pathlib import Path

import click
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from rich.console import Console

from mantis import __version__
from mantis.blast_radius import find_blast_radius
from mantis.diff_parser import parse_diff
from mantis.github_pr import (
    cleanup_clone,
    clone_pr_repo,
    get_github_token,
    parse_pr_url,
    post_pr_comment,
)
from mantis.reporter import (
    format_github_comment,
    print_analysis_header,
    print_banner,
    print_blast_radius,
    print_error,
    print_generating,
    print_no_blast_radius,
    print_no_changes,
    print_results,
    print_running,
    print_summary,
)
from mantis.differential import run_differential
from mantis.runner import run_tests
from mantis.test_generator import generate_probe_script, generate_tests

console = Console()


@click.group()
@click.version_option(version=__version__, prog_name="mantis")
def main():
    """Mantis -- catch regressions before they ship."""


@main.command()
@click.option("--repo", type=click.Path(exists=True), default=None, help="Path to the git repository.")
@click.option("--base", default=None, help="Base branch or commit (e.g. 'main').")
@click.option("--head", default=None, help="Head branch or commit (e.g. 'feature-branch').")
@click.option("--pr", "pr_url", default=None, help="GitHub PR URL (e.g. https://github.com/owner/repo/pull/123).")
@click.option("--comment", is_flag=True, help="Post results as a PR comment (requires --pr and GITHUB_TOKEN).")
@click.option("--depth", default=2, type=int, help="How many levels deep to trace the blast radius.")
@click.option("--timeout", default=120, type=int, help="Test execution timeout in seconds.")
@click.option("--dry-run", is_flag=True, help="Show blast radius without generating or running tests.")
@click.option("--show-code", is_flag=True, help="Print the generated test code before running.")
@click.option("--differential", is_flag=True, help="Use differential testing: run code on both branches and compare outputs.")
def check(
    repo: str | None,
    base: str | None,
    head: str | None,
    pr_url: str | None,
    comment: bool,
    depth: int,
    timeout: int,
    dry_run: bool,
    show_code: bool,
    differential: bool,
):
    """Analyze a diff and run regression tests on the blast radius."""
    print_banner()

    if not repo and not pr_url:
        print_error("Provide either --repo or --pr.")
        sys.exit(1)

    if pr_url and repo:
        print_error("Use --pr or --repo, not both.")
        sys.exit(1)

    if comment and not pr_url:
        print_error("--comment requires --pr.")
        sys.exit(1)

    # --- Resolve repo source ---
    clone_dir: str | None = None
    pr_title = ""
    pr_owner = pr_repo_name = ""
    pr_number = 0

    if pr_url:
        token = get_github_token()
        if comment and not token:
            print_error("--comment requires GITHUB_TOKEN in .env or environment.")
            sys.exit(1)

        try:
            pr_owner, pr_repo_name, pr_number = parse_pr_url(pr_url)
            console.print(f"\n[bold blue]Fetching PR:[/] {pr_owner}/{pr_repo_name}#{pr_number}")

            pr_info = clone_pr_repo(pr_owner, pr_repo_name, pr_number, token)
            repo = pr_info.clone_dir
            clone_dir = pr_info.clone_dir
            base = f"origin/{pr_info.base_ref}"
            head = "pr_head"
            pr_title = pr_info.title
            console.print(f"[dim]   {pr_title}[/]")
            console.print(f"[dim]   base={pr_info.base_ref}  head={pr_info.head_ref}[/]")
        except (ValueError, RuntimeError) as e:
            print_error(str(e))
            sys.exit(1)

    try:
        _run_pipeline(
            repo=repo,
            base=base,
            head=head,
            depth=depth,
            timeout=timeout,
            dry_run=dry_run,
            show_code=show_code,
            comment=comment,
            pr_url=pr_url,
            pr_owner=pr_owner,
            pr_repo_name=pr_repo_name,
            pr_number=pr_number,
            pr_title=pr_title,
            differential=differential,
        )
    finally:
        if clone_dir:
            cleanup_clone(clone_dir)


def _run_pipeline(
    *,
    repo: str,
    base: str | None,
    head: str | None,
    depth: int,
    timeout: int,
    dry_run: bool,
    show_code: bool,
    comment: bool,
    pr_url: str | None,
    pr_owner: str,
    pr_repo_name: str,
    pr_number: int,
    pr_title: str,
    differential: bool = False,
):
    # Step 1: Parse the diff
    try:
        diff_result = parse_diff(repo, base=base, head=head)
    except RuntimeError as e:
        print_error(str(e))
        sys.exit(1)

    if not diff_result.changed_functions and not diff_result.changed_files:
        print_no_changes()
        sys.exit(0)

    print_analysis_header(diff_result)

    if not diff_result.changed_functions:
        console.print("\n[dim]Changed files detected but no Python function changes found.[/]")
        sys.exit(0)

    # Step 2: Compute blast radius
    blast_radius = find_blast_radius(
        diff_result.changed_functions, repo, max_depth=depth
    )
    print_blast_radius(blast_radius)

    if not blast_radius:
        print_no_blast_radius()
        if comment:
            _post_comment(
                pr_owner, pr_repo_name, pr_number,
                format_github_comment(diff_result, blast_radius, None, pr_title),
            )
        sys.exit(0)

    if dry_run:
        console.print("\n[dim]Dry run -- skipping test generation and execution.[/]")
        if comment:
            _post_comment(
                pr_owner, pr_repo_name, pr_number,
                format_github_comment(diff_result, blast_radius, None, pr_title),
            )
        sys.exit(0)

    if differential:
        _run_differential_pipeline(
            diff_result=diff_result,
            blast_radius=blast_radius,
            repo=repo,
            base=base,
            head=head,
            timeout=timeout,
            show_code=show_code,
        )
        return

    # Step 3: Generate tests
    print_generating()
    from rich.status import Status
    with Status("[magenta]Calling Claude to generate targeted tests...[/]", console=console, spinner="dots"):
        try:
            test_code = generate_tests(
                changed_functions=diff_result.changed_functions,
                blast_radius=blast_radius,
                raw_diff=diff_result.raw_diff,
                repo_path=repo,
            )
        except RuntimeError as e:
            print_error(str(e))
            sys.exit(1)
        except Exception as e:
            print_error(f"Test generation failed: {e}")
            sys.exit(1)
    console.print("[green]   ✓[/] [dim]Tests generated[/]")

    if show_code:
        console.print("\n[bold]Generated test code:[/]")
        console.print(f"[dim]{'─' * 60}[/]")
        console.print(test_code)
        console.print(f"[dim]{'─' * 60}[/]\n")

    # Step 4: Run tests
    print_running()
    with Status("[magenta]Executing regression tests...[/]", console=console, spinner="dots"):
        run_result = run_tests(test_code, repo, timeout=timeout)
    console.print("[green]   ✓[/] [dim]Tests complete[/]\n")

    # Step 5: Report
    print_results(run_result)
    print_summary(run_result)

    # Step 6: Post GitHub comment
    if comment:
        _post_comment(
            pr_owner, pr_repo_name, pr_number,
            format_github_comment(diff_result, blast_radius, run_result, pr_title),
        )

    if not run_result.success:
        sys.exit(1)


def _run_differential_pipeline(
    *,
    diff_result,
    blast_radius,
    repo: str,
    base: str | None,
    head: str | None,
    timeout: int,
    show_code: bool,
):
    from rich.status import Status
    from mantis.reporter import print_differential_results, print_differential_summary

    console.print()
    console.print("[bold magenta]🔬 Differential testing mode[/]")
    console.print("[dim]   Generating probe script to exercise blast radius functions...[/]")

    with Status("[magenta]Calling Claude to generate probe script...[/]", console=console, spinner="dots"):
        try:
            probe_code = generate_probe_script(
                changed_functions=diff_result.changed_functions,
                blast_radius=blast_radius,
                raw_diff=diff_result.raw_diff,
                repo_path=repo,
            )
        except Exception as e:
            from mantis.reporter import print_error
            print_error(f"Probe generation failed: {e}")
            sys.exit(1)
    console.print("[green]   ✓[/] [dim]Probe script generated[/]")

    if show_code:
        from mantis.differential import wrap_probe_code
        console.print("\n[bold]Probe script (wrapped):[/]")
        console.print(f"[dim]{'─' * 60}[/]")
        console.print(wrap_probe_code(probe_code))
        console.print(f"[dim]{'─' * 60}[/]\n")

    if not base or not head:
        from mantis.reporter import print_error
        print_error("Differential mode requires --base and --head.")
        sys.exit(1)

    console.print(f"\n[bold blue]⏪ Running probe on base:[/] [dim]{base}[/]")
    console.print(f"[bold blue]⏩ Running probe on head:[/] [dim]{head}[/]")

    with Status("[magenta]Running probes on both branches...[/]", console=console, spinner="dots"):
        diff_result_obj = run_differential(
            probe_code=probe_code,
            repo_path=repo,
            base_ref=base,
            head_ref=head,
            timeout=timeout,
        )
    console.print("[green]   ✓[/] [dim]Probes complete[/]\n")

    print_differential_results(diff_result_obj)
    print_differential_summary(diff_result_obj)

    if diff_result_obj.regressions:
        sys.exit(1)


def _post_comment(owner: str, repo: str, pr_number: int, body: str):
    token = get_github_token()
    if not token:
        print_error("Cannot post comment: GITHUB_TOKEN not set.")
        return
    try:
        comment_url = post_pr_comment(owner, repo, pr_number, token, body)
        console.print(f"\n[bold green]💬 Comment posted:[/] {comment_url}")
    except RuntimeError as e:
        print_error(f"Failed to post comment: {e}")


if __name__ == "__main__":
    main()
