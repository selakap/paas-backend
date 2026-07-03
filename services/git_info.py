import re
import shutil
import subprocess
import tempfile


def get_branch_head_sha(repo_url: str, branch: str) -> str:
    """
    Resolves a branch to its current HEAD commit SHA via `git ls-remote`,
    without cloning. Used when a request doesn't specify an exact commit —
    the approval gate always checks against a concrete SHA, never a
    moving branch pointer.
    """
    cmd = ["git", "ls-remote", "--heads", repo_url, branch]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"git ls-remote failed: {(result.stderr or '').strip()}")

    line = result.stdout.strip()
    if not line:
        raise RuntimeError(f"Branch '{branch}' not found in {repo_url}")

    return line.split()[0]


def list_branches(repo_url: str) -> list[str]:
    """
    Uses `git ls-remote --heads` to list branch names without cloning.
    Works for any public git remote (GitHub, Bitbucket, etc.).
    """
    cmd = ["git", "ls-remote", "--heads", repo_url]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"git ls-remote failed: {(result.stderr or '').strip()}")

    branches = []
    for line in result.stdout.splitlines():
        # each line: "<sha>\trefs/heads/<branch-name>"
        match = re.search(r"refs/heads/(.+)$", line.strip())
        if match:
            branches.append(match.group(1))
    return branches


def list_commits(repo_url: str, branch: str, limit: int = 20) -> list[dict]:
    """
    Shallow-clones the given branch (depth capped at `limit`+buffer) and
    reads recent commits via `git log`. Cleans up the temp clone afterward.
    """
    tmp_dir = tempfile.mkdtemp(prefix="paas-commits-")
    try:
        clone_cmd = [
            "git", "clone",
            "--branch", branch,
            "--single-branch",
            "--depth", str(limit),
            repo_url, tmp_dir,
        ]
        result = subprocess.run(clone_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            raise RuntimeError(f"git clone failed: {(result.stderr or '').strip()}")

        log_cmd = [
            "git", "-C", tmp_dir, "log",
            f"-{limit}",
            "--pretty=format:%H%x1f%h%x1f%s%x1f%ci",
        ]
        result = subprocess.run(log_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            raise RuntimeError(f"git log failed: {(result.stderr or '').strip()}")

        commits = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            sha, short_sha, message, date = line.split("\x1f")
            commits.append({
                "sha": sha,
                "short_sha": short_sha,
                "message": message,
                "date": date,
            })
        return commits
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)