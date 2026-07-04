"""Slack posting — in-place progress updates and confirmation buttons.

MessagePoster wraps a Slack WebClient for one exchange: it posts a single
placeholder message, updates it in place as the ReAct loop progresses
("Searching Gmail…" → "Checking TickTick…"), and finally replaces it with
the answer — instead of littering the thread with stranded status messages.
"""

import logging

from tools.slack_format import markdown_to_mrkdwn

logger = logging.getLogger(__name__)

# Slack chat.postMessage caps text around 40k chars; stay well under it.
MAX_MESSAGE_CHARS = 39_000


class MessagePoster:
    """Posts to one Slack channel/thread with an updatable placeholder."""

    def __init__(self, client, channel: str, thread_ts: str | None = None):
        self.client = client
        self.channel = channel
        self.thread_ts = thread_ts
        self._placeholder_ts: str | None = None

    def start(self, text: str = "🔍 Working on it…") -> None:
        """Post the placeholder message that progress/final text will replace."""
        try:
            resp = self.client.chat_postMessage(
                channel=self.channel, text=text, thread_ts=self.thread_ts
            )
            self._placeholder_ts = resp["ts"]
        except Exception:
            logger.exception("Failed to post placeholder message")

    def progress(self, text: str) -> None:
        """Update the placeholder with a short status line."""
        if not self._placeholder_ts:
            return
        try:
            self.client.chat_update(
                channel=self.channel, ts=self._placeholder_ts, text=text
            )
        except Exception:
            logger.exception("Failed to update progress message")

    def finalize(self, text: str) -> None:
        """Replace the placeholder with the final answer (or post fresh)."""
        text = markdown_to_mrkdwn(text)[:MAX_MESSAGE_CHARS]
        if self._placeholder_ts:
            try:
                self.client.chat_update(
                    channel=self.channel, ts=self._placeholder_ts, text=text
                )
                self._placeholder_ts = None
                return
            except Exception:
                logger.exception("chat_update failed; falling back to new message")
        self.post(text, convert=False)

    def post(self, text: str, blocks: list | None = None, convert: bool = True) -> None:
        """Post a standalone message (confirmation cards, errors)."""
        if convert:
            text = markdown_to_mrkdwn(text)
        try:
            self.client.chat_postMessage(
                channel=self.channel,
                text=text[:MAX_MESSAGE_CHARS],
                blocks=blocks,
                thread_ts=self.thread_ts,
            )
        except Exception:
            logger.exception("Failed to post Slack message")


def confirmation_blocks(summary_text: str, action_key: str) -> list[dict]:
    """Block Kit card with Confirm/Cancel buttons for a pending write action.

    The button value carries only an opaque key; the actual tool payload
    lives in the pending_actions table, so nothing sensitive or oversized
    goes into the Slack message.
    """
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": summary_text},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Confirm"},
                    "style": "primary",
                    "action_id": "confirm_write",
                    "value": action_key,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ Cancel"},
                    "style": "danger",
                    "action_id": "cancel_write",
                    "value": action_key,
                },
            ],
        },
    ]
