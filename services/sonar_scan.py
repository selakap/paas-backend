import logging
import os
import shutil
import subprocess
import time

import requests

logger = logging.getLogger(__name__)

SONAR_HOST_URL = os.environ.get("SONAR_HOST_URL", "https://sonarcloud.io")
SONAR_TOKEN = os.environ.get("SONAR_TOKEN")
SONAR_ORGANIZATION = os.environ.get("SONAR_ORGANIZATION")


def is_configured() -> bool:
    return bool(SONAR_TOKEN and SONAR_ORGANIZATION)


def _resolve_scanner_executable() -> str:
    scanner_path = shutil.which("sonar-scanner")
    if not scanner_path:
        raise RuntimeError(
            "Could not find 'sonar-scanner' on PATH. Install the SonarScanner CLI "
            "(and a JRE 17+) and ensure it's on PATH."
        )
    return scanner_path


def run_scan(source_dir: str, project_key: str, project_name: str = None, timeout_seconds: int = 300) -> dict:
    """
    Runs `sonar-scanner` against source_dir, waits for SonarCloud's background
    analysis to finish, and returns the Quality Gate result.

    Requires SONAR_TOKEN and SONAR_ORGANIZATION env vars to be set, and the
    sonar-scanner CLI (+ a JRE) to be installed and on PATH.
    """
    logger.info(
        f"Sonar scan requested: project_key={project_key} project_name={project_name} "
        f"source_dir={source_dir} timeout_seconds={timeout_seconds}"
    )

    if not is_configured():
        raise RuntimeError("SONAR_TOKEN / SONAR_ORGANIZATION env vars are not set")

    scanner_exe = _resolve_scanner_executable()

    cmd = [
        scanner_exe,
        f"-Dsonar.projectKey={project_key}",
        f"-Dsonar.organization={SONAR_ORGANIZATION}",
        "-Dsonar.sources=.",
        f"-Dsonar.host.url={SONAR_HOST_URL}",
        f"-Dsonar.token={SONAR_TOKEN}",
    ]
    if project_name:
        cmd.append(f"-Dsonar.projectName={project_name}")

    logger.info(f"Running sonar-scanner: project_key={project_key} host={SONAR_HOST_URL}")
    result = subprocess.run(
        cmd, cwd=source_dir, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode != 0:
        logger.error(
            f"sonar-scanner failed: project_key={project_key} returncode={result.returncode} "
            f"stderr={(result.stderr or result.stdout or '').strip()}"
        )
        raise RuntimeError(f"sonar-scanner failed:\n{(result.stderr or result.stdout or '').strip()}")
    logger.info(f"sonar-scanner completed: project_key={project_key} returncode={result.returncode}")

    report_file = os.path.join(source_dir, ".scannerwork", "report-task.txt")
    if not os.path.exists(report_file):
        raise RuntimeError("sonar-scanner did not produce report-task.txt; cannot poll analysis status")

    report = {}
    with open(report_file) as f:
        for line in f:
            if "=" in line:
                k, v = line.strip().split("=", 1)
                report[k] = v

    ce_task_id = report.get("ceTaskId")
    dashboard_url = report.get("dashboardUrl")
    if not ce_task_id:
        raise RuntimeError("No ceTaskId found in report-task.txt")

    logger.info(f"Polling sonar background task: ce_task_id={ce_task_id}")

    # Poll the background analysis task until it finishes
    deadline = time.time() + timeout_seconds
    task_status = None
    analysis_id = None
    while time.time() < deadline:
        logger.info(f"GET {SONAR_HOST_URL}/api/ce/task params={{'id': '{ce_task_id}'}}")
        resp = requests.get(
            f"{SONAR_HOST_URL}/api/ce/task",
            params={"id": ce_task_id},
            auth=(SONAR_TOKEN, ""),
            timeout=15,
        )
        resp.raise_for_status()
        task = resp.json()["task"]
        task_status = task["status"]
        logger.info(f"Sonar task status response: ce_task_id={ce_task_id} status={task_status}")
        if task_status in ("SUCCESS", "FAILED", "CANCELED"):
            analysis_id = task.get("analysisId")
            break
        time.sleep(3)

    if task_status != "SUCCESS":
        logger.warning(f"Sonar background task did not succeed: ce_task_id={ce_task_id} status={task_status}")
        return {
            "task_status": task_status,
            "quality_gate_status": "UNKNOWN",
            "dashboard_url": dashboard_url,
        }

    logger.info(f"GET {SONAR_HOST_URL}/api/qualitygates/project_status params={{'analysisId': '{analysis_id}'}}")
    resp = requests.get(
        f"{SONAR_HOST_URL}/api/qualitygates/project_status",
        params={"analysisId": analysis_id},
        auth=(SONAR_TOKEN, ""),
        timeout=15,
    )
    resp.raise_for_status()
    qg = resp.json()["projectStatus"]
    logger.info(f"Quality gate response: analysis_id={analysis_id} status={qg['status']}")

    result = {
        "task_status": task_status,
        "quality_gate_status": qg["status"],  # "OK" or "ERROR"
        "conditions": qg.get("conditions", []),
        "dashboard_url": dashboard_url,
    }
    logger.info(f"Sonar scan result: project_key={project_key} {result}")
    return result