"""Entry point — starts the Slack bot with Socket Mode."""

import logging
import os

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import orchestrator
from tools.memory_store import (
    init_db,
    get_or_create_conversation,
    get_message_gists,
    complete_conversation,
)
from tools.summarizer import generate_conversation_gist

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = App(token=os.environ["SLACK_BOT_TOKEN"])


@app.event("message")
def handle_message(event, say):
    """Handle incoming DM messages."""
    # Ignore bot messages and message edits
    if event.get("subtype") is not None:
        return

    user_message = event.get("text", "")
    if not user_message.strip():
        return

    # thread_ts is the conversation ID; if this is a new DM, it's the message ts
    # If it's a reply in a thread, thread_ts is already set
    thread_ts = event.get("thread_ts", event["ts"])
    message_ts = event["ts"]  # Unique ID for this specific message

    logger.info("Received message: %s", user_message[:100])

    # Check if user said "Done" — if so, complete the conversation
    if user_message.strip().lower() == "done":
        _handle_done_command(thread_ts, say)
        return

    # Ensure conversation exists in memory store
    get_or_create_conversation(thread_ts)

    try:
        orchestrator.run(
            user_message=user_message,
            say_func=say,
            thread_ts=thread_ts,
            conv_id=thread_ts,
            message_id=message_ts,
        )
    except Exception:
        logger.exception("Orchestrator failed")
        say(text="\u274c Something went wrong. Please try again.", thread_ts=thread_ts)


def _handle_done_command(conv_id: str, say) -> None:
    """Handle the 'Done' command — generate conversation gist and mark complete.

    Args:
        conv_id: The conversation (thread_ts) to complete.
        say: Slack say function for posting response.
    """
    try:
        # Get all message gists from this conversation
        message_gists_data = get_message_gists(conv_id)

        if not message_gists_data:
            say(
                text="✓ Thread archived (no messages to summarize).",
                thread_ts=conv_id
            )
            return

        # Extract just the gist strings
        gists = [msg["gist"] for msg in message_gists_data]

        # Generate conversation-level gist
        conversation_gist = generate_conversation_gist(gists)

        # Mark conversation as complete
        complete_conversation(conv_id, conversation_gist)

        say(
            text=f"✓ Thread archived.\n\nSummary: {conversation_gist}",
            thread_ts=conv_id
        )

        logger.info("Completed conversation %s: %s", conv_id, conversation_gist)

    except Exception:
        logger.exception("Failed to complete conversation %s", conv_id)
        say(
            text="⚠️ Could not archive thread. Please try again.",
            thread_ts=conv_id
        )


if __name__ == "__main__":
    logger.info("Starting Silo Bridge bot...")

    # Initialize memory store
    try:
        init_db()
    except Exception:
        logger.exception("Failed to initialize memory store")
        # Continue anyway — bot will work without memory if needed

    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
