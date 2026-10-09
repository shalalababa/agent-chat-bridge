# chat_mcp: writable file exchange for MCP clients

A small MCP server (official Python SDK, FastMCP) that lets an MCP client
list, read, create, upload and edit files under one configured directory.
Its main use here is giving an ordinary ChatGPT chat in developer mode a
file channel to the same folder local agents read, so a Codex agent can leave
a prompt file for a chat and pick up the chat's report as a file.

## Run

```bash
mkdir -p exchange
.venv/bin/python chat_mcp/server.py --root "$PWD/exchange"          # stdio (default)
.venv/bin/python chat_mcp/server.py --root "$PWD/exchange" --transport streamable-http
```

`--root` can also come from `CHAT_FILE_MCP_ROOT`. The HTTP endpoint is
`http://127.0.0.1:8765/mcp` (`--port` to change). It has **no application
authentication** and binds to loopback only. Do not expose it through a
public proxy; use stdio behind an authenticated tunnel instead.

## Tools

- `list_files(path="")`: list a directory (up to 1,000 entries).
- `read_file(path, encoding="utf-8")`: read text or base64 bytes, with SHA-256.
- `write_file(path, content, encoding="utf-8", expected_sha256=null)`: create
  a file; upload binary with `encoding="base64"`. Replacing an existing file
  requires its current SHA-256 from `read_file`.
- `edit_file(path, old_text, new_text, expected_sha256)`: replace exactly one
  matching passage, with the same SHA-256 check.
- `make_directory(path)`: create a directory; its parent must exist.

Read-only tools carry `readOnlyHint`; write tools carry `destructiveHint`.

## Sandbox rules

- Paths are relative to `--root`. Empty, `.`, `..`, absolute, `.git` and
  `.history` components are rejected.
- Every path component is opened from a root directory file descriptor with
  `O_NOFOLLOW`, so symlinks (file or directory) cannot escape. Files with
  more than one hard link are rejected.
- Maximum file size 8 MiB. No shell or execution tool.
- New files are published with an atomic no-clobber `link`; replacements
  re-check the hash immediately before an atomic `rename`. Replaced content
  is saved to `ROOT/.history/<uuid>` (returned as `backup`); backups are not
  pruned automatically.
- Single-user. SHA checks prevent accidental stale overwrites by the client;
  they are not a transaction against other local processes writing the same
  file.

## Connect an ordinary ChatGPT chat

The server does not create chats, send prompts, or install itself in
ChatGPT. Use developer mode and an authenticated private MCP tunnel:

1. Create a Secure MCP Tunnel in the OpenAI Platform tunnel settings, note
   its tunnel ID, and associate it with the intended ChatGPT workspace.
2. Install the official `tunnel-client` and provide its API key through the
   documented environment variable, from a file outside this repository.
3. Create a stdio profile that launches this server:

   ```bash
   tunnel-client init --sample sample_mcp_stdio_local --profile research-files \
     --tunnel-id YOUR_TUNNEL_ID \
     --mcp-command "/path/to/agent-chat-bridge/.venv/bin/python /path/to/agent-chat-bridge/chat_mcp/server.py --root /path/to/agent-chat-bridge/exchange"
   tunnel-client doctor --profile research-files --explain
   tunnel-client run --profile research-files
   ```

4. In ChatGPT, add a developer-mode MCP connection using that tunnel and
   confirm the five tools are discovered.
5. In a chat with the connection enabled, ask it to read a test file, create
   another, and edit it; verify the bytes locally.

Reference: https://developers.openai.com/api/docs/guides/secure-mcp-tunnels

For Claude Code, Codex or another local client, use stdio directly; see
`mcp-client.example.json`. Connecting this server to a local agent does not
by itself let the agent send prompts to ChatGPT chats.

## Tests

`python -m pytest chat_mcp` (13 tests, temporary directories only): file
confinement, symlink and hard-link rejection, backups, stale writes, binary
transfer, and real MCP round trips over a stdio subprocess and loopback
Streamable HTTP.
