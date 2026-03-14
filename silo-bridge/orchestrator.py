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

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-20250514"
MAX_ITERATIONS = 10

SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "system_prompt.txt"


def _load_system_prompt() -> str:
    return SYSTEM_PROMPT_PATH.read_text()


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


def run(user_message: str, say_func, thread_ts: str | None = None) -> None:
    """Run the agentic loop for a single user message.

    Args:
        user_message: The text of the user's Slack message.
        say_func: slack-bolt's say() function for posting replies.
        thread_ts: Thread timestamp to keep replies threaded.
    """
    client = anthropic.Anthropic()
    system_prompt = _load_system_prompt()

    messages = [{"role": "user", "content": user_message}]

    # Post a brief "thinking" indicator
    post_slack_message(say_func, "\U0001f50d Working on it...", thread_ts)

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
            # Final text response — extract and post to Slack
            text_parts = [
                block.text
                for block in response.content
                if block.type == "text"
            ]
            final_text = "\n".join(text_parts) if text_parts else "Done."
            post_slack_message(say_func, final_text, thread_ts)
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
            text_parts = [
                block.text
                for block in response.content
                if block.type == "text"
            ]
            final_text = "\n".join(text_parts) if text_parts else "I wasn't able to complete that request."
            post_slack_message(say_func, final_text, thread_ts)
            return

    # Hit iteration cap
    post_slack_message(
        say_func,
        "\u26a0\ufe0f I hit my processing limit for this request. Here's what I have so far — could you try breaking it into smaller questions?",
        thread_ts,
    )
