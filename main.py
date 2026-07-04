"""Entry point — starts the Slack bot with Socket Mode."""

import json
import logging
import os
import re

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import orchestrator
from scheduler import Scheduler
from tools import memory_store
from tools.slack_tool import MessagePoster
from tools.summarizer import generate_conversation_gist

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = App(token=os.environ["SLACK_BOT_TOKEN"])

# The single Slack user this bot serves. Anyone else's messages (and button
# clicks) are ignored — the bot has access to the owner's email and tasks.
ALLOWED_USER_ID = os.environ.get("SLACK_ALLOWED_USER_ID", "").strip()

_DONE_RE = re.compile(r"^\W*done\W*$", re.IGNORECASE)


def _is_authorized(user_id: str | None) -> bool:
    if not ALLOWED_USER_ID:
        logger.warning(
            "SLACK_ALLOWED_USER_ID is not set — responding to ALL users. "
            "Set SLACK_ALLOWED_USER_ID=%s in .env to lock the bot to you.",
            user_id or "<your-user-id>",
        )
        return True
    return user_id == ALLOWED_USER_ID


@app.event("message")
def handle_message(event, client):
    """Handle incoming DM messages."""
    # Ignore bot messages, edits, and other subtypes
    if event.get("subtype") is not None or event.get("bot_id"):
        return

    # DMs only — even if the app gets invited to channels later
    if event.get("channel_type") not in (None, "im"):
        return

    if not _is_authorized(event.get("user")):
        logger.info("Ignoring message from unauthorized user %s", event.get("user"))
        return

    user_message = event.get("text", "")
    if not user_message.strip():
        return

    # thread_ts is the conversation ID; if this is a new DM, it's the message ts
    # If it's a reply in a thread, thread_ts is already set
    thread_ts = event.get("thread_ts", event["ts"])
    message_ts = event["ts"]  # Unique ID for this specific message

    logger.info("Received message: %s", user_message[:100])

    poster = MessagePoster(client, event["channel"], thread_ts)

    # "Done" (with optional punctuation) completes and archives the thread
    if _DONE_RE.match(user_message):
        _handle_done_command(thread_ts, poster)
        return

    # Ensure conversation exists in memory store
    memory_store.get_or_create_conversation(thread_ts)

    try:
        orchestrator.run(
            user_message=user_message,
            poster=poster,
            conv_id=thread_ts,
            message_id=message_ts,
        )
    except Exception:
        logger.exception("Orchestrator failed")
        poster.post("❌ Something went wrong. Please try again.")


@app.action("confirm_write")
def handle_confirm_write(ack, body, respond):
    """Execute a pending write action after the user presses Confirm."""
    ack()
    if not _is_authorized(body.get("user", {}).get("id")):
        return

    action_key = body["actions"][0]["value"]
    record = memory_store.get_pending_action(action_key)
    if not record or record["status"] != "pending":
        respond(
            replace_original=True,
            text="⚠️ This action has expired or was already handled.",
        )
        return

    summary = orchestrator.describe_write_action(record["tool"], record["input"])
    result_str = orchestrator.execute_tool(record["tool"], record["input"])
    try:
        result = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        result = {}

    if isinstance(result, dict) and result.get("error"):
        memory_store.set_pending_action_status(action_key, "failed")
        respond(
            replace_original=True,
            text=f"{summary}\n\n⚠️ Failed: {result['error']}",
        )
        outcome = f"failed: {result['error']}"
    else:
        memory_store.set_pending_action_status(action_key, "executed")
        respond(replace_original=True, text=f"{summary}\n\n✅ Done.")
        outcome = "succeeded"

    _record_action_in_memory(record, action_key, result_str, outcome)


@app.action("cancel_write")
def handle_cancel_write(ack, body, respond):
    """Discard a pending write action when the user presses Cancel."""
    ack()
    if not _is_authorized(body.get("user", {}).get("id")):
        return

    action_key = body["actions"][0]["value"]
    record = memory_store.get_pending_action(action_key)
    if not record or record["status"] != "pending":
        respond(
            replace_original=True,
            text="⚠️ This action has expired or was already handled.",
        )
        return

    memory_store.set_pending_action_status(action_key, "cancelled")
    summary = orchestrator.describe_write_action(record["tool"], record["input"])
    respond(replace_original=True, text=f"{summary}\n\n❌ Cancelled — no changes made.")

    _record_action_in_memory(record, action_key, "{}", "cancelled by user")


def _record_action_in_memory(record: dict, action_key: str, result_str: str, outcome: str) -> None:
    """Keep memory truthful about what happened after the button click.

    Built without a summarizer call — button handlers should stay fast.
    """
    if not record.get("conv_id"):
        return
    try:
        input_summary = json.dumps(record["input"])[:120]
        gist = f"{record['tool']} ({input_summary}) — {outcome}"
        detail = [
            {"role": "user", "content": f"(Slack button) {outcome} for {record['tool']}"},
            {"role": "assistant", "content": result_str},
        ]
        memory_store.save_message(record["conv_id"], f"action-{action_key[:12]}", gist, detail)
    except Exception:
        logger.exception("Failed to record confirmed action in memory")


def _handle_done_command(conv_id: str, poster: MessagePoster) -> None:
    """Handle the 'Done' command — generate conversation gist and mark complete.

    Args:
        conv_id: The conversation (thread_ts) to complete.
        poster: MessagePoster bound to this thread.
    """
    try:
        message_gists_data = memory_store.get_message_gists(conv_id)

        if not message_gists_data:
            poster.post("✓ Thread archived (no messages to summarize).")
            return

        gists = [msg["gist"] for msg in message_gists_data]
        conversation_gist = generate_conversation_gist(gists)
        memory_store.complete_conversation(conv_id, conversation_gist)

        poster.post(f"✓ Thread archived.\n\nSummary: {conversation_gist}")
        logger.info("Completed conversation %s: %s", conv_id, conversation_gist)

    except Exception:
        logger.exception("Failed to complete conversation %s", conv_id)
        poster.post("⚠️ Could not archive thread. Please try again.")


if __name__ == "__main__":
    logger.info("Starting digime bot...")

    if not ALLOWED_USER_ID:
        logger.warning(
            "SLACK_ALLOWED_USER_ID is not set. The bot will answer anyone who "
            "can DM it — set it in .env before sharing your workspace."
        )

    try:
        memory_store.init_db()
    except Exception:
        logger.exception("Failed to initialize memory store")
        # Continue anyway — bot will work without memory if needed

    # Background thread: morning digest, stale-thread archiving, housekeeping
    Scheduler(app.client, ALLOWED_USER_ID or None).start()

    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
