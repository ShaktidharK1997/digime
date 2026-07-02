"""TickTick tool — task management via TickTick Open API."""

import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import yaml

TICKTICK_BASE = "https://api.ticktick.com/open/v1"
TOKEN_PATH = Path(__file__).resolve().parent.parent / "config" / "ticktick_token.json"
PROFILE_PATH = Path(__file__).resolve().parent.parent / "config" / "profile.yaml"

# Cache project list to avoid repeated lookups
_project_cache: dict[str, str] = {}  # name -> id
_project_cache_ts: float = 0

GET_TASKS_SCHEMA = {
    "name": "get_tasks",
    "description": (
        "Read tasks from Shakti's TickTick. Returns tasks with title, due date, "
        "priority, and status. Filter by project/list and date range."
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
                "description": "Date range filter. Defaults to 'week'.",
            },
        },
        "required": [],
    },
}

CREATE_TASK_SCHEMA = {
    "name": "create_task",
    "description": (
        "Create a new task in Shakti's TickTick. ALWAYS confirm with Shakti "
        "before calling this — never create tasks silently."
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


def _load_token() -> dict:
    """Load the TickTick OAuth token from disk."""
    if not TOKEN_PATH.exists():
        raise FileNotFoundError(
            "TickTick token not found. Run the OAuth flow first. "
            "See README for setup instructions."
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

    # Check if token needs refresh
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

    resp = httpx.post(
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
    resp = httpx.get(f"{TICKTICK_BASE}/project", headers=headers)
    resp.raise_for_status()

    projects = resp.json()
    _project_cache = {p["name"]: p["id"] for p in projects}
    _project_cache_ts = time.time()
    return _project_cache


def _resolve_project_id(project_name: str | None) -> str | None:
    """Resolve a project name to its ID."""
    if not project_name:
        return None
    projects = _get_projects()
    # Case-insensitive lookup
    for name, pid in projects.items():
        if name.lower() == project_name.lower():
            return pid
    return None


def _normalize_due_date(due_date: str) -> str:
    """Normalize various date formats to TickTick's required format.

    Args:
        due_date: Date string in formats like:
            - "YYYY-MM-DD" (date only)
            - "YYYY-MM-DDTHH:MM:SS" (datetime)
            - "YYYY-MM-DDTHH:MM" (datetime without seconds)
            - "YYYY-MM-DDTHH:MM:SS.000+0000" (already in TickTick format)

    Returns:
        Date string in TickTick's required format: "YYYY-MM-DDTHH:MM:SS.000+0000"
        For date-only inputs, defaults to 09:00:00 in user's timezone (America/New_York).
    """
    if not due_date:
        return due_date

    # If already in TickTick format, return as-is
    if re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{4}", due_date):
        return due_date

    # Load user's timezone from profile
    user_tz = ZoneInfo("America/New_York")  # default
    try:
        with open(PROFILE_PATH) as f:
            profile = yaml.safe_load(f)
            tz_name = profile.get("location", {}).get("timezone", "America/New_York")
            user_tz = ZoneInfo(tz_name)
    except Exception:
        pass  # Use default if profile can't be loaded

    # Parse the input date string
    dt = None

    # Try date-only format: YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", due_date):
        # Parse as date, set time to 09:00:00 in user's timezone
        date_part = datetime.strptime(due_date, "%Y-%m-%d")
        dt = datetime(
            date_part.year,
            date_part.month,
            date_part.day,
            9, 0, 0,  # 9:00 AM
            tzinfo=user_tz
        )

    # Try datetime formats: YYYY-MM-DDTHH:MM:SS or YYYY-MM-DDTHH:MM
    elif "T" in due_date:
        # Remove any existing timezone info for parsing
        base_date = due_date.split("+")[0].split("-", 3)[-1] if "+" in due_date else due_date.split("Z")[0]
        base_date = due_date.split("+")[0].split("Z")[0]

        # Try with seconds
        try:
            dt_naive = datetime.strptime(base_date, "%Y-%m-%dT%H:%M:%S")
            dt = dt_naive.replace(tzinfo=user_tz)
        except ValueError:
            # Try without seconds
            try:
                dt_naive = datetime.strptime(base_date, "%Y-%m-%dT%H:%M")
                dt = dt_naive.replace(tzinfo=user_tz)
            except ValueError:
                pass  # Fall through to return original

    # If we couldn't parse it, return the original string
    if dt is None:
        return due_date

    # Convert to UTC and format for TickTick
    dt_utc = dt.astimezone(ZoneInfo("UTC"))
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def _filter_by_date(tasks: list[dict], date_range: str) -> list[dict]:
    """Filter tasks by date range."""
    now = datetime.utcnow()

    if date_range == "all":
        return tasks

    filtered = []
    for task in tasks:
        due = task.get("dueDate")
        if not due:
            if date_range == "overdue":
                continue
            # Include tasks without due dates for today/week views
            filtered.append(task)
            continue

        # Parse ISO date string
        try:
            due_dt = datetime.fromisoformat(due.replace("Z", "+00:00")).replace(tzinfo=None)
        except (ValueError, TypeError):
            filtered.append(task)
            continue

        if date_range == "today":
            if due_dt.date() <= now.date():
                filtered.append(task)
        elif date_range == "week":
            week_end = now + timedelta(days=7)
            if due_dt.date() <= week_end.date():
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
            resp = httpx.get(
                f"{TICKTICK_BASE}/project/{project_id}/data",
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            tasks = data.get("tasks", [])
        else:
            # Fetch all projects and aggregate tasks
            projects = _get_projects()
            tasks = []
            for pid in projects.values():
                resp = httpx.get(
                    f"{TICKTICK_BASE}/project/{pid}/data",
                    headers=headers,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    tasks.extend(data.get("tasks", []))

        # Filter by date range
        tasks = _filter_by_date(tasks, date_range)

        # Map project IDs back to names
        projects = _get_projects()
        id_to_name = {v: k for k, v in projects.items()}

        result = []
        for t in tasks:
            result.append(
                {
                    "id": t.get("id", ""),
                    "title": t.get("title", ""),
                    "content": t.get("content", ""),
                    "dueDate": t.get("dueDate", ""),
                    "priority": t.get("priority", 0),
                    "status": t.get("status", 0),
                    "project_name": id_to_name.get(t.get("projectId", ""), "Unknown"),
                }
            )

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
        project_id = _resolve_project_id(project)
        if not project_id:
            # Fall back to Inbox
            project_id = _resolve_project_id("Inbox")

        task_data = {
            "title": title,
            "priority": priority,
        }
        if project_id:
            task_data["projectId"] = project_id
        if due_date:
            task_data["dueDate"] = _normalize_due_date(due_date)

        resp = httpx.post(
            f"{TICKTICK_BASE}/task",
            headers=headers,
            json=task_data,
        )
        resp.raise_for_status()
        created = resp.json()

        return json.dumps(
            {
                "success": True,
                "task": {
                    "id": created.get("id", ""),
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
