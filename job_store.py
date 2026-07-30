"""Persistent Seedance job store (survives refresh / logout / reconnect)."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

JOBS_FILE = os.path.join(os.path.dirname(__file__), "seedance_jobs.json")
TERMINAL = {"succeeded", "failed", "canceled", "cancelled"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_all() -> List[Dict[str, Any]]:
    if not os.path.isfile(JOBS_FILE):
        return []
    try:
        with open(JOBS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        return [j for j in data if isinstance(j, dict)]
    if isinstance(data, dict) and isinstance(data.get("jobs"), list):
        return [j for j in data["jobs"] if isinstance(j, dict)]
    return []


def _write_all(jobs: List[Dict[str, Any]]) -> None:
    dir_name = os.path.dirname(JOBS_FILE) or "."
    fd, tmp = tempfile.mkstemp(prefix="seedance_jobs_", suffix=".tmp", dir=dir_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(jobs, f, indent=2)
        os.replace(tmp, JOBS_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load(username: str | None = None, *, limit: int = 50) -> List[Dict[str, Any]]:
    jobs = _read_all()
    if username:
        jobs = [j for j in jobs if j.get("username") == username]
    jobs.sort(key=lambda j: j.get("updated_at") or j.get("created_at") or "", reverse=True)
    return jobs[:limit]


def upsert(job: Dict[str, Any]) -> Dict[str, Any]:
    task_id = str(job.get("task_id") or "").strip()
    if not task_id:
        raise ValueError("job.task_id required")

    jobs = _read_all()
    merged = dict(job)
    merged["task_id"] = task_id
    merged["updated_at"] = _now()
    found = False
    for i, existing in enumerate(jobs):
        if existing.get("task_id") == task_id:
            created = existing.get("created_at") or merged.get("created_at") or _now()
            jobs[i] = {**existing, **merged, "created_at": created}
            merged = jobs[i]
            found = True
            break
    if not found:
        merged.setdefault("created_at", _now())
        jobs.insert(0, merged)
    _write_all(jobs)
    return merged


def update(
    task_id: str,
    *,
    status: Optional[str] = None,
    video_url: Optional[str] = None,
    usage: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    jobs = _read_all()
    for i, job in enumerate(jobs):
        if job.get("task_id") != task_id:
            continue
        if status is not None:
            job["status"] = status
        if video_url is not None:
            job["video_url"] = video_url
        if usage is not None:
            job["usage"] = usage
        if error is not None:
            job["error"] = error
        job["updated_at"] = _now()
        jobs[i] = job
        _write_all(jobs)
        return job
    return None


def running(username: str | None = None) -> List[Dict[str, Any]]:
    return [
        j
        for j in load(username, limit=200)
        if str(j.get("status") or "").lower() not in TERMINAL
    ]
