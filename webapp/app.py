"""Flask HTTP wrapper that exposes the Google Drive MCP core over HTTP.

Hosted on Azure App Service (Linux/Python). The MCP endpoint is::

    POST /mcp?key=<MCP_SHARED_SECRET>

A shared secret protects the endpoint (the Foundry agent puts it in the URL, the
same way a Function key would be). The Google identity lives entirely server-side
in ``mcp_core`` via a stored refresh token, so this works from the Foundry portal
with no per-user OAuth and no localhost.
"""

from __future__ import annotations

import json
import os

from flask import Flask, Response, request

import mcp_core

app = Flask(__name__)


def _authorized(req) -> bool:
    """Validate the shared secret. If none is configured, allow (dev/local)."""
    expected = os.environ.get("MCP_SHARED_SECRET", "")
    if not expected:
        return True
    provided = (
        req.args.get("key")
        or req.args.get("code")
        or req.headers.get("x-mcp-key", "")
    )
    return provided == expected


def _json(payload, status: int = 200) -> Response:
    return Response(json.dumps(payload), status=status, mimetype="application/json")


@app.get("/")
def health() -> Response:
    """Unauthenticated health check (used by App Service warmup and quick probes)."""
    return _json({"status": "ok", "server": mcp_core.SERVER_INFO})


@app.route("/mcp", methods=["POST", "GET"])
def mcp() -> Response:
    if request.method == "GET":
        # Some clients probe with GET; we only implement request/response over POST.
        return Response(status=405, headers={"Allow": "POST"})

    if not _authorized(request):
        return _json({"error": "unauthorized"}, status=401)

    raw = request.get_data()
    try:
        message = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return _json(mcp_core.error(None, -32700, "Parse error"), status=400)

    # Support a single message or a JSON-RPC batch (list).
    if isinstance(message, list):
        responses = [r for r in (mcp_core.handle_rpc(m) for m in message) if r is not None]
        if not responses:
            return Response(status=202)
        return _json(responses)

    response = mcp_core.handle_rpc(message)
    if response is None:
        # Notification (e.g. notifications/initialized): acknowledge with 202.
        return Response(status=202)
    return _json(response)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
