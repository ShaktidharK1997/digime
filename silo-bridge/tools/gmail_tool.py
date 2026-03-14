"""Gmail tool — read-only email access via Gmail API."""

import base64
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
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
    """Authenticate and return a Gmail API service instance."""
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_PATH.exists():
                raise FileNotFoundError(
                    f"Gmail credentials.json not found at {CREDENTIALS_PATH}. "
                    "Download it from Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_PATH), SCOPES
            )
            creds = flow.run_local_server(port=0)

        TOKEN_PATH.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _time_range_to_after(time_range: str) -> str:
    """Convert a time range string to a Gmail 'after:' date filter."""
    deltas = {"24h": timedelta(hours=24), "48h": timedelta(hours=48), "7d": timedelta(days=7)}
    delta = deltas.get(time_range, timedelta(hours=48))
    cutoff = datetime.utcnow() - delta
    return cutoff.strftime("%Y/%m/%d")


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
    except FileNotFoundError as e:
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
