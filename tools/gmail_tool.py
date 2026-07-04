"""Gmail tool — read-only email access via Gmail API."""

import base64
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
TOKEN_PATH = CONFIG_DIR / "gmail_token.json"
CREDENTIALS_PATH = CONFIG_DIR.parent / "credentials.json"

GMAIL_TOOL_SCHEMA = {
    "name": "read_emails",
    "description": (
        "Search and read Shakti's Gmail. Returns matching emails with subject, "
        "from, to, date, and snippet. Use Gmail search syntax for the query."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Gmail search query (e.g. 'from:boss@company.com', "
                    "'subject:deadline', 'is:unread'). Leave empty for recent emails."
                ),
            },
            "time_range": {
                "type": "string",
                "enum": ["24h", "48h", "7d"],
                "description": "How far back to search. Defaults to '48h'.",
            },
        },
        "required": [],
    },
}


def _get_gmail_service():
    """Return a Gmail API service instance from the stored token.

    Deliberately non-interactive: this runs inside Slack handler threads
    (possibly on a headless server), so it never opens a browser. If the
    token is missing or unrefreshable, it raises with instructions to run
    the one-time setup script.
    """
    if not TOKEN_PATH.exists():
        raise RuntimeError(
            "Gmail token not found. Run: python gmail_oauth_flow.py "
            "(see README for setup instructions)."
        )

    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                raise RuntimeError(
                    f"Gmail token refresh failed ({e}). "
                    "Re-run: python gmail_oauth_flow.py"
                ) from e
            TOKEN_PATH.write_text(creds.to_json())
        else:
            raise RuntimeError(
                "Gmail token is invalid and cannot be refreshed. "
                "Re-run: python gmail_oauth_flow.py"
            )

    return build("gmail", "v1", credentials=creds)


def _time_range_to_after(time_range: str) -> str:
    """Convert a time range string to a Gmail 'after:' epoch filter.

    Epoch seconds give hour-level precision; the YYYY/MM/DD form only has
    day granularity, so '24h' would really return up to ~48h of mail.
    """
    deltas = {"24h": timedelta(hours=24), "48h": timedelta(hours=48), "7d": timedelta(days=7)}
    delta = deltas.get(time_range, timedelta(hours=48))
    cutoff = datetime.now(timezone.utc) - delta
    return str(int(cutoff.timestamp()))


def _strip_html(html: str) -> str:
    """Rough HTML tag stripping."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_body(payload: dict) -> str:
    """Extract plain text body from a message payload."""
    # Try plain text first
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")

    # Walk multipart
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
        # Recurse into nested multipart
        if part.get("parts"):
            result = _extract_body(part)
            if result:
                return result

    # Fall back to HTML
    if payload.get("mimeType") == "text/html" and payload.get("body", {}).get("data"):
        html = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
        return _strip_html(html)

    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/html" and part.get("body", {}).get("data"):
            html = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
            return _strip_html(html)

    return ""


def read_emails(query: str = "", time_range: str = "48h") -> str:
    """Search Gmail and return matching messages as JSON."""
    try:
        service = _get_gmail_service()
    except RuntimeError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Gmail auth failed: {e}"})

    after_date = _time_range_to_after(time_range)
    full_query = f"after:{after_date}"
    if query:
        full_query = f"{query} {full_query}"

    try:
        results = (
            service.users()
            .messages()
            .list(userId="me", q=full_query, maxResults=20)
            .execute()
        )
    except Exception as e:
        return json.dumps({"error": f"Gmail search failed: {e}"})

    messages = results.get("messages", [])
    if not messages:
        return json.dumps({"emails": [], "message": "No emails found matching your query."})

    emails = []
    for msg_ref in messages:
        try:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=msg_ref["id"], format="full")
                .execute()
            )
        except Exception:
            continue

        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        body = _extract_body(msg.get("payload", {}))
        snippet = body[:500] if body else msg.get("snippet", "")

        emails.append(
            {
                "id": msg["id"],
                "subject": headers.get("subject", "(no subject)"),
                "from": headers.get("from", ""),
                "to": headers.get("to", ""),
                "date": headers.get("date", ""),
                "snippet": snippet,
            }
        )

    return json.dumps({"emails": emails, "count": len(emails)})
