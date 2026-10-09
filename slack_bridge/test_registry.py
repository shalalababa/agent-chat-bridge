import json
import tempfile
import unittest
from pathlib import Path

from core import Store
from registry import Registry


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'config.json'
        self.path.write_text(json.dumps({'agents': {'astra': {'kind': 'codex', 'session': 's1', 'transcript': '/tmp/test'}}}))
        self.store = Store(Path(self.tmp.name) / 'queue.db')
        self.registry = Registry(self.path, self.store)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_rename_preserves_identity_and_rejects_stale_edits(self):
        cfg = self.registry.load()
        old = cfg['agents']['astra']
        updated = self.registry.save('reviewer', old, 'astra', cfg['revision'])
        self.assertEqual(updated['agents']['reviewer']['id'], old['id'])
        with self.assertRaises(ValueError):
            self.registry.save('other', old, 'reviewer', cfg['revision'])

    def test_no_rebinding_pending_messages(self):
        self.store.enqueue('e', 'user:U', ['astra'], 'hello', 'C', 'T')
        cfg = self.registry.load()
        with self.assertRaises(ValueError):
            self.registry.save('astra', {**cfg['agents']['astra'], 'session': 's2'}, 'astra', cfg['revision'])

    def test_pause_and_remove_cancel_only_unsent_messages(self):
        mid = self.store.enqueue('e', 'user:U', ['astra'], 'hello', 'C', 'T')[0]
        self.registry.pause('astra', True)
        self.assertTrue(self.registry.load()['agents']['astra']['paused'])
        self.registry.remove('astra')
        self.assertEqual(self.store.message(mid)['status'], 'cancelled')
        self.assertNotIn('astra', self.registry.load()['agents'])

    def test_duplicate_session_and_alias_rejected(self):
        cfg = self.registry.load()
        with self.assertRaises(ValueError):
            self.registry.save('fable', cfg['agents']['astra'])
        with self.assertRaises(ValueError):
            self.registry.save('user', {'kind': 'claude', 'session': 's2', 'transcript': '/tmp/t'})


if __name__ == '__main__':
    unittest.main()
