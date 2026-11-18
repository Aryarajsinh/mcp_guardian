"""
Data models for intercepted Model Context Protocol (MCP) JSON-RPC 2.0 requests,
user approval decisions, risk classification, and audit log entries.
"""

from __future__ import annotations
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, Optional


class TransportType(str, Enum):
    STDIO = "stdio (Claude Desktop)"
    HTTP_SSE = "http/sse (Claude Web)"


class DecisionStatus(str, Enum):
    PENDING = "PENDING"
    ALLOWED = "ALLOWED"
    BLOCKED = "BLOCKED"
    TIMEOUT_BLOCKED = "TIMEOUT_BLOCKED"
    ERROR = "ERROR"


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


HIGH_RISK_KEYWORDS = {
    "delete", "remove", "drop", "truncate", "exec", "shell", "bash", "cmd",
    "powershell", "write_file", "execute_command", "run_script", "send_email",
    "transfer", "chmod", "chown", "rm ", "sudo"
}

MEDIUM_RISK_KEYWORDS = {
    "update", "patch", "put", "post", "create", "insert", "modify", "edit",
    "fetch", "http", "sql", "query"
}


def classify_risk(method: str, tool_name: str, arguments: Dict[str, Any]) -> RiskLevel:
    """Heuristically classify the risk of an intercepted MCP request for visual cues."""
    combined = f"{method} {tool_name} {str(arguments)}".lower()
    for kw in HIGH_RISK_KEYWORDS:
        if kw in combined:
            return RiskLevel.HIGH
    for kw in MEDIUM_RISK_KEYWORDS:
        if kw in combined:
            return RiskLevel.MEDIUM
    return RiskLevel.LOW


@dataclass
class MCPInterceptedRequest:
    """Represents a single pending MCP JSON-RPC 2.0 request awaiting user decision."""
    request_uuid: str = field(default_factory=lambda: str(uuid.uuid4()))
    jsonrpc_id: Any = None
    client_source: str = "Claude Desktop"
    transport: str = TransportType.STDIO.value
    server_name: str = "unknown-mcp-server"
    method: str = "tools/call"
    tool_name: str = ""
    arguments: Dict[str, Any] = field(default_factory=dict)
    raw_payload: Dict[str, Any] = field(default_factory=dict)
    risk_level: str = RiskLevel.LOW.value
    timestamp: float = field(default_factory=time.time)
    status: str = DecisionStatus.PENDING.value
    decision_reason: Optional[str] = None
    decided_at: Optional[float] = None

    @classmethod
    def from_jsonrpc(
        cls,
        payload: Dict[str, Any],
        server_name: str,
        client_source: str = "Claude Desktop",
        transport: str = TransportType.STDIO.value,
    ) -> "MCPInterceptedRequest":
        method = payload.get("method", "unknown")
        params = payload.get("params") or {}
        if method == "tools/call":
            tool_name = params.get("name", "<unnamed_tool>")
            arguments = params.get("arguments") or {}
        elif method == "resources/read":
            tool_name = f"resource:{params.get('uri', '<unknown_uri>')}"
            arguments = params
        elif method == "prompts/get":
            tool_name = f"prompt:{params.get('name', '<unknown_prompt>')}"
            arguments = params.get("arguments") or params
        elif method == "sampling/createMessage":
            tool_name = "sampling:createMessage"
            arguments = params
        else:
            tool_name = method
            arguments = params if isinstance(params, dict) else {"value": params}

        risk = classify_risk(method, tool_name, arguments)
        return cls(
            jsonrpc_id=payload.get("id"),
            client_source=client_source,
            transport=transport,
            server_name=server_name,
            method=method,
            tool_name=tool_name,
            arguments=arguments,
            raw_payload=payload,
            risk_level=risk.value,
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ApprovalDecision:
    """Result of the user's Allow / Block action in the confirmation popup."""
    request_uuid: str
    allowed: bool
    status: DecisionStatus
    reason: str = ""
    decided_at: float = field(default_factory=time.time)

    def to_jsonrpc_denial(
        self,
        jsonrpc_id: Any,
        method: str = "tools/call",
        soft_tool_denial: bool = True,
    ) -> Dict[str, Any]:
        """
        Constructs a standards-compliant MCP JSON-RPC 2.0 denial response when blocked.
        - If `soft_tool_denial=True` and `method == "tools/call"`, returns a structured
          MCP tool execution error result (`isError: True`) so Claude gracefully sees
          that the user denied permission without terminating the MCP server transport.
        - Otherwise, returns a standard JSON-RPC 2.0 error object (code -32001).
        """
        msg = self.reason or "Execution blocked by user via MCP Guardian confirmation modal."
        if soft_tool_denial and method == "tools/call":
            return {
                "jsonrpc": "2.0",
                "id": jsonrpc_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": f"[MCP Guardian Security Block] {msg}",
                        }
                    ],
                    "isError": True,
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": jsonrpc_id,
            "error": {
                "code": -32001,
                "message": f"MCP Guardian: Request blocked by user ({msg})",
                "data": {
                    "request_uuid": self.request_uuid,
                    "status": self.status.value,
                    "decided_at": self.decided_at,
                },
            },
        }
