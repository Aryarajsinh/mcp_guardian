"""
Always-On-Top User Confirmation Popup Modal for MCP Guardian.
Displays pending MCP requests above all active windows on Windows, macOS, and Linux,
supports concurrent queued requests with tabs/navigation, and provides clear Allow / Block controls.
"""

from __future__ import annotations
import json
import logging
import platform
import queue
import threading
import time
from typing import Callable, Dict, Optional

from .models import MCPInterceptedRequest, RiskLevel

logger = logging.getLogger("mcp_guardian.ui")

try:
    import tkinter as tk
    from tkinter import ttk
    TK_AVAILABLE = True
except Exception:
    TK_AVAILABLE = False


class ConfirmationModalController:
    """
    Runs a dedicated background GUI thread that keeps an Always-On-Top confirmation window
    ready to pop up immediately whenever one or more MCP requests arrive.
    Handles multiple concurrent requests reliably by allowing the user to inspect and
    Allow/Block each pending request in real time.
    """

    def __init__(
        self,
        resolve_callback: Callable[[str, bool, str], None],
        headless_mode: Optional[str] = None,
    ) -> None:
        """
        :param resolve_callback: Function `(request_uuid, allowed, reason)` called when user decides.
        :param headless_mode: Optional fallback if no display is attached (`"allow"`, `"block"`, or `None`).
        """
        self._resolve_callback = resolve_callback
        self._headless_mode = headless_mode
        self._incoming_queue: "queue.Queue[Optional[MCPInterceptedRequest]]" = queue.Queue()
        self._pending_map: Dict[str, MCPInterceptedRequest] = {}
        self._selected_uuid: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._root: Optional["tk.Tk"] = None

    def start(self) -> None:
        """Starts the GUI loop on a daemon thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._gui_thread_main, name="MCPGuardianGUI", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._incoming_queue.put(None)

    def enqueue_request(self, req: MCPInterceptedRequest) -> None:
        """Thread-safe method called when a new MCP request is intercepted."""
        if self._headless_mode in ("allow", "block"):
            allowed = self._headless_mode == "allow"
            self._resolve_callback(
                req.request_uuid,
                allowed,
                f"Auto-{self._headless_mode}ed by configured policy.",
            )
            return
        self._incoming_queue.put(req)

    def _enable_windows_dpi_awareness(self) -> None:
        if platform.system() == "Windows":
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass

    def _force_foreground_windows(self, window: "tk.Tk") -> None:
        """Uses Win32 APIs on Windows alongside Tkinter `-topmost` to guarantee visibility above active apps."""
        window.attributes("-topmost", True)
        window.lift()
        window.focus_force()
        if platform.system() == "Windows":
            try:
                import ctypes
                hwnd = ctypes.windll.user32.GetParent(window.winfo_id()) or window.winfo_id()
                SW_SHOW = 5
                ctypes.windll.user32.ShowWindow(hwnd, SW_SHOW)
                ctypes.windll.user32.SetForegroundWindow(hwnd)
            except Exception:
                pass

    def _gui_thread_main(self) -> None:
        if not TK_AVAILABLE:
            logger.warning("Tkinter not available; falling back to console/fail-closed confirmation.")
            self._fallback_loop()
            return

        self._enable_windows_dpi_awareness()
        try:
            root = tk.Tk()
        except Exception as exc:
            logger.warning("Display not available for Tkinter (%s); using fallback loop.", exc)
            self._fallback_loop()
            return

        self._root = root
        root.title("MCP Guardian — Security Confirmation Required")
        root.geometry("680x540")
        root.minsize(620, 480)
        root.configure(bg="#0F172A")
        root.attributes("-topmost", True)
        root.protocol("WM_DELETE_WINDOW", self._on_window_close_attempt)

        # Start hidden until a request arrives
        root.withdraw()

        # Build UI widgets
        self._build_widgets(root)

        # Poll queue every 80ms
        root.after(80, self._poll_incoming)
        root.mainloop()

    def _build_widgets(self, root: "tk.Tk") -> None:
        # Top Header Banner
        header_frame = tk.Frame(root, bg="#1E293B", padx=18, pady=14)
        header_frame.pack(fill=tk.X)

        title_row = tk.Frame(header_frame, bg="#1E293B")
        title_row.pack(fill=tk.X)

        self._shield_label = tk.Label(
            title_row,
            text="🛡️ MCP GUARDIAN — INTERCEPTED TOOL INVOCATION",
            font=("Segoe UI", 12, "bold"),
            fg="#F8FAFC",
            bg="#1E293B",
        )
        self._shield_label.pack(side=tk.LEFT)

        self._queue_badge = tk.Label(
            title_row,
            text="1 Pending",
            font=("Segoe UI", 9, "bold"),
            fg="#38BDF8",
            bg="#0F172A",
            padx=10,
            pady=3,
        )
        self._queue_badge.pack(side=tk.RIGHT)

        subtitle = tk.Label(
            header_frame,
            text="An AI client requested execution of an external MCP tool. Review parameters before allowing.",
            font=("Segoe UI", 9),
            fg="#94A3B8",
            bg="#1E293B",
            anchor="w",
        )
        subtitle.pack(fill=tk.X, pady=(4, 0))

        # Selector bar for concurrent requests
        selector_frame = tk.Frame(root, bg="#0F172A", padx=18, pady=8)
        selector_frame.pack(fill=tk.X)

        tk.Label(
            selector_frame,
            text="Pending Request Queue:",
            font=("Segoe UI", 9, "bold"),
            fg="#CBD5E1",
            bg="#0F172A",
        ).pack(side=tk.LEFT)

        self._request_combo = ttk.Combobox(selector_frame, state="readonly", width=52)
        self._request_combo.pack(side=tk.LEFT, padx=(10, 0), fill=tk.X, expand=True)
        self._request_combo.bind("<<ComboboxSelected>>", self._on_combo_select)

        # Metadata Grid Card
        meta_frame = tk.Frame(root, bg="#1E293B", padx=16, pady=12, highlightthickness=1, highlightbackground="#334155")
        meta_frame.pack(fill=tk.X, padx=18, pady=(4, 10))

        self._meta_vars = {
            "client": tk.StringVar(value="-"),
            "server": tk.StringVar(value="-"),
            "method": tk.StringVar(value="-"),
            "tool": tk.StringVar(value="-"),
            "risk": tk.StringVar(value="-"),
        }

        fields = [
            ("Origin Client:", "client"),
            ("MCP Server:", "server"),
            ("JSON-RPC Method:", "method"),
            ("Tool / Resource:", "tool"),
            ("Risk Assessment:", "risk"),
        ]
        for idx, (label_text, key) in enumerate(fields):
            tk.Label(
                meta_frame,
                text=label_text,
                font=("Segoe UI", 9, "bold"),
                fg="#94A3B8",
                bg="#1E293B",
                anchor="w",
                width=16,
            ).grid(row=idx, column=0, sticky="w", pady=2)

            val_lbl = tk.Label(
                meta_frame,
                textvariable=self._meta_vars[key],
                font=("Consolas", 10, "bold" if key in ("tool", "risk") else "normal"),
                fg="#F8FAFC",
                bg="#1E293B",
                anchor="w",
            )
            val_lbl.grid(row=idx, column=1, sticky="w", pady=2)
            if key == "risk":
                self._risk_label_widget = val_lbl

        # Request Parameters JSON Viewer
        params_header = tk.Frame(root, bg="#0F172A", padx=18)
        params_header.pack(fill=tk.X)
        tk.Label(
            params_header,
            text="Request Parameters (JSON Payload):",
            font=("Segoe UI", 9, "bold"),
            fg="#CBD5E1",
            bg="#0F172A",
            anchor="w",
        ).pack(side=tk.LEFT)

        text_container = tk.Frame(root, bg="#0F172A", padx=18, pady=6)
        text_container.pack(fill=tk.BOTH, expand=True)

        self._params_text = tk.Text(
            text_container,
            font=("Consolas", 10),
            bg="#020617",
            fg="#E2E8F0",
            insertbackground="#F8FAFC",
            relief=tk.FLAT,
            padx=12,
            pady=10,
            wrap=tk.WORD,
            height=9,
        )
        scrollbar = tk.Scrollbar(text_container, command=self._params_text.yview)
        self._params_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._params_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Action Buttons Footer
        footer = tk.Frame(root, bg="#0F172A", padx=18, pady=14)
        footer.pack(fill=tk.X, side=tk.BOTTOM)

        self._block_btn = tk.Button(
            footer,
            text="✖  BLOCK REQUEST (Esc)",
            font=("Segoe UI", 10, "bold"),
            bg="#EF4444",
            fg="#FFFFFF",
            activebackground="#DC2626",
            activeforeground="#FFFFFF",
            relief=tk.FLAT,
            padx=20,
            pady=10,
            cursor="hand2",
            command=self._on_block_clicked,
        )
        self._block_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 8))

        self._allow_btn = tk.Button(
            footer,
            text="✔  ALLOW REQUEST (Enter)",
            font=("Segoe UI", 10, "bold"),
            bg="#10B981",
            fg="#FFFFFF",
            activebackground="#059669",
            activeforeground="#FFFFFF",
            relief=tk.FLAT,
            padx=20,
            pady=10,
            cursor="hand2",
            command=self._on_allow_clicked,
        )
        self._allow_btn.pack(side=tk.RIGHT, expand=True, fill=tk.X, padx=(8, 0))

        root.bind("<Return>", lambda _e: self._on_allow_clicked())
        root.bind("<Escape>", lambda _e: self._on_block_clicked())

    def _poll_incoming(self) -> None:
        if not self._running or self._root is None:
            return

        updated = False
        while True:
            try:
                item = self._incoming_queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                self._root.destroy()
                return
            self._pending_map[item.request_uuid] = item
            if self._selected_uuid is None:
                self._selected_uuid = item.request_uuid
            updated = True

        if updated:
            self._refresh_view()

        self._root.after(80, self._poll_incoming)

    def _refresh_view(self) -> None:
        if self._root is None:
            return
        count = len(self._pending_map)
        if count == 0:
            self._selected_uuid = None
            self._root.withdraw()
            return

        # Ensure window is visible and on top of all active windows
        self._root.deiconify()
        self._force_foreground_windows(self._root)

        self._queue_badge.configure(
            text=f"{count} Pending Request{'s' if count > 1 else ''}",
            fg="#F59E0B" if count > 1 else "#38BDF8",
        )

        uuids = list(self._pending_map.keys())
        if self._selected_uuid not in self._pending_map:
            self._selected_uuid = uuids[0]

        display_items = [
            f"[{idx + 1}/{count}] {req.server_name} → {req.tool_name} ({req.risk_level})"
            for idx, req in enumerate(self._pending_map.values())
        ]
        self._request_combo["values"] = display_items
        selected_idx = uuids.index(self._selected_uuid)
        self._request_combo.current(selected_idx)

        req = self._pending_map[self._selected_uuid]
        self._meta_vars["client"].set(f"{req.client_source} [{req.transport}]")
        self._meta_vars["server"].set(req.server_name)
        self._meta_vars["method"].set(f"{req.method} (id={req.jsonrpc_id})")
        self._meta_vars["tool"].set(req.tool_name)
        self._meta_vars["risk"].set(req.risk_level)

        risk_colors = {
            RiskLevel.HIGH.value: "#F87171",
            RiskLevel.MEDIUM.value: "#FBBF24",
            RiskLevel.LOW.value: "#34D399",
        }
        self._risk_label_widget.configure(fg=risk_colors.get(req.risk_level, "#F8FAFC"))

        self._params_text.configure(state=tk.NORMAL)
        self._params_text.delete("1.0", tk.END)
        formatted_json = json.dumps(req.arguments, indent=2, ensure_ascii=False)
        self._params_text.insert(tk.END, formatted_json)
        self._params_text.configure(state=tk.DISABLED)

    def _on_combo_select(self, _event: object) -> None:
        idx = self._request_combo.current()
        uuids = list(self._pending_map.keys())
        if 0 <= idx < len(uuids):
            self._selected_uuid = uuids[idx]
            self._refresh_view()

    def _on_allow_clicked(self) -> None:
        if not self._selected_uuid:
            return
        uid = self._selected_uuid
        self._pending_map.pop(uid, None)
        self._resolve_callback(uid, True, "User clicked Allow in MCP Guardian modal.")
        self._refresh_view()

    def _on_block_clicked(self) -> None:
        if not self._selected_uuid:
            return
        uid = self._selected_uuid
        self._pending_map.pop(uid, None)
        self._resolve_callback(uid, False, "User clicked Block in MCP Guardian modal.")
        self._refresh_view()

    def _on_window_close_attempt(self) -> None:
        """Treat closing the confirmation window as blocking the currently selected request (fail-closed)."""
        self._on_block_clicked()

    def _fallback_loop(self) -> None:
        while self._running:
            try:
                item = self._incoming_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break
            allowed = self._headless_mode == "allow"
            self._resolve_callback(
                item.request_uuid,
                allowed,
                "Headless environment fallback policy applied.",
            )
