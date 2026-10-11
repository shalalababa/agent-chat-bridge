import json
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
import time
import tomllib
import urllib.request
import urllib.parse
import uuid

from settings import CLAUDE_HOME, CREDENTIALS

SLACK_KEYS = ('SLACK_BOT_TOKEN', 'SLACK_APP_TOKEN')


class Unavailable(Exception):
    pass


def slack_response(url, payload):
    """Update an ephemeral interaction response; never log the capability URL."""
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ''
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443)
            or not any(host == domain or host.endswith('.' + domain) for domain in ('slack.com', 'slack-gov.com'))):
        raise ValueError('Invalid Slack interaction response endpoint')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=15) as response:
        body = response.read()
    if body.strip() not in (b'', b'ok'):
        result = json.loads(body)
        if result.get('ok') is False:
            raise RuntimeError('Slack interaction response was rejected')


def slack_credentials():
    # Prefer the 0600 credentials file; fall back to the process environment.
    if CREDENTIALS.exists():
        return dict(line.strip().split('=', 1) for line in CREDENTIALS.read_text().splitlines()
                    if '=' in line and not line.lstrip().startswith('#'))
    return {key: os.environ[key] for key in SLACK_KEYS if os.environ.get(key)}


def slack(method, payload, app=False):
    credentials = slack_credentials()
    key = 'SLACK_APP_TOKEN' if app else 'SLACK_BOT_TOKEN'
    if key not in credentials:
        raise RuntimeError('Missing ' + key + ' in ' + str(CREDENTIALS) + ' or the environment')
    req = urllib.request.Request('https://slack.com/api/' + method, data=json.dumps(payload).encode(),
                                 headers={'Authorization': 'Bearer ' + credentials[key], 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=20) as response:
        data = json.load(response)
    if not data.get('ok'):
        raise RuntimeError('Slack rejected ' + method + ': ' + str(data.get('error')))
    return data


def claude_owner(session_id):
    matches = []
    for path in (CLAUDE_HOME / 'sessions').glob('*.json'):
        try:
            state = json.loads(path.read_text())
            proc = Path('/proc') / str(state.get('pid'))
            if state.get('sessionId') != session_id or (proc / 'comm').read_text().strip() != 'claude':
                continue
            if Path(state['messagingSocketPath']).exists():
                matches.append(state)
        except (OSError, ValueError):
            continue
    if len(matches) != 1:
        raise Unavailable('Claude session has no unique live endpoint')
    return matches[0]


def readiness(agent, origin):
    if agent['kind'] == 'claude':
        owner = claude_owner(agent['session'])
        # Native UDS messages enter Claude's priority=next queue. Busy can
        # include background children, so waiting for idle starves delivery.
        if owner.get('status') not in ('idle', 'busy'):
            raise Unavailable('Claude session is unavailable')
        return owner
    try:
        result = codex_call('read_thread', {'threadId': agent['session'], 'turnLimit': 1, 'maxOutputCharsPerItem': 100}, origin)
        item = json.loads(next(x['text'] for x in result['content'] if x.get('type') == 'text'))
        if item['thread']['status']['type'] not in ('idle', 'notLoaded', 'active'):
            raise Unavailable('Codex session is busy or unavailable')
        return None
    except Unavailable:
        raise
    except Exception as error:
        if isinstance(error, RuntimeError) and str(error) == 'Desktop Codex app-tools configuration not found':
            raise Unavailable('Codex WSL backend connection missing; the desktop may have failed to start its configured WSL agent') from error
        raise Unavailable('Codex desktop connection unavailable') from error


def dispatch(agent, prompt, origin, prepared):
    if agent['kind'] == 'codex':
        return codex_call('send_message_to_thread', {'threadId': agent['session'], 'prompt': prompt}, origin)
    owner = claude_owner(agent['session'])
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        try:
            client.connect(owner['messagingSocketPath'])
        except OSError as error:
            raise Unavailable('Claude endpoint disconnected before delivery') from error
        peer_pid = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[0]
        if peer_pid != owner['pid']:
            raise Unavailable('Claude endpoint owner changed')
        frame = {'type': 'user', 'session_id': agent['session'], 'uuid': str(uuid.uuid4()),
                 'from': 'Agent Bridge', 'priority': 'next', 'message': {'role': 'user', 'content': prompt}}
        client.sendall((json.dumps(frame) + '\n').encode())
        client.shutdown(socket.SHUT_WR)

def normalize_app_tools(settings):
    # New desktop builds expose a plugin path instead of a launcher args list.
    if not settings.get('args'):
        node = settings.get('env', {}).get('CODEX_MCP_NODE_PATH')
        plugin = settings.get('plugin_path')
        if not node or not plugin:
            raise ValueError('Incomplete desktop app-tools launch configuration')
        settings = {**settings, 'args': [node, plugin.rstrip('/\\') + '/server.mjs']}
    return settings


def app_tools_config():
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            if (process / 'comm').read_text().strip() != 'codex':
                continue
            args = (process / 'cmdline').read_bytes().decode().split('\0')
            for value in args:
                if value.startswith('mcp_servers.codex_app='):
                    settings = tomllib.loads(value)['mcp_servers']['codex_app']
                    process_env = dict(part.split(b'=', 1) for part in (process / 'environ').read_bytes().split(b'\0') if b'=' in part)
                    if b'WSL_INTEROP' in process_env:
                        settings['env']['WSL_INTEROP'] = process_env[b'WSL_INTEROP'].decode()
                    state_home = process_env.get(b'CODEX_SQLITE_HOME') or process_env.get(b'CODEX_HOME')
                    if state_home:
                        settings['codex_state_home'] = state_home.decode()
                    return normalize_app_tools(settings)
        except (OSError, ValueError):
            continue
    raise RuntimeError('Desktop Codex app-tools configuration not found')

def codex_call(name, arguments, origin_thread):
    settings = app_tools_config()
    environment = os.environ.copy()
    environment.update(settings['env'])
    for key in SLACK_KEYS:
        environment.pop(key, None)  # never hand Slack tokens to the Codex subprocess
    environment['WSLENV'] = environment.get('WSLENV', '') + ':CODEX_APP_TOOLS_PIPE_PATH/w'
    node = settings['args'][-2]
    node = ('/mnt/' + node[0].lower() + node[2:].replace('\\', '/')).replace('//', '/')
    server = settings['args'][-1].replace('\\\\', '\\')
    process = subprocess.Popen([node, server], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)

    def request(message):
        process.stdin.write((json.dumps(message) + '\n').encode())
        process.stdin.flush()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ready, _, _ = select.select([process.stdout, process.stderr], [], [], max(0, deadline - time.monotonic()))
            for stream in ready:
                line = stream.readline()
                if not line:
                    raise RuntimeError('Codex MCP connection closed')
                if stream is process.stderr:
                    continue
                reply = json.loads(line)
                if reply.get('id') == message['id']:
                    if 'error' in reply:
                        raise RuntimeError(str(reply['error']))
                    return reply['result']
        raise TimeoutError('Codex MCP request timed out')

    try:
        request({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 'agent-bridge-feasibility', 'version': '0.1'}}})
        process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        process.stdin.flush()
        result = request({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments, '_meta': {'openai/threadId': origin_thread}}})
        if result.get('isError'):
            raise RuntimeError('Codex app rejected tool request: ' + json.dumps(result.get('content')))
        return result
    finally:
        process.terminate()
        process.wait(timeout=5)
