# 🛡️ MCP Guardian — Real-Time Model Context Protocol Interception & Approval Firewall

**MCP Guardian** is a lightweight, zero-dependency Python security proxy and real-time user confirmation firewall that monitors outgoing [Model Context Protocol (MCP)](https://modelcontextprotocol.io/docs/2026-07-28/learn/architecture) requests initiated by **Claude Desktop** and **Claude Web (`claude.ai`)**. Whenever an AI model attempts to invoke an MCP tool (`tools/call`), read a protected resource (`resources/read`), or execute a prompt (`prompts/get`), MCP Guardian intercepts the [JSON-RPC 2.0 message](https://modelcontextprotocol.io/specification/2025-06-18/basic), pauses execution in an asynchronous concurrent queue, and displays an **always-on-top confirmation modal** above all active windows so the user can inspect the payload and either **Allow** or **Block** the request.

## ✨ Key Capabilities

* **Dual-Transport Interception (Claude Desktop & Claude Web)**:
  * **Claude Desktop (`stdio` Subprocess Wrapper)**: Wraps local MCP server commands configured in [`claude_desktop_config.json`](https://modelcontextprotocol.io/docs/2026-07-28/develop/connect-local-servers) using `mcp-guardian stdio-proxy`. Transparently passes protocol handshakes (`initialize`, `tools/list`) while intercepting tool invocations over [MCP `stdio` transport](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports).
  * **Claude Web (`Streamable HTTP` & `SSE` Reverse Proxy + Browser Userscript)**: Provides an asynchronous HTTP reverse proxy (`http://127.0.0.1:8765/mcp/<server_name>`) compatible with [Claude Remote MCP Custom Connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp) and [unlisted web connectors](https://claude.com/docs/connectors/custom/add-unlisted), plus a companion browser userscript (`examples/claude_web_interceptor.user.js`) that routes in-browser `fetch` MCP JSON-RPC calls through the local confirmation daemon.
* **Always-On-Top Confirmation Modal**:
  * Pops up above all active desktop windows (`-topmost True` + Win32 `SetForegroundWindow` & DPI scaling awareness on Windows).
  * Displays Origin Client (`Claude Desktop` vs. `Claude Web`), MCP Server Name, JSON-RPC Method & ID, Tool Name, Automated Heuristic Risk Badge (`LOW`, `MEDIUM`, `HIGH`), and pretty-printed JSON Request Parameters.
  * Keyboard shortcuts (`Enter` to Allow, `Esc` to Block) and fail-closed window-close / timeout handling.
* **Concurrent Multi-Request Queue**:
  * Uses per-request `asyncio.Future` waiters (`ApprovalQueueManager`) so multiple simultaneous MCP tool calls from Claude Desktop and Claude Web are held in parallel without head-of-line blocking.
  * Includes a live queue dropdown selector in the modal (`[1/2]`, `[2/2]`) allowing out-of-order inspection and approval/denial.
* **Graceful Denial & Fail-Closed Reliability**:
  * When blocked, prevents the request from reaching the MCP server and returns a standards-compliant MCP tool error response (`isError: true`) or JSON-RPC 2.0 error (`code: -32001`) back to Claude so the chat session continues cleanly without crashing the transport.
  * Logs all intercepted requests and user decisions to `~/.mcp_guardian/audit.jsonl`.

## 🏗️ Architecture & Interception Approach

### Why Two Interception Layers Are Required

According to the [Model Context Protocol Transports Specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports), MCP communication uses UTF-8 [JSON-RPC 2.0 messages](https://modelcontextprotocol.io/specification/2025-06-18/basic) over two distinct transport mechanisms depending on the host application:

1. **Claude Desktop ([Local Stdio Servers](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop))**:
   * Claude Desktop reads [`claude_desktop_config.json`](https://modelcontextprotocol.io/docs/2026-07-28/develop/connect-local-servers) at launch and spawns each configured MCP server as a local child process, communicating over `stdin` and `stdout` pipes.
   * **Interception Mechanism**:Because OS pipes between parent and child processes do not traverse the network stack, packet sniffers cannot see them. MCP Guardian intercepts this traffic by acting as a **Man-in-the-Middle (MITM) Subprocess Wrapper (`StdioMCPProxy`)**. Claude Desktop launches `mcp-guardian stdio-proxy`, which in turn spawns the real MCP server subprocess and asynchronously pumps `stdin`/`stdout`.
2. **Claude Web ([Remote MCP Custom Connectors](https://modelcontextprotocol.io/docs/2026-07-28/develop/connect-remote-servers))**:
   * Claude Web (`claude.ai`) communicates with remote MCP servers over HTTPS using **Streamable HTTP** (`POST /mcp`) or **HTTP + Server-Sent Events (SSE)** as documented in [Anthropic's Remote MCP Connector Guide](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp).
   * **Interception Mechanism**: MCP Guardian runs a local asynchronous **HTTP/SSE Reverse Proxy (`MCPGuardianHTTPServer`)** on `http://127.0.0.1:8765/mcp/<server_name>`. Users can either place MCP Guardian in front of their remote MCP server (via reverse tunnel such as `cloudflared` / `ngrok` registered in [Claude Custom Connectors](https://claude.com/docs/connectors/custom/add-unlisted)) or load the included `examples/claude_web_interceptor.user.js` script in their browser to route outgoing JSON-RPC `POST` calls from `claude.ai` through the local MCP Guardian daemon.

### System Data Flow

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                        AI HOST CLIENTS (REQUESTERS)                          │
│                                                                              │
│  ┌─────────────────────────────────┐    ┌─────────────────────────────────┐  │
│  │         Claude Desktop          │    │      Claude Web (claude.ai)     │  │
│  │  (Spawns stdio child processes) │    │ (Streamable HTTP / SSE / Fetch) │  │
│  └────────────────┬────────────────┘    └────────────────┬────────────────┘  │
└───────────────────┼──────────────────────────────────────┼───────────────────┘
                    │ newline-delimited JSON-RPC (stdin)   │ HTTP POST /mcp/<srv>
                    ▼                                      ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                         MCP GUARDIAN INTERCEPTION LAYER                      │
│                                                                              │
│  ┌─────────────────────────────────┐    ┌─────────────────────────────────┐  │
│  │   StdioMCPProxy (stdio_proxy)   │    │ MCPGuardianHTTPServer (http)    │  │
│  │ - Passes initialize, tools/list │    │ - Parses JSON-RPC 2.0 POST body │  │
│  │ - Holds tools/call, resources/* │    │ - Holds tools/call, resources/* │  │
│  └────────────────┬────────────────┘    └────────────────┬────────────────┘  │
│                   │ POST /api/v1/confirm                 │ Direct async call │
│                   └──────────────────┬───────────────────┘                   │
│                                      ▼                                       │
│                   ┌─────────────────────────────────────┐                    │
│                   │     ApprovalQueueManager (Async)    │                    │
│                   │  - Per-request asyncio.Future map   │                    │
│                   │  - Risk heuristic classifier        │                    │
│                   │  - Persistent audit.jsonl logger    │                    │
│                   └──────────────────┬──────────────────┘                    │
│                                      │ Thread-safe UI Queue                  │
│                                      ▼                                       │
│                   ┌─────────────────────────────────────┐                    │
│                   │    ConfirmationModalController      │                    │
│                   │  - Always-On-Top Tkinter Popup      │                    │
│                   │  - Multi-request selector dropdown  │                    │
│                   │  - [ALLOW (Enter)] / [BLOCK (Esc)]  │                    │
│                   └──────┬───────────────────────┬──────┘                    │
└──────────────────────────┼───────────────────────┼───────────────────────────┘
                           │ ALLOWED               │ BLOCKED
                           ▼                       ▼
        ┌────────────────────────────────┐  ┌──────────────────────────────────┐
        │ Forward unmodified JSON-RPC to │  │ Return JSON-RPC Denial Response  │
        │ Target MCP Server & return res │  │ (isError: true / code: -32001)   │
        └────────────────────────────────┘  └──────────────────────────────────┘
```

## 📂 Repository Structure

```
mcp-guardian/
├── mcp_guardian/
│   ├── __init__.py          # Package metadata
│   ├── models.py            # JSON-RPC request parsing, risk heuristics & denial payloads
│   ├── queue_manager.py     # Asyncio/Thread-safe concurrent pending request manager & audit log
│   ├── ui_modal.py          # Always-on-top Tkinter confirmation popup with multi-request selector
│   ├── stdio_proxy.py       # Claude Desktop JSON-RPC stdio subprocess wrapper proxy
│   ├── http_proxy.py        # Claude Web Streamable HTTP/SSE reverse proxy & daemon IPC server
│   ├── config_patcher.py    # Automated claude_desktop_config.json backup, wrapper & restore tool
│   └── cli.py               # Unified CLI entry point (`mcp-guardian`)
├── examples/
│   ├── mock_mcp_server.py             # Ready-to-run stdio MCP server for local testing
│   └── claude_web_interceptor.user.js # Browser userscript for claude.ai web interception
├── tests/
│   └── test_mcp_guardian.py           # Automated unit & integration test suite (5 end-to-end tests)
├── demo/
│   └── mcp_guardian_demo.mp4          # Video demonstration of request approval & blocking
├── pyproject.toml
├── requirements.txt
└── README.md
```

## 🚀 Installation & Setup

### Prerequisites

* **Python 3.10+** (uses Python's standard library `asyncio`, `tkinter`, `urllib`, `threading`, `json` — **zero external pip packages required** to run).
* Works on **Windows**, **macOS**, and **Linux**.

### Install from Repository

```bash
git clone https://github.com/aryarajsinh/mcp-guardian.git
cd mcp-guardian
pip install -e .
```

*(Or run directly without installation via `python -m mcp_guardian.cli ...`)*

## 🛠️ Configuration & Execution Guide

### Step 1: Start the Background MCP Guardian Daemon

Start the background daemon first. It hosts the always-on-top confirmation modal manager and listens on `http://127.0.0.1:8765` for both local Claude Desktop `stdio-proxy` wrappers and Claude Web HTTP MCP requests:

```bash
python -m mcp_guardian.cli daemon --host 127.0.0.1 --port 8765 --timeout 120
```

### Step 2: Configure Claude Desktop (Automatic or Manual)

#### Option A: Automatic One-Command Patching (Recommended)

MCP Guardian can automatically back up and wrap all MCP servers in your [`claude_desktop_config.json`](https://modelcontextprotocol.io/docs/2026-07-28/develop/connect-local-servers) (`%APPDATA%\Claude\claude_desktop_config.json` on Windows or `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```bash
python -m mcp_guardian.cli patch-config
```

To restore your original unmodified configuration at any time:

```bash
python -m mcp_guardian.cli restore-config
```

#### Option B: Manual `claude_desktop_config.json` Wrapper Example

Wrap any MCP server command in `claude_desktop_config.json` using `mcp_guardian.cli stdio-proxy`:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "python",
      "args": [
        "-m",
        "mcp_guardian.cli",
        "stdio-proxy",
        "--server-name",
        "filesystem",
        "--daemon-url",
        "http://127.0.0.1:8765",
        "--",
        "npx",
        "-y",
        "@modelcontextprotocol/server-filesystem",
        "C:/Users/Public/Documents"
      ]
    }
  }
}
```

Restart Claude Desktop after saving.

### Step 3: Configure Claude Web (`claude.ai`) Interception

1. **Via Remote MCP Custom Connector Proxy**:
   * Run the daemon forwarding to your upstream MCP HTTP server:
     ```bash
     python -m mcp_guardian.cli daemon --port 8765 --upstream-url https://your-upstream-mcp.example.com/mcp
     ```
   * Expose `http://127.0.0.1:8765/mcp/my-connector` via HTTPS (e.g., `cloudflared tunnel --url http://127.0.0.1:8765` or `ngrok http 8765`) and add the URL under **Settings → Connectors → Add Custom Connector** in [Claude Web](https://claude.com/docs/connectors/custom/add-unlisted).
2. **Via Browser Userscript (`examples/claude_web_interceptor.user.js`)**:
   * Install the userscript in Tampermonkey/Violentmonkey on `https://claude.ai/*`. Any browser-side JSON-RPC `tools/call` `fetch` request is automatically redirected through `http://127.0.0.1:8765/mcp/claude-web` and held until you click **Allow** or **Block** in the desktop popup.

## 🧪 Running the Automated Verification Suite

Run the included end-to-end test suite to verify risk classification, concurrent multi-request queuing, Claude Desktop `stdio` interception, Claude Web HTTP proxy interception, and config patching:

```bash
python -m unittest discover -s tests -v
```

## 🎬 Demonstration Walkthrough

The included video `demo/mcp_guardian_demo.mp4` demonstrates:

1. **Scenario 1 — Safe Request Approval (`ALLOW`)**: Claude Desktop invokes `read_file` (`{"path": "/workspace/project/config.yaml"}`) on `filesystem-mcp`. The always-on-top MCP Guardian modal displays the `LOW` risk badge and request parameters. Clicking **ALLOW REQUEST** releases the JSON-RPC call to the MCP server and returns the file content to Claude.
2. **Scenario 2 — Concurrent Requests & High-Risk Blocking (`BLOCK`)**: Claude Web and Claude Desktop simultaneously trigger `run_sql` (`{"sql": "DROP TABLE production_users;"}`) on `postgres-connector` (`HIGH` risk) and `list_commits` on `github-mcp` (`LOW` risk). MCP Guardian holds both requests concurrently (`2 Pending Requests`), blocks the destructive SQL query with a structured JSON-RPC denial response, and allows the safe GitHub query.
