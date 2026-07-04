"""Convert common Markdown that Claude emits into Slack mrkdwn.

Slack does not render standard Markdown: **bold** shows literal asterisks,
headers show literal hashes, and [text](url) links don't linkify. The system
prompt asks the model to write Slack-flavored text, but this converter is the
safety net so replies always render cleanly.
"""

import re


def markdown_to_mrkdwn(text: str) -> str:
    """Best-effort Markdown → Slack mrkdwn conversion.

    Handles: **bold**, __bold__, [text](url) links, #-headers, and -/* list
    bullets. Fenced code blocks are passed through untouched.
    """
    if not text:
        return text

    # Split out fenced code blocks so we don't rewrite their contents
    parts = re.split(r"(```.*?```)", text, flags=re.DOTALL)
    converted = [
        part if part.startswith("```") else _convert_segment(part)
        for part in parts
    ]
    return "".join(converted)


def _convert_segment(text: str) -> str:
    # [text](url) -> <url|text>
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", text)

    # **bold** / __bold__ -> *bold*
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"__(.+?)__", r"*\1*", text)

    lines = []
    for line in text.split("\n"):
        # "# Header" -> "*Header*"
        header = re.match(r"^(#{1,6})\s+(.*)$", line)
        if header:
            lines.append(f"*{header.group(2).strip()}*")
            continue
        # "- item" / "* item" -> "• item" (preserve indentation)
        bullet = re.match(r"^(\s*)[-*]\s+(.*)$", line)
        if bullet:
            lines.append(f"{bullet.group(1)}• {bullet.group(2)}")
            continue
        lines.append(line)
    return "\n".join(lines)
