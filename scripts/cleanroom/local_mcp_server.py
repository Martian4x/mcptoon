#!/usr/bin/env python3
# Copyright 2025-2026 activeing123
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A real, tiny stdio MCP server for the clean room.

mcptoon's proxy path (`manifest`, `call`) only needs *a* server that speaks the
stdio JSON-RPC handshake. This is one, in the standard library, so the clean room
does not depend on any package being installed or fetched:

    {"jsonrpc":"2.0","id":1,"method":"initialize",...}      -> serverInfo
    {"jsonrpc":"2.0","id":2,"method":"tools/list"}          -> the tools below
    {"jsonrpc":"2.0","id":3,"method":"tools/call",...}      -> a result

It is deliberately a *separate* file from `tests/echo_server.py`: the test suite's
copy is excluded from the Docker build context by `.dockerignore` (tests/ is
skipped), and the clean room must run inside that container too. Keeping its own
copy makes the toolkit self-contained -- three files, no repo-internal coupling.

Run it directly to smoke-test it:
    echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python local_mcp_server.py
"""
from __future__ import annotations

import json
import sys

_TOOLS = [
    {
        "name": "echo",
        "description": "Echo back the message you send.",
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    },
    {
        "name": "add",
        "description": "Add two numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
]


def _handle(req: dict) -> dict:
    method = req.get("method", "")
    if method == "initialize":
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "cleanroom-server", "version": "0.1.0"},
        }
    if method == "tools/list":
        return {"tools": _TOOLS}
    if method == "tools/call":
        name = req["params"]["name"]
        args = req["params"].get("arguments") or {}
        if name == "echo":
            out = {"echo": args.get("message", "")}
        elif name == "add":
            out = {"sum": args["a"] + args["b"]}
        else:
            return {"content": [{"type": "text", "text": f"unknown tool: {name}"}],
                    "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(out)}]}
    raise KeyError(method)


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" not in req:            # a notification (e.g. initialized)
            continue
        try:
            result = _handle(req)
            resp = {"jsonrpc": "2.0", "id": req["id"], "result": result}
        except KeyError as e:
            resp = {"jsonrpc": "2.0", "id": req["id"],
                    "error": {"code": -32601, "message": f"unknown method: {e.args[0]}"}}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
