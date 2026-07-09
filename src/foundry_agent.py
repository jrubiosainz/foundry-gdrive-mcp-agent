"""Foundry agent wired to the Google Drive MCP server."""

from __future__ import annotations

import time
from typing import Optional

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from azure.ai.agents.models import (
    ListSortOrder,
    McpTool,
    RequiredMcpToolCall,
    SubmitToolApprovalAction,
    ToolApproval,
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

_ACTIVE_STATUSES = ("queued", "in_progress", "requires_action")


class GoogleDriveAgent:
    """Create and drive a Foundry agent that can read the user's Google Drive."""

    def __init__(self, settings: Settings, verbose: bool = True):
        self.settings = settings
        self.verbose = verbose
        self._client: Optional[AIProjectClient] = None
        self._agents = None
        self._mcp_tool: Optional[McpTool] = None
        self._agent = None
        self._thread = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "GoogleDriveAgent":
        self._client = AIProjectClient(
            endpoint=self.settings.project_endpoint,
            credential=DefaultAzureCredential(),
        )
        self._agents = self._client.agents

        self._mcp_tool = McpTool(
            server_label=self.settings.mcp_server_label,
            server_url=self.settings.mcp_server_url,
            allowed_tools=self.settings.allowed_tools,
        )
        if self.settings.require_approval == "never":
            self._mcp_tool.set_approval_mode("never")

        self._refresh_google_token()

        self._agent = self._agents.create_agent(
            model=self.settings.model_deployment_name,
            name=self.settings.agent_name,
            instructions=INSTRUCTIONS,
            tools=self._mcp_tool.definitions,
        )
        self._log(f"Created agent: {self._agent.id}")
        self._log(f"MCP server: {self._mcp_tool.server_label} -> {self._mcp_tool.server_url}")

        self._thread = self._agents.threads.create()
        self._log(f"Created thread: {self._thread.id}")
        return self

    def close(self) -> None:
        try:
            if self._agent is not None and self._agents is not None:
                self._agents.delete_agent(self._agent.id)
                self._log("Deleted agent")
        finally:
            if self._client is not None:
                self._client.close()

    def __enter__(self) -> "GoogleDriveAgent":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- conversation ------------------------------------------------------
    def ask(self, question: str) -> str:
        """Send one question and return the agent's answer (same thread = memory)."""
        if self._agent is None:
            raise RuntimeError("Agent not started. Call start() first.")

        # Refresh the Google token before every turn so long sessions don't
        # break when the ~1h access token expires.
        self._refresh_google_token()

        self._agents.messages.create(
            thread_id=self._thread.id, role="user", content=question
        )
        run = self._agents.runs.create(
            thread_id=self._thread.id,
            agent_id=self._agent.id,
            tool_resources=self._mcp_tool.resources,
        )

        while run.status in _ACTIVE_STATUSES:
            time.sleep(1)
            run = self._agents.runs.get(thread_id=self._thread.id, run_id=run.id)
            if run.status == "requires_action" and isinstance(
                run.required_action, SubmitToolApprovalAction
            ):
                self._approve_tool_calls(run)

        if run.status == "failed":
            return f"[run failed] {run.last_error}"

        return self._latest_answer()

    # -- internals ---------------------------------------------------------
    def _refresh_google_token(self) -> None:
        token = get_access_token(
            self.settings.google_client_secrets_file,
            self.settings.google_token_file,
            oauth_port=self.settings.google_oauth_port,
            interactive=False,
        )
        self._mcp_tool.update_headers("Authorization", f"Bearer {token}")

    def _approve_tool_calls(self, run) -> None:
        tool_calls = run.required_action.submit_tool_approval.tool_calls or []
        if not tool_calls:
            self._agents.runs.cancel(thread_id=self._thread.id, run_id=run.id)
            return

        approvals = []
        for call in tool_calls:
            if isinstance(call, RequiredMcpToolCall):
                self._log(f"  approving MCP tool call: {getattr(call, 'name', call.id)}")
                approvals.append(
                    ToolApproval(
                        tool_call_id=call.id,
                        approve=True,
                        headers=self._mcp_tool.headers,
                    )
                )
        if approvals:
            self._agents.runs.submit_tool_outputs(
                thread_id=self._thread.id, run_id=run.id, tool_approvals=approvals
            )

    def _latest_answer(self) -> str:
        messages = self._agents.messages.list(
            thread_id=self._thread.id, order=ListSortOrder.ASCENDING
        )
        answer = ""
        for msg in messages:
            # Capture the most recent non-user (assistant/agent) text message.
            if msg.text_messages and msg.role != "user":
                answer = msg.text_messages[-1].text.value
        return answer or "(no response)"

    def _log(self, message: str) -> None:
        if self.verbose:
            print(message)
