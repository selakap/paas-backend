import threading
from datetime import datetime, timezone

# In-memory store for the POC — avoids any filesystem I/O (and the Windows
# Defender / antivirus file-locking issues that came with it). Data is lost
# on every process restart; that's an accepted tradeoff for now.

_lock = threading.Lock()
_approvals: dict[int, dict] = {}
_next_id = 1


def create_request(
        repo_url: str,
        branch: str,
        commit_sha: str,
        function_name: str,
        requested_by: str = None,
        notes: str = None,
        sonar_scan_status: str = "not_requested",
) -> dict:
    global _next_id
    with _lock:
        record_id = _next_id
        _next_id += 1
        record = {
            "id": record_id,
            "repo_url": repo_url,
            "branch": branch,
            "commit_sha": commit_sha,
            "function_name": function_name,
            "requested_by": requested_by,
            "notes": notes,
            "status": "pending",
            "sonar_scan_status": sonar_scan_status,
            "sonar_quality_gate": None,
            "sonar_dashboard_url": None,
            "decided_by": None,
            "decision_notes": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "decided_at": None,
        }
        _approvals[record_id] = record
        return dict(record)


def update_sonar_result(
        request_id: int,
        sonar_scan_status: str,
        sonar_quality_gate: str = None,
        sonar_dashboard_url: str = None,
) -> dict:
    with _lock:
        record = _approvals.get(request_id)
        if not record:
            return None
        record["sonar_scan_status"] = sonar_scan_status
        record["sonar_quality_gate"] = sonar_quality_gate
        record["sonar_dashboard_url"] = sonar_dashboard_url
        return dict(record)


def list_requests(status: str = None) -> list:
    with _lock:
        records = list(_approvals.values())
    if status:
        records = [r for r in records if r["status"] == status]
    return sorted(records, key=lambda r: r["id"], reverse=True)


def get_request(request_id: int) -> dict:
    with _lock:
        record = _approvals.get(request_id)
        return dict(record) if record else None


def decide_request(request_id: int, status: str, decided_by: str = None, decision_notes: str = None) -> dict:
    if status not in ("approved", "rejected"):
        raise ValueError("status must be 'approved' or 'rejected'")
    with _lock:
        record = _approvals.get(request_id)
        if not record:
            return None
        record["status"] = status
        record["decided_by"] = decided_by
        record["decision_notes"] = decision_notes
        record["decided_at"] = datetime.now(timezone.utc).isoformat()
        return dict(record)


def find_approved(repo_url: str, commit_sha: str) -> dict:
    """
    Returns the most recent 'approved' record for this exact repo+commit,
    or None if there isn't one. This is the actual enforcement check used
    by /build.
    """
    with _lock:
        matches = [
            r for r in _approvals.values()
            if r["repo_url"] == repo_url and r["commit_sha"] == commit_sha and r["status"] == "approved"
        ]
    if not matches:
        return None
    return dict(max(matches, key=lambda r: r["id"]))