"""
Claude Desktop Stdio Wrapper Proxy (`mcp-guardian stdio-proxy`).
Intercepts newline-delimited JSON-RPC 2.0 traffic between Claude Desktop and local MCP servers.
Supports multiple simultaneous tool calls by holding intercepted requests asynchronously while
forwarding allowed requests and responses without head-of-line blocking.
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import sys
import urllib.request
from typing import Any, Dict, Optional, Set

from .models import ApprovalDecision, DecisionStatus, MCPInterceptedRequest, TransportType
from .queue_manager import ApprovalQueueManager

logger = logging.getLogger("mcp_guardian.stdio")

DEFAULT_INTERCEPT_METHODS: Set[str] = {
    "tools/call",
    "resources/read",
    "prompts/get",
    "sampling/createMessage",
}


class StdioMCPProxy:
    """
    Transparent JSON-RPC 2.0 Stdio Interceptor between Claude Desktop and an MCP Server subprocess.
    - Spawns the target MCP server command with its original arguments and environment.
    - Reads JSON-RPC messages from Claude Desktop on `stdin`.
    - Passes handshake and discovery calls (`initialize`, `tools/list`, `resources/list`, etc.)
      straight through so server startup is instantaneous.
    - Intercepts tool invocations (`tools/call`) and holds them pending until the user clicks
      Allow or Block in the confirmation modal (either via the central MCP Guardian Daemon or
      an in-process modal).
    """

    def __init__(
        self,
        server_name: str,
        command: list[str],
        queue_manager: Optional[ApprovalQueueManager] = None,
        daemon_url: Optional[str] = "http://127.0.0.1:8765",
        intercept_methods: Optional[Set[str]] = None,
        soft_tool_denial: bool = True,
    ) -> None:
        self.server_name = server_name
        self.command = command
        self.queue_manager = queue_manager
        self.daemon_url = daemon_url
        self.intercept_methods = intercept_methods or DEFAULT_INTERCEPT_METHODS
        self.soft_tool_denial = soft_tool_denial
        self._stdout_lock = asyncio.Lock()
        self._child_stdin_lock = asyncio.Lock()
        self._process: Optional[asyncio.subprocess.Process] = None
        self._running = False

    async def run(self, stdin_reader: Optional[asyncio.StreamReader] = None, stdout_writer: Optional[ Any] = None) -> int:
        """Starts the child MCP server subprocess and proxies stdio streams concurrently."""
        self._running = True
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=os.environ.copy(),
            )
        except Exception as exc:
            logger.error("Failed to launch MCP server '%s' (%s): %s", self.server_name, self.command, exc)
            return 1

        if stdin_reader is None:
            loop = asyncio.get_running_loop()
            stdin_reader = asyncio.StreamReader()
            protocol = asyncio.StreamReaderProtocol(stdin_reader)
            await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        client_to_server = asyncio.create_task(self._pump_client_to_server(stdin_reader, stdout_writer))
        server_to_client = asyncio.create_task(self._pump_server_to_client(stdout_writer))
        stderr_pump = asyncio.create_task(self._pump_stderr())

        done, pending = await asyncio.wait(
            [client_to_server, server_to_client],
            return_when=asyncio.FIRST_COMPLETED,
        )
        self._running = False
        for task in pending:
            task.cancel()
        stderr_pump.cancel()

        if self._process and self._process.returncode is None:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
            except Exception:
                self._process.kill()

        return self._process.returncode or 0

    async def _pump_client_to_server(
        self,
        reader: asyncio.StreamReader,
        stdout_writer: Optional[Any] = None,
    ) -> None:
        pending_tasks: Set[asyncio.Task] = set()
        while self._running:
            line = await reader.readline()
            if not line:
                break
            raw_str = line.decode("utf-8", errors="replace").strip()
            if not raw_str:
                continue

            # Dispatch each incoming line concurrently so held requests never block other messages
            task = asyncio.create_task(self._handle_client_line(raw_str, stdout_writer))
            pending_tasks.add(task)
            task.add_done_callback(pending_tasks.discard)

        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)

    async def _handle_client_line(self, raw_str: str, stdout_writer: Optional[Any]) -> None:
        try:
            payload = json.loads(raw_str)
        except json.JSONDecodeError:
            await self._write_to_child_stdin(raw_str + "\n")
            return

        if not isinstance(payload, dict):
            await self._write_to_child_stdin(raw_str + "\n")
            return

        method = payload.get("method", "")
        is_request = "id" in payload and bool(method)

        if is_request and method in self.intercept_methods:
            intercepted = MCPInterceptedRequest.from_jsonrpc(
                payload=payload,
                server_name=self.server_name,
                client_source="Claude Desktop",
                transport=TransportType.STDIO.value,
            )
            decision = await self._obtain_decision(intercepted)
            if decision.allowed:
                await self._write_to_child_stdin(raw_str + "\n")
            else:
                denial_msg = decision.to_jsonrpc_denial(
                    jsonrpc_id=payload.get("id"),
                    method=method,
                    soft_tool_denial=self.soft_tool_denial,
                )
                await self._write_to_client_stdout(json.dumps(denial_msg) + "\n", stdout_writer)
        else:
            await self._write_to_child_stdin(raw_str + "\n")

    async def _obtain_decision(self, req: MCPInterceptedRequest) -> ApprovalDecision:
        """
        First checks if an in-process ApprovalQueueManager is configured; otherwise queries
        the central background MCP Guardian Daemon over localhost HTTP.
        """
        if self.queue_manager is not None:
            return await self.queue_manager.request_approval_async(req)

        if self.daemon_url:
            try:
                return await asyncio.to_thread(self._query_daemon_sync, req)
            except Exception as exc:
                logger.error("Failed to contact MCP Guardian daemon (%s): %s — blocking fail-closed.", self.daemon_url, exc)
                return ApprovalDecision(
                    request_uuid=req.request_uuid,
                    allowed=False,
                    status=DecisionStatus.ERROR,
                    reason=f"Communication failure with confirmation daemon: {exc}",
                )

        return ApprovalDecision(
            request_uuid=req.request_uuid,
            allowed=False,
            status=DecisionStatus.ERROR,
            reason="No confirmation mechanism available (fail-closed).",
        )

    def _query_daemon_sync(self, req: MCPInterceptedRequest) -> ApprovalDecision:
        data = json.dumps(req.to_dict()).encode("utf-8")
        http_req = urllib.request.Request(
            f"{self.daemon_url.rstrip('/')}/api/v1/confirm",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(http_req, timeout=130) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return ApprovalDecision(
                request_uuid=req.request_uuid,
                allowed=bool(body.get("allowed", False)),
                status=DecisionStatus(body.get("status", DecisionStatus.BLOCKED.value)),
                reason=body.get("reason", ""),
            )

    async def _pump_server_to_client(self, stdout_writer: Optional[Any]) -> None:
        assert self._process and self._process.stdout
        while self._running:
            line = await self._process.stdout.readline()
            if not line:
                break
            await self._write_to_client_stdout(line.decode("utf-8", errors="replace"), stdout_writer)

    async def _pump_stderr(self) -> None:
        assert self._process and self._process.stderr
        while self._running:
            line = await self._process.stderr.readline()
            if not line:
                break
            sys.stderr.write(line.decode("utf-8", errors="replace"))
            sys.stderr.flush()

    async def _write_to_child_stdin(self, text: str) -> None:
        if not self._process or not self._process.stdin:
            return
        async with self._child_stdin_lock:
            self._process.stdin.write(text.encode("utf-8"))
            await self._process.stdin.drain()

    async def _write_to_client_stdout(self, text: str, stdout_writer: Optional[Any]) -> None:
        async with self._stdout_lock:
            if stdout_writer is not None:
                stdout_writer(text)
            else:
                sys.stdout.write(text)
                sys.stdout.flush()
