"""Framework-agnostic Google Drive MCP core.

This module implements the MCP (Model Context Protocol) JSON-RPC surface plus the
Google Drive API v3 calls behind it. It has **no** web-framework or Azure
dependency, so it can be hosted from Flask (App Service) or tested directly.
See ``app.py`` for the HTTP wrapper.

Auth model
----------
* Client -> this server:  a shared secret (validated by the HTTP wrapper).
* This server -> Google:   a long-lived **refresh token** stored in app settings
  (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN), exchanged for
  short-lived access tokens on demand.

Because the Google identity lives server-side, the agent works identically from the
local SDK and from the Foundry web portal -- no localhost, no per-user consent.

Tools exposed: ``search_files``, ``list_files``, ``get_file_content``.
"""

from __future__ import annotations

import io
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

import requests

# MCP protocol version we advertise (matches Google's hosted server).
PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "gdrive-mcp-appservice", "version": "1.0.0"}

DRIVE_API = "https://www.googleapis.com/drive/v3"
TOKEN_URI = os.environ.get("GOOGLE_TOKEN_URI", "https://oauth2.googleapis.com/token")
MAX_CONTENT_CHARS = int(os.environ.get("MAX_CONTENT_CHARS", "12000"))

# Simple in-process access-token cache: token + expiry epoch.
_token_cache: Dict[str, Any] = {"token": None, "exp": 0.0}


# ---------------------------------------------------------------------------
# Google auth + Drive helpers
# ---------------------------------------------------------------------------
class DriveError(RuntimeError):
    """Raised when a Drive/Google call fails; message is surfaced to the agent."""


def _google_access_token() -> str:
    """Return a valid Google access token, refreshing via the stored refresh token."""
    now = time.time()
    if _token_cache["token"] and now < _token_cache["exp"] - 60:
        return _token_cache["token"]

    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
    missing = [
        n
        for n, v in (
            ("GOOGLE_CLIENT_ID", client_id),
            ("GOOGLE_CLIENT_SECRET", client_secret),
            ("GOOGLE_REFRESH_TOKEN", refresh_token),
        )
        if not v
    ]
    if missing:
        raise DriveError(
            "The server is missing Google credentials app settings: " + ", ".join(missing)
        )

    resp = requests.post(
        TOKEN_URI,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise DriveError(
            f"Google token refresh failed (HTTP {resp.status_code}): {resp.text[:300]}"
        )
    payload = resp.json()
    token = payload.get("access_token")
    if not token:
        raise DriveError("Google token refresh returned no access_token.")
    _token_cache["token"] = token
    _token_cache["exp"] = now + float(payload.get("expires_in", 3600))
    return token


def _drive_get(path: str, params: Optional[Dict[str, Any]] = None, stream: bool = False):
    token = _google_access_token()
    return requests.get(
        f"{DRIVE_API}{path}",
        params=params or {},
        headers={"Authorization": "Bearer " + token},
        timeout=60,
        stream=stream,
    )


def _escape_q(value: str) -> str:
    """Escape a value for a Drive `q` query string (backslashes and single quotes)."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _files_to_rows(files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "id": f.get("id"),
            "name": f.get("name"),
            "mimeType": f.get("mimeType"),
            "modifiedTime": f.get("modifiedTime"),
        }
        for f in files
    ]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------
def tool_search_files(arguments: Dict[str, Any]) -> str:
    query = (arguments.get("query") or arguments.get("q") or "").strip()
    page_size = int(arguments.get("page_size") or 10)
    if not query:
        raise DriveError("search_files requires a non-empty 'query' argument.")
    term = _escape_q(query)
    q = f"(name contains '{term}' or fullText contains '{term}') and trashed = false"
    resp = _drive_get(
        "/files",
        {
            "q": q,
            "pageSize": max(1, min(page_size, 50)),
            "fields": "files(id,name,mimeType,modifiedTime)",
            "orderBy": "modifiedTime desc",
            "spaces": "drive",
        },
    )
    if resp.status_code != 200:
        raise DriveError(f"Drive search failed (HTTP {resp.status_code}): {resp.text[:300]}")
    rows = _files_to_rows(resp.json().get("files", []))
    return json.dumps({"query": query, "count": len(rows), "files": rows}, ensure_ascii=False)


def tool_list_files(arguments: Dict[str, Any]) -> str:
    page_size = int(arguments.get("page_size") or 15)
    resp = _drive_get(
        "/files",
        {
            "pageSize": max(1, min(page_size, 50)),
            "fields": "files(id,name,mimeType,modifiedTime)",
            "orderBy": "modifiedTime desc",
            "q": "trashed = false",
            "spaces": "drive",
        },
    )
    if resp.status_code != 200:
        raise DriveError(f"Drive list failed (HTTP {resp.status_code}): {resp.text[:300]}")
    rows = _files_to_rows(resp.json().get("files", []))
    return json.dumps({"count": len(rows), "files": rows}, ensure_ascii=False)


def _resolve_file(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve a file by explicit id, or by (fuzzy) name search."""
    file_id = (arguments.get("file_id") or arguments.get("id") or "").strip()
    if file_id:
        resp = _drive_get(f"/files/{file_id}", {"fields": "id,name,mimeType"})
        if resp.status_code != 200:
            raise DriveError(
                f"Could not get file {file_id} (HTTP {resp.status_code}): {resp.text[:200]}"
            )
        return resp.json()

    name = (arguments.get("name") or arguments.get("title") or "").strip()
    if not name:
        raise DriveError("get_file_content requires 'file_id' or 'name'.")
    term = _escape_q(name)
    resp = _drive_get(
        "/files",
        {
            "q": f"(name contains '{term}' or fullText contains '{term}') and trashed = false",
            "pageSize": 1,
            "fields": "files(id,name,mimeType)",
            "orderBy": "modifiedTime desc",
        },
    )
    if resp.status_code != 200:
        raise DriveError(f"Drive lookup failed (HTTP {resp.status_code}): {resp.text[:200]}")
    files = resp.json().get("files", [])
    if not files:
        raise DriveError(f"No file found matching name '{name}'.")
    return files[0]


def _extract_text(meta: Dict[str, Any]) -> str:
    mime = meta.get("mimeType", "")
    file_id = meta.get("id")

    export_map = {
        "application/vnd.google-apps.document": "text/plain",
        "application/vnd.google-apps.presentation": "text/plain",
        "application/vnd.google-apps.spreadsheet": "text/csv",
    }
    if mime in export_map:
        resp = _drive_get(f"/files/{file_id}/export", {"mimeType": export_map[mime]})
        if resp.status_code != 200:
            raise DriveError(f"Export failed (HTTP {resp.status_code}): {resp.text[:200]}")
        return resp.text

    if mime == "application/pdf":
        resp = _drive_get(f"/files/{file_id}", {"alt": "media"}, stream=True)
        if resp.status_code != 200:
            raise DriveError(f"Download failed (HTTP {resp.status_code}): {resp.text[:200]}")
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(resp.content))
            return "\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            raise DriveError(f"Could not extract PDF text: {exc}")

    if mime.startswith("text/") or mime in ("application/json", "application/xml"):
        resp = _drive_get(f"/files/{file_id}", {"alt": "media"})
        if resp.status_code != 200:
            raise DriveError(f"Download failed (HTTP {resp.status_code}): {resp.text[:200]}")
        return resp.text

    raise DriveError(
        f"File '{meta.get('name')}' has unsupported type '{mime}' for text extraction."
    )


def tool_get_file_content(arguments: Dict[str, Any]) -> str:
    meta = _resolve_file(arguments)
    text = _extract_text(meta) or ""
    truncated = len(text) > MAX_CONTENT_CHARS
    if truncated:
        text = text[:MAX_CONTENT_CHARS]
    return json.dumps(
        {
            "id": meta.get("id"),
            "name": meta.get("name"),
            "mimeType": meta.get("mimeType"),
            "truncated": truncated,
            "content": text,
        },
        ensure_ascii=False,
    )


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "search_files",
        "description": "Search the user's Google Drive by keyword (matches file name and full text). Returns matching files with id, name, mimeType and modifiedTime.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword(s) to search for."},
                "page_size": {"type": "integer", "description": "Max results (1-50)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_files",
        "description": "List the user's most recently modified Google Drive files (id, name, mimeType, modifiedTime).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "page_size": {"type": "integer", "description": "Max results (1-50)."}
            },
        },
    },
    {
        "name": "get_file_content",
        "description": "Read the text content of a Drive file. Provide 'file_id' (preferred) or 'name'. Google Docs/Slides export as text, Sheets as CSV, PDFs are text-extracted, text files are returned as-is.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "The Drive file id."},
                "name": {"type": "string", "description": "A file name to look up if id is unknown."},
            },
        },
    },
]

TOOL_IMPL = {
    "search_files": tool_search_files,
    "list_files": tool_list_files,
    "get_file_content": tool_get_file_content,
}


# ---------------------------------------------------------------------------
# MCP JSON-RPC dispatch
# ---------------------------------------------------------------------------
def result(req_id: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": payload}


def error(req_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle_rpc(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Handle one JSON-RPC message. Returns a response dict, or None for notifications."""
    method = message.get("method")
    req_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        return result(
            req_id,
            {
                "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            },
        )

    if method is not None and method.startswith("notifications/"):
        return None  # notifications get an empty 202

    if method == "ping":
        return result(req_id, {})

    if method == "tools/list":
        return result(req_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        impl = TOOL_IMPL.get(name)
        if impl is None:
            return result(
                req_id,
                {
                    "content": [{"type": "text", "text": f"Unknown tool '{name}'."}],
                    "isError": True,
                },
            )
        try:
            text = impl(arguments)
            return result(
                req_id, {"content": [{"type": "text", "text": text}], "isError": False}
            )
        except DriveError as exc:
            logging.warning("tool %s failed: %s", name, exc)
            return result(
                req_id, {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            )
        except Exception as exc:  # noqa: BLE001
            logging.exception("tool %s crashed", name)
            return result(
                req_id,
                {"content": [{"type": "text", "text": f"Internal error: {exc}"}], "isError": True},
            )

    return error(req_id, -32601, f"Method not found: {method}")
