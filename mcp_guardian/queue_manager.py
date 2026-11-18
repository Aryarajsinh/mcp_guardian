"""
Thread-safe and Asyncio-safe Concurrent Request Queue Manager.
Holds multiple simultaneous MCP requests pending until the user approves or blocks each one,
and logs every decision to a persistent JSONL audit log.
"""

from __future__ import annotations
import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .models import ApprovalDecision, DecisionStatus, MCPInterceptedRequest

logger = logging.getLogger("mcp_guardian.queue")


class ApprovalQueueManager:
    """
    Coordinates concurrent pending MCP requests across Stdio and HTTP/SSE worker threads/tasks.
    Supports:
    - Multiple simultaneous pending requests (`Dict[request_uuid, asyncio.Future]`)
    - Thread-safe resolution from the Tkinter GUI thread (`resolve_request`)
    - Automatic fail-closed timeout handling (`default_timeout_seconds`)
    - Persistent JSONL audit logging
    """

    def __init__(
        self,
        ui_notify_callback: Optional[Callable[[MCPInterceptedRequest], None]] = None,
        default_timeout_seconds: float = 120.0,
        audit_log_path: Optional[Path] = None,
    ) -> None:
        self._lock = threading.RLock()
        self._pending_requests: Dict[str, MCPInterceptedRequest] = {}
        self._async_waiters: Dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Future]] = {}
        self._sync_waiters: Dict[str, tuple[threading.Event, List[ApprovalDecision]]] = {}
        self._history: List[MCPInterceptedRequest] = []
        self._ui_notify_callback = ui_notify_callback
        self._default_timeout = default_timeout_seconds
        self._audit_log_path = audit_log_path or (Path.home() / ".mcp_guardian" / "audit.jsonl")
        self._audit_log_path.parent.mkdir(parents=True, exist_ok=True)

    def set_ui_callback(self, callback: Callable[[MCPInterceptedRequest], None]) -> None:
        with self._lock:
            self._ui_notify_callback = callback

    def get_pending_requests(self) -> List[MCPInterceptedRequest]:
        with self._lock:
            return list(self._pending_requests.values())

    def get_history(self, limit: int = 100) -> List[MCPInterceptedRequest]:
        with self._lock:
            return list(self._history[-limit:])

    async def request_approval_async(
        self,
        req: MCPInterceptedRequest,
        timeout: Optional[float] = None,
    ) -> ApprovalDecision:
        """
        Registers a pending MCP request and awaits user confirmation asynchronously.
        Multiple requests can await concurrently without blocking one another.
        """
        effective_timeout = timeout if timeout is not None else self._default_timeout
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ApprovalDecision] = loop.create_future()

        with self._lock:
            self._pending_requests[req.request_uuid] = req
            self._async_waiters[req.request_uuid] = (loop, future)
            callback = self._ui_notify_callback

        logger.info(
            "Held MCP request [%s] server=%s method=%s tool=%s (pending=%d)",
            req.request_uuid[:8],
            req.server_name,
            req.method,
            req.tool_name,
            len(self._pending_requests),
        )

        if callback is not None:
            try:
                callback(req)
            except Exception as exc:
                logger.error("UI notification callback error: %s", exc)

        try:
            decision = await asyncio.wait_for(future, timeout=effective_timeout)
            return decision
        except asyncio.TimeoutError:
            logger.warning("Request [%s] timed out after %.1fs — auto-blocking.", req.request_uuid[:8], effective_timeout)
            decision = ApprovalDecision(
                request_uuid=req.request_uuid,
                allowed=False,
                status=DecisionStatus.TIMEOUT_BLOCKED,
                reason=f"Timed out waiting for user confirmation ({int(effective_timeout)}s). Fail-closed block applied.",
            )
            self._finalize_request(req.request_uuid, decision)
            return decision

    def request_approval_sync(
        self,
        req: MCPInterceptedRequest,
        timeout: Optional[float] = None,
    ) -> ApprovalDecision:
        """
        Synchronous wrapper for threads that do not run an asyncio event loop.
        """
        effective_timeout = timeout if timeout is not None else self._default_timeout
        event = threading.Event()
        holder: List[ApprovalDecision] = []

        with self._lock:
            self._pending_requests[req.request_uuid] = req
            self._sync_waiters[req.request_uuid] = (event, holder)
            callback = self._ui_notify_callback

        if callback is not None:
            try:
                callback(req)
            except Exception as exc:
                logger.error("UI callback failed: %s", exc)

        signaled = event.wait(timeout=effective_timeout)
        if signaled and holder:
            return holder[0]

        decision = ApprovalDecision(
            request_uuid=req.request_uuid,
            allowed=False,
            status=DecisionStatus.TIMEOUT_BLOCKED,
            reason=f"Timed out waiting for user confirmation ({int(effective_timeout)}s). Fail-closed block applied.",
        )
        self._finalize_request(req.request_uuid, decision)
        return decision

    def resolve_request(self, request_uuid: str, allowed: bool, reason: str = "") -> Optional[ApprovalDecision]:
        """
        Called by the GUI modal thread or REST API when the user clicks Allow or Block.
        Immediately wakes up the waiting request handler.
        """
        status = DecisionStatus.ALLOWED if allowed else DecisionStatus.BLOCKED
        if not reason:
            reason = "Approved by user via confirmation popup." if allowed else "Blocked by user via confirmation popup."

        decision = ApprovalDecision(
            request_uuid=request_uuid,
            allowed=allowed,
            status=status,
            reason=reason,
        )
        return self._finalize_request(request_uuid, decision)

    def _finalize_request(self, request_uuid: str, decision: ApprovalDecision) -> Optional[ApprovalDecision]:
        with self._lock:
            req = self._pending_requests.pop(request_uuid, None)
            async_waiter = self._async_waiters.pop(request_uuid, None)
            sync_waiter = self._sync_waiters.pop(request_uuid, None)

            if req is not None:
                req.status = decision.status.value
                req.decision_reason = decision.reason
                req.decided_at = decision.decided_at
                self._history.append(req)
                self._write_audit_entry(req)

        if async_waiter is not None:
            loop, future = async_waiter
            if not future.done():
                loop.call_soon_threadsafe(self._safe_set_future, future, decision)

        if sync_waiter is not None:
            event, holder = sync_waiter
            holder.append(decision)
            event.set()

        return decision

    @staticmethod
    def _safe_set_future(future: asyncio.Future, decision: ApprovalDecision) -> None:
        if not future.done():
            future.set_result(decision)

    def _write_audit_entry(self, req: MCPInterceptedRequest) -> None:
        try:
            with self._audit_log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(req.to_dict(), ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.debug("Could not write audit log entry: %s", exc)
