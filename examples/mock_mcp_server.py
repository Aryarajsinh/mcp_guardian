#!/usr/bin/env python3
"""
Mock MCP Server implementing JSON-RPC 2.0 over stdio for testing and demonstration.
Supports `initialize`, `tools/list`, and `tools/call` (e.g. `read_file`, `execute_command`, `delete_database`).
"""

import json
import sys


def handle_message(msg: dict) -> dict | None:
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    if req_id is None:
        return None  # JSON-RPC notification

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "demo-filesystem-mcp", "version": "1.0.0"},
            },
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {
                        "name": "read_file",
                        "description": "Read contents of a local file",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                    },
                    {
                        "name": "delete_file",
                        "description": "Permanently delete a file from disk",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                    },
                ]
            },
        }

    if method == "tools/call":
        tool_name = params.get("name", "unknown")
        args = params.get("arguments") or {}
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": f"[MCP Server Executed] Successfully ran {tool_name} with args={json.dumps(args)}",
                    }
                ],
                "isError": False,
            },
        }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {"ok": True},
    }


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            resp = handle_message(payload)
            if resp is not None:
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
        except Exception as exc:
            sys.stderr.write(f"Mock MCP server error: {exc}\n")
            sys.stderr.flush()


if __name__ == "__main__":
    main()
