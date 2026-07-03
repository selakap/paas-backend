import base64
import os
import shutil
import subprocess
import tempfile
import uuid

import boto3

ecr_client = boto3.client("ecr")


def clone_repo(repo_url: str, branch: str = "main") -> str:
    tmp_dir = tempfile.mkdtemp(prefix="paas-build-")
    cmd = ["git", "clone", "--depth", "1", "--branch", branch, repo_url, tmp_dir]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError(f"git clone failed: {result.stderr.strip()}")
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


def build_and_push(repo_url: str, branch: str, function_name: str, subdir: str = None) -> dict:
    """
    Clones the repo, builds the Dockerfile found at repo (or repo/subdir) root,
    tags it, pushes to an ECR repo named after function_name, and returns the
    image URI / ARN info needed by the deploy endpoints.

    Assumes the Dockerfile already targets a Lambda base image
    (e.g. FROM public.ecr.aws/lambda/python:3.12) — this service does not
    inspect or rewrite the Dockerfile.
    """
    build_dir = clone_repo(repo_url, branch)
    try:
        context_dir = os.path.join(build_dir, subdir) if subdir else build_dir
        dockerfile_path = os.path.join(context_dir, "Dockerfile")
        if not os.path.exists(dockerfile_path):
            raise RuntimeError(
                f"No Dockerfile found at '{dockerfile_path}'. "
                "The repo must contain a Lambda-compatible Dockerfile."
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

        return {
            "image_uri": image_uri,
            "image_tag": image_tag,
            "repository_arn": repository["repositoryArn"],
            "repository_uri": repository["repositoryUri"],
        }
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)
