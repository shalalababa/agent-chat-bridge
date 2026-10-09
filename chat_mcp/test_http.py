"""A real local Streamable HTTP transport test, without publishing a port."""
from pathlib import Path
import socket
import subprocess
import sys
import time

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def test_http_roundtrip(tmp_path):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('server.py')),
                                '--root', str(tmp_path), '--transport', 'streamable-http', '--port', str(port)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f'http://127.0.0.1:{port}/mcp'
    try:
        with httpx.Client(timeout=1) as client:
            for _ in range(100):
                if process.poll() is not None:
                    raise AssertionError('HTTP server exited before ready')
                try:
                    client.get(url)
                    break
                except httpx.ConnectError:
                    time.sleep(0.1)
            else:
                raise AssertionError('HTTP server did not start')

        async def run():
            async with streamable_http_client(url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool('write_file', {'path': 'http.txt', 'content': 'http-roundtrip'})
                    assert not result.isError
                    result = await session.call_tool('read_file', {'path': 'http.txt'})
                    assert result.structuredContent['content'] == 'http-roundtrip'
                    assert (tmp_path / 'http.txt').read_text() == 'http-roundtrip'
        anyio.run(run)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
