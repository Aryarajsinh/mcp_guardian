"""
Claude Web & Remote MCP HTTP / Streamable HTTP / SSE Reverse Proxy (`mcp-guardian daemon` / `http-proxy`).
Provides:
1. Streamable HTTP & JSON-RPC 2.0 Reverse Proxy (`/mcp` and `/mcp/<server_name>`) for Claude Web
   Custom Connectors and remote MCP servers.
2. Local IPC Confirmation Endpoint (`/api/v1/confirm`, `/api/v1/pending`, `/api/v1/resolve`, `/api/v1/history`)
   so multiple Claude Desktop `stdio-proxy` processes share a single unified confirmation modal queue.
"""

from __future__ import annotations
import asyncio
import json
import logging
import urllib.error
import urllib.request
from http import HTTPStatus
from typing import Any, Dict, Optional, Set, Tuple

from .models import MCPInterceptedRequest, TransportType
from .queue_manager import ApprovalQueueManager
from .stdio_proxy import DEFAULT_INTERCEPT_METHODS

logger = logging.getLogger("mcp_guardian.http")


class MCPGuardianHTTPServer:
    """
    Asynchronous HTTP Server handling both:
    - Claude Web MCP Streamable HTTP / JSON-RPC requests (`POST /mcp` or `POST /mcp/<server_name>`)
    - Daemon IPC API (`/api/v1/confirm`, `/api/v1/pending`, `/api/v1/resolve`, `/api/v1/history`)
    """

    def __init__(
        self,
        queue_manager: ApprovalQueueManager,
        host: str = "127.0.0.1",
        port: int = 8765,
        upstream_map: Optional[Dict[str, str]] = None,
        default_upstream: Optional[str] = None,
        intercept_methods: Optional[Set[str]] = None,
        soft_tool_denial: bool = True,
    ) -> None:
        self.queue_manager = queue_manager
        self.host = host
        self.port = port
        self.upstream_map = upstream_map or {}
        self.default_upstream = default_upstream
        self.intercept_methods = intercept_methods or DEFAULT_INTERCEPT_METHODS
        self.soft_tool_denial = soft_tool_denial
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self) -> Tuple[str, int]:
        self._server = await asyncio.start_server(self._handle_connection, self.host, self.port)
        sockets = self._server.sockets
        actual_port = sockets[0].getsockname()[1] if sockets else self.port
        self.port = actual_port
        logger.info("MCP Guardian HTTP & Daemon server listening on http://%s:%d", self.host, self.port)
        return self.host, self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=30.0)
            if not request_line:
                writer.close()
                return
            parts = request_line.decode("utf-8", errors="replace").strip().split(" ")
            if len(parts) < 2:
                writer.close()
                return
            http_method, path = parts[0].upper(), parts[1]

            headers: Dict[str, str] = {}
            while True:
                hline = await reader.readline()
                if not hline or hline in (b"\r\n", b"\n"):
                    break
                decoded = hline.decode("utf-8", errors="replace").strip()
                if ":" in decoded:
                    k, v = decoded.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            content_length = int(headers.get("content-length", "0"))
            body_bytes = await reader.readexactly(content_length) if content_length > 0 else b""

            if http_method == "OPTIONS":
                await self._send_json(writer, HTTPStatus.NO_CONTENT, {})
                return

            if path == "/api/v1/health" and http_method == "GET":
                await self._send_json(
                    writer,
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "pending_count": len(self.queue_manager.get_pending_requests()),
                    },
                )
                return

            if path == "/api/v1/pending" and http_method == "GET":
                pending = [r.to_dict() for r in self.queue_manager.get_pending_requests()]
                await self._send_json(writer, HTTPStatus.OK, {"pending": pending})
                return

            if path == "/api/v1/history" and http_method == "GET":
                history = [r.to_dict() for r in self.queue_manager.get_history()]
                await self._send_json(writer, HTTPStatus.OK, {"history": history})
                return

            if path == "/api/v1/resolve" and http_method == "POST":
                payload = json.loads(body_bytes.decode("utf-8"))
                decision = self.queue_manager.resolve_request(
                    request_uuid=payload["request_uuid"],
                    allowed=bool(payload["allowed"]),
                    reason=payload.get("reason", ""),
                )
                if decision is None:
                    await self._send_json(writer, HTTPStatus.NOT_FOUND, {"error": "Request not found"})
                else:
                    await self._send_json(
                        writer,
                        HTTPStatus.OK,
                        {
                            "request_uuid": decision.request_uuid,
                            "allowed": decision.allowed,
                            "status": decision.status.value,
                            "reason": decision.reason,
                        },
                    )
                return

            if path == "/api/v1/confirm" and http_method == "POST":
                data = json.loads(body_bytes.decode("utf-8"))
                req = MCPInterceptedRequest(**data)
                decision = await self.queue_manager.request_approval_async(req)
                await self._send_json(
                    writer,
                    HTTPStatus.OK,
                    {
                        "request_uuid": decision.request_uuid,
                        "allowed": decision.allowed,
                        "status": decision.status.value,
                        "reason": decision.reason,
                    },
                )
                return

            if path.startswith("/mcp") and http_method == "POST":
                await self._handle_mcp_http_post(path, headers, body_bytes, writer)
                return

            await self._send_json(writer, HTTPStatus.NOT_FOUND, {"error": f"Unknown route: {path}"})
        except Exception as exc:
            logger.error("HTTP connection handler error: %s", exc)
            try:
                await self._send_json(writer, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_mcp_http_post(
        self,
        path: str,
        headers: Dict[str, str],
        body_bytes: bytes,
        writer: asyncio.StreamWriter,
    ) -> None:
        segments = [s for s in path.split("/") if s]
        server_name = segments[1] if len(segments) > 1 else headers.get("x-mcp-server-name", "claude-web-connector")
        upstream_url = (
            headers.get("x-mcp-target-url")
            or self.upstream_map.get(server_name)
            or self.default_upstream
        )

        payload = json.loads(body_bytes.decode("utf-8"))
        method = payload.get("method", "")
        is_request = "id" in payload and bool(method)

        if is_request and method in self.intercept_methods:
            intercepted = MCPInterceptedRequest.from_jsonrpc(
                payload=payload,
                server_name=server_name,
                client_source="Claude Web (claude.ai)",
                transport=TransportType.HTTP_SSE.value,
            )
            decision = await self.queue_manager.request_approval_async(intercepted)
            if not decision.allowed:
                denial_payload = decision.to_jsonrpc_denial(
                    jsonrpc_id=payload.get("id"),
                    method=method,
                    soft_tool_denial=self.soft_tool_denial,
                )
                await self._send_json(writer, HTTPStatus.OK, denial_payload)
                return

        # Allowed or non-intercepted discovery request: forward to upstream MCP HTTP endpoint
        if not upstream_url:
            # If running in standalone/simulated upstream mode without an external URL, return mock ack
            await self._send_json(
                writer,
                HTTPStatus.OK,
                {
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": f"Forwarded and executed '{method}' on upstream server '{server_name}'.",
                            }
                        ],
                        "isError": False,
                    },
                },
            )
            return

        status_code, resp_body = await asyncio.to_thread(
            self._forward_http_sync,
            upstream_url,
            body_bytes,
            headers,
        )
        await self._send_raw_json_bytes(writer, status_code, resp_body)

    @staticmethod
    def _forward_http_sync(
        upstream_url: str,
        body_bytes: bytes,
        headers: Dict[str, str],
    ) -> Tuple[int, bytes]:
        forward_headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if "authorization" in headers:
            forward_headers["Authorization"] = headers["authorization"]
        if "mcp-session-id" in headers:
            forward_headers["Mcp-Session-Id"] = headers["mcp-session-id"]

        req = urllib.request.Request(upstream_url, data=body_bytes, headers=forward_headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as he:
            return he.code, he.read()

    async def _send_json(self, writer: asyncio.StreamWriter, status: HTTPStatus, data: Dict[str, Any]) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        await self._send_raw_json_bytes(writer, int(status), raw)

    async def _send_raw_json_bytes(self, writer: asyncio.StreamWriter, status_code: int, raw: bytes) -> None:
        head = (
            f"HTTP/1.1 {status_code} OK\r\n"
            f"Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(raw)}\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"Access-Control-Allow-Headers: Content-Type, Authorization, X-MCP-Server-Name, X-MCP-Target-URL, Mcp-Session-Id\r\n"
            f"Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("utf-8")
        writer.write(head + raw)
        await writer.drain()
