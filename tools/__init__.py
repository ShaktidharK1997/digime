from tools.profile_tool import read_profile, PROFILE_TOOL_SCHEMA
from tools.gmail_tool import read_emails, GMAIL_TOOL_SCHEMA
from tools.ticktick_tool import (
    get_tasks,
    create_task,
    complete_task,
    update_task,
    delete_task,
    GET_TASKS_SCHEMA,
    CREATE_TASK_SCHEMA,
    COMPLETE_TASK_SCHEMA,
    UPDATE_TASK_SCHEMA,
    DELETE_TASK_SCHEMA,
)
from tools.memory_tool import recall_message, RECALL_MESSAGE_SCHEMA

# Server-side web search — executed by Anthropic, not dispatched locally.
# The _20260209 variant (dynamic filtering) requires Sonnet 4.6+ / Opus 4.6+.
WEB_SEARCH_SCHEMA = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 5,
}

TOOL_SCHEMAS = [
    GMAIL_TOOL_SCHEMA,
    GET_TASKS_SCHEMA,
    CREATE_TASK_SCHEMA,
    COMPLETE_TASK_SCHEMA,
    UPDATE_TASK_SCHEMA,
    DELETE_TASK_SCHEMA,
    WEB_SEARCH_SCHEMA,
    PROFILE_TOOL_SCHEMA,
    RECALL_MESSAGE_SCHEMA,
]

# Client-side tools only. Server-side web tools run on Anthropic's
# infrastructure and are intentionally absent here.
TOOL_DISPATCH = {
    "read_emails": read_emails,
    "get_tasks": get_tasks,
    "create_task": create_task,
    "complete_task": complete_task,
    "update_task": update_task,
    "delete_task": delete_task,
    "read_profile": read_profile,
    "recall_message": recall_message,
}

# Tools with side effects. The orchestrator never executes these directly —
# it posts a Slack Confirm/Cancel card and only runs them after the user
# presses Confirm (see main.py action handlers).
WRITE_TOOLS = frozenset({"create_task", "complete_task", "update_task", "delete_task"})
