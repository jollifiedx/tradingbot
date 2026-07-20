---
name: webull-mcp-server
description: Build or extend the read-only dev MCP server wrapping the Webull client — positions, orders, account, bars — for debugging against the paper account. Use for Webull MCP server work.
argument-hint: [tool to add or change]
---

Webull MCP task: $ARGUMENTS

Purpose: let Claude Code inspect live paper-account state during development
("what does Webull think our positions are right now?") without touching the audited
worker path.

- Built on the MCP Python SDK, wrapping the existing typed client in `backend/app/core/`
  — never calling the SDK directly.
- **READ-ONLY is structural, not conventional:** the server module imports only the read
  methods of the client wrapper. No tool that places, modifies, or cancels orders, moves
  funds, or writes settings may EVER be added — a request to add one is an escalation,
  not a task. Add a test asserting the server exposes no mutating tool names.
- Paper environment credentials only; the server refuses to start if `WEBULL_ENV=live`.
- Registered in `.mcp.json` for dev sessions; never deployed to Railway.
- Tools return compact JSON (positions, open orders, account snapshot, recent bars) sized
  for context windows — summarize, don't dump.
