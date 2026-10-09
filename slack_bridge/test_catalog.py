import json
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from catalog import Catalog


class TranscriptLookupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sid = '00000000-0000-7000-8000-000000000001'
        self.old = self.transcript('original-' + self.sid + '.jsonl', self.sid)
        self.active = self.transcript('continued-' + self.sid + '_segment.jsonl', self.sid)
        self.db = self.root / 'state_5.sqlite'
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('create table threads(id text primary key, rollout_path text)')
            conn.execute('insert into threads values (?,?)', (self.sid, str(self.active)))
            conn.commit()

    def transcript(self, name, sid):
        p = self.root / name
        p.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': sid}}) + '\n')
        return p

    def tearDown(self):
        self.tmp.cleanup()

    def test_active_index_resolves_multiple_same_session_segments(self):
        cat = Catalog()
        cat.sessions['codex:' + self.sid] = {'transcript': str(self.old)}
        with patch('catalog.app_tools_config', return_value={'codex_state_home': str(self.root)}, create=True):
            self.assertEqual(cat.transcript('codex', self.sid), str(self.active))

    def test_wrong_session_in_index_is_not_accepted(self):
        wrong = self.transcript('wrong.jsonl', 'another-id')
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('update threads set rollout_path=?', (str(wrong),))
            conn.commit()
        with patch('catalog.app_tools_config', return_value={'codex_state_home': str(self.root)}, create=True):
            self.assertIsNone(Catalog().indexed_transcript(self.sid))


if __name__ == '__main__':
    unittest.main()
