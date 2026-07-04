# digime

A Slack bot that bridges your information silos (Gmail, TickTick, web) using an LLM orchestrator with a ReAct (Reasoning + Action) loop — a step toward a digital twin that can interact with the digital world on your behalf.

## Features

- **Cross-silo reasoning** — ask "what's on my plate?" and it reads email + tasks, flags commitments that have no task, and synthesizes a priority view
- **Full task management** — read, create, complete, update, and delete TickTick tasks
- **Confirm/Cancel buttons** — every write action shows a Slack Block Kit card; nothing changes your data until you press Confirm
- **Proactive morning digest** — a scheduled daily briefing (email needing replies, tasks due, gaps between the two) posted to your DM
- **Two-tier conversation memory** — every exchange is summarized into a gist (SQLite); past threads are recalled by BM25 relevance to your current message, and the model can drill into full detail with `recall_message`
- **Live progress** — one message updates in place ("📧 Searching Gmail…" → answer) instead of littering the thread
- **Owner lock** — the bot only answers your Slack user ID

## Setup

### 1. Install dependencies

**Using uv (recommended):**

```bash
# Install uv if you haven't already
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install dependencies from lock file
uv sync
source .venv/bin/activate
```

**Or using traditional pip:**

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

(`uv.lock` is the source of truth; `requirements.txt` mirrors the top-level dependencies for pip users.)

### 2. Configure environment

```bash
cp .env.example .env
# Fill in your API keys and tokens
```

**Set `SLACK_ALLOWED_USER_ID`** to your Slack member ID (Slack profile → ⋮ → Copy member ID). The bot has access to your email and tasks — without this it will answer *anyone* who can DM it.

### 3. Slack App Setup

1. Create a new app at https://api.slack.com/apps
2. Enable **Socket Mode** — copy the App-Level Token (`xapp-...`) → `SLACK_APP_TOKEN`
3. Under **OAuth & Permissions**, add these Bot Token Scopes:
   - `chat:write`
   - `im:history`
   - `im:read`
   - `im:write`
   - `app_mentions:read`
4. Under **Interactivity & Shortcuts**, toggle **Interactivity ON** (no Request URL needed with Socket Mode — this is required for the Confirm/Cancel buttons)
5. Install the app to your workspace — copy the Bot Token (`xoxb-...`) → `SLACK_BOT_TOKEN`
6. Under **Event Subscriptions**, subscribe to bot event: `message.im`

### 4. Gmail Setup

1. Create a project in Google Cloud Console
2. Enable the Gmail API
3. Create OAuth 2.0 credentials (Desktop app type)
4. Add authorized redirect URI: `http://localhost:8080/`
5. Download `credentials.json` and place it in the repo root
6. Run the one-time auth flow (opens a browser):

```bash
python gmail_oauth_flow.py
```

The bot itself never does interactive auth (it may run headless) — re-run this script if the token ever becomes invalid.

### 5. TickTick Setup

1. Register an app at https://developer.ticktick.com
2. Set redirect URI to `http://127.0.0.1:8765/callback`
3. Add client ID and secret to `.env`
4. Run the one-time OAuth flow:

```bash
python ticktick_oauth_flow.py
```

### 6. Run

```bash
python main.py
```

Message the bot in a Slack DM to start using it. Say **done** in a thread to archive it into long-term memory.

## Morning digest

Configured in `config/profile.yaml`:

```yaml
digest:
  enabled: true
  time: "08:00"   # local time (uses location.timezone)
```

Each day at that time the bot checks your last 24h of email and today's tasks, cross-references them, and DMs you a short briefing. Requires `SLACK_ALLOWED_USER_ID` so it knows whose DM to open. State is tracked in SQLite, so restarts won't double-send; if the bot was down past the window, the digest is skipped until tomorrow.

## Deployment (Docker)

The bot uses Socket Mode — outbound connection only, nothing to expose.

```bash
docker compose up -d --build
```

`docker-compose.yml` mounts `./config` (OAuth tokens, profile, `memory.db`) and `credentials.json` into the container and restarts the bot unless stopped. Run both OAuth flows on the host *before* first start so the tokens exist.

## Architecture

```
User (Slack DM)                     Scheduler (daemon thread)
  → Slack Bot (Socket Mode)           • morning digest
    → LLM Orchestrator (Claude,       • auto-archive stale threads
      ReAct loop, prompt-cached)      • expire stale confirmations
      → read tools: executed inline
      → write tools: held as pending actions
        → Slack Confirm/Cancel card → button press executes
    ← One message, updated in place: progress → final answer
Memory: SQLite (conversations, message gists + full detail, pending actions)
```

## Tools

| Tool | Kind | Description |
|------|------|-------------|
| `read_emails` | read | Search Gmail (read-only, precise time windows) |
| `get_tasks` | read | Read TickTick tasks (today / week / overdue / all) |
| `create_task` | write | Create a TickTick task (confirm button) |
| `complete_task` | write | Mark a task done (confirm button) |
| `update_task` | write | Change title / due date / priority (confirm button) |
| `delete_task` | write | Delete a task (confirm button) |
| `web_search` | server | Anthropic server-side web search |
| `read_profile` | read | Read local YAML profile (diet, preferences, location) |
| `recall_message` | read | Retrieve full detail of a past exchange from memory |

## Development

```bash
uv sync            # includes dev group (pytest)
uv run pytest      # run the test suite
```

Tests cover the pure logic: date normalization, task filtering, BM25 ranking, mrkdwn conversion, and the SQLite memory store.
