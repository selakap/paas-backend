import json
import logging
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from services.builder import build_and_push
from services.cdk_deploy import run_cdk_deploy
from services.git_info import list_branches, list_commits

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="PaaS POC Backend", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- Request models ----------

class BuildRequest(BaseModel):
    repo_url: str = Field(..., description="Git URL, e.g. https://github.com/org/repo.git")
    branch: str = "main"
    commit: Optional[str] = Field(None, description="Full commit SHA to build; defaults to branch HEAD if omitted")
    function_name: str = Field(..., description="Used as ECR repo name + lambda name base")
    subdir: Optional[str] = Field(None, description="Subfolder in repo containing the Dockerfile, if not root")


class CronDeployRequest(BaseModel):
    function_name: str
    image_uri: str = Field(..., description="Image URI returned by /build")
    schedule_expression: str = Field(
        ..., description='EventBridge schedule, e.g. "rate(5 minutes)" or "cron(0 12 * * ? *)"'
    )
    memory_size: int = 512
    timeout_seconds: int = 60
    environment: Optional[Dict[str, str]] = Field(
        default=None, description="Environment variables to set on the Lambda function"
    )


class ApiDeployRequest(BaseModel):
    function_name: str
    image_uri: str = Field(..., description="Image URI returned by /build")
    memory_size: int = 512
    timeout_seconds: int = 30
    environment: Optional[Dict[str, str]] = Field(
        default=None, description="Environment variables to set on the Lambda function"
    )


# ---------- Endpoints ----------

@app.post("/build")
def build(req: BuildRequest):
    """
    1. Clone the repo, 2. docker build (expects a Lambda-compatible Dockerfile
    in the repo/subdir already), 3. push to ECR, 4. return the image URI/ARN.
    """
    logger.info(f"Build requested: repo={req.repo_url} branch={req.branch} commit={req.commit} function_name={req.function_name}")
    try:
        result = build_and_push(req.repo_url, req.branch, req.function_name, req.subdir, req.commit)
        logger.info(f"Build completed successfully: {result}")
        return {"status": "success", **result}
    except Exception as e:
        logger.error(f"Build failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/repo/branches")
def get_branches(repo_url: str):
    """
    Lists branch names for a public git repo (GitHub, Bitbucket, etc.)
    without cloning, via `git ls-remote`.
    """
    try:
        return {"branches": list_branches(repo_url)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/repo/commits")
def get_commits(repo_url: str, branch: str = "main"):
    """
    Lists the most recent commits on a branch (shallow-clones internally).
    """
    try:
        return {"commits": list_commits(repo_url, branch)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/deploy/cron")
def deploy_cron(req: CronDeployRequest):
    """
    Creates a container-image Lambda + EventBridge schedule rule via CDK.
    """
    logger.info(f"Cron deploy requested: {req}")
    stack_name = f"{req.function_name}-cron-stack"
    context = {
        "deploy_type": "cron",
        "function_name": req.function_name,
        "image_uri": req.image_uri,
        "schedule_expression": req.schedule_expression,
        "memory_size": req.memory_size,
        "timeout_seconds": req.timeout_seconds,
        "environment": json.dumps(req.environment or {}),
    }
    try:
        outputs = run_cdk_deploy(context, stack_name)
        logger.info(f"Cron deploy completed successfully: {outputs}")
        return {"status": "success", "stack_name": stack_name, "outputs": outputs}
    except Exception as e:
        logger.error(f"Error occurred while deploying cron job: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/deploy/api")
def deploy_api(req: ApiDeployRequest):
    """
    Creates a container-image Lambda + HTTP API (API Gateway v2) via CDK.
    """
    logger.info(f"API deploy requested: {req}")
    stack_name = f"{req.function_name}-api-stack"
    context = {
        "deploy_type": "api",
        "function_name": req.function_name,
        "image_uri": req.image_uri,
        "memory_size": req.memory_size,
        "timeout_seconds": req.timeout_seconds,
        "environment": json.dumps(req.environment or {}),
    }
    try:
        outputs = run_cdk_deploy(context, stack_name)
        logger.info(f"API deploy completed successfully: {outputs}")
        return {"status": "success", "stack_name": stack_name, "outputs": outputs}
    except Exception as e:
        logger.error(f"Error occurred while deploying API: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
def health():
    return {"status": "ok"}