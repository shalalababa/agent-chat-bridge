"""Delivery receipts must survive replies, restarts, and transcript rollover."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bridge import Bridge
from core import Store


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / 'queue.sqlite3'
        self.store = Store(self.db_path)
        self.sid = 'exact-session'
        self.old = self.root / 'old.jsonl'
        self.active = self.root / 'active.jsonl'
        self.agent = {'kind': 'codex', 'id': 'astra-registration',
                      'session': self.sid, 'transcript': str(self.old)}
        self.bridge = Bridge({'origin_thread': 'bridge-origin'}, self.store)
        self.write(self.old, [])
        self.write(self.active, [])

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def enqueue(self, key, status='queued'):
        mid = self.store.enqueue(key, 'fable', ['astra'], 'Please review the report.',
                                 'CHANNEL', '123.456', {'astra': self.agent['id']})[0]
        if status != 'queued':
            self.store.set_state(mid, status)
        return mid

    def write(self, path, items):
        items = [{'type': 'session_meta', 'payload': {'id': self.sid}}] + items
        path.write_text(''.join(json.dumps(item) + '\n' for item in items))

    def incoming(self, mid):
        prompt = self.bridge.prompt(self.store.message(mid))
        return {'type': 'response_item', 'payload': {
            'type': 'function_call_output', 'name': 'send_message_to_thread',
            'namespace': 'codex_app',
            'output': '<codex_delegation>\n<source_thread_id>bridge-origin</source_thread_id>\n<input>'
                      + prompt + '</input>\n</codex_delegation>',
            'internal_chat_message_metadata_passthrough': {'turn_id': 'native-turn'}}}

    def scan_active(self):
        with patch.object(self.bridge.catalog, 'indexed_transcript', return_value=str(self.active)):
            self.bridge.scan('astra', self.agent)

    def test_receipt_is_durable_and_releases_next_message_without_reply(self):
        mid = self.enqueue('first', 'sent')
        following = self.enqueue('second')
        self.assertTrue(self.store.received('astra', mid, self.sid, 'native-envelope-evidence'))
        self.store.db.close()
        self.store = Store(self.db_path)
        self.assertEqual(self.store.message(mid)['status'], 'received')
        self.assertIsNone(self.store.message(mid)['reply'])
        self.assertEqual(self.store.next_for('astra')['id'], following)
        # Receipt metadata must be durable, regardless of its chosen SQL layout.
        saved = '\n'.join(self.store.db.iterdump())
        self.assertIn(self.sid, saved)
        self.assertIn('native-envelope-evidence', saved)

    def test_duplicate_receipt_does_not_duplicate_notice_or_regress_complete(self):
        mid = self.enqueue('first', 'sent')
        self.assertTrue(self.store.received('astra', mid, self.sid, 'native-envelope'))
        count = self.store.db.execute('select count(*) from outbox').fetchone()[0]
        self.assertFalse(self.store.received('astra', mid, self.sid, 'native-envelope'))
        self.assertEqual(self.store.db.execute('select count(*) from outbox').fetchone()[0], count)
        self.assertTrue(self.store.complete('astra', mid, 'Finished review.'))
        self.assertFalse(self.store.received('astra', mid, self.sid, 'native-envelope'))
        self.assertEqual(self.store.message(mid)['status'], 'complete')
        self.assertEqual(self.store.message(mid)['reply'], 'Finished review.')

    def test_wrong_recipient_cannot_acknowledge_message(self):
        mid = self.enqueue('first', 'sent')
        self.assertFalse(self.store.received('fable', mid, self.sid, 'native-envelope'))
        self.assertEqual(self.store.message(mid)['status'], 'sent')

    def test_delayed_oldest_message_is_not_overtaken(self):
        first = self.enqueue('first')
        self.store.set_state(first, 'queued', delay=600)
        self.enqueue('second')
        self.assertIsNone(self.store.next_for('astra'))

    def test_uncertain_dispatch_does_not_block_others_or_get_resent(self):
        first = self.enqueue('first', 'sending')
        self.store.recover()
        following = self.enqueue('second')
        self.assertEqual(self.store.message(first)['status'], 'uncertain')
        self.assertEqual(self.store.next_for('astra')['id'], following)
        self.store.complete('astra', first, 'Late answer from uncertain dispatch.')
        self.assertEqual(self.store.next_for('astra')['id'], following)

    def test_native_receipt_in_new_transcript_releases_queue_before_answer(self):
        mid = self.enqueue('first', 'sent')
        following = self.enqueue('second')
        self.write(self.active, [self.incoming(mid)])
        self.scan_active()
        self.assertEqual(self.store.message(mid)['status'], 'received')
        self.assertEqual(self.store.next_for('astra')['id'], following)

    def test_reply_already_written_in_new_segment_is_recovered_once(self):
        mid = self.enqueue('first', 'sent')
        # Existing turn tracking must not cause a fresh continuation to start at EOF.
        with self.store.db:
            self.store.db.execute('insert into delivery_turns values (?,?,?)',
                                  (mid, self.sid, 'native-turn'))
            self.store.db.execute('insert into cursors values (?,?)',
                                  (str(self.old), self.old.stat().st_size))
        reply = {'type': 'response_item', 'payload': {'role': 'assistant', 'phase': 'final_answer',
                 'content': [{'type': 'output_text', 'text': 'BRIDGE_REPLY ' + mid + '\nFull reply.'}]}}
        self.write(self.active, [reply])
        self.scan_active()
        self.scan_active()
        self.assertEqual(self.store.message(mid)['status'], 'complete')
        self.assertEqual(self.store.message(mid)['reply'], 'Full reply.')
        self.assertEqual(self.store.db.execute("select count(*) from outbox where key like 'reply:%'").fetchone()[0], 1)

    def test_quoted_envelopes_and_compaction_summaries_are_not_receipts(self):
        mid = self.enqueue('first', 'sent')
        native = self.incoming(mid)
        quoted = copy.deepcopy(native)
        quoted['payload']['name'] = 'exec_command'
        quoted['payload']['namespace'] = 'functions'
        compact = {'type': 'compacted', 'payload': {'message': native['payload']['output']}}
        summary = {'type': 'response_item', 'payload': {'role': 'assistant', 'phase': 'commentary',
                   'content': [{'type': 'output_text', 'text': native['payload']['output']}]}}
        self.write(self.active, [quoted, compact, summary])
        self.scan_active()
        self.assertEqual(self.store.message(mid)['status'], 'sent')

    def test_different_delegation_source_cannot_acknowledge_message(self):
        mid = self.enqueue('first', 'sent')
        native = self.incoming(mid)
        native['payload']['output'] = native['payload']['output'].replace(
            '<source_thread_id>bridge-origin</source_thread_id>',
            '<source_thread_id>unrelated-origin</source_thread_id>')
        self.write(self.active, [native])
        self.scan_active()
        self.assertEqual(self.store.message(mid)['status'], 'sent')


    def claude_incoming(self, mid):
        agent = {'kind': 'claude', 'id': 'fable-registration', 'session': 'claude-exact-session'}
        item = {'type': 'user', 'sessionId': agent['session'], 'uuid': 'inbound-record',
                'message': {'role': 'user', 'content': 'Another Claude session sent a message:\n'
                            + self.bridge.prompt(self.store.message(mid))}}
        return agent, item

    def claude_message(self):
        mid = self.store.enqueue('claude-message', 'astra', ['fable'], 'Review this report.',
                                 'CHANNEL', '123.456', {'fable': 'fable-registration'})[0]
        self.store.set_state(mid, 'sent')
        return mid

    def test_claude_native_receipt_releases_next_message_without_reply(self):
        mid = self.claude_message()
        following = self.store.enqueue('claude-next', 'astra', ['fable'], 'Another report.',
                                       'CHANNEL', '123.456', {'fable': 'fable-registration'})[0]
        agent, item = self.claude_incoming(mid)
        self.bridge.track_claude_receipt('fable', agent, item)
        self.assertEqual(self.store.message(mid)['status'], 'received')
        self.assertEqual(self.store.next_for('fable')['id'], following)
        self.assertTrue(self.store.complete('fable', mid, 'Review ready.'))
        self.assertEqual(self.store.message(mid)['status'], 'complete')

    def test_claude_compact_summary_quoting_envelope_is_not_receipt(self):
        mid = self.claude_message()
        agent, item = self.claude_incoming(mid)
        item['isCompactSummary'] = True
        self.bridge.track_claude_receipt('fable', agent, item)
        self.assertEqual(self.store.message(mid)['status'], 'sent')

    def test_claude_wrong_session_or_registration_is_not_receipt(self):
        mid = self.claude_message()
        agent, item = self.claude_incoming(mid)
        wrong_session = copy.deepcopy(item)
        wrong_session['sessionId'] = 'different-session'
        self.bridge.track_claude_receipt('fable', agent, wrong_session)
        self.assertEqual(self.store.message(mid)['status'], 'sent')
        wrong_registration = dict(agent, id='replacement-registration')
        self.bridge.track_claude_receipt('fable', wrong_registration, item)
        self.assertEqual(self.store.message(mid)['status'], 'sent')

    def test_claude_tool_output_quoting_envelope_is_not_receipt(self):
        mid = self.claude_message()
        agent, item = self.claude_incoming(mid)
        item['message']['content'] = [{'type': 'tool_result', 'content': item['message']['content']}]
        self.bridge.track_claude_receipt('fable', agent, item)
        self.assertEqual(self.store.message(mid)['status'], 'sent')

    def test_quarantine_timeout_releases_queue_without_resending_and_recovers_late_receipt(self):
        with patch('core.time.time', return_value=1000):
            mid = self.enqueue('first', 'sent')
            following = self.enqueue('second')
        with patch('core.time.time', return_value=1119):
            self.store.quarantine_unconfirmed(seconds=120)
            self.assertEqual(self.store.message(mid)['status'], 'sent')
            self.assertIsNone(self.store.next_for('astra'))
        with patch('core.time.time', return_value=1121):
            self.store.quarantine_unconfirmed(seconds=120)
            self.assertEqual(self.store.message(mid)['status'], 'uncertain')
            self.assertEqual(self.store.next_for('astra')['id'], following)
            count = self.store.db.execute('select count(*) from outbox').fetchone()[0]
            self.store.quarantine_unconfirmed(seconds=120)
            self.assertEqual(self.store.db.execute('select count(*) from outbox').fetchone()[0], count)
        self.assertTrue(self.store.received('astra', mid, self.sid, 'late-native-envelope'))
        self.assertEqual(self.store.message(mid)['status'], 'received')
        self.assertTrue(self.store.complete('astra', mid, 'Late valid answer.'))
        self.assertEqual(self.store.message(mid)['status'], 'complete')
        self.assertEqual(self.store.next_for('astra')['id'], following)

    def test_quarantined_message_accepts_late_reply_without_separate_receipt(self):
        with patch('core.time.time', return_value=1000):
            mid = self.enqueue('first', 'sent')
        with patch('core.time.time', return_value=1121):
            self.store.quarantine_unconfirmed(seconds=120)
        self.assertTrue(self.store.complete('astra', mid, 'Late valid answer.'))
        self.assertEqual(self.store.message(mid)['status'], 'complete')


if __name__ == '__main__':
    unittest.main()
