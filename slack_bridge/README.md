# slack_bridge: Slack delivery into existing agent sessions

A single-user daemon that turns a Slack channel into a front door for
long-running Claude Code and Codex sessions already open on your machine. It
does not start new conversations, wrap an LLM API, or change either runtime's
permissions; it delivers text into the exact session you registered and posts
that session's final answer back to the Slack thread.

## Use

In a configured channel, type plain-text aliases:

```text
@astra your message
@fable your message
@astra @fable your message
```

Aliases are not separate Slack bot users and do not appear in mention
autocomplete. Only leading aliases select recipients; an alias in the body
stays part of the message. Bot messages and edits are ignored. A native app
mention also works:

```text
@Agent Bridge astra your message
@Agent Bridge to:astra,fable your message
```

The bridge acknowledges receipt ("Queued for Astra."), posts a delivery
receipt when the agent's own transcript shows the message arrived, and posts
the final answer in the same thread. Other channel discussion does not wake
agents.

## Agent manager

Open **Agent Bridge -> Home** in Slack, or use `/agents` / `/agents list`.
The Home tab offers Add, Edit, Pause/Resume, Check connection, and Remove.
Add and Edit open a form with a session picker listing recent local Codex
tasks and live local Claude Code sessions; `codex:SESSION_ID` or
`claude:SESSION_ID` can be pasted for a saved session not in the list.
Check connection tests availability without waking the model.

```text
/agents add
/agents edit astra
/agents pause fable
/agents resume fable
/agents remove fable
```

Local equivalents (via the `bin/agent-bridge` launcher):

```bash
agent-bridge list
agent-bridge discover
agent-bridge add reviewer --session codex:SESSION_ID
agent-bridge edit reviewer --name reviewer2
agent-bridge pause reviewer2
agent-bridge resume reviewer2
agent-bridge remove reviewer2
```

Aliases start with a letter and use letters, digits, `-` or `_` (`user` and
`agents` are reserved). Rename preserves the registration identity, so old
reply contexts keep their association. Changing an alias or session waits
until pending deliveries finish. Remove cancels unsent messages, waits for
in-flight replies, and never closes the underlying conversation. Settings are
written atomically under a file lock with a revision counter, so stale forms
are rejected, and the running service reloads them automatically. Only the
configured owner can manage agents.

## Configuration

| Item | Default | Override |
| --- | --- | --- |
| Non-secret config (`config.example.json`) | `~/.config/agent-bridge/config.json` | `AGENT_BRIDGE_CONFIG` |
| Slack tokens, `KEY=value`, mode 0600 | `~/.config/agent-bridge/slack.env` | `AGENT_BRIDGE_SLACK_ENV` (or `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` in the environment when the file is absent) |
| Queue, outbox, cursors (`bridge.sqlite3`), `health.json` | `~/.local/state/agent-bridge` | `AGENT_BRIDGE_STATE` |
| Claude Code session registrations and transcripts | `~/.claude` | `CLAUDE_CONFIG_DIR` |
| Codex rollout transcripts | `~/.codex/sessions` | `CODEX_SESSIONS_DIR` |
| systemd unit name | `agent-bridge.service` | `AGENT_BRIDGE_SERVICE` |

Config fields: `team` (Slack workspace ID), `bot` (the app's bot user ID),
`users` (owner user IDs allowed to send and manage), `channels` (channel IDs
the bridge listens in), `origin_thread` (a Codex thread ID used as the source
of native Codex deliveries; receipts are only accepted from envelopes carrying
it), and `agents` (alias -> `kind`, `session`, `transcript`, `label`). The
manager fills `id`, `paused` and `revision`.

## Delivery protocol

Queue acceptance, recipient receipt, and optional reply are separate
milestones: `queued -> sending -> sent -> received -> complete`.

- Incoming Slack events are written to SQLite before the Socket Mode ack and
  deduplicated by channel and timestamp (Slack can emit both `message` and
  `app_mention` for one post). The queued receipt is committed with the event.
- One unconfirmed send per agent at a time; FIFO order is preserved even when
  the oldest message has a retry delay.
- `received` requires a native incoming record in the exact session
  transcript (a Codex `send_message_to_thread` envelope bound to its turn ID,
  or a Claude Code inbound user record for that session), never
  model-written acknowledgement text. It releases the next message, even if
  the first turn has not answered. A received message is never resent just
  because its answer is missing.
- Final answers carry `BRIDGE_REPLY <message id>` on their own line. The
  bridge accepts a prose preamble before it, ignores fenced or quoted
  examples and ambiguous multiple markers, strips the marker and posts the
  rest. A finished Codex turn without a marker becomes `delivered_no_reply`
  and is reported; late marked answers are still accepted.
- A dispatch interrupted by a crash, or an accepted send with no receipt
  after 120 seconds, becomes `uncertain`: it is reported in Slack, never
  blindly resent, and does not block later messages.
- Slack posts go through a persistent outbox with idempotent keys and
  exponential backoff. A post accepted by Slack just before a crash may be
  sent again because its success was not recorded locally. Socket Mode
  reconnects automatically.
- Codex conversations that span several rollout files are followed through
  the desktop backend's thread index; replies already written in a new
  segment are recovered once.
- `health.json` exposes the Slack connection, per-status message counts,
  connection errors, and receipt-reader errors.

The protocol gives durable local deduplication and evidence-backed receipt
tracking. It does not promise exactly-once execution by the external
runtimes, instant attention during approvals, or availability while the apps
or network are down.

## Agent-to-agent messages

Each delivered message links to `AGENT_GUIDE.md`. An agent runs
`agent-bridge send --to ALIAS --context MESSAGE_ID --text-file FILE`; the
sender is identified from `CODEX_THREAD_ID` or by matching a registered
Claude Code process among its ancestors, and the context ID must belong to a
message delivered to that agent. The peer message is posted visibly in the
original thread and queued for the recipient, labeled as carrying no new user
authorization. `--to user` posts to the thread without waking another agent.

Before enqueueing, `send` checks the systemd unit, a fresh heartbeat from the
same PID, and a live Slack connection; if any fails it runs one
`systemctl --user start` and re-checks for up to 15 seconds, otherwise it
exits with an error and queues nothing.

## How delivery works (integration points)

- **Codex**: the bridge finds the running Codex desktop process in `/proc`,
  reads its `mcp_servers.codex_app` launch configuration and WSL interop
  environment, spawns the bundled app-tools MCP server over stdio, and calls
  `read_thread` (readiness) and `send_message_to_thread` (delivery).
- **Claude Code**: the bridge reads the session's registration in
  `~/.claude/sessions/*.json`, connects to its messaging Unix socket, checks
  the socket owner's PID with `SO_PEERCRED`, and writes one `priority: next`
  user frame, which Claude Code queues through its normal inbound checks even
  while busy. Verified against Claude Code 2.1.280.

Both are internal desktop interfaces and can change with app updates;
failures surface as connection errors, not silent successes.

## Run

```bash
systemctl --user status agent-bridge.service
journalctl --user -u agent-bridge.service -n 50
agent-bridge status          # prints health.json
```

The service does not boot WSL or launch missing agent sessions; keep the
Codex desktop app and the registered Claude Code sessions open. Without
systemd linger, the service stops with the user's login session.

## Tests

`python -m pytest slack_bridge` (no network). They cover routing,
sender/channel filtering, durable deduplication, reply identity, restart
during dispatch, transcript cursor replay and rollover, native receipts for
both runtimes, quarantine of unconfirmed sends, registry edits, Slack modal
submission, owner checks, duplicate interactive callbacks, and service
activation.
