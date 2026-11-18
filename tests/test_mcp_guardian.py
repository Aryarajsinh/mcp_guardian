"""
Automated Unit & Integration Test Suite for MCP Guardian.
Verifies:
1. MCP JSON-RPC 2.0 request parsing and risk classification (`LOW`, `MEDIUM`, `HIGH`).
2. Concurrent multi-request holding and independent resolution (`Allow` vs `Block`).
3. End-to-end Claude Desktop `StdioMCPProxy` subprocess interception (passthrough discovery, allowed tool execution, blocked tool execution).
4. End-to-end Claude Web `MCPGuardianHTTPServer` HTTP/SSE reverse proxy interception and concurrent requests.
5. Automatic patching and restoration of `claude_desktop_config.json`.
"""

from __future__ import annotations
import asyncio
import json
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

from mcp_guardian.config_patcher import patch_claude_desktop_config, restore_claude_desktop_config
from mcp_guardian.http_proxy import MCPGuardianHTTPServer
from mcp_guardian.models import DecisionStatus, MCPInterceptedRequest, RiskLevel
from mcp_guardian.queue_manager import ApprovalQueueManager
from mcp_guardian.stdio_proxy import StdioMCPProxy

REPO_ROOT = Path(__file__).resolve().parent.parent
MOCK_SERVER = REPO_ROOT / "examples" / "mock_mcp_server.py"


class TestMCPGuardian(unittest.IsolatedAsyncioTestCase):
    async def test_risk_classification_and_parsing(self) -> None:
        low_req = MCPInterceptedRequest.from_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "read_file", "arguments": {"path": "/project/README.md"}},
            },
            server_name="filesystem",
        )
        self.assertEqual(low_req.risk_level, RiskLevel.LOW.value)
        self.assertEqual(low_req.tool_name, "read_file")

        high_req = MCPInterceptedRequest.from_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "delete_file", "arguments": {"path": "/etc/hosts"}},
            },
            server_name="filesystem",
        )
        self.assertEqual(high_req.risk_level, RiskLevel.HIGH.value)

    async def test_concurrent_multiple_requests_queue(self) -> None:
        audit_file = Path(tempfile.mktemp(suffix=".jsonl"))
        qm = ApprovalQueueManager(default_timeout_seconds=10.0, audit_log_path=audit_file)

        req1 = MCPInterceptedRequest.from_jsonrpc(
            {"jsonrpc": "2.0", "id": 101, "method": "tools/call", "params": {"name": "read_file", "arguments": {"path": "a.txt"}}},
            server_name="fs-1",
        )
        req2 = MCPInterceptedRequest.from_jsonrpc(
            {"jsonrpc": "2.0", "id": 102, "method": "tools/call", "params": {"name": "delete_file", "arguments": {"path": "b.txt"}}},
            server_name="fs-2",
        )
        req3 = MCPInterceptedRequest.from_jsonrpc(
            {"jsonrpc": "2.0", "id": 103, "method": "tools/call", "params": {"name": "list_commits", "arguments": {"repo": "main"}}},
            server_name="github",
        )

        # Fire 3 simultaneous requests
        t1 = asyncio.create_task(qm.request_approval_async(req1))
        t2 = asyncio.create_task(qm.request_approval_async(req2))
        t3 = asyncio.create_task(qm.request_approval_async(req3))
        await asyncio.sleep(0.05)

        pending = qm.get_pending_requests()
        self.assertEqual(len(pending), 3)

        # Resolve out-of-order: Block req2 first, then Allow req3, then Allow req1
        qm.resolve_request(req2.request_uuid, allowed=False, reason="Dangerous file deletion")
        qm.resolve_request(req3.request_uuid, allowed=True, reason="Safe git read")
        qm.resolve_request(req1.request_uuid, allowed=True, reason="Safe file read")

        d1, d2, d3 = await asyncio.gather(t1, t2, t3)
        self.assertTrue(d1.allowed)
        self.assertFalse(d2.allowed)
        self.assertEqual(d2.status, DecisionStatus.BLOCKED)
        self.assertTrue(d3.allowed)
        self.assertEqual(len(qm.get_pending_requests()), 0)
        self.assertEqual(len(qm.get_history()), 3)

    async def test_stdio_proxy_allow_and_block(self) -> None:
        audit_file = Path(tempfile.mktemp(suffix=".jsonl"))
        qm = ApprovalQueueManager(default_timeout_seconds=10.0, audit_log_path=audit_file)

        # Auto-approve `read_file` and auto-block `delete_file`
        def auto_reviewer(req: MCPInterceptedRequest) -> None:
            allow = req.tool_name == "read_file"
            asyncio.get_running_loop().call_later(
                0.05,
                qm.resolve_request,
                req.request_uuid,
                allow,
                "Approved read_file" if allow else "Blocked delete_file",
            )

        qm.set_ui_callback(auto_reviewer)

        proxy = StdioMCPProxy(
            server_name="demo-filesystem",
            command=[sys.executable, str(MOCK_SERVER)],
            queue_manager=qm,
            daemon_url=None,
        )

        reader = asyncio.StreamReader()
        outputs: list[dict] = []

        def capture_stdout(line: str) -> None:
            for part in line.strip().splitlines():
                if part.strip():
                    outputs.append(json.loads(part))

        proxy_task = asyncio.create_task(proxy.run(stdin_reader=reader, stdout_writer=capture_stdout))

        # 1. Send initialize (should pass through without modal)
        reader.feed_data((json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n").encode())
        # 2. Send allowed tool call (`read_file`) and blocked tool call (`delete_file`) simultaneously
        reader.feed_data(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "read_file", "arguments": {"path": "/notes.txt"}},
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "delete_file", "arguments": {"path": "/secret.key"}},
                    }
                )
                + "\n"
            ).encode()
        )

        await asyncio.sleep(0.35)
        reader.feed_eof()
        await proxy_task

        by_id = {msg["id"]: msg for msg in outputs}
        self.assertIn(1, by_id)
        self.assertIn(2, by_id)
        self.assertIn(3, by_id)
        self.assertFalse(by_id[2]["result"]["isError"])
        self.assertIn("Successfully ran read_file", by_id[2]["result"]["content"][0]["text"])
        self.assertTrue(by_id[3]["result"]["isError"])
        self.assertIn("MCP Guardian Security Block", by_id[3]["result"]["content"][0]["text"])

    async def test_http_proxy_claude_web_interception(self) -> None:
        audit_file = Path(tempfile.mktemp(suffix=".jsonl"))
        qm = ApprovalQueueManager(default_timeout_seconds=10.0, audit_log_path=audit_file)

        def auto_reviewer(req: MCPInterceptedRequest) -> None:
            allow = req.arguments.get("action") == "safe_query"
            asyncio.get_running_loop().call_later(
                0.05,
                qm.resolve_request,
                req.request_uuid,
                allow,
                "Allowed safe query" if allow else "Blocked unsafe mutation",
            )

        qm.set_ui_callback(auto_reviewer)
        server = MCPGuardianHTTPServer(queue_manager=qm, host="127.0.0.1", port=0)
        host, port = await server.start()

        try:
            def post_mcp(payload: dict) -> dict:
                req = urllib.request.Request(
                    f"http://{host}:{port}/mcp/postgres-connector",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return json.loads(resp.read().decode("utf-8"))

            allowed_resp, blocked_resp = await asyncio.gather(
                asyncio.to_thread(
                    post_mcp,
                    {
                        "jsonrpc": "2.0",
                        "id": 10,
                        "method": "tools/call",
                        "params": {"name": "run_sql", "arguments": {"action": "safe_query", "sql": "SELECT 1"}},
                    },
                ),
                asyncio.to_thread(
                    post_mcp,
                    {
                        "jsonrpc": "2.0",
                        "id": 11,
                        "method": "tools/call",
                        "params": {"name": "run_sql", "arguments": {"action": "drop_table", "sql": "DROP TABLE users"}},
                    },
                ),
            )
            self.assertFalse(allowed_resp["result"]["isError"])
            self.assertTrue(blocked_resp["result"]["isError"])
            self.assertIn("Blocked unsafe mutation", blocked_resp["result"]["content"][0]["text"])
        finally:
            await server.stop()

    def test_claude_desktop_config_patch_and_restore(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "claude_desktop_config.json"
            original = {
                "mcpServers": {
                    "filesystem": {
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/Users/demo/Desktop"],
                    }
                }
            }
            cfg_path.write_text(json.dumps(original, indent=2), encoding="utf-8")

            patched_count, backup_path = patch_claude_desktop_config(cfg_path)
            self.assertEqual(patched_count, 1)
            self.assertTrue(backup_path.exists())

            patched_data = json.loads(cfg_path.read_text(encoding="utf-8"))
            fs_spec = patched_data["mcpServers"]["filesystem"]
            self.assertIn("mcp_guardian.cli", fs_spec["args"])
            self.assertIn("stdio-proxy", fs_spec["args"])

            restored_count = restore_claude_desktop_config(cfg_path)
            self.assertEqual(restored_count, 1)
            restored_data = json.loads(cfg_path.read_text(encoding="utf-8"))
            self.assertEqual(restored_data["mcpServers"]["filesystem"]["command"], "npx")


if __name__ == "__main__":
    unittest.main()
