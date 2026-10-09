"""Real subprocess MCP integration: handshake, discovery, reads and mutations."""
import base64
from pathlib import Path
import sys

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_stdio_roundtrip(tmp_path):
    async def run():
        parameters = StdioServerParameters(command=sys.executable, args=[
            str(Path(__file__).with_name('server.py')), '--root', str(tmp_path)])
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {x.name for x in (await session.list_tools()).tools}
                assert names == {'list_files', 'read_file', 'write_file', 'edit_file', 'make_directory'}
                result = await session.call_tool('write_file', {'path': 'note.txt', 'content': 'first'})
                assert not result.isError
                result = await session.call_tool('read_file', {'path': 'note.txt'})
                data = result.structuredContent
                assert data['content'] == 'first'
                result = await session.call_tool('edit_file', {'path': 'note.txt', 'old_text': 'first', 'new_text': 'second', 'expected_sha256': data['sha256']})
                assert not result.isError
                assert (tmp_path / 'note.txt').read_text() == 'second'
                result = await session.call_tool('write_file', {'path': 'upload.bin', 'content': base64.b64encode(b'\x00\xff\x01').decode(), 'encoding': 'base64'})
                assert not result.isError
                assert (tmp_path / 'upload.bin').read_bytes() == b'\x00\xff\x01'
                result = await session.call_tool('write_file', {'path': '../escape', 'content': 'no'})
                assert result.isError
                result = await session.call_tool('write_file', {'path': 'note.txt', 'content': 'no'})
                assert result.isError
    anyio.run(run)
