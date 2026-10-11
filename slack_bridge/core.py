import html
import json
import re
import sqlite3
import time
import uuid


def parse_mention(text, bot, agents):
    text = text.strip()
    native = re.fullmatch(r'<@' + re.escape(bot) + r'>\s+(?:to:)?([a-zA-Z0-9_,\-]+)\s+(.+)', text, re.S)
    if native:
        targets, body = native[1].lower().split(','), native[2].strip()
    else:
        targets = []
        rest = text
        while match := re.match(r'^@([a-zA-Z][a-zA-Z0-9_\-]*)(?=\s|,|$)', rest):
            targets.append(match[1].lower())
            rest = rest[match.end():].lstrip(' ,')
        body = rest.strip()
    targets = list(dict.fromkeys(targets))
    if not targets or any(name not in agents for name in targets):
        raise ValueError('Address a registered agent: ' + ', '.join('@' + name for name in agents))
    if not body:
        raise ValueError('Add a message after the agent names.')
    return targets, html.unescape(body)


class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('pragma journal_mode=WAL')
        self.db.executescript('''
            create table if not exists events (key text primary key);
            create table if not exists messages (
              id text primary key, sender text, target text, body text,
              channel text, thread text, status text, created real, updated real,
              next_attempt real default 0, notice integer default 0, error text, reply text);
            create table if not exists outbox (
              key text primary key, payload text, status text default 'queued',
              next_attempt real default 0, attempts integer default 0, slack_ts text);
            create table if not exists cursors (path text primary key, offset integer);
            create table if not exists delivery_receipts (
                message_id text primary key, session text, evidence text, received_at real);
            create table if not exists delivery_turns (message_id text primary key, session text, turn_id text);
        ''')
        self.db.commit()
        columns = {r[1] for r in self.db.execute('pragma table_info(messages)')}
        if 'agent_id' not in columns:
            self.db.execute('alter table messages add column agent_id text')
            self.db.commit()

    def outbox(self, key, channel, thread, text):
        chunks = [text[i:i + 3500] for i in range(0, len(text), 3500)] or ['(empty response)']
        for index, part in enumerate(chunks):
            entry_key = key + ':' + str(index)
            payload = {'channel': channel, 'text': part, 'unfurl_links': False, 'unfurl_media': False,
                       'client_msg_id': str(uuid.uuid5(uuid.NAMESPACE_URL, entry_key))}
            if thread:
                payload['thread_ts'] = thread
            self.db.execute('insert or ignore into outbox(key,payload) values (?,?)', (entry_key, json.dumps(payload)))

    def enqueue(self, key, sender, targets, body, channel, thread, agent_ids=None, receipt=None):
        with self.db:
            if not self.db.execute('insert or ignore into events values (?)', (key,)).rowcount:
                return []
            ids = []
            for target in targets:
                mid = uuid.uuid4().hex
                now = time.time()
                self.db.execute('insert into messages(id,sender,target,body,channel,thread,status,created,updated,agent_id) values (?,?,?,?,?,?,?,?,?,?)',
                                (mid, sender, target, body, channel, thread, 'queued', now, now, (agent_ids or {}).get(target)))
                ids.append(mid)
            if receipt:
                self.outbox('receipt:' + key, channel, thread, receipt)
            return ids

    def message(self, mid):
        row = self.db.execute('select * from messages where id=?', (mid,)).fetchone()
        return dict(row) if row else None

    def set_state(self, mid, status, error=None, delay=0):
        with self.db:
            self.db.execute('update messages set status=?,updated=?,error=?,next_attempt=? where id=?',
                            (status, time.time(), error, time.time() + delay, mid))

    def recover(self):
        with self.db:
            self.db.execute("update messages set status='uncertain',error='Service stopped during dispatch' where status='sending'")

    def next_for(self, agent):
        if self.db.execute("select 1 from messages where target=? and status in ('sending','sent')", (agent,)).fetchone():
            return None
        row = self.db.execute("select * from messages where target=? and status='queued' order by created,rowid limit 1", (agent,)).fetchone()
        return dict(row) if row and row['next_attempt'] <= time.time() else None

    def complete(self, agent, mid, text):
        with self.db:
            row = self.message(mid)
            if not row or row['target'] != agent or row['status'] not in ('sending', 'sent', 'uncertain', 'received', 'delivered_no_reply'):
                return False
            self.db.execute("update messages set status='complete',reply=?,updated=? where id=?", (text, time.time(), mid))
            self.outbox('reply:' + mid, row['channel'], row['thread'], agent.title() + ':\n' + text)
            return True

    def finish_without_reply(self, agent, mid):
        with self.db:
            row = self.message(mid)
            if not row or row['target'] != agent or row['status'] not in ('sending', 'sent', 'uncertain', 'received'):
                return False
            self.db.execute("update messages set status='delivered_no_reply',updated=?,error=? where id=?",
                            (time.time(), 'Delivered turn finished without a bridge reply marker', mid))
            self.outbox('no-reply:' + mid, row['channel'], row['thread'],
                        f'{agent.title()}: message {mid[:8]} was delivered and its turn finished, but no marked reply was returned. Later messages can proceed; this does not confirm the request was fulfilled.')
            return True

    def received(self, agent, mid, session, evidence):
        with self.db:
            row = self.message(mid)
            if not row or row['target'] != agent or row['status'] not in ('sending', 'sent', 'uncertain'):
                return False
            now = time.time()
            self.db.execute('insert or ignore into delivery_receipts values (?,?,?,?)',
                            (mid, session, json.dumps(evidence), now))
            self.db.execute("update messages set status='received',updated=?,error=null where id=?", (now, mid))
            self.outbox('delivered:' + mid, row['channel'], row['thread'],
                        f'{agent.title()}: received message {mid[:8]}. A reply, if any, will appear here.')
            return True

    def quarantine_unconfirmed(self, seconds=120):
        # No duplicate sends after an ambiguous RPC or missing transcript receipt.
        # Isolate that message rather than holding every later message hostage.
        with self.db:
            rows = self.db.execute("select * from messages where status='sent' and updated<?", (time.time() - seconds,)).fetchall()
            for row in rows:
                self.db.execute("update messages set status='uncertain',error=?,updated=? where id=?",
                                ('Send accepted but recipient receipt not observed; not automatically resent', time.time(), row['id']))
                self.outbox('unconfirmed:' + row['id'], row['channel'], row['thread'],
                            f'{row["target"].title()}: receipt for {row["id"][:8]} is unconfirmed. It will not be resent automatically; later messages may proceed. Check /agents list.')
