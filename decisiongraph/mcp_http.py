"""HTTP-MCP endpoint for EXTERNAL agents (Claude, Cursor, custom clients).

Same security model as the in-sandbox stdio path:
  * grant-token-authenticated (Authorization: Bearer ag_xxx)
  * scoped per call via the existing Step-1 gate
  * every call audited to the same immutable agent_audit.jsonl

URL form:
    POST /api/mcp/v1/ws/{ws_token}
    Authorization: Bearer ag_xxxxxxxx
    Content-Type: application/json
    body: <JSON-RPC 2.0 request>

`{ws_token}` is the company workspace id; `ag_xxx` is the scoped grant
minted by /api/agent/grants. Any MCP-capable client that supports HTTP
transport can connect.
"""
from __future__ import annotations

import os
import json
from typing import Optional

from fastapi import HTTPException, Request

from .agent_access import AgentAccessStore
from .mcp_gateway import MCPGateway


def _bearer(req: Request) -> Optional[str]:
    h = req.headers.get("authorization", "")
    if not h.lower().startswith("bearer "):
        return None
    return h[7:].strip() or None


async def handle_mcp_request(req: Request, ws_token: str, wsm, dg_for_ws):
    """Single JSON-RPC over HTTP. Returns the JSON-RPC response dict.

    `wsm` is the WorkspaceManager, `dg_for_ws(ws)` is a callable that returns
    the workspace's DecisionGraph instance (server provides this so we don't
    import server.S here).
    """
    token = _bearer(req)
    if not token:
        raise HTTPException(401, "missing Authorization: Bearer header")

    if not wsm._safe(ws_token):
        raise HTTPException(400, "invalid workspace id")
    ws = wsm.get(ws_token)
    if ws is None:
        raise HTTPException(404, "workspace not found")

    store = AgentAccessStore(ws.root)
    grant = store.get(token)
    if not grant:
        raise HTTPException(403, "unknown or invalid agent token")

    try:
        body = await req.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        raise HTTPException(400, "expected JSON-RPC 2.0 request")

    # build a per-call adapter + gateway (lightweight, no global state)
    from .agent_job import WorkspaceMemoryAdapter
    dg = dg_for_ws(ws)
    adapter = WorkspaceMemoryAdapter(dg)
    gw = MCPGateway(store=store, adapter=adapter, agent_token=token,
                    job_id=f"ext_{body.get('id', '?')}", dg=dg)
    return gw.handle(body)
