"""Direct probe of the Google Drive remote MCP server (bypasses Foundry).

This performs the MCP "Streamable HTTP" handshake straight against
``https://drivemcp.googleapis.com/mcp/v1`` using your cached Google OAuth token,
then lists tools and calls ``search_files``. Use it to tell whether an error
like *"The caller does not have permission"* comes from **Google** (your Cloud
project / API enablement / OAuth setup) or from **Foundry** (token forwarding).

    python check_drivemcp.py                 # default broad search
    python check_drivemcp.py "viaje japon"   # search for a specific term

If this script reproduces the same error, the problem is on the Google side and
Foundry is off the hook -- fix the Cloud project setup (see the hints printed at
the end). If this script works but the agent doesn't, the issue is how Foundry
forwards the token.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

from src.config import DEFAULT_MCP_SERVER_URL
from src.google_auth import get_access_token

load_dotenv()


class MCPError(RuntimeError):
    pass


class DriveMCPClient:
    """Minimal MCP Streamable-HTTP client, just enough to probe Drive."""

    def __init__(self, url: str, token: str, quota_project: Optional[str] = None):
        self.url = url
        self.token = token
        self.quota_project = quota_project
        self.session = requests.Session()
        self.mcp_session_id: Optional[str] = None
        self.protocol_version: str = "2025-06-18"

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.quota_project:
            headers["X-Goog-User-Project"] = self.quota_project
        if self.mcp_session_id:
            headers["Mcp-Session-Id"] = self.mcp_session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    def _post(self, payload: Dict[str, Any]) -> requests.Response:
        return self.session.post(
            self.url, headers=self._headers(), data=json.dumps(payload), timeout=60
        )

    @staticmethod
    def _parse(resp: requests.Response) -> List[Dict[str, Any]]:
        ctype = resp.headers.get("Content-Type", "")
        if "text/event-stream" in ctype:
            messages: List[Dict[str, Any]] = []
            for line in resp.text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    data = line[len("data:"):].strip()
                    if data and data != "[DONE]":
                        try:
                            messages.append(json.loads(data))
                        except json.JSONDecodeError:
                            pass
            return messages
        if not resp.text.strip():
            return []
        try:
            return [resp.json()]
        except json.JSONDecodeError:
            return []

    def _rpc(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method}
        if params is not None:
            payload["params"] = params
        resp = self._post(payload)
        print(f"  -> {method}: HTTP {resp.status_code} ({resp.headers.get('Content-Type', '?')})")

        # Capture the session id handed back on initialize.
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self.mcp_session_id = sid

        if resp.status_code >= 400:
            raise MCPError(f"{method} failed: HTTP {resp.status_code}\n{resp.text[:1200]}")

        messages = self._parse(resp)
        for msg in messages:
            if isinstance(msg, dict) and msg.get("error"):
                raise MCPError(f"{method} JSON-RPC error: {json.dumps(msg['error'], indent=2)}")
        for msg in messages:
            if isinstance(msg, dict) and "result" in msg:
                return msg["result"]
        return {}

    def _notify(self, method: str) -> None:
        resp = self._post({"jsonrpc": "2.0", "method": method})
        print(f"  -> {method} (notification): HTTP {resp.status_code}")

    # -- high-level steps --------------------------------------------------
    def initialize(self) -> Dict[str, Any]:
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "check-drivemcp", "version": "1.0"},
            },
        )
        server_proto = result.get("protocolVersion")
        if server_proto:
            self.protocol_version = server_proto
        self._notify("notifications/initialized")
        return result

    def list_tools(self) -> List[Dict[str, Any]]:
        return self._rpc("tools/list").get("tools", [])

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return self._rpc("tools/call", {"name": name, "arguments": arguments})


def _build_search_args(tool: Optional[Dict[str, Any]], query: str) -> Dict[str, Any]:
    """Fit the query into whatever the search_files input schema expects."""
    if not tool:
        return {"query": query}
    schema = tool.get("inputSchema") or tool.get("input_schema") or {}
    props: Dict[str, Any] = schema.get("properties", {}) or {}
    required = schema.get("required", []) or []

    # Prefer an obvious query-like property.
    for candidate in ("query", "q", "name", "searchQuery", "text"):
        if candidate in props:
            return {candidate: query}
    # Otherwise fill the first required string property.
    for key in required:
        if props.get(key, {}).get("type") == "string":
            return {key: query}
    return {"query": query}


def _read_client_info(path: str) -> Dict[str, str]:
    """Extract OAuth client type / project / client_id from credentials.json."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    # Desktop clients live under "installed", web clients under "web".
    if "installed" in data:
        block, kind = data["installed"], "Desktop app (installed)"
    elif "web" in data:
        block, kind = data["web"], "Web application"
    else:
        block, kind = {}, "unknown"
    client_id = block.get("client_id", "")
    # A Google client_id is "<project_number>-<random>.apps.googleusercontent.com".
    project_number = client_id.split("-", 1)[0] if "-" in client_id else ""
    return {
        "type": kind,
        "project_id": block.get("project_id", ""),
        "project_number": project_number,
        "client_id": client_id,
    }


def _tokeninfo(token: str) -> Dict[str, Any]:
    """Ask Google exactly what this access token carries.

    Returns the parsed tokeninfo document (scopes, azp/aud = the OAuth client
    that minted it, the account email, and expiry). This is the ground truth:
    if ``drive.readonly`` is missing here, the consent didn't actually grant it,
    no matter what the Data Access page shows.
    """
    try:
        resp = requests.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"access_token": token},
            timeout=30,
        )
    except requests.RequestException as exc:  # pragma: no cover - network only
        print(f"  tokeninfo: request failed ({exc})")
        return {}
    if resp.status_code != 200:
        print(f"  tokeninfo: HTTP {resp.status_code} {resp.text[:200]}")
        return {}
    info = resp.json()
    scopes = (info.get("scope") or "").split()
    print("Token introspection (Google tokeninfo):")
    print(f"  client (azp): {info.get('azp', '?')}")
    if info.get("email"):
        print(f"  account:      {info.get('email')}")
    print(f"  expires_in:   {info.get('expires_in', '?')} s")
    print("  scopes:")
    for scope in scopes:
        print(f"    - {scope}")
    for needed in (
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/drive.file",
    ):
        mark = "OK " if needed in scopes else "MISSING"
        print(f"  [{mark}] {needed}")
    print()
    return info


def _try_tool(client: "DriveMCPClient", name: str, args: Dict[str, Any]) -> bool:
    """Call one tool and print a compact pass/fail line. Returns True on success."""
    print(f"      tools/call {name}  args={json.dumps(args)}")
    try:
        result = client.call_tool(name, args)
    except MCPError as exc:
        print(f"        -> transport error: {exc}")
        return False
    blocks = [
        b.get("text", "")
        for b in result.get("content", [])
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    if result.get("isError"):
        print("        -> DENIED: " + (" ".join(blocks) or json.dumps(result)[:400]))
        return False
    print("        -> OK: " + (" ".join(blocks)[:400] or json.dumps(result)[:400]))
    return True


def _probe_raw_drive_api(token: str, quota_project: Optional[str] = None) -> None:
    """Call the standard Drive API v3 directly with the same token (no MCP).

    This is the decisive isolation test:
      * raw Drive WORKS but MCP does not -> the block is specific to the Drive
        MCP server (restricted preview / your project not enrolled). We should
        pivot off drivemcp and read Drive directly or via a Foundry connector.
      * raw Drive is ALSO denied         -> the token/account/project cannot read
        Drive at all (Workspace admin policy, or the Drive API call is attributed
        to a project where it is not enabled). Fix that before anything else.

    ``about.get`` also surfaces the account behind the token, so we learn whether
    it is a personal @gmail.com or an org-managed Workspace account.
    """
    print("Direct Drive API v3 probe (bypasses the MCP server entirely):")
    headers = {"Authorization": "Bearer " + token}
    if quota_project:
        headers["X-Goog-User-Project"] = quota_project

    try:
        about = requests.get(
            "https://www.googleapis.com/drive/v3/about",
            params={"fields": "user(displayName,emailAddress)"},
            headers=headers,
            timeout=30,
        )
        if about.status_code == 200:
            user = about.json().get("user", {})
            print(
                f"  about.get:  HTTP 200  account: "
                f"{user.get('emailAddress', '?')} ({user.get('displayName', '')})"
            )
        else:
            print(f"  about.get:  HTTP {about.status_code}  {about.text[:400]}")
    except requests.RequestException as exc:  # pragma: no cover - network only
        print(f"  about.get:  request failed ({exc})")

    try:
        files = requests.get(
            "https://www.googleapis.com/drive/v3/files",
            params={"pageSize": 5, "fields": "files(id,name)"},
            headers=headers,
            timeout=30,
        )
        if files.status_code == 200:
            names = [f.get("name", "?") for f in files.json().get("files", [])]
            print(
                f"  files.list: HTTP 200  {len(names)} file(s): "
                f"{', '.join(names) or '(none)'}"
            )
            print(
                "  => RAW DRIVE API WORKS. Your token/account CAN read Drive, so the\n"
                "     denial is specific to the Drive MCP server (drivemcp.googleapis.com)."
            )
        else:
            print(f"  files.list: HTTP {files.status_code}  {files.text[:600]}")
            print(
                "  => RAW DRIVE API ALSO DENIED. The token/account/project cannot read\n"
                "     Drive at all -- fix this (Workspace admin app access, or the Drive\n"
                "     API's quota project) before worrying about the MCP server."
            )
    except requests.RequestException as exc:  # pragma: no cover - network only
        print(f"  files.list: request failed ({exc})")
    print()


def main() -> None:
    query = sys.argv[1] if len(sys.argv) > 1 else "report"
    url = os.environ.get("MCP_SERVER_URL", DEFAULT_MCP_SERVER_URL).strip()
    creds_path = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRETS", "credentials.json")

    print(f"Google Drive MCP direct probe\n  endpoint: {url}\n  query:    {query!r}\n")

    client_info = _read_client_info(creds_path)
    if client_info:
        print("OAuth client (from %s):" % creds_path)
        print(f"  type:           {client_info.get('type', '?')}")
        print(f"  project_id:     {client_info.get('project_id') or '(missing from JSON)'}")
        print(f"  project_number: {client_info.get('project_number') or '?'}  (from client_id)")
        cid = client_info.get("client_id", "")
        print(f"  client_id:      {cid[:32]}… " if cid else "  client_id:      ?")
        if "Web application" not in client_info.get("type", ""):
            print("  NOTE: Google documents ONLY 'Web application' OAuth clients for Drive MCP.")
            print("        A Desktop client can list tools but is likely rejected on data calls.")
        print()

    token = get_access_token(
        creds_path,
        os.environ.get("GOOGLE_TOKEN_PATH", "token.json"),
        oauth_port=int(os.environ.get("GOOGLE_OAUTH_PORT", "0")),
        interactive=True,
    )
    print("Got Google access token (…%s).\n" % token[-6:])

    _tokeninfo(token)
    _probe_raw_drive_api(token)

    client = DriveMCPClient(url, token)

    print("[1/3] initialize")
    info = client.initialize()
    server_info = info.get("serverInfo", {})
    print(f"      server: {server_info.get('name', '?')} {server_info.get('version', '')}")
    print(f"      protocol: {client.protocol_version}")
    if client.mcp_session_id:
        print(f"      session: {client.mcp_session_id}")

    print("\n[2/3] tools/list")
    tools = client.list_tools()
    print("      tools:", ", ".join(t.get("name", "?") for t in tools) or "(none)")
    search_tool = next((t for t in tools if t.get("name") == "search_files"), None)

    print("\n[3/3] tools/call search_files")
    args = _build_search_args(search_tool, query)
    print(f"      arguments: {json.dumps(args)}")
    result = client.call_tool("search_files", args)
    text_blocks = [
        block.get("text", "")
        for block in result.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    if result.get("isError"):
        print("      RESULT: tool reported an error:")
        print("      " + ("\n      ".join(text_blocks) or json.dumps(result)[:1000]))
        print("      raw result JSON:")
        print("      " + json.dumps(result)[:1200])

        # Is the WHOLE data plane denied, or only search_files? list_recent_files
        # needs no query and exercises a different code path. If it also fails,
        # the denial is account/project/preview-wide (not a search-specific or
        # scope-specific problem).
        print("\n      Comparison: trying other data-plane tools with the same token")
        _try_tool(client, "list_recent_files", {})
        any_ok = _try_tool(client, "list_recent_files", {"pageSize": 5})

        # The token was accepted for initialize + tools/list, so this is a
        # data-plane authorization denial. A common cause is a missing quota
        # project. Retry once with X-Goog-User-Project to test that theory.
        project_id = ""
        if client_info:
            project_id = client_info.get("project_id") or client_info.get("project_number") or ""
        if project_id:
            print(
                f"\n      Retrying with quota project header "
                f"X-Goog-User-Project: {project_id} ..."
            )
            client.quota_project = project_id
            client.mcp_session_id = None  # force a fresh session for the retry
            client.initialize()
            retry = client.call_tool("search_files", args)
            retry_blocks = [
                b.get("text", "")
                for b in retry.get("content", [])
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            if retry.get("isError"):
                print("      Still denied WITH quota project header:")
                print("      " + ("\n      ".join(retry_blocks) or json.dumps(retry)[:800]))
                print(
                    "      => Not a quota-project issue. Most likely the Drive API isn't"
                    " enabled in this project, or the OAuth client type is rejected."
                )
            else:
                print("      SUCCESS once X-Goog-User-Project was sent!")
                print("      " + ("\n      ".join(retry_blocks) or json.dumps(retry)[:800]))
                print(
                    "      => It's a QUOTA-PROJECT issue. The raw user token had no quota"
                    " project attached. Foundry can't send this header, so the fix is to"
                    " use a Foundry connection (MCP_PROJECT_CONNECTION_ID) or the native"
                    " connector_googledrive, OR ensure the Drive API is enabled in the"
                    " OAuth client's own project so it becomes the default quota project."
                )
        raise MCPError("search_files returned isError=true")
    print("      RESULT:")
    print("      " + ("\n      ".join(text_blocks) or json.dumps(result, indent=2)[:1500]))

    print("\nSUCCESS: the Google Drive MCP server works with your token directly.")
    print("If the Foundry agent still fails, the problem is how Foundry forwards the token,")
    print("not your Google setup. Consider MCP_PROJECT_CONNECTION_ID or connector_googledrive.")


def _hint(exc: Exception) -> None:
    text = str(exc)
    print("\n" + "=" * 74)
    print("PROBE FAILED:")
    print(text[:1500])
    print("=" * 74)
    low = text.lower()
    if "caller does not have permission" in low or "permission_denied" in low or "403" in low:
        print(
            "\nThis is the SAME class of error the agent hit, reproduced WITHOUT Foundry,\n"
            "so it's a Google Cloud setup issue. Check, in the SAME project that owns your\n"
            "OAuth client:\n"
            "  1. Enable the MCP service (separate from the Drive API):\n"
            "       https://console.cloud.google.com/flows/enableapi?apiid=drivemcp.googleapis.com\n"
            "     and the Drive API:\n"
            "       https://console.cloud.google.com/flows/enableapi?apiid=drive.googleapis.com\n"
            "  2. Make sure your OAuth client and BOTH enabled APIs live in the SAME project\n"
            "     (a token minted by a client in project A can't use APIs enabled only in B).\n"
            "  3. Google's docs only document *Web application* OAuth clients for Drive MCP.\n"
            "     If yours is a Desktop app, create a Web application client, add redirect URI\n"
            "     http://localhost:8765/ , set GOOGLE_OAUTH_PORT=8765, re-download credentials.json,\n"
            "     delete token.json, and run `python main.py auth` again.\n"
            "  4. After changing anything above, delete token.json and re-consent so a fresh\n"
            "     token is minted."
        )
    elif "has not been used" in low or "service_disabled" in low or "it is disabled" in low:
        print(
            "\nThe API is disabled for your project. Enable it here (same project as your\n"
            "OAuth client), wait ~1 minute, then retry:\n"
            "  https://console.cloud.google.com/flows/enableapi?apiid=drivemcp.googleapis.com\n"
            "  https://console.cloud.google.com/flows/enableapi?apiid=drive.googleapis.com"
        )
    elif "invalid_grant" in low or "401" in low or "unauthorized" in low or "invalid authentication" in low:
        print(
            "\nThe token was rejected. Delete token.json and run `python main.py auth` to\n"
            "mint a fresh one, then retry."
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - diagnostic: show everything
        _hint(exc)
        sys.exit(1)
