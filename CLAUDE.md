# digime — personal AI assistant Slack bot

## What this is

A single-user Slack bot (Socket Mode) that bridges Gmail, TickTick, and the
web through a Claude-driven ReAct loop, with SQLite-backed conversation
memory and a proactive morning digest.

## Commands

- `uv sync` — install dependencies (includes dev group)
- `uv run pytest` — run tests (pure-logic tests only; no network or API keys needed)
- `uv run python main.py` — start the bot (needs .env + OAuth tokens)
- `python gmail_oauth_flow.py` / `python ticktick_oauth_flow.py` — one-time OAuth setup

## Architecture

- `main.py` — Slack event/action handlers, owner allowlist, "done" archiving; starts the Scheduler thread
- `orchestrator.py` — ReAct loop. Read tools execute inline; write tools become pending actions + Slack Confirm/Cancel cards (never executed directly). Prompt caching via cache_control on system blocks and the newest tool_result
- `scheduler.py` — daemon thread: morning digest (config in `config/profile.yaml`), auto-archive of stale threads, pending-action expiry
- `tools/` — one module per integration; each tool returns a JSON string (errors included as `{"error": ...}` so the model can react)
  - `memory_store.py` — SQLite (conversations, messages, pending_actions, meta); all access behind a module lock, WAL mode
  - `memory_search.py` — dependency-free BM25 over gists
  - `slack_tool.py` — MessagePoster (placeholder → progress → final, updated in place), confirmation blocks
  - `slack_format.py` — Markdown → Slack mrkdwn safety net
- `prompts/` — system prompt (static; dynamic date/memory context is appended as a separate system block at runtime) and digest prompt

## Conventions

- Write tools are listed in `tools.WRITE_TOOLS`; adding a new write tool means adding it there plus a case in `orchestrator.describe_write_action`
- Tool functions never raise to the caller — they return JSON error strings
- Anthropic clients are lazy singletons (`_get_client()`) so modules import without an API key (keeps tests hermetic)
- Secrets/tokens live in `.env` and `config/*.json` (gitignored); `config/memory.db` holds full tool results — treat as sensitive
- Keep `requirements.txt` in sync with `pyproject.toml` runtime deps when they change
