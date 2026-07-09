"""Foundry *prompt agent* wired to the Google Drive MCP server.

Uses the new Microsoft Foundry projects API (``azure-ai-projects`` 2.x):

* ``project.agents.create_version(...)`` with a ``PromptAgentDefinition``
* an ``MCPTool`` that points at the Google Drive remote MCP server and carries
  the user's Google access token as an ``Authorization`` header
* the OpenAI-compatible **Responses API** (``conversations`` + ``responses``)
  for multi-turn chat, including the MCP tool-approval round-trip.

See: https://learn.microsoft.com/azure/foundry/agents/quickstarts/prompt-agent
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

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "GoogleDriveAgent":
        self._project = AIProjectClient(
            endpoint=self.settings.project_endpoint,
            credential=DefaultAzureCredential(),
        )
        self._openai = self._project.get_openai_client()

        self._google_token = self._fresh_google_token()
        self._agent = self._create_agent_version(self._google_token)

        self._conversation = self._openai.conversations.create()
        self._log(f"MCP server: {self.settings.mcp_server_label} -> {self.settings.mcp_server_url}")
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

        The Google access token expires (~1h). We refresh it before every turn
        and, if it actually changed, publish a new agent *version* so the MCP
        tool keeps a valid ``Authorization`` header.
        """
        if self._agent is None:
            raise RuntimeError("Agent not started. Call start() first.")

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

    def _create_agent_version(self, token: str) -> Any:
        mcp_tool = MCPTool(
            server_label=self.settings.mcp_server_label,
            server_url=self.settings.mcp_server_url,
            headers={"Authorization": f"Bearer {token}"},
            require_approval=self.settings.require_approval,
            allowed_tools=self.settings.allowed_tools or None,
        )
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
        # No text came back -> surface any MCP error to make debugging easier.
        errors: List[str] = []
        for item in getattr(response, "output", []) or []:
            if getattr(item, "type", "") == "mcp_call":
                err = getattr(item, "error", None)
                if err:
                    errors.append(f"{getattr(item, 'name', 'mcp_call')}: {err}")
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
