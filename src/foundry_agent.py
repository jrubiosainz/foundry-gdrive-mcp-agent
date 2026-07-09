"""Foundry *prompt agent* wired to the Google Drive MCP server.

Uses the new Microsoft Foundry projects API (``azure-ai-projects`` 2.x):

* ``project.agents.create_version(...)`` with a ``PromptAgentDefinition``
* an ``MCPTool`` that points at the Google Drive remote MCP server. The user's
  Google OAuth access token is passed in the MCP tool ``authorization`` field.
  (Foundry rejects putting the token in ``headers`` -> error
  ``Headers that can include sensitive information are not allowed``.) You can
  instead point the tool at a Foundry **project connection** or the native
  ``connector_googledrive`` connector -- see ``MCP_PROJECT_CONNECTION_ID`` /
  ``MCP_CONNECTOR_ID`` in ``config.py``.
* the OpenAI-compatible **Responses API** (``conversations`` + ``responses``)
  for multi-turn chat, including the MCP tool-approval round-trip.

See:
* https://learn.microsoft.com/azure/foundry/agents/quickstarts/prompt-agent
* https://learn.microsoft.com/azure/foundry/agents/how-to/mcp-authentication
"""

from __future__ import annotations

from typing import Any, List, Optional

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import MCPTool, PromptAgentDefinition
from azure.identity import DefaultAzureCredential
from openai.types.responses.response_input_param import (
    McpApprovalResponse,
    ResponseInputParam,
)

from .config import Settings
from .google_auth import get_access_token

INSTRUCTIONS = (
    "You are a helpful assistant that answers questions about the user's Google "
    "Drive. Always use the Google Drive MCP tools to search for files, read "
    "metadata, and read file content before answering. Mention the file names you "
    "relied on. If you cannot find a relevant file, say so clearly instead of "
    "guessing."
)


class GoogleDriveAgent:
    """Create and drive a Foundry prompt agent that can read the user's Drive."""

    def __init__(self, settings: Settings, verbose: bool = True):
        self.settings = settings
        self.verbose = verbose
        self._project: Optional[AIProjectClient] = None
        self._openai: Any = None
        self._agent: Any = None
        self._conversation: Any = None
        self._google_token: Optional[str] = None
        # When a project connection carries the credential, we don't pass the
        # Google token inline and therefore never need to republish a version.
        self._uses_inline_token: bool = not bool(
            self.settings.mcp_project_connection_id
        )

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "GoogleDriveAgent":
        self._project = AIProjectClient(
            endpoint=self.settings.project_endpoint,
            credential=DefaultAzureCredential(),
        )
        self._openai = self._project.get_openai_client()

        if self._uses_inline_token:
            self._google_token = self._fresh_google_token()
        self._agent = self._create_agent_version(self._google_token)

        self._conversation = self._openai.conversations.create()
        self._log(
            f"MCP server: {self.settings.mcp_server_label} -> "
            f"{self._mcp_target_description()}"
        )
        self._log(f"Created conversation: {self._conversation.id}")
        return self

    def close(self) -> None:
        try:
            if self._agent is not None and self._project is not None:
                self._project.agents.delete_version(
                    agent_name=self._agent.name,
                    agent_version=self._agent.version,
                )
                self._log("Deleted agent version")
        except Exception as exc:  # best-effort cleanup
            self._log(f"(cleanup) could not delete agent version: {exc}")
        finally:
            if self._project is not None:
                self._project.close()

    def __enter__(self) -> "GoogleDriveAgent":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- conversation ------------------------------------------------------
    def ask(self, question: str) -> str:
        """Send one question and return the agent's answer.

        When the Google token is passed inline it expires (~1h). We refresh it
        before every turn and, if it actually changed, publish a new agent
        *version* so the MCP tool keeps a valid ``authorization`` token. When a
        project connection carries the credential, no refresh is needed here.
        """
        if self._agent is None:
            raise RuntimeError("Agent not started. Call start() first.")

        if self._uses_inline_token:
            token = self._fresh_google_token()
            if token != self._google_token:
                self._google_token = token
                self._agent = self._create_agent_version(token)

        response = self._openai.responses.create(
            conversation=self._conversation.id,
            input=question,
            extra_body=self._agent_reference(),
        )
        response = self._resolve_approvals(response)
        return self._extract_answer(response)

    # -- internals ---------------------------------------------------------
    def _agent_reference(self) -> dict:
        return {
            "agent_reference": {
                "name": self._agent.name,
                "type": "agent_reference",
            }
        }

    def _mcp_target_description(self) -> str:
        if self.settings.mcp_project_connection_id:
            return f"connection {self.settings.mcp_project_connection_id}"
        if self.settings.mcp_connector_id:
            return f"connector {self.settings.mcp_connector_id}"
        return self.settings.mcp_server_url

    def _build_mcp_tool(self, token: Optional[str]) -> MCPTool:
        """Build the MCP tool for one of three auth modes.

        1. project connection  -> ``project_connection_id`` (credential lives in
           the Foundry connection; token is NOT sent from here).
        2. native connector    -> ``connector_id`` (e.g. ``connector_googledrive``)
           plus the user's Google token in ``authorization``.
        3. inline (default)    -> ``server_url`` plus the user's Google token in
           ``authorization``. The token goes in the dedicated ``authorization``
           field, never in ``headers`` (Foundry blocks sensitive headers).
        """
        kwargs: dict = {
            "server_label": self.settings.mcp_server_label,
            "require_approval": self.settings.require_approval,
            "allowed_tools": self.settings.allowed_tools or None,
        }

        if self.settings.mcp_project_connection_id:
            # Sample: sample_agent_mcp_with_project_connection.py passes both.
            kwargs["server_url"] = self.settings.mcp_server_url
            kwargs["project_connection_id"] = self.settings.mcp_project_connection_id
        elif self.settings.mcp_connector_id:
            kwargs["connector_id"] = self.settings.mcp_connector_id
            if token:
                kwargs["authorization"] = token
        else:
            kwargs["server_url"] = self.settings.mcp_server_url
            if token:
                kwargs["authorization"] = token

        return MCPTool(**kwargs)

    def _create_agent_version(self, token: Optional[str]) -> Any:
        mcp_tool = self._build_mcp_tool(token)
        agent = self._project.agents.create_version(
            agent_name=self.settings.agent_name,
            definition=PromptAgentDefinition(
                model=self.settings.model_deployment_name,
                instructions=INSTRUCTIONS,
                tools=[mcp_tool],
            ),
        )
        self._log(f"Created agent version: {agent.name} (v{agent.version})")
        return agent

    def _resolve_approvals(self, response: Any) -> Any:
        """Auto-approve any MCP tool calls until the agent produces its answer."""
        while True:
            approvals: ResponseInputParam = []
            for item in getattr(response, "output", []) or []:
                if getattr(item, "type", None) == "mcp_approval_request" and getattr(
                    item, "id", None
                ):
                    name = getattr(item, "name", None) or item.id
                    self._log(f"  approving MCP tool call: {name}")
                    approvals.append(
                        McpApprovalResponse(
                            type="mcp_approval_response",
                            approve=True,
                            approval_request_id=item.id,
                        )
                    )
            if not approvals:
                return response
            response = self._openai.responses.create(
                input=approvals,
                previous_response_id=response.id,
                extra_body=self._agent_reference(),
            )

    def _extract_answer(self, response: Any) -> str:
        text = (getattr(response, "output_text", "") or "").strip()
        if text:
            return text
        # No text came back -> surface any MCP / OAuth signal to ease debugging.
        errors: List[str] = []
        for item in getattr(response, "output", []) or []:
            itype = getattr(item, "type", "") or ""
            if itype == "mcp_call":
                err = getattr(item, "error", None)
                if err:
                    errors.append(f"{getattr(item, 'name', 'mcp_call')}: {err}")
            elif itype == "oauth_consent_request":
                link = getattr(item, "consent_link", None)
                if link:
                    errors.append(
                        "OAuth consent required. Open this link to authorize, "
                        f"then ask again: {link}"
                    )
        if errors:
            return "[MCP tool error] " + " | ".join(errors)
        return "(no response)"

    def _fresh_google_token(self) -> str:
        return get_access_token(
            self.settings.google_client_secrets_file,
            self.settings.google_token_file,
            oauth_port=self.settings.google_oauth_port,
            interactive=False,
        )

    def _log(self, message: str) -> None:
        if self.verbose:
            print(message)
