"""One-time Gmail OAuth setup — run this before starting the bot.

Opens a browser for Google authorization and saves the token to
config/gmail_token.json. The bot itself never does interactive auth (it may
run headless), so re-run this script whenever the token becomes invalid.
"""

import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

REPO_ROOT = Path(__file__).resolve().parent
CREDENTIALS_PATH = REPO_ROOT / "credentials.json"
TOKEN_PATH = REPO_ROOT / "config" / "gmail_token.json"


def main() -> None:
    if not CREDENTIALS_PATH.exists():
        print(f"ERROR: credentials.json not found at {CREDENTIALS_PATH}")
        print("Download OAuth 2.0 credentials (Desktop app) from Google Cloud Console.")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    creds = flow.run_local_server(port=8080)

    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json())
    print(f"✓ Gmail token saved to {TOKEN_PATH}")


if __name__ == "__main__":
    main()
