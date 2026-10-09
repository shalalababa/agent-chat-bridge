# Changelog

This repository starts with a fresh history. The two components were
developed in separate private repositories; their history is summarized here
instead of being carried over (old commits contained deployment-specific
identifiers).

## 0.1.0 - 2026-10-09: standalone package

- Combined the Slack bridge and the file-exchange MCP server into one
  repository (`slack_bridge/`, `chat_mcp/`).
- Moved all machine-specific paths into `slack_bridge/settings.py` with
  environment overrides; added `config.example.json`, `.env.example`, a
  portable systemd unit using `%h`, and a single `bin/agent-bridge` launcher
  that also exposes `send`, `run` and `status`.
- Slack tokens can come from the environment when no credentials file
  exists, and are removed from the environment passed to the Codex
  subprocess.
- `chat_mcp/server.py` accepts `CHAT_FILE_MCP_ROOT` in place of `--root`.
- The Home tab example uses the registered aliases instead of fixed names.
- Agent guide generalized; deployment-specific authorizations removed.
- One pytest configuration runs both test suites (68 tests).

## slack_bridge history (23 September - 2 October 2026)

- 23 Sep: persistent Socket Mode bridge delivering Slack mentions into
  existing Codex and Claude Code sessions, with a durable SQLite queue,
  outbox and transcript cursors (preceded the same day by a feasibility
  probe that woke each runtime from a standalone process).
- 23 Sep: plain-text `@alias` addressing, Slack Home tab and `/agents`
  manager with add/edit/pause/resume/remove and a session picker; shorter
  per-message instructions; reply marker as a separate first line; refresh
  of `/agents list` cards; Codex rollout resolution through the desktop
  thread index.
- 24 Sep: release the queue when a Codex turn finishes without a reply
  marker (`delivered_no_reply`) instead of stalling.
- 25 Sep: deliver to busy Claude Code sessions through the native
  `priority: next` queue.
- 27 Sep: accept reply markers after a prose preamble while ignoring fenced
  or quoted examples and ambiguous markers.
- 1 Oct: distinguish a missing Codex backend from a busy recipient; support
  the plugin-path form of the desktop app-tools launch configuration.
- 2 Oct: separate native delivery receipts from replies
  (`queued -> sending -> sent -> received -> complete`), follow transcript
  rollovers, quarantine unconfirmed sends after 120 s; `send` starts and
  health-checks the service before accepting a message, with one automatic
  retry and a concise activation hint.

## chat_mcp history (22 - 25 September 2026)

- 22 Sep: writable file MCP server with confinement, hash-checked writes,
  backups, and stdio plus loopback HTTP protocol tests. The first protocol
  run exposed missing structured tool output; fixed with return annotations.
- 24-25 Sep: connected to ChatGPT through OpenAI's Secure MCP Tunnel and
  validated a real text and binary round trip (see `docs/VALIDATION.md`).
