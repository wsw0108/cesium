"""
Git/GitHub tools for the CesiumJS PR Review MCP server.

get_pr_diff: Fetches the open PR for the current branch (requires an open PR),
             returns PR metadata + full diff.

Authentication (in order of preference):
  1. GH_TOKEN or GITHUB_TOKEN environment variable (GitHub REST API)
  2. gh CLI (if installed and authenticated)
"""

import json
import os
import re
import subprocess
import urllib.request
import urllib.error


def _run(cmd: list[str], check: bool = True, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=check,
        cwd=cwd,
    )


def _get_repo_root() -> str:
    result = _run(["git", "rev-parse", "--show-toplevel"])
    return result.stdout.strip()


def _parse_github_remote(remote_url: str) -> tuple[str, str]:
    """Extract owner/repo from a GitHub remote URL (HTTPS or SSH)."""
    # HTTPS: https://github.com/owner/repo.git
    # SSH:   git@github.com:owner/repo.git
    match = re.search(r"github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?$", remote_url)
    if not match:
        raise RuntimeError(f"Could not parse GitHub owner/repo from remote URL: {remote_url}")
    return match.group(1), match.group(2)


def _github_api(path: str, token: str, accept: str = "application/vnd.github+json") -> bytes:
    """Make a GitHub REST API request and return raw bytes."""
    url = f"https://api.github.com{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", accept)
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API error {e.code} for {url}: {body}") from e


def _get_token() -> str | None:
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")


def _get_pr_via_api(owner: str, repo: str, branch: str, token: str) -> dict:
    """Fetch PR info and diff using the GitHub REST API."""
    # Find open PR for this branch
    prs_raw = _github_api(f"/repos/{owner}/{repo}/pulls?head={owner}:{branch}&state=open", token)
    prs = json.loads(prs_raw)
    if not prs:
        raise RuntimeError(
            f"No open PR found for branch '{branch}' in {owner}/{repo}. "
            "Please open a PR on GitHub before running the review agent."
        )
    pr = prs[0]

    # Get full PR diff
    diff_raw = _github_api(
        f"/repos/{owner}/{repo}/pulls/{pr['number']}",
        token,
        accept="application/vnd.github.diff",
    )
    diff = diff_raw.decode("utf-8", errors="replace")

    # Get changed files
    files_raw = _github_api(f"/repos/{owner}/{repo}/pulls/{pr['number']}/files", token)
    files = json.loads(files_raw)

    return pr, diff, files


def _get_pr_via_gh_cli(branch: str) -> tuple[dict, str, list]:
    """Fetch PR info and diff using the gh CLI (fallback)."""
    pr_result = _run(
        ["gh", "pr", "view", "--json",
         "number,title,body,url,headRefName,baseRefName,author,additions,deletions,changedFiles,files"],
        check=False,
    )
    if pr_result.returncode != 0:
        raise RuntimeError(
            f"No open PR found for branch '{branch}'. "
            f"Details: {pr_result.stderr.strip()}"
        )
    pr = json.loads(pr_result.stdout)
    diff_result = _run(["gh", "pr", "diff"], check=False)
    if diff_result.returncode != 0:
        raise RuntimeError(f"Failed to fetch PR diff: {diff_result.stderr.strip()}")
    files = pr.get("files", [])
    return pr, diff_result.stdout, files


def get_pr_diff() -> dict:
    """
    Returns the open PR diff and metadata for the current branch.
    Raises RuntimeError if there is no open PR.
    """
    repo_root = _get_repo_root()

    # Get current branch
    branch_result = _run(["git", "branch", "--show-current"], cwd=repo_root)
    branch = branch_result.stdout.strip()
    if not branch:
        raise RuntimeError("Not on a git branch (detached HEAD?). Check out a feature branch first.")

    # Try GitHub REST API first (no external tools needed)
    token = _get_token()
    if token:
        remote_result = _run(["git", "remote", "get-url", "origin"], cwd=repo_root, check=False)
        if remote_result.returncode == 0:
            owner, repo = _parse_github_remote(remote_result.stdout.strip())
            pr, diff, files = _get_pr_via_api(owner, repo, branch, token)

            changed_files = [{"path": f["filename"], "status": f.get("status", "")} for f in files]
            pr_meta = {
                "number": pr["number"],
                "url": pr["html_url"],
                "title": pr["title"],
                "description": pr.get("body", "") or "",
                "author": pr.get("user", {}).get("login", "unknown"),
                "base_branch": pr.get("base", {}).get("ref", "main"),
                "additions": pr.get("additions", 0),
                "deletions": pr.get("deletions", 0),
                "changed_files_count": pr.get("changed_files", 0),
            }
        else:
            raise RuntimeError("Could not determine git remote URL.")
    else:
        # Fall back to gh CLI
        try:
            _run(["gh", "--version"])
        except FileNotFoundError:
            raise RuntimeError(
                "No GitHub token found (GH_TOKEN / GITHUB_TOKEN) and gh CLI is not installed.\n"
                "Set GH_TOKEN in your environment or install gh CLI from https://cli.github.com/"
            )
        pr_raw, diff, files_raw = _get_pr_via_gh_cli(branch)
        changed_files = [{"path": f.get("path", f.get("filename", "")), "status": ""} for f in files_raw]
        pr_meta = {
            "number": pr_raw["number"],
            "url": pr_raw["url"],
            "title": pr_raw["title"],
            "description": pr_raw.get("body", "") or "",
            "author": pr_raw.get("author", {}).get("login", "unknown"),
            "base_branch": pr_raw.get("baseRefName", "main"),
            "additions": pr_raw.get("additions", 0),
            "deletions": pr_raw.get("deletions", 0),
            "changed_files_count": pr_raw.get("changedFiles", 0),
        }

    # Classify files by type so the orchestrator can decide which agents to activate
    glsl_files = [f["path"] for f in changed_files if f["path"].endswith(".glsl")]
    js_files = [f["path"] for f in changed_files if f["path"].endswith(".js") and "Spec" not in f["path"]]
    spec_files = [f["path"] for f in changed_files if "Spec" in f["path"] or f["path"].endswith(".test.js")]
    changes_md_updated = any("CHANGES.md" in f["path"] for f in changed_files)

    return {
        "branch": branch,
        **pr_meta,
        "file_classification": {
            "glsl_files": glsl_files,
            "js_files": js_files,
            "spec_files": spec_files,
            "changes_md_updated": changes_md_updated,
            "all_files": [f["path"] for f in changed_files],
        },
        "diff": diff,
    }
