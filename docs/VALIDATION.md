# Validation record

Identifiers (Slack workspace, channel and user IDs, session and chat IDs,
tunnel IDs, hostnames) have been removed from this public record.

## slack_bridge

- 23 September 2026, feasibility probe (not included in this repository): a
  standalone process woke an idle Codex desktop session through its bundled
  app-tools MCP connector, and delivered a message to a Claude Code session
  over its messaging socket. A Slack mention reached each agent and each
  agent's acknowledgement came back to the Slack thread.
- 23 September to 8 October 2026, daily use: 242 messages among four
  registered agents (two Codex, two Claude Code). Final states: 210 complete
  with a posted reply, 26 delivered without a reply marker (reported, queue
  released), 4 received and awaiting a reply, 1 uncertain (reported, not
  resent), 1 cancelled by removal. 79 native delivery receipts were recorded
  after receipt tracking shipped on 2 October.
- Not validated: machine reboot, desktop application upgrades, macOS.

## chat_mcp

- 22 September 2026: 13 tests passed over real stdio and loopback Streamable
  HTTP transports (create, read, edit, binary upload, listing, backups,
  stale-write rejection, path-escape rejection, symlink and hard-link
  rejection). An earlier run failed because tool results lacked structured
  output; return annotations were fixed and all tests reran green.
- 24 September 2026: official `tunnel-client` v0.0.14 installed (archive
  matched the release digest); `doctor` passed; health and readiness
  endpoints returned 200.
- 25 September 2026: an ordinary ChatGPT chat with the tunnel connector
  enabled read a JSON input file through MCP (the prompt did not contain the
  file's token or payload), then wrote a Markdown file with Unicode text, a
  64-byte binary file, and a completion file. All bytes and the completion
  token matched locally.
- Not validated: large files near the 8 MiB limit, files generated inside
  ChatGPT's own execution sandbox.

## This repository

- `python -m pytest`: 68 passed (55 slack_bridge, 13 chat_mcp) on Python
  3.13 with the pinned `requirements.lock`.
