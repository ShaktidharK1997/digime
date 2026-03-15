"""Memory tool — recall full details from past exchanges."""

import json
import logging

from tools.memory_store import get_message_detail

logger = logging.getLogger(__name__)


def recall_message(message_id: str) -> str:
    """Retrieve the full detail of a previous exchange.

    Args:
        message_id: The unique message ts identifying the exchange.

    Returns:
        A formatted readable log of the full ReAct loop, or error message if not found.
    """
    detail = get_message_detail(message_id)

    if not detail:
        return json.dumps({
            "error": f"No message found with ID: {message_id}"
        })

    # Format the messages array into a readable log
    formatted = _format_message_detail(detail)

    logger.info("Recalled message %s (%d chars)", message_id, len(formatted))
    return formatted


def _format_message_detail(messages: list[dict]) -> str:
    """Format a messages array into a readable log.

    Args:
        messages: The messages array from orchestrator.run().

    Returns:
        A formatted string showing the flow of the exchange.

    Example output:
        User: "Check my emails for anything from my manager"
        → Called read_emails(query="from:manager", time_range="48h")
        → Tool result: Found 3 emails...
        → Called get_tasks(date_range="week")
        → Tool result: Found 5 tasks...
        → Assistant response: "Found 3 emails from your manager but no matching tasks..."
    """
    lines = []
    lines.append("=" * 60)
    lines.append("FULL EXCHANGE DETAIL")
    lines.append("=" * 60)

    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", [])

        if role == "user":
            # Extract user text or tool results
            if isinstance(content, str):
                lines.append(f"\nUser: {content}")
            elif isinstance(content, list):
                for block in content:
                    if block.get("type") == "text":
                        lines.append(f"\nUser: {block.get('text', '')}")
                    elif block.get("type") == "tool_result":
                        # Tool result
                        tool_use_id = block.get("tool_use_id", "unknown")
                        result_content = block.get("content", "")
                        # Truncate long results
                        if len(result_content) > 500:
                            result_content = result_content[:500] + "... (truncated)"
                        lines.append(f"  → Tool result ({tool_use_id[:8]}): {result_content}")

        elif role == "assistant":
            # Extract assistant text and tool calls
            if isinstance(content, str):
                lines.append(f"\nAssistant: {content}")
            elif isinstance(content, list):
                for block in content:
                    if block.get("type") == "text":
                        text = block.get("text", "")
                        if text.strip():
                            lines.append(f"\nAssistant: {text}")
                    elif block.get("type") == "tool_use":
                        tool_name = block.get("name", "unknown_tool")
                        tool_id = block.get("id", "unknown_id")
                        tool_input = block.get("input", {})
                        # Format input nicely
                        input_str = json.dumps(tool_input, indent=2)
                        lines.append(f"\n→ Called {tool_name} (id: {tool_id[:8]})")
                        lines.append(f"  Input: {input_str}")

    lines.append("\n" + "=" * 60)
    return "\n".join(lines)


# Tool schema for Claude
RECALL_MESSAGE_SCHEMA = {
    "name": "recall_message",
    "description": (
        "Retrieve the full detail of a previous exchange in any conversation thread. "
        "Use this when you need exact details (specific email content, task data, tool results) "
        "beyond what the gist provides. The message_id corresponds to a specific exchange "
        "listed in the conversation history."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "message_id": {
                "type": "string",
                "description": "The unique message ID (Slack ts) of the exchange to recall"
            }
        },
        "required": ["message_id"]
    }
}
