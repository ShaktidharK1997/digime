"""Entry point — starts the Slack bot with Socket Mode."""

import logging
import os

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import orchestrator

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

    thread_ts = event.get("thread_ts", event["ts"])

    logger.info("Received message: %s", user_message[:100])

    try:
        orchestrator.run(
            user_message=user_message,
            say_func=say,
            thread_ts=thread_ts,
        )
    except Exception:
        logger.exception("Orchestrator failed")
        say(text="\u274c Something went wrong. Please try again.", thread_ts=thread_ts)


if __name__ == "__main__":
    logger.info("Starting Silo Bridge bot...")
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
