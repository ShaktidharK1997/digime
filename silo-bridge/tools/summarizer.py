"""Summarizer — generates concise gists of messages and conversations using Claude."""

import json
import logging

import anthropic

logger = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-4-5-20251001"


def generate_message_gist(messages_array: list[dict]) -> str:
    """Generate a one-sentence summary of a single exchange (user input + ReAct loop).

    Args:
        messages_array: The full messages array from one orchestrator.run() call.

    Returns:
        A one-sentence factual summary capturing:
        - What the user asked
        - What tools were used
        - The outcome

    Example: "Checked Gmail for emails from manager about deadline, found 3 matching emails, no corresponding TickTick task exists"
    """
    client = anthropic.Anthropic()

    # Convert messages array to a readable format for the summarizer
    context = _format_messages_for_summary(messages_array)

    prompt = f"""Summarize this exchange in ONE sentence (no more than 25 words). Include:
- What the user asked
- What tools were used
- The key outcome or finding

Be factual and concise. Do not editorialize.

Exchange:
{context}

One-sentence summary:"""

    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )

        gist = response.content[0].text.strip()
        logger.info("Generated message gist: %s", gist)
        return gist

    except Exception as e:
        logger.exception("Failed to generate message gist")
        # Fallback: extract just the user's message
        user_text = messages_array[0].get("content", "Unknown request") if messages_array else "Unknown request"
        if isinstance(user_text, list):
            user_text = " ".join(block.get("text", "") for block in user_text if block.get("type") == "text")
        return f"User asked: {user_text[:100]}"


def generate_conversation_gist(message_gists: list[str]) -> str:
    """Generate a one-sentence summary of an entire conversation thread.

    Args:
        message_gists: List of individual message gists from the thread.

    Returns:
        A one-sentence summary of the overall conversation.

    Example: "Reviewed emails about project launch, identified missing tasks, created follow-up task for vendor coordination"
    """
    client = anthropic.Anthropic()

    # Join message gists with line breaks
    gists_text = "\n".join(f"- {gist}" for gist in message_gists)

    prompt = f"""Summarize this conversation thread in ONE sentence (no more than 30 words).
Capture the overall flow: what was discussed, what actions were taken, what was discovered.

Thread summary points:
{gists_text}

One-sentence conversation summary:"""

    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )

        gist = response.content[0].text.strip()
        logger.info("Generated conversation gist: %s", gist)
        return gist

    except Exception as e:
        logger.exception("Failed to generate conversation gist")
        # Fallback: use the first message gist
        return message_gists[0] if message_gists else "Conversation completed"


def _format_messages_for_summary(messages: list[dict]) -> str:
    """Convert messages array to a readable text format for summarization.

    Args:
        messages: The messages array from orchestrator.

    Returns:
        Formatted string representation of the exchange.
    """
    lines = []

    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", [])

        if role == "user":
            # Extract user text
            if isinstance(content, str):
                lines.append(f"User: {content}")
            elif isinstance(content, list):
                for block in content:
                    if block.get("type") == "text":
                        lines.append(f"User: {block.get('text', '')}")
                    elif block.get("type") == "tool_result":
                        # Skip tool results in summary (too verbose)
                        pass

        elif role == "assistant":
            # Extract assistant text and tool calls
            if isinstance(content, str):
                lines.append(f"Assistant: {content}")
            elif isinstance(content, list):
                for block in content:
                    if block.get("type") == "text":
                        text = block.get("text", "")
                        if text.strip():
                            lines.append(f"Assistant: {text}")
                    elif block.get("type") == "tool_use":
                        tool_name = block.get("name", "unknown_tool")
                        tool_input = block.get("input", {})
                        # Truncate long inputs
                        input_str = json.dumps(tool_input)[:150]
                        lines.append(f"→ Called {tool_name}({input_str})")

    return "\n".join(lines)
