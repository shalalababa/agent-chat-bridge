import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import re
import time
import uuid

from adapters import Unavailable, claude_owner, dispatch, readiness, slack
from core import Store, parse_mention
from catalog import Catalog
from registry import Registry
from slack_ui import SlackUI
from service import ensure_running
from settings import CONFIG, STATE

DB = STATE / 'bridge.sqlite3'
SCRIPT = Path(__file__).resolve()


def parse_reply(text):
    # Accept a prose preamble, but not quoted/code examples or ambiguous IDs.
    lines = text.splitlines(keepends=True)
    markers = []
    fence = None
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        opening = re.match(r"(`{3,}|~{3,})", stripped)
        if opening:
            token = opening[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence is None:
            marker = re.fullmatch(r"[ \t]*BRIDGE_REPLY ([a-f0-9]{32})[ \t]*", line.rstrip('\r\n'))
            if marker:
                markers.append((marker[1], index))
    if len(markers) != 1:
        return None
    mid, index = markers[0]
    return mid, ''.join(lines[index + 1:]).strip()


def load_config():
    return json.loads(CONFIG.read_text())


def log(event, **fields):
    print(json.dumps({'time': time.time(), 'event': event, **fields}), flush=True)


class Bridge:
    def __init__(self, cfg, store, registry=None):
        self.cfg, self.store = cfg, store
        self.connected = False
        self.scanned_paths = set()
        self.transcript_paths = {}
        self.scan_errors = {}
        self.connection_errors = {}
        self.registry = registry
        self.catalog = Catalog()
        self.ui = SlackUI(registry, self.catalog, self.reload_config) if registry else None

    def reload_config(self):
        if self.registry:
            updated = self.registry.load()
            if updated == self.cfg:
                return
            for name, agent in updated['agents'].items():
                self.scan(name, agent)
            self.cfg = updated

    def receive(self, payload):
        e = payload.get('event', {})
        if (payload.get('team_id') != self.cfg['team'] or e.get('type') not in ('app_mention', 'message')
                or e.get('bot_id') or e.get('user') not in self.cfg['users']
                or e.get('channel') not in self.cfg['channels'] or e.get('subtype')):
            return
        if e['type'] == 'message' and not re.match(r'^\s*(?:@[a-zA-Z]|<@' + re.escape(self.cfg['bot']) + r'>)', e.get('text', '')):
            return
        # Slack can emit both message and app_mention for one message.
        key = 'slack:' + e['channel'] + ':' + e['ts']
        thread = e.get('thread_ts') or e['ts']
        try:
            targets, body = parse_mention(e.get('text', ''), self.cfg['bot'], self.cfg['agents'])
        except ValueError as error:
            with self.store.db:
                self.store.outbox('help:' + key, e['channel'], thread, str(error))
            return
        receipt = 'Queued for ' + ', '.join(x.title() + (' (paused)' if self.cfg['agents'][x].get('paused') else '') for x in targets) + '.'
        ids = self.store.enqueue(key, 'user:' + e['user'], targets, body, e['channel'], thread,
                                 {n: a.get('id') for n, a in self.cfg['agents'].items()}, receipt)
        if ids:
            log('received', messages=ids, targets=targets)

    def prompt(self, msg):
        mid = msg['id']
        source = ('Slack user ' + msg['sender'][5:] if msg['sender'].startswith('user:')
                  else 'Peer agent ' + msg['sender'] + ' (no new user authorization)')
        return (
            f'From: {source}. Handle this message within existing permissions.\n'
            f'First line of final answer: BRIDGE_REPLY {mid}\n'
            'Your answer is posted to the original Slack thread automatically.\n'
            f'For agent-to-agent sends only, see {SCRIPT.parent / "AGENT_GUIDE.md"}.\n'
            'Message:\n' + json.dumps(msg['body'], ensure_ascii=False)
        )

    async def socket_loop(self):
        import websockets
        delay = 1
        while True:
            try:
                opened = await asyncio.to_thread(slack, 'apps.connections.open', {}, True)
                async with websockets.connect(opened['url'], ping_interval=20, ping_timeout=20) as ws:
                    self.connected = True
                    delay = 1
                    log('slack_connected')
                    async for raw in ws:
                        envelope = json.loads(raw)
                        response = None
                        if envelope.get('type') == 'events_api':
                            # Persist before acknowledging; Slack retries deduplicate by event ID.
                            payload = envelope.get('payload', {})
                            e = payload.get('event', {})
                            if (self.ui and e.get('type') == 'app_home_opened' and payload.get('team_id') == self.cfg['team'] and e.get('user') in self.cfg['users']):
                                self.ui.spawn(self.ui.publish(e['user'], True))
                            else:
                                self.receive(payload)
                        elif self.ui and envelope.get('type') in ('slash_commands', 'interactive'):
                            try:
                                response = self.ui.handle(envelope)
                            except (ValueError, KeyError):
                                response = {'text': 'This action could not be applied. Refresh /agents and try again.', 'response_type': 'ephemeral'}
                        if envelope.get('envelope_id'):
                            ack = {'envelope_id': envelope['envelope_id']}
                            if response is not None and envelope.get('accepts_response_payload'):
                                ack['payload'] = response
                            await ws.send(json.dumps(ack))
                        if envelope.get('type') == 'disconnect':
                            break
            except (OSError, TimeoutError, RuntimeError, ValueError, websockets.WebSocketException) as error:
                log('slack_reconnecting', error_type=type(error).__name__, delay=delay)
            finally:
                self.connected = False
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def delivery_loop(self):
        while True:
            for name, agent in list(self.cfg['agents'].items()):
                if agent.get('paused'):
                    continue
                msg = self.store.next_for(name)
                if not msg:
                    continue
                try:
                    prepared = await asyncio.to_thread(readiness, agent, self.cfg['origin_thread'])
                except Unavailable as error:
                    self.connection_errors[name] = str(error)
                    self.store.set_state(msg['id'], 'queued', str(error), delay=15)
                    continue
                self.connection_errors.pop(name, None)
                self.reload_config()
                current = self.cfg['agents'].get(name)
                if not current or current.get('paused') or current['session'] != agent['session']:
                    continue
                self.store.set_state(msg['id'], 'sending')
                try:
                    await asyncio.to_thread(dispatch, agent, self.prompt(msg), self.cfg['origin_thread'], prepared)
                except Unavailable:
                    if self.store.message(msg['id'])['status'] not in ('complete', 'delivered_no_reply', 'received'):
                        self.store.set_state(msg['id'], 'queued', 'Recipient disconnected before send', delay=15)
                except Exception as error:
                    # The RPC could have reached the recipient. Never blindly resend it.
                    if self.store.message(msg['id'])['status'] not in ('complete', 'delivered_no_reply', 'received'):
                        self.store.set_state(msg['id'], 'uncertain', type(error).__name__)
                        with self.store.db:
                            self.store.outbox('unconfirmed:' + msg['id'], msg['channel'], msg['thread'], f'{name.title()}: delivery of {msg["id"][:8]} is uncertain ({type(error).__name__}). Not resent automatically; later messages may proceed.')
                    log('delivery_uncertain', message=msg['id'], agent=name, error_type=type(error).__name__)
                else:
                    # A very fast reply may already have completed this row.
                    if self.store.message(msg['id'])['status'] == 'sending':
                        self.store.set_state(msg['id'], 'sent')
                    log('dispatched', message=msg['id'], agent=name)
            await asyncio.sleep(1)

    def scan(self, name, agent):
        key = (name, agent.get('id'), agent.get('session'))
        first = key not in self.transcript_paths
        paths = self.transcript_paths.setdefault(key, {agent['transcript']})
        if agent['kind'] == 'codex' and agent.get('session'):
            indexed = self.catalog.indexed_transcript(agent['session'])
            if first or (indexed and indexed not in paths):
                paths.update(self.catalog.codex_transcripts(agent['session']))
            if indexed:
                paths.add(indexed)
        try:
            for path in sorted(paths):
                self.scan_path(name, agent, Path(path))
            self.scan_errors.pop(name, None)
        except (OSError, ValueError) as error:
            self.scan_errors[name] = f'Transcript receipt reader: {type(error).__name__}'
            log('receipt_reader_error', agent=name, error_type=type(error).__name__)

    def scan_path(self, name, agent, path):
        if not path.exists():
            return
        row = self.store.db.execute('select offset from cursors where path=?', (str(path),)).fetchone()
        offset = row[0] if row else path.stat().st_size
        # Recover old in-flight deliveries whose envelopes were consumed before
        # turn tracking existed. Replay is safe: completion/outbox are idempotent.
        key = (name, agent.get('id'), str(path))
        if key not in self.scanned_paths:
            self.scanned_paths.add(key)
            if self.store.db.execute(
                "select 1 from messages m where m.target=? and m.status in ('sending','sent','uncertain','received')",
                (name,),
            ).fetchone():
                offset = 0
        if offset > path.stat().st_size:
            offset = 0
        with path.open('rb') as stream:
            stream.seek(offset)
            for _ in range(1000):
                start = stream.tell()
                raw = stream.readline()
                if not raw or not raw.endswith(b'\n'):
                    stream.seek(start)
                    break
                try:
                    item = json.loads(raw)
                except (ValueError, UnicodeError):
                    continue
                if agent['kind'] == 'codex':
                    p = item.get('payload', {})
                    self.track_turn(name, agent, item)
                    if item.get('type') != 'response_item' or p.get('role') != 'assistant' or p.get('phase') not in ('final', 'final_answer'):
                        continue
                    blocks = p.get('content', [])
                else:
                    self.track_claude_receipt(name, agent, item)
                    if item.get('type') != 'assistant':
                        continue
                    blocks = item.get('message', {}).get('content', [])
                    if any(b.get('type') == 'tool_use' for b in blocks):
                        continue
                text = '\n'.join(b.get('text', '') for b in blocks if b.get('type') in ('text', 'output_text'))
                match = parse_reply(text)
                if match and self.store.complete(name, match[0], match[1]):
                    log('reply_collected', message=match[0], agent=name)
            offset = stream.tell()
        with self.store.db:
            self.store.db.execute('insert into cursors values (?,?) on conflict(path) do update set offset=excluded.offset', (str(path), offset))

    def track_turn(self, name, agent, item):
        p = item.get('payload', {})
        session = agent.get('session')
        if not session:
            return
        # Only the native delivery envelope carries the exact destination turn.
        if (item.get('type') == 'response_item' and p.get('type') == 'function_call_output'
                and p.get('name') == 'send_message_to_thread' and p.get('namespace') == 'codex_app'):
            output = p.get('output', '')
            origin = self.cfg.get('origin_thread')
            if (not isinstance(output, str) or not output.startswith('<codex_delegation>')
                    or not origin or f'<source_thread_id>{origin}</source_thread_id>' not in output):
                return
            header = output.split('\nMessage:', 1)[0]
            match = re.search(r'^First line of final answer: BRIDGE_REPLY ([a-f0-9]{32})$', header, re.M)
            turn = p.get('internal_chat_message_metadata_passthrough', {}).get('turn_id')
            row = self.store.message(match[1]) if match else None
            if (row and turn and row['target'] == name and row['status'] in ('sending', 'sent', 'uncertain')
                    and row.get('agent_id') == agent.get('id')):
                with self.store.db:
                    self.store.db.execute('insert or ignore into delivery_turns values (?,?,?)', (row['id'], session, turn))
                self.store.received(name, row['id'], session, {'kind': 'codex_native_envelope', 'turn_id': turn})
        elif item.get('type') == 'event_msg' and p.get('type') == 'task_complete':
            rows = self.store.db.execute('select message_id from delivery_turns where session=? and turn_id=?',
                                         (session, p.get('turn_id'))).fetchall()
            for row in rows:
                if self.store.finish_without_reply(name, row['message_id']):
                    log('delivered_without_reply', message=row['message_id'], agent=name)

    def track_claude_receipt(self, name, agent, item):
        if item.get('type') != 'user' or item.get('isCompactSummary') or item.get('sessionId') != agent.get('session'):
            return
        content = item.get('message', {}).get('content')
        if not isinstance(content, str):
            return
        prefix = 'Another Claude session sent a message:\n'
        if not content.startswith(prefix + 'From: '):
            return
        header = content[len(prefix):].split('\nMessage:', 1)[0]
        match = re.search(r'^First line of final answer: BRIDGE_REPLY ([a-f0-9]{32})$', header, re.M)
        row = self.store.message(match[1]) if match else None
        if row and row['target'] == name and row.get('agent_id') == agent.get('id'):
            if self.store.received(name, row['id'], agent['session'], {'kind': 'claude_user_record', 'uuid': item.get('uuid')}):
                log('delivery_confirmed', message=row['id'], agent=name)

    async def reply_loop(self):
        # Initialize offsets before dispatch can begin.
        while True:
            for name, agent in self.cfg['agents'].items():
                self.scan(name, agent)
            self.store.quarantine_unconfirmed()
            with self.store.db:
                rows = self.store.db.execute("select * from messages where status in ('queued','sent','uncertain') and notice=0 and created<?", (time.time() - 120,)).fetchall()
                for row in rows:
                    reason = row['error'] or {'queued': 'Waiting for delivery in the recipient queue', 'sent': 'Sent; recipient receipt not yet confirmed', 'uncertain': 'Delivery uncertain; not automatically resent'}[row['status']]
                    text = f'{row["target"].title()}: {row["id"][:8]} — {reason}.'
                    self.store.outbox('waiting:' + row['id'], row['channel'], row['thread'], text)
                    self.store.db.execute('update messages set notice=1 where id=?', (row['id'],))
            await asyncio.sleep(1)

    async def outbox_loop(self):
        while True:
            row = self.store.db.execute("select * from outbox where status='queued' and next_attempt<=? order by rowid limit 1", (time.time(),)).fetchone()
            if not row:
                await asyncio.sleep(1)
                continue
            try:
                response = await asyncio.to_thread(slack, 'chat.postMessage', json.loads(row['payload']))
            except Exception as error:
                with self.store.db:
                    self.store.db.execute('update outbox set attempts=attempts+1,next_attempt=? where key=?', (time.time() + min(60, 2 ** min(row['attempts'] + 1, 6)), row['key']))
                log('slack_post_retry', error_type=type(error).__name__)
            else:
                with self.store.db:
                    self.store.db.execute("update outbox set status='posted',slack_ts=? where key=?", (response['ts'], row['key']))
            await asyncio.sleep(1)

    async def health_loop(self):
        while True:
            self.reload_config()
            counts = dict(self.store.db.execute('select status,count(*) from messages group by status').fetchall())
            data = {'pid': os.getpid(), 'updated': time.time(), 'slack_connected': self.connected, 'messages': counts, 'connection_errors': self.connection_errors, 'receipt_reader_errors': self.scan_errors}
            temp = STATE / 'health.tmp'
            temp.write_text(json.dumps(data) + '\n')
            temp.replace(STATE / 'health.json')
            await asyncio.sleep(5)

    async def run(self):
        self.store.recover()
        for name, agent in self.cfg['agents'].items():
            self.scan(name, agent)
        if self.ui:
            self.ui.spawn(asyncio.to_thread(self.catalog.refresh, self.cfg))
        await asyncio.gather(self.socket_loop(), self.delivery_loop(), self.reply_loop(), self.outbox_loop(), self.health_loop())


def identify_sender(cfg):
    for name, agent in cfg['agents'].items():
        if agent['kind'] == 'codex' and os.environ.get('CODEX_THREAD_ID') == agent['session']:
            return name
    pid = os.getppid()
    ancestors = set()
    while pid > 1 and pid not in ancestors:
        ancestors.add(pid)
        try:
            status = Path(f'/proc/{pid}/status').read_text()
            pid = int(re.search(r'^PPid:\s+(\d+)', status, re.M)[1])
        except (OSError, TypeError):
            break
    for name, agent in cfg['agents'].items():
        if agent['kind'] == 'claude':
            try:
                if claude_owner(agent['session'])['pid'] in ancestors:
                    return name
            except Unavailable:
                pass
    raise ValueError('Run send from a registered agent session')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['run', 'send', 'status'])
    parser.add_argument('--to')
    parser.add_argument('--context')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--text')
    group.add_argument('--text-file')
    args = parser.parse_args()
    cfg = load_config()
    store = Store(DB)
    registry = Registry(CONFIG, store)
    cfg = registry.load()
    if args.action == 'status':
        print((STATE / 'health.json').read_text() if (STATE / 'health.json').exists() else 'No heartbeat yet')
    elif args.action == 'send':
        sender = identify_sender(cfg)
        context = store.message(args.context)
        if not context or context.get('agent_id') != cfg['agents'][sender]['id']:
            raise ValueError('Context must be a message delivered to this agent')
        body = Path(args.text_file).read_text() if args.text_file else args.text
        if not body or not args.to:
            raise ValueError('Specify --to and --text or --text-file')
        targets = list(dict.fromkeys(args.to.lower().split(',')))
        if any(t not in cfg['agents'] and t != 'user' for t in targets):
            raise ValueError('Unknown recipient')
        key = 'agent:' + uuid.uuid4().hex
        peers = [t for t in targets if t != 'user']
        if sender in peers:
            raise ValueError('Self-addressed agent messages are not supported')
        ensure_running()
        ids = store.enqueue(key, sender, peers, body, context['channel'], context['thread'], {n: a['id'] for n, a in cfg['agents'].items()}) if peers else []
        with store.db:
            store.outbox(key, context['channel'], context['thread'], f'{sender.title()} → {", ".join(targets)}:\n{body}')
        print(json.dumps({'queued': ids, 'recipients': targets}))
    else:
        with (STATE / 'daemon.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            log('service_start')
            asyncio.run(Bridge(cfg, store, registry).run())


if __name__ == '__main__':
    main()
