"""Foundry *prompt agent* wired to our self-hosted Google Drive MCP server.

Uses the new Microsoft Foundry projects API (``azure-ai-projects`` 2.x):

* ``project.agents.create_version(...)`` with a ``PromptAgentDefinition``
* an ``MCPTool`` that points at our Azure App Service MCP server
  (``https://<app>.azurewebsites.net/mcp?key=<secret>``). The Google identity
  lives **server-side** in the App Service (a stored refresh token), so no Google
  token is ever sent from here and the agent works identically from the local SDK
  and from the Foundry web portal.
* the OpenAI-compatible **Responses API** (``conversations`` + ``responses``)
  for multi-turn chat.

The created agent version is **persisted** (not deleted on exit) so you can open
it in the Foundry portal and chat with it there.

See:
* https://learn.microsoft.com/azure/foundry/agents/quickstarts/prompt-agent
* https://learn.microsoft.com/azure/foundry/agents/how-to/tools/model-context-protocol
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

INSTRUCTIONS = (
    "You are a helpful assistant that answers questions about the user's Google "
    "Drive. Use the Google Drive MCP tools to do your work: 'search_files' to find "
    "files by keyword, 'list_files' to list recent files, and 'get_file_content' to "
    "read a file's text (pass the file_id you got from a search, or a name). Always "
    "consult these tools before answering and mention the file names you relied on. "
    "If you cannot find a relevant file, say so clearly instead of guessing. Answer "
    "in the same language the user writes in."
)


class GoogleDriveAgent:
    """Create and drive a Foundry prompt agent that can read the user's Drive."""

    def __init__(self, settings: Settings, verbose: bool = True, persist: bool = True):
        self.settings = settings
        self.verbose = verbose
        # Persist the agent version by default so it stays available in the portal.
        self.persist = persist
        self._project: Optional[AIProjectClient] = None
        self._openai: Any = None
        self._agent: Any = None
        self._conversation: Any = None

    # -- lifecycle ---------------------------------------------------------
    def _connect(self) -> None:
        if self._project is None:
            self._project = AIProjectClient(
                endpoint=self.settings.project_endpoint,
                credential=DefaultAzureCredential(),
            )
            self._openai = self._project.get_openai_client()

    def create_agent(self) -> Any:
        """Create (and persist) the agent version. Returns the agent handle."""
        self._connect()
        self._agent = self._create_agent_version()
        self._log(
            f"MCP server: {self.settings.mcp_server_label} -> "
            f"{self._mcp_target_description()}"
        )
        return self._agent

    def start(self) -> "GoogleDriveAgent":
        self.create_agent()
        self._conversation = self._openai.conversations.create()
        self._log(f"Created conversation: {self._conversation.id}")
        return self

    def close(self) -> None:
        try:
            if not self.persist and self._agent is not None and self._project is not None:
                self._project.agents.delete_version(
                    agent_name=self._agent.name,
                    agent_version=self._agent.version,
                )
                self._log("Deleted agent version")
            elif self._agent is not None:
                self._log(
                    f"Agent '{self._agent.name}' (v{self._agent.version}) persisted "
                    "— open it in the Foundry portal to chat there."
                )
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
        """Send one question and return the agent's answer."""
        if self._agent is None:
            raise RuntimeError("Agent not started. Call start() first.")
        if self._conversation is None:
            self._conversation = self._openai.conversations.create()

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

    def _build_mcp_tool(self) -> MCPTool:
        """Build the MCP tool.

        The self-hosted server authenticates the caller with a shared secret in
        the ``server_url`` itself, so no Google token/authorization is sent from
        here. Two optional advanced modes are still supported via config:

        * project connection  -> ``project_connection_id`` (credential in a
          Foundry connection).
        * native connector     -> ``connector_id``.
        """
        if not (
            self.settings.mcp_server_url
            or self.settings.mcp_project_connection_id
            or self.settings.mcp_connector_id
        ):
            raise SystemExit(
                "No MCP target configured. Set MCP_SERVER_URL in your .env to your "
                "App Service MCP URL, e.g. "
                "https://<app>.azurewebsites.net/mcp?key=<shared-secret>"
            )

        kwargs: dict = {
            "server_label": self.settings.mcp_server_label,
            "require_approval": self.settings.require_approval,
            "allowed_tools": self.settings.allowed_tools or None,
        }

        if self.settings.mcp_project_connection_id:
            kwargs["server_url"] = self.settings.mcp_server_url
            kwargs["project_connection_id"] = self.settings.mcp_project_connection_id
        elif self.settings.mcp_connector_id:
            kwargs["connector_id"] = self.settings.mcp_connector_id
        else:
            kwargs["server_url"] = self.settings.mcp_server_url

        return MCPTool(**kwargs)

    def _create_agent_version(self) -> Any:
        mcp_tool = self._build_mcp_tool()
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
        """Auto-approve any MCP tool calls until the agent produces its answer.

        With ``require_approval='never'`` this is a no-op safety net.
        """
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
        # No text came back -> surface any MCP signal to ease debugging.
        errors: List[str] = []
        for item in getattr(response, "output", []) or []:
            itype = getattr(item, "type", "") or ""
            if itype == "mcp_call":
                err = getattr(item, "error", None)
                if err:
                    errors.append(f"{getattr(item, 'name', 'mcp_call')}: {err}")
        if errors:
            return "[MCP tool error] " + " | ".join(errors)
        return "(no response)"

    def _log(self, message: str) -> None:
        if self.verbose:
            print(message)
