import httpx
import json
import time
import os
import sys
from urllib.parse import urlencode, urlparse, parse_qs
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Get credentials from environment variables
CLIENT_ID = os.getenv("TICKTICK_CLIENT_ID")
CLIENT_SECRET = os.getenv("TICKTICK_CLIENT_SECRET")
REDIRECT_URI = os.getenv("TICKTICK_REDIRECT_URI", "http://127.0.0.1:8765/callback")

# Validate required environment variables
if not CLIENT_ID or not CLIENT_SECRET:
    print("ERROR: Missing required environment variables!")
    print("Please set TICKTICK_CLIENT_ID and TICKTICK_CLIENT_SECRET in your .env file")
    sys.exit(1)

if CLIENT_ID == "..." or CLIENT_SECRET == "...":
    print("ERROR: Please update your .env file with actual TickTick credentials")
    sys.exit(1)

# Build authorization URL
auth_url = f"https://ticktick.com/oauth/authorize?{urlencode({
    'client_id': CLIENT_ID,
    'redirect_uri': REDIRECT_URI,
    'response_type': 'code',
    'scope': 'tasks:read tasks:write'
})}"

print(f"Open this URL in your browser:\n{auth_url}\n")
print("Waiting for authorization callback...")

code = None

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        global code
        code = parse_qs(urlparse(self.path).query).get("code", [None])[0]
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        if code:
            self.wfile.write(b"<h1>Authorization successful!</h1><p>You can close this window.</p>")
        else:
            self.wfile.write(b"<h1>Authorization failed!</h1><p>No code received.</p>")

    def log_message(self, format, *args):
        # Suppress server logs (intentionally not using format/args)
        pass

try:
    HTTPServer(("127.0.0.1", 8765), Handler).handle_request()
except Exception as e:
    print(f"ERROR: Failed to start callback server: {e}")
    sys.exit(1)

# Validate authorization code was received
if not code:
    print("ERROR: No authorization code received. Authorization may have been cancelled.")
    sys.exit(1)

print("Authorization code received, exchanging for access token...")

# Exchange authorization code for access token
try:
    resp = httpx.post("https://ticktick.com/oauth/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
    }, timeout=30.0)

    resp.raise_for_status()

except httpx.HTTPError as e:
    print(f"ERROR: Failed to exchange authorization code for token: {e}")
    if hasattr(e, 'response') and e.response is not None:
        print(f"Response: {e.response.text}")
    sys.exit(1)

# Parse token response
try:
    token = resp.json()

    if "access_token" not in token:
        print(f"ERROR: Invalid token response - missing access_token")
        print(f"Response: {token}")
        sys.exit(1)

    # Add expiration timestamp
    token["expires_at"] = time.time() + token.get("expires_in", 3600)

except json.JSONDecodeError as e:
    print(f"ERROR: Failed to parse token response as JSON: {e}")
    print(f"Response text: {resp.text}")
    sys.exit(1)

# Save token to file
token_path = "config/ticktick_token.json"
try:
    os.makedirs(os.path.dirname(token_path), exist_ok=True)
    with open(token_path, "w") as f:
        json.dump(token, f, indent=2)
    print(f"✓ Token saved successfully to {token_path}")
    print(f"Access token expires in {token.get('expires_in', 'unknown')} seconds")

except Exception as e:
    print(f"ERROR: Failed to save token to file: {e}")
    sys.exit(1)