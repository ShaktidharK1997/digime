"""Slack tool — posts messages back to Slack DMs."""


def post_slack_message(say_func, message: str, thread_ts: str | None = None) -> None:
    """Post a message to Slack using the say function from slack-bolt."""
    kwargs = {"text": message}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    say_func(**kwargs)
