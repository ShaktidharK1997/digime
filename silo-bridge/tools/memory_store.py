"""Memory storage — SQLite-backed conversation and message persistence.

Implements a two-tier hierarchical memory system:
- Conversations (keyed by Slack thread_ts): entire threads with status and gist
- Messages (keyed by individual message ts): each exchange with gist + full detail
"""

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "config" / "memory.db"

# In-memory cache of recent completed conversation gists
# Populated on init_db() and updated when conversations complete
_conversation_gist_cache: dict[str, str] = {}

_conn: sqlite3.Connection | None = None


def _get_connection() -> sqlite3.Connection:
    """Get or create the database connection."""
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


def init_db() -> None:
    """Initialize database tables and load conversation gist cache."""
    conn = _get_connection()

    # Create conversations table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            conv_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            gist TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)

    # Create messages table
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

    # Create index for faster conversation lookups
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_messages_conv_id
        ON messages(conv_id)
    """)

    conn.commit()

    # Load recent completed conversation gists into cache
    _load_conversation_gist_cache()

    logger.info("Memory store initialized. DB at %s", DB_PATH)


def _load_conversation_gist_cache() -> None:
    """Load the last 20 completed conversation gists into memory cache."""
    global _conversation_gist_cache
    conn = _get_connection()

    rows = conn.execute("""
        SELECT conv_id, gist
        FROM conversations
        WHERE status = 'complete' AND gist IS NOT NULL
        ORDER BY updated_at DESC
        LIMIT 20
    """).fetchall()

    _conversation_gist_cache = {row["conv_id"]: row["gist"] for row in rows}
    logger.info("Loaded %d conversation gists into cache", len(_conversation_gist_cache))


def get_or_create_conversation(conv_id: str) -> dict[str, Any]:
    """Get an existing conversation or create a new active one.

    Args:
        conv_id: The Slack thread_ts identifying this conversation.

    Returns:
        dict with keys: conv_id, status, gist, created_at, updated_at
    """
    conn = _get_connection()

    row = conn.execute(
        "SELECT * FROM conversations WHERE conv_id = ?",
        (conv_id,)
    ).fetchone()

    if row:
        return dict(row)

    # Create new conversation
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
    conn = _get_connection()
    now = time.time()

    detail_json = json.dumps(detail)

    conn.execute("""
        INSERT OR REPLACE INTO messages (message_id, conv_id, gist, detail, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (message_id, conv_id, gist, detail_json, now))

    # Update conversation's updated_at timestamp
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
    conn = _get_connection()
    now = time.time()

    conn.execute("""
        UPDATE conversations
        SET status = 'complete', gist = ?, updated_at = ?
        WHERE conv_id = ?
    """, (gist, now, conv_id))

    conn.commit()
    logger.info("Completed conversation %s with gist: %s", conv_id, gist[:100])

    # Add to cache
    _conversation_gist_cache[conv_id] = gist

    # If cache exceeds 20, remove oldest (this is approximate, good enough for cache)
    if len(_conversation_gist_cache) > 20:
        # Just reload from DB to keep the most recent 20
        _load_conversation_gist_cache()


def get_recent_conversation_gists(limit: int = 20) -> list[dict[str, str]]:
    """Get recent completed conversation gists.

    Args:
        limit: Maximum number of gists to return (default 20).

    Returns:
        List of dicts with keys: conv_id, gist
    """
    # Return from cache if available
    if _conversation_gist_cache:
        items = [
            {"conv_id": conv_id, "gist": gist}
            for conv_id, gist in _conversation_gist_cache.items()
        ]
        return items[:limit]

    # Fallback to DB query if cache is empty
    conn = _get_connection()
    rows = conn.execute("""
        SELECT conv_id, gist
        FROM conversations
        WHERE status = 'complete' AND gist IS NOT NULL
        ORDER BY updated_at DESC
        LIMIT ?
    """, (limit,)).fetchall()

    return [{"conv_id": row["conv_id"], "gist": row["gist"]} for row in rows]
