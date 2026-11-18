"""
Automatic Configuration Patcher & Restorer for Claude Desktop (`claude_desktop_config.json`).
Wraps configured MCP servers with `mcp-guardian stdio-proxy` so all outgoing MCP tool calls
from Claude Desktop are transparently intercepted without manual JSON editing.
"""

from __future__ import annotations
import json
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def get_default_claude_config_path() -> Path:
    """Returns the platform-specific path to `claude_desktop_config.json`."""
    system = platform.system()
    if system == "Windows":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def patch_claude_desktop_config(
    config_path: Optional[Path] = None,
    daemon_url: str = "http://127.0.0.1:8765",
    python_executable: Optional[str] = None,
) -> Tuple[int, Path]:
    """
    Wraps each entry in `mcpServers` inside `claude_desktop_config.json` with
    `python -m mcp_guardian.cli stdio-proxy --server-name <name> --daemon-url <url> -- <orig_cmd> <orig_args...>`.
    Creates a `.mcp_guardian.bak` backup of the original file before modifying.
    Returns `(wrapped_count, backup_path)`.
    """
    target_path = config_path or get_default_claude_config_path()
    if not target_path.exists():
        raise FileNotFoundError(f"Claude Desktop config not found at {target_path}")

    backup_path = target_path.with_suffix(".json.mcp_guardian.bak")
    if not backup_path.exists():
        shutil.copy2(target_path, backup_path)

    data: Dict[str, Any] = json.loads(target_path.read_text(encoding="utf-8"))
    mcp_servers: Dict[str, Any] = data.get("mcpServers", {})
    py_exec = python_executable or sys.executable
    wrapped_count = 0

    for name, spec in mcp_servers.items():
        if not isinstance(spec, dict) or "command" not in spec:
            continue
        orig_cmd = spec["command"]
        orig_args = spec.get("args", [])

        # Skip if already wrapped by mcp_guardian
        if "mcp_guardian" in str(orig_cmd) or any("mcp_guardian" in str(a) for a in orig_args):
            continue

        spec["_mcp_guardian_original"] = {
            "command": orig_cmd,
            "args": orig_args,
        }
        spec["command"] = py_exec
        spec["args"] = [
            "-m",
            "mcp_guardian.cli",
            "stdio-proxy",
            "--server-name",
            name,
            "--daemon-url",
            daemon_url,
            "--",
            orig_cmd,
            *orig_args,
        ]
        wrapped_count += 1

    target_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return wrapped_count, backup_path


def restore_claude_desktop_config(config_path: Optional[Path] = None) -> int:
    """Restores original MCP server commands in `claude_desktop_config.json`."""
    target_path = config_path or get_default_claude_config_path()
    if not target_path.exists():
        raise FileNotFoundError(f"Claude Desktop config not found at {target_path}")

    data: Dict[str, Any] = json.loads(target_path.read_text(encoding="utf-8"))
    mcp_servers: Dict[str, Any] = data.get("mcpServers", {})
    restored_count = 0

    for _name, spec in mcp_servers.items():
        if isinstance(spec, dict) and "_mcp_guardian_original" in spec:
            orig = spec.pop("_mcp_guardian_original")
            spec["command"] = orig["command"]
            spec["args"] = orig.get("args", [])
            restored_count += 1

    target_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return restored_count
