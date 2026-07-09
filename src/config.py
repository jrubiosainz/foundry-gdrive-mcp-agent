"""Configuration loaded from environment variables / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

# Load variables from a local .env file if present. Real env vars win.
load_dotenv()

# Official Google Drive remote MCP server endpoint (Developer Preview).
# https://developers.google.com/workspace/drive/api/guides/configure-mcp-server
DEFAULT_MCP_SERVER_URL = "https://drivemcp.googleapis.com/mcp/v1"
DEFAULT_MCP_SERVER_LABEL = "google_drive"

# Scopes required by the Google Drive MCP server.
#   drive.readonly -> read/search every file the user can see (needed to answer
#                     questions about existing documents).
#   drive.file     -> create files and manage files opened/created by this app.
DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
]


@dataclass
class Settings:
    """Runtime configuration for the agent."""

    project_endpoint: str
    model_deployment_name: str
    mcp_server_url: str = DEFAULT_MCP_SERVER_URL
    mcp_server_label: str = DEFAULT_MCP_SERVER_LABEL
    google_client_secrets_file: str = "credentials.json"
    google_token_file: str = "token.json"
    google_oauth_port: int = 0
    require_approval: str = "always"
    agent_name: str = "gdrive-mcp-agent"
    allowed_tools: List[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Settings":
        endpoint = os.environ.get("PROJECT_ENDPOINT", "").strip()
        model = os.environ.get("MODEL_DEPLOYMENT_NAME", "").strip()

        missing = [
            name
            for name, value in (
                ("PROJECT_ENDPOINT", endpoint),
                ("MODEL_DEPLOYMENT_NAME", model),
            )
            if not value
        ]
        if missing:
            raise SystemExit(
                "Missing required environment variables: "
                f"{', '.join(missing)}.\nCopy .env.example to .env and fill it in."
            )

        allowed_raw = os.environ.get("MCP_ALLOWED_TOOLS", "").strip()
        allowed_tools = (
            [t.strip() for t in allowed_raw.split(",") if t.strip()] if allowed_raw else []
        )

        return cls(
            project_endpoint=endpoint,
            model_deployment_name=model,
            mcp_server_url=os.environ.get("MCP_SERVER_URL", DEFAULT_MCP_SERVER_URL).strip(),
            mcp_server_label=os.environ.get(
                "MCP_SERVER_LABEL", DEFAULT_MCP_SERVER_LABEL
            ).strip(),
            google_client_secrets_file=os.environ.get(
                "GOOGLE_OAUTH_CLIENT_SECRETS", "credentials.json"
            ).strip(),
            google_token_file=os.environ.get("GOOGLE_TOKEN_PATH", "token.json").strip(),
            google_oauth_port=int(os.environ.get("GOOGLE_OAUTH_PORT", "0")),
            require_approval=os.environ.get("MCP_REQUIRE_APPROVAL", "always").strip().lower(),
            agent_name=os.environ.get("AGENT_NAME", "gdrive-mcp-agent").strip(),
            allowed_tools=allowed_tools,
        )
