"""Orchestrator — the agentic ReAct (Reasoning + Action) loop.

Calls Claude with the user's message and tool definitions. If Claude returns
tool calls, executes them, appends results, and loops. Repeats until Claude
returns a final text response or we hit the iteration cap.

Write tools (create/complete/update/delete task) are never executed here:
they're stored as pending actions and surfaced to the user as Slack
Confirm/Cancel buttons. The button handlers in main.py execute them. This
makes confirmation structural instead of relying on the model to remember
what it proposed.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from tools import TOOL_DISPATCH, TOOL_SCHEMAS, WRITE_TOOLS
from tools import memory_search
from tools.slack_tool import MessagePoster, confirmation_blocks
from tools.memory_store import (
    create_pending_action,
    get_message_gists,
    get_recent_conversation_gists,
    save_message,
)
from tools.summarizer import generate_message_gist

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_ITERATIONS = 10

SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "system_prompt.txt"

# How many past-conversation gists to inject: top-K by BM25 relevance to the
# current message, plus the most recent few for continuity.
MEMORY_RELEVANT_K = 8
MEMORY_RECENT_K = 5
MEMORY_MAX_GISTS = 12
MEMORY_CANDIDATE_POOL = 200

PROGRESS_LABELS = {
    "read_emails": "📧 Searching Gmail…",
    "get_tasks": "📋 Checking TickTick…",
    "create_task": "📝 Preparing a task…",
    "complete_task": "📝 Preparing a task update…",
    "update_task": "📝 Preparing a task update…",
    "delete_task": "📝 Preparing a task update…",
    "read_profile": "👤 Checking your profile…",
    "recall_message": "🧠 Recalling a past conversation…",
}

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    """Lazy singleton so importing this module never requires an API key."""
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def _load_static_prompt() -> str:
    return SYSTEM_PROMPT_PATH.read_text()


def _build_dynamic_context(conv_id: str | None, user_message: str) -> str:
    """Date/time plus memory context. Kept out of the static prompt block so
    the static block stays byte-identical and prompt-cacheable."""
    now = datetime.now(timezone.utc).astimezone()
    context = (
        f"## Current Date & Time\n"
        f"Today is {now.strftime('%A, %B %d, %Y')} "
        f"({now.strftime('%Y-%m-%d')}). "
        f"Current time: {now.strftime('%I:%M %p %Z')}. "
        f"Always use this as the reference for 'today', 'tomorrow', 'this week', etc."
    )

    memory = _build_memory_section(conv_id, user_message)
    if memory:
        context += "\n\n" + memory
    return context


def _build_memory_section(conv_id: str | None, user_message: str) -> str:
    """Select past-conversation gists by relevance + recency, plus the
    current thread's message gists."""
    section_parts = []

    candidates = [
        c for c in get_recent_conversation_gists(limit=MEMORY_CANDIDATE_POOL)
        if c["conv_id"] != conv_id
    ]
    if candidates:
        relevant_idx = memory_search.rank(
            user_message, [c["gist"] for c in candidates], top_k=MEMORY_RELEVANT_K
        )
        chosen: list[dict] = []
        seen: set[str] = set()
        for i in relevant_idx:
            chosen.append(candidates[i])
            seen.add(candidates[i]["conv_id"])
        for c in candidates[:MEMORY_RECENT_K]:  # candidates are newest-first
            if c["conv_id"] not in seen:
                chosen.append(c)
                seen.add(c["conv_id"])
        chosen = chosen[:MEMORY_MAX_GISTS]

        if chosen:
            lines = "\n".join(
                f"- [{c['conv_id'][:8]}...] {c['gist']}" for c in chosen
            )
            section_parts.append(
                "### Past Conversations (relevant & recent threads)\n" + lines
            )

    if conv_id:
        current_messages = get_message_gists(conv_id)
        if current_messages:
            lines = "\n".join(
                f"- [{m['message_id']}] {m['gist']}" for m in current_messages
            )
            section_parts.append("### Current Thread History\n" + lines)

    if not section_parts:
        return ""
    return "## Conversation Memory\n\n" + "\n\n".join(section_parts)


def _save_message_to_memory(conv_id: str, message_id: str, messages: list[dict]) -> None:
    """Generate gist and save message to memory store.

    Runs synchronously on the handler thread — one Haiku call, typically
    under a second, and the user already has their answer by this point.
    """
    try:
        _strip_cache_control(messages)
        gist = generate_message_gist(messages)
        save_message(conv_id, message_id, gist, messages)
    except Exception:
        logger.exception("Failed to save message to memory")
        # Don't fail the main flow if memory saving fails


def execute_tool(tool_name: str, tool_input: dict) -> str:
    """Dispatch a tool call and return its result as a string.

    Also used by main.py's confirmation button handlers to run write tools
    after the user confirms.
    """
    func = TOOL_DISPATCH.get(tool_name)
    if not func:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    try:
        return func(**tool_input)
    except Exception as e:
        logger.exception("Tool %s failed", tool_name)
        return json.dumps({"error": f"Tool '{tool_name}' failed: {e}"})


def describe_write_action(tool_name: str, tool_input: dict) -> str:
    """Human-readable summary of a pending write action, for the Slack
    confirmation card and the post-execution receipt."""
    if tool_name == "create_task":
        lines = [f"*Create task:* {tool_input.get('title', '(untitled)')}"]
        if tool_input.get("due_date"):
            lines.append(f"• Due: {tool_input['due_date']}")
        lines.append(f"• Project: {tool_input.get('project', 'Inbox')}")
        if tool_input.get("priority"):
            lines.append(f"• Priority: {tool_input['priority']}")
        return "\n".join(lines)

    title = tool_input.get("title") or tool_input.get("task_id", "(unknown task)")
    if tool_name == "complete_task":
        return f"*Mark task complete:* {title}"
    if tool_name == "delete_task":
        return f"*Delete task:* {title} ⚠️ _This cannot be undone._"
    if tool_name == "update_task":
        target = tool_input.get("current_title") or tool_input.get("task_id", "(unknown task)")
        lines = [f"*Update task:* {target}"]
        field_labels = {"title": "New title", "due_date": "New due date", "priority": "New priority"}
        for field, label in field_labels.items():
            if tool_input.get(field) not in (None, ""):
                lines.append(f"• {label}: {tool_input[field]}")
        return "\n".join(lines)
    return f"*{tool_name}*: {json.dumps(tool_input)}"


def _serialize_content(content) -> list[dict]:
    """Convert SDK response content blocks into plain dicts.

    Keeps the messages array JSON-serializable (memory persistence does
    json.dumps on it) and re-sendable to the API. Every block type is
    preserved — including the server-side web_search result blocks that must
    be echoed back verbatim when the turn pauses or continues.
    """
    blocks = []
    for block in content:
        if hasattr(block, "model_dump"):
            blocks.append(block.model_dump(exclude_none=True, mode="json"))
        else:
            blocks.append(block)
    return blocks


def _strip_cache_control(messages: list[dict]) -> None:
    """Remove cache breakpoints from all message content blocks."""
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block.pop("cache_control", None)


def _refresh_message_cache_breakpoint(messages: list[dict]) -> None:
    """Move the incremental cache breakpoint to the newest tool_result block.

    Each ReAct iteration re-sends the whole growing messages array; caching
    up through the latest tool results means the next iteration only pays
    for the new tokens. Only tool_result blocks are marked — server-side
    web_search blocks don't accept cache_control.
    """
    _strip_cache_control(messages)
    last = messages[-1]
    if last.get("role") == "user" and isinstance(last.get("content"), list):
        for block in reversed(last["content"]):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                block["cache_control"] = {"type": "ephemeral"}
                break


def run(
    user_message: str,
    poster: MessagePoster,
    conv_id: str | None = None,
    message_id: str | None = None,
) -> None:
    """Run the agentic loop for a single user message.

    Args:
        user_message: The text of the user's Slack message (or a synthetic
            prompt, e.g. the morning digest).
        poster: MessagePoster bound to the target channel/thread.
        conv_id: The conversation ID (Slack thread_ts) for memory context,
            or None to skip memory persistence (digest runs).
        message_id: The unique message ID (Slack ts) for saving this exchange.
    """
    client = _get_client()

    # Static prompt gets a cache breakpoint; the dynamic block (date + memory)
    # gets its own so all iterations within this run share a cached prefix.
    system_blocks = [
        {
            "type": "text",
            "text": _load_static_prompt(),
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": _build_dynamic_context(conv_id, user_message),
            "cache_control": {"type": "ephemeral"},
        },
    ]

    messages: list[dict] = [{"role": "user", "content": user_message}]
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "calls": 0}
    last_text = ""

    poster.start()

    for iteration in range(MAX_ITERATIONS):
        logger.info("Orchestrator iteration %d", iteration + 1)
        _refresh_message_cache_breakpoint(messages)

        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=system_blocks,
                tools=TOOL_SCHEMAS,
                messages=messages,
            )
        except anthropic.APIError as e:
            logger.error("Anthropic API error: %s", e)
            poster.finalize(f"⚠️ API error: {e}. Please try again.")
            return

        usage["calls"] += 1
        usage["input"] += getattr(response.usage, "input_tokens", 0) or 0
        usage["output"] += getattr(response.usage, "output_tokens", 0) or 0
        usage["cache_read"] += getattr(response.usage, "cache_read_input_tokens", 0) or 0
        usage["cache_write"] += getattr(response.usage, "cache_creation_input_tokens", 0) or 0

        # Append the assistant's full content — text, client tool_use, and any
        # server-side web_search blocks (already executed by Anthropic) — so
        # the conversation stays faithful across iterations.
        messages.append(
            {"role": "assistant", "content": _serialize_content(response.content)}
        )

        for block in response.content:
            if block.type == "text" and block.text.strip():
                last_text = block.text

        # The server-side web_search tool exhausted its per-turn loop and
        # paused. Re-send as-is to let Anthropic resume — no extra user
        # message needed.
        if response.stop_reason == "pause_turn":
            poster.progress("🌐 Searching the web…")
            continue

        if response.stop_reason == "tool_use":
            # Execute only client-side tools. server_tool_use blocks were run
            # by Anthropic already; their results are in response.content.
            if any(b.type == "server_tool_use" for b in response.content):
                poster.progress("🌐 Searching the web…")

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                logger.info(
                    "Calling tool: %s(%s)", block.name, json.dumps(block.input)[:200]
                )
                poster.progress(PROGRESS_LABELS.get(block.name, f"⚙️ {block.name}…"))

                if block.name in WRITE_TOOLS:
                    result_str = _request_confirmation(
                        poster, block.name, block.input, conv_id
                    )
                else:
                    result_str = execute_tool(block.name, block.input)

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_str,
                    }
                )

            if tool_results:
                messages.append({"role": "user", "content": tool_results})
            continue

        # end_turn (or any other terminal stop reason): post the final text.
        text_parts = [
            block.text
            for block in response.content
            if block.type == "text" and block.text.strip()
        ]
        if text_parts:
            final_text = "\n".join(text_parts)
        elif response.stop_reason == "end_turn":
            final_text = "Done."
        else:
            final_text = "I wasn't able to complete that request."
        poster.finalize(final_text)

        _log_usage(usage, message_id)
        if conv_id and message_id:
            _save_message_to_memory(conv_id, message_id, messages)
        return

    # Hit iteration cap — surface whatever the model last said instead of
    # silently discarding partial work.
    cap_message = "⚠️ I hit my processing limit for this request."
    if last_text:
        cap_message += f" Here's where I got to:\n\n{last_text}"
    cap_message += "\n\nCould you try breaking it into smaller questions?"
    poster.finalize(cap_message)

    _log_usage(usage, message_id)
    if conv_id and message_id:
        _save_message_to_memory(conv_id, message_id, messages)


def _request_confirmation(
    poster: MessagePoster,
    tool_name: str,
    tool_input: dict,
    conv_id: str | None,
) -> str:
    """Store the write action and post a Confirm/Cancel card.

    Returns the tool_result string telling the model the action is pending.
    """
    try:
        action_key = create_pending_action(tool_name, tool_input, conv_id)
        summary = describe_write_action(tool_name, tool_input)
        poster.post(
            text=summary,
            blocks=confirmation_blocks(summary, action_key),
            convert=False,
        )
    except Exception as e:
        logger.exception("Failed to post confirmation card")
        return json.dumps({"error": f"Could not request confirmation: {e}"})

    return json.dumps(
        {
            "status": "awaiting_confirmation",
            "note": (
                "A Confirm/Cancel card was shown to Shakti in Slack for this "
                "action. It will execute only if he presses Confirm. Do NOT "
                "claim the action happened — briefly tell him it's waiting "
                "for his confirmation."
            ),
        }
    )


def _log_usage(usage: dict, message_id: str | None) -> None:
    logger.info(
        "Token usage (message %s): %d API calls, input=%d output=%d "
        "cache_read=%d cache_write=%d",
        message_id or "-",
        usage["calls"],
        usage["input"],
        usage["output"],
        usage["cache_read"],
        usage["cache_write"],
    )
