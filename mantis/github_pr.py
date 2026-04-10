"""GitHub PR integration -- clone PRs by URL and post results as comments."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass

import requests

from mantis.git_utils import run_git as _run_git


@dataclass
class PRInfo:
    owner: str
    repo: str
    number: int
    base_sha: str
    head_sha: str
    base_ref: str
    head_ref: str
    title: str
    clone_dir: str


def parse_pr_url(url: str) -> tuple[str, str, int]:
    """Extract (owner, repo, pr_number) from a GitHub PR URL."""
    match = re.match(
        r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)", url
    )
    if not match:
        raise ValueError(
            f"Invalid GitHub PR URL: {url}\n"
            "Expected format: https://github.com/owner/repo/pull/123"
        )
    return match.group(1), match.group(2), int(match.group(3))


def clone_pr_repo(
    owner: str,
    repo: str,
    pr_number: int,
    token: str | None = None,
) -> PRInfo:
    """Clone a GitHub repo and fetch PR branches for diffing.

    Returns a PRInfo with the temp directory path and branch info.
    The caller is responsible for cleaning up clone_dir.
    """
    pr_data = _fetch_pr_metadata(owner, repo, pr_number, token)

    base_sha = pr_data["base"]["sha"]
    head_sha = pr_data["head"]["sha"]
    base_ref = pr_data["base"]["ref"]
    head_ref = pr_data["head"]["ref"]
    title = pr_data.get("title", "")

    clone_url = f"https://github.com/{owner}/{repo}.git"
    if token:
        clone_url = f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"

    clone_dir = tempfile.mkdtemp(prefix="mantis_pr_")

    try:
        _run_git(["git", "clone", "--no-checkout", "--filter=blob:none",
                   clone_url, clone_dir])

        _run_git(["git", "-C", clone_dir, "fetch", "origin",
                   base_ref, f"pull/{pr_number}/head:pr_head"])

        _run_git(["git", "-C", clone_dir, "checkout", "pr_head"])

    except RuntimeError:
        shutil.rmtree(clone_dir, ignore_errors=True)
        raise

    return PRInfo(
        owner=owner,
        repo=repo,
        number=pr_number,
        base_sha=base_sha,
        head_sha=head_sha,
        base_ref=base_ref,
        head_ref=head_ref,
        title=title,
        clone_dir=clone_dir,
    )


def post_pr_comment(
    owner: str,
    repo: str,
    pr_number: int,
    token: str,
    body: str,
) -> str:
    """Post a comment on a GitHub PR. Returns the comment URL."""
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    resp = requests.post(url, json={"body": body}, headers=headers, timeout=30)

    if resp.status_code == 201:
        return resp.json().get("html_url", "")

    raise RuntimeError(
        f"Failed to post PR comment (HTTP {resp.status_code}): {resp.text}"
    )


def get_github_token() -> str | None:
    return os.environ.get("GITHUB_TOKEN")


def cleanup_clone(clone_dir: str) -> None:
    shutil.rmtree(clone_dir, ignore_errors=True)


def _fetch_pr_metadata(
    owner: str, repo: str, pr_number: int, token: str | None
) -> dict:
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = requests.get(url, headers=headers, timeout=30)

    if resp.status_code == 404:
        raise RuntimeError(
            f"PR not found: {owner}/{repo}#{pr_number}. "
            "If it's a private repo, make sure GITHUB_TOKEN is set."
        )
    if resp.status_code != 200:
        raise RuntimeError(
            f"GitHub API error (HTTP {resp.status_code}): {resp.text}"
        )

    return resp.json()


