"""Orchestrator — the agentic ReAct (Reasoning + Action) loop.

Calls Claude with the user's message and tool definitions. If Claude returns
tool calls, executes them, appends results, and loops. Repeats until Claude
returns a final text response or we hit the iteration cap.
"""

import json
import logging
from pathlib import Path

import anthropic

from tools import TOOL_DISPATCH, TOOL_SCHEMAS
from tools.slack_tool import post_slack_message
from tools.memory_store import (
    get_message_gists,
    get_recent_conversation_gists,
    save_message,
)
from tools.summarizer import generate_message_gist
from datetime import datetime, timezone
logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_ITERATIONS = 10

SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "system_prompt.txt"


def _load_system_prompt() -> str:
    prompt = SYSTEM_PROMPT_PATH.read_text()
    now = datetime.now(timezone.utc).astimezone()
    date_context = (
        f"\n\n## Current Date & Time\n"
        f"Today is {now.strftime('%A, %B %d, %Y')} "
        f"({now.strftime('%Y-%m-%d')}). "
        f"Current time: {now.strftime('%I:%M %p %Z')}. "
        f"Always use this as the reference for 'today', 'tomorrow', 'this week', etc."
    )
    return prompt + date_context


def _build_system_prompt_with_memory(base_prompt: str, conv_id: str | None) -> str:
    """Build system prompt with conversation history injected.

    Args:
        base_prompt: The base system prompt text.
        conv_id: The current conversation ID (thread_ts), or None for new threads.

    Returns:
        System prompt with memory context appended.
    """
    if not conv_id:
        return base_prompt

    # Get recent completed conversation gists (from other threads)
    recent_convs = get_recent_conversation_gists(limit=20)

    # Get message gists from the current thread
    current_messages = get_message_gists(conv_id)

    # Build memory section
    memory_section = "\n\n## Conversation Memory\n\n"

    if recent_convs:
        memory_section += "### Past Conversations (Recent Threads)\n"
        for conv in recent_convs:
            # Don't include the current conversation in "past conversations"
            if conv["conv_id"] != conv_id:
                memory_section += f"- [{conv['conv_id'][:8]}...] {conv['gist']}\n"
        memory_section += "\n"

    if current_messages:
        memory_section += "### Current Thread History\n"
        for msg in current_messages:
            memory_section += f"- [{msg['message_id']}] {msg['gist']}\n"
        memory_section += "\n"

    if not recent_convs and not current_messages:
        # No memory to inject
        return base_prompt

    return base_prompt + memory_section


def _save_message_to_memory(conv_id: str, message_id: str, messages: list[dict]) -> None:
    """Generate gist and save message to memory store.

    This runs synchronously but should be fast (one Haiku call).

    Args:
        conv_id: The conversation ID.
        message_id: The unique message ID.
        messages: The full messages array from this exchange.
    """
    try:
        # Generate gist
        gist = generate_message_gist(messages)

        # Save to store
        save_message(conv_id, message_id, gist, messages)

    except Exception:
        logger.exception("Failed to save message to memory")
        # Don't fail the main flow if memory saving fails


def _execute_tool(tool_name: str, tool_input: dict) -> str:
    """Dispatch a tool call and return its result as a string."""
    func = TOOL_DISPATCH.get(tool_name)
    if not func:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    try:
        result = func(**tool_input)
        return result
    except Exception as e:
        logger.exception("Tool %s failed", tool_name)
        return json.dumps({"error": f"Tool '{tool_name}' failed: {e}"})


def run(
    user_message: str,
    say_func,
    thread_ts: str | None = None,
    conv_id: str | None = None,
    message_id: str | None = None,
) -> None:
    """Run the agentic loop for a single user message.

    Args:
        user_message: The text of the user's Slack message.
        say_func: slack-bolt's say() function for posting replies.
        thread_ts: Thread timestamp to keep replies threaded.
        conv_id: The conversation ID (Slack thread_ts) for memory context.
        message_id: The unique message ID (Slack ts) for saving this exchange.
    """
    client = anthropic.Anthropic()
    base_system_prompt = _load_system_prompt()

    # Load memory context and inject into system prompt
    system_prompt = _build_system_prompt_with_memory(base_system_prompt, conv_id)

    messages = [{"role": "user", "content": user_message}]

    # Post a brief "thinking" indicator
    post_slack_message(say_func, "\U0001f50d Robot is roboting..", thread_ts)

    for iteration in range(MAX_ITERATIONS):
        logger.info("Orchestrator iteration %d", iteration + 1)

        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=system_prompt,
                tools=TOOL_SCHEMAS,
                messages=messages,
            )
        except anthropic.APIError as e:
            post_slack_message(
                say_func,
                f"\u26a0\ufe0f API error: {e}. Please try again.",
                thread_ts,
            )
            return

        # Check stop reason
        if response.stop_reason == "end_turn":
            # Append final assistant response to messages
            assistant_content = []
            for block in response.content:
                if block.type == "text":
                    assistant_content.append({"type": "text", "text": block.text})
            messages.append({"role": "assistant", "content": assistant_content})

            # Final text response — extract and post to Slack
            text_parts = [
                block.text
                for block in response.content
                if block.type == "text"
            ]
            final_text = "\n".join(text_parts) if text_parts else "Done."
            post_slack_message(say_func, final_text, thread_ts)

            # Save message to memory store (async, non-blocking)
            if conv_id and message_id:
                _save_message_to_memory(conv_id, message_id, messages)

            return

        if response.stop_reason == "tool_use":
            # Append the assistant's message (contains tool_use blocks)
            assistant_content = []
            for block in response.content:
                if block.type == "text":
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    assistant_content.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        }
                    )

            messages.append({"role": "assistant", "content": assistant_content})

            # Execute each tool call and build tool_result messages
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    logger.info("Calling tool: %s(%s)", block.name, json.dumps(block.input)[:200])
                    result_str = _execute_tool(block.name, block.input)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_str,
                        }
                    )

            messages.append({"role": "user", "content": tool_results})

        else:
            # Unexpected stop reason
            # Append whatever we got from the assistant
            assistant_content = []
            for block in response.content:
                if block.type == "text":
                    assistant_content.append({"type": "text", "text": block.text})
            if assistant_content:
                messages.append({"role": "assistant", "content": assistant_content})

            text_parts = [
                block.text
                for block in response.content
                if block.type == "text"
            ]
            final_text = "\n".join(text_parts) if text_parts else "I wasn't able to complete that request."
            post_slack_message(say_func, final_text, thread_ts)

            # Save even incomplete exchanges
            if conv_id and message_id:
                _save_message_to_memory(conv_id, message_id, messages)

            return

    # Hit iteration cap
    post_slack_message(
        say_func,
        "\u26a0\ufe0f I hit my processing limit for this request. Here's what I have so far — could you try breaking it into smaller questions?",
        thread_ts,
    )

    # Save even if we hit iteration cap
    if conv_id and message_id:
        _save_message_to_memory(conv_id, message_id, messages)
