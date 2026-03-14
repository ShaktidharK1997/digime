# Silo Bridge — Personal AI Assistant

A Slack bot that bridges your information silos (Gmail, TickTick, web) using an LLM orchestrator with a ReAct (Reasoning + Action) loop.

## Setup

### 1. Install dependencies

```bash
cd silo-bridge
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Fill in your API keys and tokens
```

### 3. Slack App Setup

1. Create a new app at https://api.slack.com/apps
2. Enable **Socket Mode** — copy the App-Level Token (`xapp-...`) → `SLACK_APP_TOKEN`
3. Under **OAuth & Permissions**, add these Bot Token Scopes:
   - `chat:write`
   - `im:history`
   - `im:read`
   - `im:write`
   - `app_mentions:read`
4. Install the app to your workspace — copy the Bot Token (`xoxb-...`) → `SLACK_BOT_TOKEN`
5. Under **Event Subscriptions**, subscribe to bot event: `message.im`

### 4. Gmail Setup

1. Create a project in Google Cloud Console
2. Enable the Gmail API
3. Create OAuth 2.0 credentials (Desktop app type)
4. Download `credentials.json` and place it in `silo-bridge/`
5. On first run, a browser window will open for authorization

### 5. TickTick Setup

1. Register an app at https://developer.ticktick.com
2. Set redirect URI to `http://127.0.0.1:8765/callback`
3. Add client ID and secret to `.env`
4. Run the OAuth flow to get `config/ticktick_token.json` (see below)

#### TickTick OAuth Flow

Run this one-time script to get your TickTick access token:

```python
import httpx, json, time
from urllib.parse import urlencode
from http.server import HTTPServer, BaseHTTPRequestHandler

CLIENT_ID = "your_client_id"
CLIENT_SECRET = "your_client_secret"
REDIRECT_URI = "http://127.0.0.1:8765/callback"

auth_url = f"https://ticktick.com/oauth/authorize?{urlencode({'client_id': CLIENT_ID, 'redirect_uri': REDIRECT_URI, 'response_type': 'code', 'scope': 'tasks:read tasks:write'})}"
print(f"Open this URL:\n{auth_url}\n")

code = None
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        nonlocal code
        from urllib.parse import urlparse, parse_qs
        code = parse_qs(urlparse(self.path).query).get("code", [None])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Done! Close this window.")

HTTPServer(("127.0.0.1", 8765), Handler).handle_request()

resp = httpx.post("https://ticktick.com/oauth/token", data={
    "grant_type": "authorization_code", "code": code,
    "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
    "redirect_uri": REDIRECT_URI,
})
token = resp.json()
token["expires_at"] = time.time() + token.get("expires_in", 3600)
with open("config/ticktick_token.json", "w") as f:
    json.dump(token, f, indent=2)
print("Token saved!")
```

### 6. Run

```bash
python main.py
```

Message the bot in a Slack DM to start using it.

## Architecture

```
User (Slack DM)
  → Slack Bot (Socket Mode, slack-bolt)
    → LLM Orchestrator (Claude, ReAct loop)
      → Tools: read_emails, get_tasks, create_task, fetch_url, read_profile
    ← Response posted back to Slack
```

## Tools

| Tool | Description |
|------|-------------|
| `read_emails` | Search Gmail (read-only) |
| `get_tasks` | Read TickTick tasks |
| `create_task` | Create TickTick task (with confirmation) |
| `fetch_url` | Fetch & extract web content (recipes, articles, jobs) |
| `read_profile` | Read local YAML profile |
