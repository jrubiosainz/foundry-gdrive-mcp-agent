"""Google OAuth 2.0 helper.

The Google Drive MCP server authenticates with standard Google OAuth 2.0 access
tokens. Foundry's MCP tool does not perform the interactive OAuth handshake for
you, so this module runs the user consent flow locally, caches the resulting
credentials (including the refresh token), and hands a fresh bearer token to the
agent whenever it needs one.
"""

from __future__ import annotations

import os
from typing import List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .config import DRIVE_SCOPES


def _save(creds: Credentials, token_file: str) -> None:
    with open(token_file, "w", encoding="utf-8") as handle:
        handle.write(creds.to_json())


def load_credentials(
    client_secrets_file: str,
    token_file: str,
    scopes: Optional[List[str]] = None,
    oauth_port: int = 0,
    interactive: bool = True,
) -> Credentials:
    """Return valid Google credentials, running the consent flow if needed.

    Args:
        client_secrets_file: Path to the OAuth client secrets JSON downloaded from
            Google Cloud Console (a "Desktop app" client works out of the box).
        token_file: Where cached credentials are stored/read.
        scopes: OAuth scopes to request. Defaults to the Drive MCP scopes.
        oauth_port: Local callback port (0 = pick a free port).
        interactive: When False, never open a browser; refresh or fail instead.
    """
    scopes = scopes or DRIVE_SCOPES
    creds: Optional[Credentials] = None

    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, scopes)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save(creds, token_file)
        return creds

    if not interactive:
        raise RuntimeError(
            "No valid Google credentials are cached. Run `python main.py auth` first."
        )

    if not os.path.exists(client_secrets_file):
        raise FileNotFoundError(
            f"OAuth client secrets file not found: '{client_secrets_file}'.\n"
            "Download it from Google Cloud Console: APIs & Services > Credentials > "
            "your OAuth 2.0 Client ID > Download JSON."
        )

    flow = InstalledAppFlow.from_client_secrets_file(client_secrets_file, scopes)
    creds = flow.run_local_server(port=oauth_port, prompt="consent")
    _save(creds, token_file)
    return creds


def get_access_token(
    client_secrets_file: str,
    token_file: str,
    scopes: Optional[List[str]] = None,
    oauth_port: int = 0,
    interactive: bool = True,
) -> str:
    """Return a valid Google OAuth access token, refreshing if necessary."""
    creds = load_credentials(client_secrets_file, token_file, scopes, oauth_port, interactive)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        _save(creds, token_file)
    return creds.token
