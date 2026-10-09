"""Atomic agent registration changes shared by Slack controls and local CLI."""
import fcntl
import json
import os
from pathlib import Path
import re
import uuid


class Registry:
    def __init__(self, path, store):
        self.path, self.store = Path(path), store
        with self.lock():
            cfg = self.load()
            changed = 'revision' not in cfg
            cfg.setdefault('revision', 0)
            for name, agent in cfg['agents'].items():
                if 'id' not in agent:
                    agent['id'] = uuid.uuid4().hex
                    changed = True
                agent.setdefault('paused', False)
                with store.db:
                    store.db.execute('update messages set agent_id=? where target=? and agent_id is null', (agent['id'], name))
            if changed:
                self.write(cfg)

    def lock(self):
        class Lock:
            def __enter__(inner):
                inner.file = self.path.with_suffix('.lock').open('a')
                fcntl.flock(inner.file, fcntl.LOCK_EX)
            def __exit__(inner, *args):
                inner.file.close()
        return Lock()

    def load(self):
        return json.loads(self.path.read_text())

    def write(self, cfg):
        temp = self.path.with_suffix('.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump(cfg, out, indent=2)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, self.path)

    def pending(self, name, statuses):
        placeholders = ','.join('?' for _ in statuses)
        return self.store.db.execute(f'select count(*) from messages where target=? and status in ({placeholders})', [name, *statuses]).fetchone()[0]

    def save(self, alias, agent, old=None, revision=None):
        alias = alias.strip().lower()
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', alias) or alias in ('user', 'agents'):
            raise ValueError('Use an alias starting with a letter, with letters, digits, - or _. “user” and “agents” are reserved.')
        with self.lock():
            cfg = self.load()
            if revision is not None and cfg['revision'] != revision:
                raise ValueError('The agent list changed. Reopen this form to use the latest settings.')
            if old and old not in cfg['agents']:
                raise ValueError('This agent was removed. Reopen the manager.')
            if alias in cfg['agents'] and alias != old:
                raise ValueError('That alias is already registered.')
            if old and self.pending(old, ['queued', 'sending', 'sent', 'uncertain', 'received']):
                raise ValueError('This agent has pending deliveries. Let them finish before changing its alias or session.')
            if any((a['kind'], a['session']) == (agent['kind'], agent['session']) for n, a in cfg['agents'].items() if n != old):
                raise ValueError('That exact session is already registered under another alias.')
            previous = cfg['agents'].pop(old) if old else {}
            cfg['agents'][alias] = {**agent, 'id': previous.get('id', uuid.uuid4().hex), 'paused': previous.get('paused', False)}
            cfg['revision'] += 1
            self.write(cfg)
            return cfg

    def pause(self, alias, paused):
        with self.lock():
            cfg = self.load()
            if alias not in cfg['agents']:
                raise ValueError('Unknown agent: ' + alias)
            cfg['agents'][alias]['paused'] = paused
            cfg['revision'] += 1
            self.write(cfg)
            return cfg

    def remove(self, alias, revision=None):
        with self.lock():
            cfg = self.load()
            if revision is not None and cfg['revision'] != revision:
                raise ValueError('The agent list changed. Reopen the removal dialog.')
            if alias not in cfg['agents']:
                raise ValueError('Unknown agent: ' + alias)
            if self.pending(alias, ['sending', 'sent', 'uncertain', 'received']):
                raise ValueError('A message is still in flight. Wait for its reply before removing this registration.')
            with self.store.db:
                for row in self.store.db.execute("select * from messages where target=? and status='queued'", (alias,)).fetchall():
                    self.store.outbox('removed:' + row['id'], row['channel'], row['thread'], alias + ' was removed before this message was delivered.')
                self.store.db.execute("update messages set status='cancelled',error='Agent registration removed' where target=? and status='queued'", (alias,))
                del cfg['agents'][alias]
                cfg['revision'] += 1
                self.write(cfg)
            return cfg
