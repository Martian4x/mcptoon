#!/usr/bin/env python3
"""Minimal MCP stdio server whose `sleep` tool really sleeps.

Used by tests/test_issue34_call_timeout.py to prove the client deadline is
actually honoured end to end — not just that a value was stored somewhere.
"""
import json
import sys
import time


def handle_initialize(req):
    return {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "slow-server", "version": "0.1.0"},
    }


def handle_tools_list(req):
    return {
        "tools": [
            {
                "name": "sleep",
                "description": "Sleep for N seconds, then answer.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"seconds": {"type": "number"}},
                    "required": ["seconds"],
                },
            }
        ]
    }


def handle_tools_call(req):
    params = req.get("params") or {}
    args = params.get("arguments") or {}
    seconds = float(args.get("seconds", 1))
    time.sleep(seconds)
    return {
        "content": [{"type": "text", "text": f"slept {seconds}s"}],
        "isError": False,
    }


HANDLERS = {
    "initialize": handle_initialize,
    "tools/list": handle_tools_list,
    "tools/call": handle_tools_call,
}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method")
        handler = HANDLERS.get(method)
        resp = {"jsonrpc": "2.0", "id": req.get("id")}
        if handler is None:
            # Notifications (no id) get no reply; unknown methods get an error.
            if req.get("id") is None:
                continue
            resp["error"] = {"code": -32601, "message": f"no method {method}"}
        else:
            resp["result"] = handler(req)
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
