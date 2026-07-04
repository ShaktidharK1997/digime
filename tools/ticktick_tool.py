"""TickTick tool — task management via TickTick Open API.

Read: get_tasks. Write: create_task, complete_task, update_task, delete_task.
Write tools are intercepted by the orchestrator and only execute after the
user presses Confirm in Slack — the functions themselves stay side-effectful
and simple.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import yaml

logger = logging.getLogger(__name__)

TICKTICK_BASE = "https://api.ticktick.com/open/v1"
TOKEN_PATH = Path(__file__).resolve().parent.parent / "config" / "ticktick_token.json"
PROFILE_PATH = Path(__file__).resolve().parent.parent / "config" / "profile.yaml"

DEFAULT_TIMEZONE = "America/New_York"
HTTP_TIMEOUT = 15.0

# Shared HTTP client (thread-safe) so we reuse connections across tool calls
_http = httpx.Client(timeout=HTTP_TIMEOUT)

# Cache project list to avoid repeated lookups
_project_cache: dict[str, str] = {}  # name -> id
_project_cache_ts: float = 0

GET_TASKS_SCHEMA = {
    "name": "get_tasks",
    "description": (
        "Read tasks from Shakti's TickTick. Returns tasks with id, project_id, "
        "title, due date, priority, and status. Filter by project/list and date "
        "range. The id and project_id are needed for complete/update/delete."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "project": {
                "type": "string",
                "description": "Project/list name to filter by (e.g. 'Inbox', 'Work'). Optional.",
            },
            "date_range": {
                "type": "string",
                "enum": ["today", "week", "overdue", "all"],
                "description": (
                    "Date range filter. 'today' = due today or overdue; "
                    "'week' = due within 7 days plus undated tasks; "
                    "'overdue' = past due only. Defaults to 'week'."
                ),
            },
        },
        "required": [],
    },
}

CREATE_TASK_SCHEMA = {
    "name": "create_task",
    "description": (
        "Create a new task in Shakti's TickTick. Calling this shows Shakti a "
        "Confirm/Cancel button in Slack — the task is only created after he "
        "confirms, so call it as soon as the intent is clear."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Task title."},
            "due_date": {
                "type": "string",
                "description": "Due date and optional time in ISO format (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS). Include time when the user specifies one. Optional.",
            },
            "project": {
                "type": "string",
                "description": "Project/list name (default 'Inbox').",
            },
            "priority": {
                "type": "integer",
                "description": "Priority 0 (none) to 5 (highest). Default 0.",
            },
        },
        "required": ["title"],
    },
}

COMPLETE_TASK_SCHEMA = {
    "name": "complete_task",
    "description": (
        "Mark a TickTick task as completed. Get task_id and project_id from "
        "get_tasks first. Shows a Confirm/Cancel button in Slack before "
        "executing."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The task's id from get_tasks."},
            "project_id": {"type": "string", "description": "The task's project_id from get_tasks."},
            "title": {"type": "string", "description": "The task title, shown in the confirmation message."},
        },
        "required": ["task_id", "project_id", "title"],
    },
}

UPDATE_TASK_SCHEMA = {
    "name": "update_task",
    "description": (
        "Update an existing TickTick task (title, due date, or priority). Get "
        "task_id and project_id from get_tasks first. Shows a Confirm/Cancel "
        "button in Slack before executing."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The task's id from get_tasks."},
            "project_id": {"type": "string", "description": "The task's project_id from get_tasks."},
            "current_title": {"type": "string", "description": "The task's current title, shown in the confirmation message."},
            "title": {"type": "string", "description": "New title. Omit to keep unchanged."},
            "due_date": {
                "type": "string",
                "description": "New due date in ISO format (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS). Omit to keep unchanged.",
            },
            "priority": {
                "type": "integer",
                "description": "New priority 0 (none) to 5 (highest). Omit to keep unchanged.",
            },
        },
        "required": ["task_id", "project_id", "current_title"],
    },
}

DELETE_TASK_SCHEMA = {
    "name": "delete_task",
    "description": (
        "Delete a TickTick task permanently. Get task_id and project_id from "
        "get_tasks first. Shows a Confirm/Cancel button in Slack before "
        "executing."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The task's id from get_tasks."},
            "project_id": {"type": "string", "description": "The task's project_id from get_tasks."},
            "title": {"type": "string", "description": "The task title, shown in the confirmation message."},
        },
        "required": ["task_id", "project_id", "title"],
    },
}


def _load_token() -> dict:
    """Load the TickTick OAuth token from disk."""
    if not TOKEN_PATH.exists():
        raise FileNotFoundError(
            "TickTick token not found. Run: python ticktick_oauth_flow.py "
            "(see README for setup instructions)."
        )
    with open(TOKEN_PATH) as f:
        return json.load(f)


def _save_token(token_data: dict) -> None:
    """Persist token data to disk."""
    with open(TOKEN_PATH, "w") as f:
        json.dump(token_data, f, indent=2)


def _get_headers() -> dict:
    """Get authorization headers, refreshing token if needed."""
    token_data = _load_token()

    expires_at = token_data.get("expires_at", 0)
    if time.time() > expires_at - 300:  # refresh 5 min before expiry
        token_data = _refresh_token(token_data)

    return {
        "Authorization": f"Bearer {token_data['access_token']}",
        "Content-Type": "application/json",
    }


def _refresh_token(token_data: dict) -> dict:
    """Refresh the TickTick OAuth access token."""
    client_id = os.getenv("TICKTICK_CLIENT_ID")
    client_secret = os.getenv("TICKTICK_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise ValueError("TICKTICK_CLIENT_ID and TICKTICK_CLIENT_SECRET must be set")

    resp = _http.post(
        "https://ticktick.com/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": token_data["refresh_token"],
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    resp.raise_for_status()
    new_token = resp.json()

    # Preserve refresh token if not returned
    if "refresh_token" not in new_token:
        new_token["refresh_token"] = token_data["refresh_token"]
    new_token["expires_at"] = time.time() + new_token.get("expires_in", 3600)

    _save_token(new_token)
    return new_token


def _get_projects() -> dict[str, str]:
    """Fetch and cache project name -> ID mapping."""
    global _project_cache, _project_cache_ts

    # Cache for 5 minutes
    if _project_cache and (time.time() - _project_cache_ts) < 300:
        return _project_cache

    headers = _get_headers()
    resp = _http.get(f"{TICKTICK_BASE}/project", headers=headers)
    resp.raise_for_status()

    projects = resp.json()
    _project_cache = {p["name"]: p["id"] for p in projects}
    _project_cache_ts = time.time()
    return _project_cache


def _resolve_project_id(project_name: str | None) -> str | None:
    """Resolve a project name to its ID (case-insensitive)."""
    if not project_name:
        return None
    projects = _get_projects()
    for name, pid in projects.items():
        if name.lower() == project_name.lower():
            return pid
    return None


def _user_timezone() -> ZoneInfo:
    """Load the user's timezone from the profile, with a sane default."""
    try:
        with open(PROFILE_PATH) as f:
            profile = yaml.safe_load(f)
        tz_name = (profile or {}).get("location", {}).get("timezone", DEFAULT_TIMEZONE)
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo(DEFAULT_TIMEZONE)


def _normalize_due_date(due_date: str, tz: ZoneInfo | None = None) -> str:
    """Normalize an ISO date string to TickTick's required format.

    Accepts date-only (YYYY-MM-DD), naive datetimes, and datetimes with any
    UTC offset or Z suffix. Date-only inputs get 09:00 in the user's timezone;
    naive datetimes are interpreted in the user's timezone.

    Returns "YYYY-MM-DDTHH:MM:SS.000+0000" (UTC), or the input unchanged if
    it can't be parsed (TickTick will reject it and the error surfaces to the
    model).
    """
    if not due_date:
        return due_date

    # Already in TickTick format
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{4}$", due_date):
        return due_date

    tz = tz or _user_timezone()
    date_only = re.match(r"^\d{4}-\d{2}-\d{2}$", due_date) is not None

    try:
        dt = datetime.fromisoformat(due_date)
    except ValueError:
        return due_date

    if date_only:
        dt = dt.replace(hour=9)  # default to 9:00 AM local
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)

    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def _filter_by_date(tasks: list[dict], date_range: str, now: datetime | None = None) -> list[dict]:
    """Filter tasks by date range. `now` must be timezone-aware if given."""
    now = now or datetime.now(timezone.utc)

    if date_range == "all":
        return tasks

    filtered = []
    for task in tasks:
        due = task.get("dueDate")
        if not due:
            # Undated tasks show up in the broad 'week' view but not in the
            # focused 'today'/'overdue' views.
            if date_range == "week":
                filtered.append(task)
            continue

        try:
            due_dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
            if due_dt.tzinfo is None:
                due_dt = due_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            filtered.append(task)
            continue

        if date_range == "today":
            if due_dt.date() <= now.date():
                filtered.append(task)
        elif date_range == "week":
            if due_dt.date() <= (now + timedelta(days=7)).date():
                filtered.append(task)
        elif date_range == "overdue":
            if due_dt < now and task.get("status", 0) != 2:  # 2 = completed
                filtered.append(task)

    return filtered


def get_tasks(project: str = "", date_range: str = "week") -> str:
    """Fetch tasks from TickTick, optionally filtered by project and date."""
    try:
        headers = _get_headers()
    except (FileNotFoundError, ValueError) as e:
        return json.dumps({"error": str(e)})

    try:
        project_id = _resolve_project_id(project) if project else None

        if project_id:
            resp = _http.get(
                f"{TICKTICK_BASE}/project/{project_id}/data",
                headers=headers,
            )
            resp.raise_for_status()
            tasks = resp.json().get("tasks", [])
        else:
            # Fetch all projects and aggregate tasks
            projects = _get_projects()
            tasks = []
            for pid in projects.values():
                resp = _http.get(
                    f"{TICKTICK_BASE}/project/{pid}/data",
                    headers=headers,
                )
                if resp.status_code == 200:
                    tasks.extend(resp.json().get("tasks", []))

        tasks = _filter_by_date(tasks, date_range)

        projects = _get_projects()
        id_to_name = {v: k for k, v in projects.items()}

        result = [
            {
                "id": t.get("id", ""),
                "project_id": t.get("projectId", ""),
                "title": t.get("title", ""),
                "content": t.get("content", ""),
                "dueDate": t.get("dueDate", ""),
                "priority": t.get("priority", 0),
                "status": t.get("status", 0),
                "project_name": id_to_name.get(t.get("projectId", ""), "Unknown"),
            }
            for t in tasks
        ]

        return json.dumps({"tasks": result, "count": len(result)})

    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"TickTick API error: {e.response.status_code} {e.response.text}"})
    except Exception as e:
        return json.dumps({"error": f"TickTick error: {e}"})


def create_task(
    title: str,
    due_date: str = "",
    project: str = "Inbox",
    priority: int = 0,
) -> str:
    """Create a new task in TickTick."""
    try:
        headers = _get_headers()
    except (FileNotFoundError, ValueError) as e:
        return json.dumps({"error": str(e)})

    try:
        project_id = _resolve_project_id(project) or _resolve_project_id("Inbox")

        task_data = {"title": title, "priority": priority}
        if project_id:
            task_data["projectId"] = project_id
        if due_date:
            task_data["dueDate"] = _normalize_due_date(due_date)

        resp = _http.post(f"{TICKTICK_BASE}/task", headers=headers, json=task_data)
        resp.raise_for_status()
        created = resp.json()

        return json.dumps(
            {
                "success": True,
                "task": {
                    "id": created.get("id", ""),
                    "project_id": created.get("projectId", ""),
                    "title": created.get("title", ""),
                    "dueDate": created.get("dueDate", ""),
                    "priority": created.get("priority", 0),
                    "project": project,
                },
            }
        )

    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"TickTick API error: {e.response.status_code} {e.response.text}"})
    except Exception as e:
        return json.dumps({"error": f"TickTick create failed: {e}"})


def complete_task(task_id: str, project_id: str, title: str = "") -> str:
    """Mark a task as completed."""
    try:
        headers = _get_headers()
    except (FileNotFoundError, ValueError) as e:
        return json.dumps({"error": str(e)})

    try:
        resp = _http.post(
            f"{TICKTICK_BASE}/project/{project_id}/task/{task_id}/complete",
            headers=headers,
        )
        resp.raise_for_status()
        return json.dumps({"success": True, "completed": title or task_id})

    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"TickTick API error: {e.response.status_code} {e.response.text}"})
    except Exception as e:
        return json.dumps({"error": f"TickTick complete failed: {e}"})


def update_task(
    task_id: str,
    project_id: str,
    current_title: str = "",
    title: str = "",
    due_date: str = "",
    priority: int | None = None,
) -> str:
    """Update fields on an existing task. current_title is display-only."""
    try:
        headers = _get_headers()
    except (FileNotFoundError, ValueError) as e:
        return json.dumps({"error": str(e)})

    task_data: dict = {"id": task_id, "projectId": project_id}
    if title:
        task_data["title"] = title
    if due_date:
        task_data["dueDate"] = _normalize_due_date(due_date)
    if priority is not None:
        task_data["priority"] = priority

    if len(task_data) == 2:
        return json.dumps({"error": "No fields to update — pass title, due_date, or priority."})

    try:
        resp = _http.post(f"{TICKTICK_BASE}/task/{task_id}", headers=headers, json=task_data)
        resp.raise_for_status()
        updated = resp.json()
        return json.dumps(
            {
                "success": True,
                "task": {
                    "id": updated.get("id", task_id),
                    "title": updated.get("title", title),
                    "dueDate": updated.get("dueDate", ""),
                    "priority": updated.get("priority", priority),
                },
            }
        )

    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"TickTick API error: {e.response.status_code} {e.response.text}"})
    except Exception as e:
        return json.dumps({"error": f"TickTick update failed: {e}"})


def delete_task(task_id: str, project_id: str, title: str = "") -> str:
    """Delete a task permanently."""
    try:
        headers = _get_headers()
    except (FileNotFoundError, ValueError) as e:
        return json.dumps({"error": str(e)})

    try:
        resp = _http.delete(
            f"{TICKTICK_BASE}/project/{project_id}/task/{task_id}",
            headers=headers,
        )
        resp.raise_for_status()
        return json.dumps({"success": True, "deleted": title or task_id})

    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"TickTick API error: {e.response.status_code} {e.response.text}"})
    except Exception as e:
        return json.dumps({"error": f"TickTick delete failed: {e}"})
