from tools.profile_tool import read_profile, PROFILE_TOOL_SCHEMA
from tools.gmail_tool import read_emails, GMAIL_TOOL_SCHEMA
from tools.ticktick_tool import (
    get_tasks,
    create_task,
    GET_TASKS_SCHEMA,
    CREATE_TASK_SCHEMA,
)
from tools.web_fetch_tool import fetch_url, FETCH_URL_SCHEMA
from tools.slack_tool import post_slack_message
from tools.memory_tool import recall_message, RECALL_MESSAGE_SCHEMA

TOOL_SCHEMAS = [
    GMAIL_TOOL_SCHEMA,
    GET_TASKS_SCHEMA,
    CREATE_TASK_SCHEMA,
    FETCH_URL_SCHEMA,
    PROFILE_TOOL_SCHEMA,
    RECALL_MESSAGE_SCHEMA,
]

TOOL_DISPATCH = {
    "read_emails": read_emails,
    "get_tasks": get_tasks,
    "create_task": create_task,
    "fetch_url": fetch_url,
    "read_profile": read_profile,
    "recall_message": recall_message,
}
