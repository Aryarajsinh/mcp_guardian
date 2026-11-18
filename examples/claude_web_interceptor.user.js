// ==UserScript==
// @name         MCP Guardian — Claude Web (claude.ai) Fetch & SSE Interceptor
// @namespace    https://github.com/aryarajsinh/mcp-guardian
// @version      1.0.0
// @description  Intercepts outgoing MCP tool calls initiated in Claude Web (claude.ai) and routes them through the local MCP Guardian confirmation daemon before execution.
// @match        https://claude.ai/*
// @grant        none
// ==/UserScript==

(function () {
  "use strict";

  const GUARDIAN_DAEMON_URL = "http://127.0.0.1:8765/mcp/claude-web";
  const originalFetch = window.fetch;

  function isMCPToolInvocation(url, init) {
    if (!init || init.method !== "POST" || !init.body) return false;
    try {
      const bodyStr = typeof init.body === "string" ? init.body : null;
      if (!bodyStr) return false;
      const parsed = JSON.parse(bodyStr);
      return (
        parsed &&
        parsed.jsonrpc === "2.0" &&
        (parsed.method === "tools/call" ||
          parsed.method === "resources/read" ||
          parsed.method === "prompts/get")
      );
    } catch (_err) {
      return false;
    }
  }

  window.fetch = async function (input, init) {
    const url = typeof input === "string" ? input : input.url;
    if (isMCPToolInvocation(url, init)) {
      const guardianHeaders = Object.assign({}, init.headers || {}, {
        "Content-Type": "application/json",
        "X-MCP-Target-URL": url,
        "X-MCP-Server-Name": new URL(url, window.location.origin).hostname,
      });
      return originalFetch(GUARDIAN_DAEMON_URL, {
        method: "POST",
        headers: guardianHeaders,
        body: init.body,
      });
    }
    return originalFetch.apply(this, arguments);
  };

  console.info("[MCP Guardian] Claude Web MCP request interceptor active ->", GUARDIAN_DAEMON_URL);
})();
