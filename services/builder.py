import base64
import os
import shutil
import stat
import subprocess
import tempfile
import uuid

import boto3

from services import sonar_scan

ecr_client = boto3.client("ecr")


def _clear_readonly_and_retry(func, path, exc_info):
    """
    shutil.rmtree error handler for Windows: git marks files under
    .git/objects as read-only, which plain shutil.rmtree can't delete
    (unlike `Remove-Item -Force`, which clears the attribute automatically).
    This clears the read-only bit and retries the failed operation.
    """
    os.chmod(path, stat.S_IWRITE)
    func(path)


def safe_rmtree(path: str) -> None:
    shutil.rmtree(path, onerror=_clear_readonly_and_retry)


def clone_repo(repo_url: str, branch: str = "main", commit: str = None) -> str:
    tmp_dir = tempfile.mkdtemp(prefix="paas-build-")

    if commit:
        # Need real history to check out an arbitrary commit, so no --depth 1.
        # depth 50 covers the same window the /repo/commits dropdown shows.
        cmd = ["git", "clone", "--branch", branch, "--single-branch", "--depth", "50", repo_url, tmp_dir]
    else:
        cmd = ["git", "clone", "--depth", "1", "--branch", branch, repo_url, tmp_dir]

    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        safe_rmtree(tmp_dir)
        raise RuntimeError(f"git clone failed: {(result.stderr or '').strip()}")

    if commit:
        checkout_cmd = ["git", "-C", tmp_dir, "checkout", commit]
        result = subprocess.run(checkout_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            safe_rmtree(tmp_dir)
            raise RuntimeError(
                f"git checkout of commit '{commit}' failed (it may be older than the "
                f"last 50 commits on '{branch}'): {(result.stderr or '').strip()}"
            )

    return tmp_dir


def ensure_ecr_repo(repo_name: str) -> dict:
    try:
        resp = ecr_client.describe_repositories(repositoryNames=[repo_name])
        return resp["repositories"][0]
    except ecr_client.exceptions.RepositoryNotFoundException:
        resp = ecr_client.create_repository(
            repositoryName=repo_name,
            imageScanningConfiguration={"scanOnPush": True},
        )
        return resp["repository"]


def docker_login(registry: str) -> None:
    auth = ecr_client.get_authorization_token()
    token = auth["authorizationData"][0]["authorizationToken"]
    username, password = base64.b64decode(token).decode().split(":")
    cmd = ["docker", "login", "--username", username, "--password-stdin", registry]
    result = subprocess.run(cmd, input=password, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"docker login failed: {result.stderr.strip()}")


def build_and_push(
        repo_url: str,
        branch: str,
        function_name: str,
        subdir: str = None,
        commit: str = None,
        run_sonar: bool = False,
        block_on_quality_gate_failure: bool = True,
) -> dict:
    """
    Clones the repo (optionally at a specific commit), optionally runs a
    SonarCloud scan, builds the Dockerfile found at repo (or repo/subdir)
    root, tags it, pushes to an ECR repo named after function_name, and
    returns the image URI / ARN info needed by the deploy endpoints.

    Assumes the Dockerfile already targets a Lambda base image
    (e.g. FROM public.ecr.aws/lambda/python:3.12) — this service does not
    inspect or rewrite the Dockerfile.
    """
    build_dir = clone_repo(repo_url, branch, commit)
    try:
        context_dir = os.path.join(build_dir, subdir) if subdir else build_dir
        dockerfile_path = os.path.join(context_dir, "Dockerfile")
        if not os.path.exists(dockerfile_path):
            raise RuntimeError(
                f"No Dockerfile found at '{dockerfile_path}'. "
                "The repo must contain a Lambda-compatible Dockerfile."
            )

        sonar_result = None
        if run_sonar:
            if not sonar_scan.is_configured():
                raise RuntimeError(
                    "Sonar scan was requested but SONAR_TOKEN / SONAR_ORGANIZATION "
                    "are not configured on the server."
                )
            sonar_result = sonar_scan.run_scan(
                source_dir=build_dir,
                project_key=function_name.lower(),
                project_name=function_name,
            )
            if block_on_quality_gate_failure and sonar_result["quality_gate_status"] == "ERROR":
                raise RuntimeError(
                    f"SonarCloud Quality Gate failed for '{function_name}'. "
                    f"Review findings at: {sonar_result.get('dashboard_url')}"
                )

        repo_name = function_name.lower()
        repository = ensure_ecr_repo(repo_name)
        registry = repository["repositoryUri"].split("/")[0]
        image_tag = uuid.uuid4().hex[:8]
        image_uri = f"{repository['repositoryUri']}:{image_tag}"

        docker_login(registry)

        build_cmd = ["docker", "build", "-t", image_uri, context_dir]
        result = subprocess.run(build_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"docker build failed:\n{result.stderr.strip()}")

        push_cmd = ["docker", "push", image_uri]
        result = subprocess.run(push_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"docker push failed:\n{result.stderr.strip()}")

        response = {
            "image_uri": image_uri,
            "image_tag": image_tag,
            "repository_arn": repository["repositoryArn"],
            "repository_uri": repository["repositoryUri"],
        }
        if sonar_result:
            response["sonar_scan"] = sonar_result
        return response
    finally:
        safe_rmtree(build_dir)