from contextlib import redirect_stdout
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import Store, parse_mention
from bridge import Bridge


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'queue.db'
        self.store = Store(self.path)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_addressing_keeps_body_intact(self):
        self.assertEqual(parse_mention('<@BOT> to:astra,fable review this\nplease', 'BOT', ['astra', 'fable']), (['astra', 'fable'], 'review this\nplease'))
        self.assertEqual(parse_mention('<@BOT> fable hello', 'BOT', ['astra', 'fable']), (['fable'], 'hello'))
        for text in ['<@BOT> unknown hi', '<@BOT> astra', 'astra hello']:
            with self.assertRaises(ValueError):
                parse_mention(text, 'BOT', ['astra', 'fable'])

    def test_plain_aliases_and_message_mentions_do_not_expand_recipients(self):
        for text in ['@astra @fable review this', '@astra,@fable review this']:
            self.assertEqual(parse_mention(text, 'BOT', ['astra', 'fable']), (['astra', 'fable'], 'review this'))
        self.assertEqual(parse_mention('@astra tell @fable hello', 'BOT', ['astra', 'fable']), (['astra'], 'tell @fable hello'))
        self.assertEqual(parse_mention('@reviewer_2 hello', 'BOT', ['reviewer_2']), (['reviewer_2'], 'hello'))
        for text in ['@unknown hello', '@astra   ', 'email someone@astra.com']:
            with self.assertRaises(ValueError):
                parse_mention(text, 'BOT', ['astra'])

    def add(self, key='event1'):
        return self.store.enqueue(key, 'user:U', ['astra', 'fable'], 'hello', 'C', '1.2')

    def test_duplicate_event_survives_restart(self):
        first = self.add()
        self.assertEqual(len(first), 2)
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(self.add(), [])
        self.assertEqual(self.store.db.execute('select count(*) from messages').fetchone()[0], 2)

    def test_reply_is_bound_to_recipient_and_only_published_once(self):
        ids = self.add()
        self.store.set_state(ids[0], 'sent')
        self.assertFalse(self.store.complete('fable', ids[0], 'wrong'))
        self.assertTrue(self.store.complete('astra', ids[0], 'right'))
        self.assertFalse(self.store.complete('astra', ids[0], 'duplicate'))
        self.assertEqual(self.store.db.execute("select count(*) from outbox where key like 'reply:%'").fetchone()[0], 1)

    def test_interrupted_dispatch_not_blindly_resent(self):
        msg = self.add()[0]
        self.store.set_state(msg, 'sending')
        self.store.recover()
        self.assertEqual(self.store.message(msg)['status'], 'uncertain')
        self.assertTrue(self.store.complete('astra', msg, 'late but valid'))

    def test_one_pending_delivery_per_agent(self):
        first = self.add()[0]
        self.store.set_state(first, 'sent')
        self.add('event2')
        self.assertIsNone(self.store.next_for('astra'))
        self.store.complete('astra', first, 'done')
        self.assertIsNotNone(self.store.next_for('astra'))

    def test_inbound_sender_channel_and_retry_validation(self):
        cfg = {'team': 'T', 'users': ['U'], 'channels': ['C'], 'bot': 'BOT', 'agents': {'astra': {}, 'fable': {}}}
        bridge = Bridge(cfg, self.store)
        payload = {'team_id': 'T', 'event_id': 'Ev1', 'event': {'type': 'app_mention', 'user': 'outsider', 'channel': 'C', 'ts': '1.2', 'text': '<@BOT> astra hello'}}
        bridge.receive(payload)
        self.assertEqual(self.store.db.execute('select count(*) from messages').fetchone()[0], 0)
        payload['event']['user'] = 'U'
        bridge.receive(payload)
        bridge.receive(payload)
        self.assertEqual(self.store.db.execute('select count(*) from messages').fetchone()[0], 1)

    def test_receipt_failure_rolls_back_event_before_socket_ack(self):
        cfg = {'team': 'T', 'users': ['U'], 'channels': ['C'], 'bot': 'BOT',
               'agents': {'astra': {}}}
        bridge = Bridge(cfg, self.store)
        payload = {'team_id': 'T', 'event': {'type': 'app_mention', 'user': 'U',
                   'channel': 'C', 'ts': '1.2', 'text': '<@BOT> astra hello'}}
        with patch.object(self.store, 'outbox', side_effect=RuntimeError('receipt write failed')):
            with self.assertRaisesRegex(RuntimeError, 'receipt write failed'):
                bridge.receive(payload)
        self.assertEqual(self.store.db.execute('select count(*) from events').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('select count(*) from messages').fetchone()[0], 0)
        bridge.receive(payload)
        self.assertEqual(self.store.db.execute('select count(*) from messages').fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("select count(*) from outbox where key like 'receipt:%'").fetchone()[0], 1)

    def test_environment_tokens_are_not_written_to_queue_receipt_or_log(self):
        cfg = {'team': 'T', 'users': ['U'], 'channels': ['C'], 'bot': 'BOT',
               'agents': {'astra': {}}}
        payload = {'team_id': 'T', 'event': {'type': 'app_mention', 'user': 'U',
                   'channel': 'C', 'ts': '1.2', 'text': '<@BOT> astra hello'}}
        output = io.StringIO()
        with patch.dict(os.environ, {'SLACK_BOT_TOKEN': 'bot-test-secret',
                                     'SLACK_APP_TOKEN': 'app-test-secret'}), redirect_stdout(output):
            Bridge(cfg, self.store).receive(payload)
        records = '\n'.join(self.store.db.iterdump()) + output.getvalue()
        self.assertNotIn('bot-test-secret', records)
        self.assertNotIn('app-test-secret', records)

    def test_reply_scanner_replays_after_restart_without_duplicate(self):
        import json
        transcript = Path(self.tmp.name) / 'transcript.jsonl'
        transcript.write_text('')
        agent = {'kind': 'codex', 'transcript': str(transcript)}
        bridge = Bridge({}, self.store)
        bridge.scan('astra', agent)
        mid = self.add()[0]
        self.store.set_state(mid, 'sent')
        item = {'type': 'response_item', 'payload': {'role': 'assistant', 'phase': 'final_answer', 'content': [{'type': 'output_text', 'text': 'BRIDGE_REPLY ' + mid + '\nhello'}]}}
        transcript.write_text(json.dumps(item) + '\n')
        bridge.scan('astra', agent)
        bridge.scan('astra', agent)
        self.assertEqual(self.store.message(mid)['reply'], 'hello')
        self.assertEqual(self.store.db.execute("select count(*) from outbox where key like 'reply:%'").fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
