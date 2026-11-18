"""
Command-Line Interface (`mcp-guardian`) for starting the background confirmation daemon,
wrapping Claude Desktop stdio servers, proxying Claude Web HTTP/SSE MCP traffic,
and patching/restoring `claude_desktop_config.json`.
"""

from __future__ import annotations
import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import List, Optional

from .config_patcher import patch_claude_desktop_config, restore_claude_desktop_config
from .http_proxy import MCPGuardianHTTPServer
from .queue_manager import ApprovalQueueManager
from .stdio_proxy import StdioMCPProxy
from .ui_modal import ConfirmationModalController


def configure_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )


async def run_daemon(
    host: str,
    port: int,
    upstream_url: Optional[str],
    timeout_seconds: float,
    headless_policy: Optional[str],
) -> int:
    queue_mgr = ApprovalQueueManager(default_timeout_seconds=timeout_seconds)
    modal = ConfirmationModalController(
        resolve_callback=queue_mgr.resolve_request,
        headless_mode=headless_policy,
    )
    queue_mgr.set_ui_callback(modal.enqueue_request)
    modal.start()

    http_server = MCPGuardianHTTPServer(
        queue_manager=queue_mgr,
        host=host,
        port=port,
        default_upstream=upstream_url,
    )
    await http_server.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        await http_server.stop()
        modal.stop()
    return 0


async def run_stdio_proxy(
    server_name: str,
    command: List[str],
    daemon_url: Optional[str],
    standalone_ui: bool,
    headless_policy: Optional[str],
) -> int:
    queue_mgr: Optional[ApprovalQueueManager] = None
    modal: Optional[ConfirmationModalController] = None

    if standalone_ui:
        queue_mgr = ApprovalQueueManager()
        modal = ConfirmationModalController(
            resolve_callback=queue_mgr.resolve_request,
            headless_mode=headless_policy,
        )
        queue_mgr.set_ui_callback(modal.enqueue_request)
        modal.start()

    proxy = StdioMCPProxy(
        server_name=server_name,
        command=command,
        queue_manager=queue_mgr,
        daemon_url=None if standalone_ui else daemon_url,
    )
    try:
        return await proxy.run()
    finally:
        if modal is not None:
            modal.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-guardian",
        description="Real-Time MCP Request Monitor & User Confirmation Firewall for Claude Desktop and Claude Web",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # 1. daemon
    p_daemon = sub.add_parser(
        "daemon",
        help="Run the background confirmation modal daemon & Claude Web HTTP/SSE proxy",
    )
    p_daemon.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    p_daemon.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765)")
    p_daemon.add_argument("--upstream-url", default=None, help="Default upstream MCP HTTP endpoint for Claude Web")
    p_daemon.add_argument("--timeout", type=float, default=120.0, help="Confirmation timeout in seconds (default: 120)")
    p_daemon.add_argument(
        "--headless-policy",
        choices=["allow", "block"],
        default=None,
        help="Optional non-interactive policy for automated testing",
    )

    # 2. stdio-proxy
    p_stdio = sub.add_parser(
        "stdio-proxy",
        help="Wrap a local Claude Desktop stdio MCP server command",
    )
    p_stdio.add_argument("--server-name", required=True, help="Logical name of the MCP server (e.g. filesystem)")
    p_stdio.add_argument("--daemon-url", default="http://127.0.0.1:8765", help="Daemon confirmation endpoint URL")
    p_stdio.add_argument("--standalone-ui", action="store_true", help="Launch an in-process modal without daemon")
    p_stdio.add_argument("--headless-policy", choices=["allow", "block"], default=None)
    p_stdio.add_argument("command", nargs=argparse.REMAINDER, help="Target MCP server command after '--'")

    # 3. patch-config
    p_patch = sub.add_parser(
        "patch-config",
        help="Automatically wrap all MCP servers in Claude Desktop's claude_desktop_config.json",
    )
    p_patch.add_argument("--config", type=Path, default=None, help="Path to claude_desktop_config.json")
    p_patch.add_argument("--daemon-url", default="http://127.0.0.1:8765", help="Daemon URL")

    # 4. restore-config
    p_restore = sub.add_parser(
        "restore-config",
        help="Restore original unwrapped MCP server commands in claude_desktop_config.json",
    )
    p_restore.add_argument("--config", type=Path, default=None, help="Path to claude_desktop_config.json")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    if args.subcommand == "daemon":
        return asyncio.run(
            run_daemon(
                host=args.host,
                port=args.port,
                upstream_url=args.upstream_url,
                timeout_seconds=args.timeout,
                headless_policy=args.headless_policy,
            )
        )

    if args.subcommand == "stdio-proxy":
        cmd = list(args.command)
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        if not cmd:
            parser.error("stdio-proxy requires a target command after '--'")
        return asyncio.run(
            run_stdio_proxy(
                server_name=args.server_name,
                command=cmd,
                daemon_url=args.daemon_url,
                standalone_ui=args.standalone_ui,
                headless_policy=args.headless_policy,
            )
        )

    if args.subcommand == "patch-config":
        count, backup = patch_claude_desktop_config(config_path=args.config, daemon_url=args.daemon_url)
        print(f"Patched {count} MCP server(s) in Claude Desktop config. Backup saved to: {backup}")
        return 0

    if args.subcommand == "restore-config":
        count = restore_claude_desktop_config(config_path=args.config)
        print(f"Restored {count} MCP server(s) in Claude Desktop config.")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
