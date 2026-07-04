"""Memory storage — SQLite-backed conversation and message persistence.

Implements a two-tier hierarchical memory system:
- Conversations (keyed by Slack thread_ts): entire threads with status and gist
- Messages (keyed by individual message ts): each exchange with gist + full detail

Also stores:
- pending_actions: write-tool calls awaiting user confirmation (Slack buttons)
- meta: small key/value state (e.g. last digest date)

slack-bolt runs handlers on a thread pool, so all access to the shared
connection is serialized through a module-level lock.
"""

import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "config" / "memory.db"

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


def _get_connection() -> sqlite3.Connection:
    """Get or create the database connection. Callers must hold _lock."""
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


def close() -> None:
    """Close the connection (used by tests to swap DB_PATH)."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def init_db() -> None:
    """Initialize database tables."""
    with _lock:
        conn = _get_connection()

        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                conv_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                gist TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY,
                conv_id TEXT NOT NULL,
                gist TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY (conv_id) REFERENCES conversations(conv_id)
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_conv_id
            ON messages(conv_id)
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_actions (
                action_key TEXT PRIMARY KEY,
                tool TEXT NOT NULL,
                input TEXT NOT NULL,
                conv_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at REAL NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        conn.commit()

    logger.info("Memory store initialized. DB at %s", DB_PATH)


def get_or_create_conversation(conv_id: str) -> dict[str, Any]:
    """Get an existing conversation or create a new active one.

    Args:
        conv_id: The Slack thread_ts identifying this conversation.

    Returns:
        dict with keys: conv_id, status, gist, created_at, updated_at
    """
    with _lock:
        conn = _get_connection()

        row = conn.execute(
            "SELECT * FROM conversations WHERE conv_id = ?",
            (conv_id,)
        ).fetchone()

        if row:
            return dict(row)

        now = time.time()
        conn.execute("""
            INSERT INTO conversations (conv_id, status, gist, created_at, updated_at)
            VALUES (?, 'active', NULL, ?, ?)
        """, (conv_id, now, now))
        conn.commit()

    logger.info("Created new conversation: %s", conv_id)

    return {
        "conv_id": conv_id,
        "status": "active",
        "gist": None,
        "created_at": now,
        "updated_at": now,
    }


def save_message(conv_id: str, message_id: str, gist: str, detail: list[dict]) -> None:
    """Save a completed exchange (message gist + full detail).

    Args:
        conv_id: The conversation this message belongs to.
        message_id: The unique ts of the user's Slack message.
        gist: One-sentence summary of this exchange.
        detail: Full messages array from orchestrator.run() for this exchange.
    """
    with _lock:
        conn = _get_connection()
        now = time.time()

        conn.execute("""
            INSERT OR REPLACE INTO messages (message_id, conv_id, gist, detail, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (message_id, conv_id, gist, json.dumps(detail), now))

        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE conv_id = ?",
            (now, conv_id)
        )

        conn.commit()
    logger.info("Saved message %s to conversation %s", message_id, conv_id)


def get_message_gists(conv_id: str) -> list[dict[str, str]]:
    """Get all message gists for a conversation, ordered chronologically.

    Args:
        conv_id: The conversation to retrieve messages from.

    Returns:
        List of dicts with keys: message_id, gist
    """
    with _lock:
        conn = _get_connection()

        rows = conn.execute("""
            SELECT message_id, gist
            FROM messages
            WHERE conv_id = ?
            ORDER BY created_at ASC
        """, (conv_id,)).fetchall()

    return [{"message_id": row["message_id"], "gist": row["gist"]} for row in rows]


def get_message_detail(message_id: str) -> list[dict] | None:
    """Get the full detail (messages array) for a specific exchange.

    Args:
        message_id: The unique message ts to retrieve.

    Returns:
        The messages array from that orchestrator.run() call, or None if not found.
    """
    with _lock:
        conn = _get_connection()

        row = conn.execute(
            "SELECT detail FROM messages WHERE message_id = ?",
            (message_id,)
        ).fetchone()

    if not row:
        return None

    return json.loads(row["detail"])


def complete_conversation(conv_id: str, gist: str) -> None:
    """Mark a conversation as complete and store its gist.

    Args:
        conv_id: The conversation to complete.
        gist: High-level summary of the entire thread.
    """
    with _lock:
        conn = _get_connection()
        now = time.time()

        conn.execute("""
            UPDATE conversations
            SET status = 'complete', gist = ?, updated_at = ?
            WHERE conv_id = ?
        """, (gist, now, conv_id))

        conn.commit()
    logger.info("Completed conversation %s with gist: %s", conv_id, gist[:100])


def get_recent_conversation_gists(limit: int = 20) -> list[dict[str, Any]]:
    """Get recent completed conversation gists, newest first.

    Args:
        limit: Maximum number of gists to return.

    Returns:
        List of dicts with keys: conv_id, gist, updated_at
    """
    with _lock:
        conn = _get_connection()
        rows = conn.execute("""
            SELECT conv_id, gist, updated_at
            FROM conversations
            WHERE status = 'complete' AND gist IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT ?
        """, (limit,)).fetchall()

    return [dict(row) for row in rows]


def get_stale_active_conversations(older_than_ts: float) -> list[str]:
    """Find active conversations not touched since the given timestamp.

    Args:
        older_than_ts: Unix timestamp cutoff.

    Returns:
        List of conv_ids eligible for auto-archiving.
    """
    with _lock:
        conn = _get_connection()
        rows = conn.execute("""
            SELECT conv_id
            FROM conversations
            WHERE status = 'active' AND updated_at < ?
        """, (older_than_ts,)).fetchall()

    return [row["conv_id"] for row in rows]


# ---------------------------------------------------------------------------
# Pending write actions (Slack confirmation buttons)
# ---------------------------------------------------------------------------

def create_pending_action(tool: str, tool_input: dict, conv_id: str | None) -> str:
    """Store a write-tool call awaiting user confirmation.

    Returns:
        A short opaque key to embed in the Slack button value.
    """
    action_key = uuid.uuid4().hex
    with _lock:
        conn = _get_connection()
        conn.execute("""
            INSERT INTO pending_actions (action_key, tool, input, conv_id, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
        """, (action_key, tool, json.dumps(tool_input), conv_id, time.time()))
        conn.commit()
    return action_key


def get_pending_action(action_key: str) -> dict[str, Any] | None:
    """Fetch a pending action by key. Input is returned as a dict."""
    with _lock:
        conn = _get_connection()
        row = conn.execute(
            "SELECT * FROM pending_actions WHERE action_key = ?",
            (action_key,)
        ).fetchone()

    if not row:
        return None
    record = dict(row)
    record["input"] = json.loads(record["input"])
    return record


def set_pending_action_status(action_key: str, status: str) -> None:
    """Mark a pending action as executed / cancelled / expired."""
    with _lock:
        conn = _get_connection()
        conn.execute(
            "UPDATE pending_actions SET status = ? WHERE action_key = ?",
            (status, action_key)
        )
        conn.commit()


def expire_old_pending_actions(older_than_ts: float) -> int:
    """Expire pending actions created before the cutoff. Returns count."""
    with _lock:
        conn = _get_connection()
        cur = conn.execute("""
            UPDATE pending_actions
            SET status = 'expired'
            WHERE status = 'pending' AND created_at < ?
        """, (older_than_ts,))
        conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# Meta key/value state
# ---------------------------------------------------------------------------

def get_meta(key: str) -> str | None:
    """Read a meta value (e.g. last digest date)."""
    with _lock:
        conn = _get_connection()
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    """Write a meta value."""
    with _lock:
        conn = _get_connection()
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, value)
        )
        conn.commit()
