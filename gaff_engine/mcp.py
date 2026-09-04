#!/usr/bin/env python3
"""MCP surface: JSON-RPC 2.0 over stdio, standard library only.

Lives inside the package (BACKLOG R3) so a pip install carries the server:
the ``gaff-mcp`` console script points here, and ``python3 -m gaff_engine.mcp``
works from any checkout or wheel with no path arithmetic.

Two protocol rules this file exists to hold, both from the 28 Aug review (E3):

1. **stdout IS the protocol.** Anything else written there corrupts a frame and
   kills the session. The engine code being productised already prints with
   ``flush=True``, so we do not rely on discipline: stdout is rebound to stderr
   at import — BEFORE ``gaff_engine.tools`` (and everything the tools lazily
   import at call time) gets a chance to print — and the real handle is kept
   private for frames. ``gaff_engine/__init__`` itself runs first by necessity;
   it imports only the print-free deterministic core.
2. **Tool execution errors are results, not transport errors.** They come back
   inside the result with ``isError: true`` so the model reads them and can
   recover. JSON-RPC error codes are for protocol failures only — a ``-32603``
   renders to the user as an opaque breakage they cannot act on.
"""
import json
import sys

# --- rule 1: claim the real stdout before importing anything that might print ---
_FRAMES = sys.stdout
sys.stdout = sys.stderr

from gaff_engine import __version__                       # noqa: E402
from gaff_engine.tools import TOOLS, DISPATCH, safe_call  # noqa: E402

PROTOCOL = "2025-06-18"


def _send(msg):
    _FRAMES.write(json.dumps(msg) + "\n")
    _FRAMES.flush()


def _result(rid, payload):
    _send({"jsonrpc": "2.0", "id": rid, "result": payload})


def _error(rid, code, message):
    """Protocol-level failure only. Tool failures go through _tool_result."""
    _send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def _tool_result(rid, payload, ok):
    _result(rid, {"content": [{"type": "text", "text": json.dumps(payload, indent=1)}],
                  "isError": not ok})


def handle(req):
    method, rid = req.get("method"), req.get("id")
    if method == "initialize":
        _result(rid, {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                      "serverInfo": {"name": "gaff", "version": __version__}})
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        _result(rid, {"tools": TOOLS})
    elif method == "tools/call":
        params = req.get("params") or {}
        if not isinstance(params, dict):           # e.g. "params": [1, 2]
            _error(rid, -32600, "params must be an object")
            return
        name = params.get("name")
        # The isinstance guard also keeps an unhashable name (e.g. a dict)
        # out of DISPATCH.get, which would raise and kill the session.
        if not isinstance(name, str) or name not in DISPATCH:
            _error(rid, -32602, "unknown tool: %s" % name)  # protocol error
            return
        fn = DISPATCH[name]
        # progress is discarded here: this surface cannot stream mid-call.
        ok, payload = safe_call(name, fn, params.get("arguments") or {}, progress=None)
        # Rule 2, applied to SOFT errors too: an honest {error, hint} dict
        # (cold street, cold town) is still a failed answer, so it must carry
        # isError:true for the model to read it as one — the server's own
        # docstring contract, previously held only for raised failures.
        if isinstance(payload, dict):
            # The 'usage' tag drives the CLI's exit codes only; here isError
            # already says everything the host needs, so keep the wire clean.
            payload.pop("usage", None)
            if ok and "error" in payload:
                ok = False
        _tool_result(rid, payload, ok)
    elif rid is not None:
        _error(rid, -32601, "method not found: %s" % method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Valid JSON that is not an object ('[1,2,3]', '"hello"', '42') used
        # to kill the whole session via req.get. Per the docstring's own rule,
        # protocol failures are ANSWERED (-32600, id null), never fatal.
        if not isinstance(req, dict):
            _error(None, -32600, "request must be a JSON object")
            continue
        try:
            handle(req)
        except Exception as exc:                   # noqa: BLE001 - the session must outlive any frame
            _error(req.get("id"), -32603,
                   "internal error: %s: %s" % (type(exc).__name__, exc))


if __name__ == "__main__":
    main()
