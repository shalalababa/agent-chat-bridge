"""Writable local-file MCP. Prefer stdio behind an authenticated MCP tunnel."""
import argparse
import os
from typing import Any
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from file_store import FileStore


def build_server(root, port=8765):
    store = FileStore(root)
    app = FastMCP('Local research files', host='127.0.0.1', port=port,
                  stateless_http=True, json_response=True)
    read = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

    @app.tool(annotations=read)
    def list_files(path: str = '') -> dict[str, Any]:
        """List a directory in the configured local folder; paths are relative."""
        return store.list_files(path)

    @app.tool(annotations=read)
    def read_file(path: str, encoding: str = 'utf-8') -> dict[str, Any]:
        """Read a local file as utf-8 or base64, with its SHA-256. Maximum 8 MiB."""
        return store.read_file(path, encoding)

    @app.tool(annotations=write)
    def write_file(path: str, content: str, encoding: str = 'utf-8', expected_sha256: str | None = None) -> dict[str, Any]:
        """Create/upload a local file or replace it. Binary uploads use base64. For an existing file, first read it and pass its current sha256. Replaced contents are backed up. Parent directory must exist."""
        return store.write_file(path, content, encoding, expected_sha256)

    @app.tool(annotations=write)
    def edit_file(path: str, old_text: str, new_text: str, expected_sha256: str) -> dict[str, Any]:
        """Replace one unique text passage in a local file, checking its current sha256 and backing up the original."""
        return store.edit_file(path, old_text, new_text, expected_sha256)

    @app.tool(annotations=write)
    def make_directory(path: str) -> dict[str, Any]:
        """Create a directory beneath the local root. Parent directory must exist."""
        return store.make_directory(path)

    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default=os.environ.get('CHAT_FILE_MCP_ROOT'),
                        help='Directory exposed to the client (or set CHAT_FILE_MCP_ROOT)')
    parser.add_argument('--transport', choices=['stdio', 'streamable-http'], default='stdio')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if not args.root:
        parser.error('--root or CHAT_FILE_MCP_ROOT is required')
    build_server(args.root, args.port).run(transport=args.transport)
