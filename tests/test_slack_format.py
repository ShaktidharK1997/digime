"""Tests for Markdown → Slack mrkdwn conversion."""

from tools.slack_format import markdown_to_mrkdwn


def test_double_asterisk_bold():
    assert markdown_to_mrkdwn("this is **bold** text") == "this is *bold* text"


def test_underscore_bold():
    assert markdown_to_mrkdwn("__bold__") == "*bold*"


def test_links():
    assert (
        markdown_to_mrkdwn("see [the docs](https://example.com/a?b=1)")
        == "see <https://example.com/a?b=1|the docs>"
    )


def test_headers():
    assert markdown_to_mrkdwn("## Tasks due") == "*Tasks due*"


def test_bullets():
    assert markdown_to_mrkdwn("- first\n* second\n  - nested") == "• first\n• second\n  • nested"


def test_code_blocks_untouched():
    text = "before\n```\n**not bold** - not a bullet\n```\nafter **bold**"
    result = markdown_to_mrkdwn(text)
    assert "**not bold** - not a bullet" in result
    assert result.endswith("after *bold*")


def test_plain_text_unchanged():
    assert markdown_to_mrkdwn("just a sentence.") == "just a sentence."


def test_empty():
    assert markdown_to_mrkdwn("") == ""
