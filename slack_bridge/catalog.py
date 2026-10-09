"""Read-only discovery of existing local sessions; never creates or resumes one."""
import json
from pathlib import Path
import re
import sqlite3
import time

from adapters import app_tools_config, claude_owner, codex_call
from settings import CLAUDE_HOME, CODEX_SESSIONS


class Catalog:
    def __init__(self):
        self.sessions = {}
        self.updated = 0
        self.errors = []

    @staticmethod
    def text_result(result):
        return json.loads(next(b['text'] for b in result['content'] if b.get('type') == 'text'))

    def refresh(self, cfg):
        sessions, errors = {}, []
        for name, agent in cfg['agents'].items():
            key = agent['kind'] + ':' + agent['session']
            sessions[key] = {**agent, 'label': agent.get('label', name.title()), 'status': 'offline' if agent['kind'] == 'claude' else 'unknown'}
        try:
            data = self.text_result(codex_call('list_threads', {'limit': 50}, cfg['origin_thread']))
            for t in data.get('pinnedThreads', []) + data.get('threads', []):
                if t.get('kind') != 'codex' or t.get('hostId') != 'local':
                    continue
                key = 'codex:' + t['id']
                sessions[key] = {**sessions.get(key, {}), 'kind': 'codex', 'session': t['id'], 'label': t['title'], 'status': t.get('status', 'unknown'), 'cwd': t.get('cwd', '')}
        except Exception:
            errors.append('Codex discovery is unavailable; registered sessions are still listed.')
        for path in (CLAUDE_HOME / 'sessions').glob('*.json'):
            try:
                state = json.loads(path.read_text())
                sid = state['sessionId']
                if claude_owner(sid)['pid'] != state['pid']:
                    continue
                key = 'claude:' + sid
                sessions[key] = {**sessions.get(key, {}), 'kind': 'claude', 'session': sid,
                                 'label': sessions.get(key, {}).get('label') or state.get('name') or sid[:8],
                                 'status': state.get('status', 'unknown'), 'cwd': state.get('cwd', '')}
            except (OSError, ValueError, KeyError, RuntimeError):
                continue
            except Exception:
                continue
        self.sessions, self.errors, self.updated = sessions, errors, time.time()

    def transcript(self, kind, sid):
        if kind == 'codex':
            indexed = self.indexed_transcript(sid)
            if indexed:
                return indexed
        cached = self.sessions.get(kind + ':' + sid, {})
        if cached.get('transcript') and Path(cached['transcript']).is_file():
            return cached['transcript']
        if kind == 'claude':
            candidates = (CLAUDE_HOME / 'projects').glob('*/' + sid + '.jsonl')
        else:
            candidates = CODEX_SESSIONS.glob('**/*' + sid + '*.jsonl')
        matches = []
        for path in candidates:
            if kind == 'codex':
                try:
                    with path.open() as f:
                        first = json.loads(f.readline())
                    if first.get('payload', {}).get('id') != sid:
                        continue
                except (OSError, ValueError):
                    continue
            matches.append(path)
        if len(matches) != 1:
            raise ValueError('Cannot identify one saved local transcript for this session. Select another session or refresh the list.')
        return str(matches[0])

    @staticmethod
    def local_path(raw):
        if re.match(r'^[A-Za-z]:[\\/]', raw):
            raw = '/mnt/' + raw[0].lower() + re.sub(r'[\\/]+', '/', raw[2:])
        return Path(raw)

    def indexed_transcript(self, sid):
        # A conversation may span several rollouts with the same session ID.
        # Use the active desktop backend's index, not a stale Windows database
        # or filename/mtime guesses.
        try:
            root = self.local_path(app_tools_config()['codex_state_home'])
        except (KeyError, OSError, RuntimeError):
            return None
        databases = sorted(root.glob('state_*.sqlite'), key=lambda p: int(p.stem.split('_')[-1]) if p.stem.split('_')[-1].isdigit() else -1, reverse=True)
        for database in databases:
            connection = None
            try:
                connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=1)
                row = connection.execute('select rollout_path from threads where id=?', (sid,)).fetchone()
                if row:
                    path = self.local_path(row[0])
                    with path.open() as stream:
                        meta = json.loads(stream.readline())
                    if meta.get('type') == 'session_meta' and (meta.get('payload', {}).get('id') or meta.get('payload', {}).get('session_id')) == sid:
                        return str(path)
                    return None
            except (sqlite3.Error, OSError, ValueError):
                continue
            finally:
                if connection:
                    connection.close()
        return None

    def codex_transcripts(self, sid):
        """Validated continuation history for recovery after offline rollovers."""
        matches = []
        for path in CODEX_SESSIONS.glob('**/*' + sid + '*.jsonl'):
            try:
                with path.open() as stream:
                    meta = json.loads(stream.readline())
                if meta.get('type') == 'session_meta' and (meta.get('payload', {}).get('id') or meta.get('payload', {}).get('session_id')) == sid:
                    matches.append(str(path))
            except (OSError, ValueError):
                continue
        return matches

    def resolve(self, key):
        if not re.fullmatch(r'(codex|claude):[a-fA-F0-9-]{36}', key):
            raise ValueError('Choose a session, or enter codex:SESSION_ID or claude:SESSION_ID.')
        kind, sid = key.split(':', 1)
        cached = self.sessions.get(key, {})
        return {'kind': kind, 'session': sid, 'transcript': self.transcript(kind, sid), 'label': cached.get('label', kind.title() + ' ' + sid[:8])}

    def options(self, query=''):
        result = []
        for key, agent in self.sessions.items():
            label = agent['kind'].title() + ' · ' + agent['label'] + ' · ' + agent['session'][:8]
            if query.lower() in (label + ' ' + agent['session']).lower():
                result.append({'text': {'type': 'plain_text', 'text': label[:75]}, 'value': key})
        return result[:100]
