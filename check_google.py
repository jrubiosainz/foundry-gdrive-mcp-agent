"""Diagnostic helper: verify the cached Google token can reach Google Drive.

This talks to the standard Drive REST API directly (not the MCP server) so you
can confirm your OAuth setup and scopes are correct before wiring up the agent.

    python check_google.py
"""

from __future__ import annotations

import os

import requests
from dotenv import load_dotenv

from src.google_auth import get_access_token

load_dotenv()

DRIVE_ABOUT = "https://www.googleapis.com/drive/v3/about"
DRIVE_FILES = "https://www.googleapis.com/drive/v3/files"


def main() -> None:
    token = get_access_token(
        os.environ.get("GOOGLE_OAUTH_CLIENT_SECRETS", "credentials.json"),
        os.environ.get("GOOGLE_TOKEN_PATH", "token.json"),
        oauth_port=int(os.environ.get("GOOGLE_OAUTH_PORT", "0")),
        interactive=True,
    )
    headers = {"Authorization": f"Bearer {token}"}

    about = requests.get(
        DRIVE_ABOUT, params={"fields": "user,storageQuota"}, headers=headers, timeout=30
    )
    print(f"[about]  HTTP {about.status_code}")
    print(about.text[:600])

    files = requests.get(
        DRIVE_FILES,
        params={"pageSize": 5, "fields": "files(id,name,mimeType,modifiedTime)"},
        headers=headers,
        timeout=30,
    )
    print(f"\n[files]  HTTP {files.status_code}")
    print(files.text[:800])

    if about.ok and files.ok:
        print("\nOK: your Google token works and can read Drive. You're ready to run the agent.")
    else:
        print("\nFAILED: check that the Drive API is enabled and the scopes were granted.")


if __name__ == "__main__":
    main()
