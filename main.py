import json
import logging
from typing import Dict, Literal, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from services import approvals_store, sonar_scan
from services.builder import build_and_push, clone_repo, safe_rmtree
from services.cdk_deploy import run_cdk_deploy
from services.git_info import get_branch_head_sha, list_branches, list_commits

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
    run_sonar: bool = Field(default=False, description="Run a SonarCloud scan on the source before building")
    block_on_quality_gate_failure: bool = Field(
        default=True, description="If run_sonar is true, fail the build when the Quality Gate fails"
    )


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


class ApprovalRequestCreate(BaseModel):
    repo_url: str
    branch: str = "main"
    commit: Optional[str] = Field(None, description="Exact commit SHA; defaults to branch HEAD if omitted")
    function_name: str
    requested_by: Optional[str] = None
    notes: Optional[str] = None


class ApprovalDecision(BaseModel):
    status: Literal["approved", "rejected"]
    decided_by: Optional[str] = None
    notes: Optional[str] = None


# ---------- Endpoints ----------

@app.post("/build")
def build(req: BuildRequest):
    """
    1. Verify this exact commit has an 'approved' record, 2. clone the repo,
    3. docker build, 4. push to ECR, 5. return the image URI/ARN.
    """
    commit_sha = req.commit or get_branch_head_sha(req.repo_url, req.branch)

    approved = approvals_store.find_approved(req.repo_url, commit_sha)
    if not approved:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Commit {commit_sha[:8]} on {req.repo_url} has not been approved for deployment. "
                "Submit an approval request via /approvals and wait for admin sign-off."
            ),
        )

    logger.info(f"Build requested: repo={req.repo_url} branch={req.branch} commit={commit_sha} function_name={req.function_name} run_sonar={req.run_sonar}")
    try:
        result = build_and_push(
            req.repo_url, req.branch, req.function_name, req.subdir, commit_sha,
            run_sonar=req.run_sonar,
            block_on_quality_gate_failure=req.block_on_quality_gate_failure,
        )
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


def _run_sonar_scan_background(request_id: int, repo_url: str, branch: str, commit_sha: str, function_name: str):
    """
    Runs in a background thread after the /approvals response has already
    been sent. Clones the commit, runs the Sonar scan, and writes the
    result back onto the approval record when done.
    """
    logger.info(f"[bg] Starting sonar scan for approval #{request_id}")
    clone_dir = None
    try:
        clone_dir = clone_repo(repo_url, branch, commit_sha)
        logger.info(f"[bg] Cloned to {clone_dir}; running sonar-scanner...")
        result = sonar_scan.run_scan(
            source_dir=clone_dir,
            project_key=function_name.lower(),
            project_name=function_name,
        )
        approvals_store.update_sonar_result(
            request_id,
            sonar_scan_status="completed",
            sonar_quality_gate=result["quality_gate_status"],
            sonar_dashboard_url=result.get("dashboard_url"),
        )
        logger.info(f"[bg] Sonar scan for approval #{request_id} completed: {result['quality_gate_status']}")
    except Exception as e:
        logger.warning(f"[bg] Sonar scan for approval #{request_id} failed: {e}")
        approvals_store.update_sonar_result(request_id, sonar_scan_status="failed")
    finally:
        if clone_dir:
            safe_rmtree(clone_dir)
            logger.info(f"[bg] Cleaned up {clone_dir}")


@app.post("/approvals")
def create_approval_request(req: ApprovalRequestCreate, background_tasks: BackgroundTasks):
    """
    Records a request to deploy a specific commit and returns immediately.
    If SonarCloud is configured, the scan is kicked off in the background —
    poll GET /approvals/{id} (or the admin list) to see sonar_scan_status
    move from 'running' to 'completed'/'failed'.
    """
    logger.info(f"Approval request received: repo={req.repo_url} branch={req.branch} commit={req.commit} function_name={req.function_name}")

    commit_sha = req.commit or get_branch_head_sha(req.repo_url, req.branch)
    logger.info(f"Resolved commit_sha={commit_sha}")

    will_scan = sonar_scan.is_configured()

    record = approvals_store.create_request(
        repo_url=req.repo_url,
        branch=req.branch,
        commit_sha=commit_sha,
        function_name=req.function_name,
        requested_by=req.requested_by,
        notes=req.notes,
        sonar_scan_status="running" if will_scan else "not_requested",
    )
    logger.info(f"Approval request created: {record}")

    if will_scan:
        background_tasks.add_task(
            _run_sonar_scan_background,
            record["id"], req.repo_url, req.branch, commit_sha, req.function_name,
        )

    return record


@app.get("/approvals")
def get_approval_requests(status: Optional[str] = None):
    """
    Lists approval requests, optionally filtered by status
    ('pending' | 'approved' | 'rejected').
    """
    return {"approvals": approvals_store.list_requests(status)}


@app.get("/approvals/{request_id}")
def get_approval_request(request_id: int):
    record = approvals_store.get_request(request_id)
    if not record:
        raise HTTPException(status_code=404, detail="Approval request not found")
    return record


@app.post("/approvals/{request_id}/decision")
def decide_approval_request(request_id: int, decision: ApprovalDecision):
    if not approvals_store.get_request(request_id):
        raise HTTPException(status_code=404, detail="Approval request not found")
    record = approvals_store.decide_request(
        request_id, decision.status, decision.decided_by, decision.notes
    )
    logger.info(f"Approval request {request_id} decided: {decision.status}")
    return record


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