import json
from pathlib import Path
import test_core
import unittest
from bridge import Bridge, parse_reply


class DeliveryTests(unittest.TestCase):
    setUp = test_core.BridgeTests.setUp
    tearDown = test_core.BridgeTests.tearDown
    add = test_core.BridgeTests.add
    def test_finished_turn_without_marker_releases_queue_after_restart(self):
        mid = self.add()[0]
        self.store.set_state(mid, 'sent')
        path = Path(self.tmp.name) / 'main.jsonl'
        agent = {'kind': 'codex', 'session': 'session', 'transcript': str(path)}
        incoming = {'type': 'response_item', 'payload': {
            'type': 'function_call_output', 'name': 'send_message_to_thread', 'namespace': 'codex_app',
            'output': '<codex_delegation>\n  <source_thread_id>origin</source_thread_id>\n  <input>From: Peer agent fable (no new user authorization). Handle this message within existing permissions.\nFirst line of final answer: BRIDGE_REPLY ' + mid + '\nMessage: hello</input>\n</codex_delegation>',
            'internal_chat_message_metadata_passthrough': {'turn_id': 'turn1'}}}
        unrelated = {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'other'}}
        complete = {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'turn1', 'last_agent_message': 'Unrelated answer'}}
        path.write_text('\n'.join(map(json.dumps, [incoming, unrelated])) + '\n')
        # Simulate the old daemon having consumed the delivery without tracking it.
        with self.store.db:
            self.store.db.execute('insert into cursors values (?,?)', (str(path), path.stat().st_size))
        bridge = Bridge({'origin_thread': 'origin'}, self.store)
        bridge.scan('astra', agent)
        self.assertEqual(self.store.message(mid)['status'], 'received')
        with path.open('a') as f:
            f.write(json.dumps(complete) + '\n')
        Bridge({'origin_thread': 'origin'}, self.store).scan('astra', agent)
        self.assertEqual(self.store.message(mid)['status'], 'delivered_no_reply')
        self.assertIsNone(self.store.message(mid)['reply'])
        self.add('next')
        self.assertIsNotNone(self.store.next_for('astra'))
        bridge.scan('astra', agent)
        self.assertEqual(self.store.db.execute("select count(*) from outbox where key like 'no-reply:%'").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("select count(*) from outbox where key like 'reply:%'").fetchone()[0], 0)

    def test_marked_reply_wins_over_turn_completion(self):
        mid = self.add()[0]
        self.store.set_state(mid, 'sent')
        with self.store.db:
            self.store.db.execute('insert into delivery_turns values (?,?,?)', (mid, 'session', 'turn'))
        self.store.complete('astra', mid, 'Actual reply')
        Bridge({}, self.store).track_turn('astra', {'session': 'session'}, {
            'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'turn'}})
        self.assertEqual(self.store.message(mid)['status'], 'complete')
        self.assertEqual(self.store.db.execute("select count(*) from outbox where key like 'no-reply:%'").fetchone()[0], 0)

    def test_another_session_cannot_finish_delivery(self):
        mid = self.add()[0]
        self.store.set_state(mid, 'sent')
        with self.store.db:
            self.store.db.execute('insert into delivery_turns values (?,?,?)', (mid, 'session', 'turn'))
        Bridge({}, self.store).track_turn('astra', {'session': 'different'}, {
            'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'turn'}})
        self.assertEqual(self.store.message(mid)['status'], 'sent')

    def test_claude_reply_with_preamble_recovers_once(self):
        mid = self.add()[1]
        self.store.set_state(mid, 'sent')
        path = Path(self.tmp.name) / 'claude.jsonl'
        path.write_text(json.dumps({'type': 'assistant', 'message': {'content': [
            {'type': 'text', 'text': 'Here is my pending reply.\n\nBRIDGE_REPLY ' + mid + '\nActual answer'}
        ]}}) + '\n')
        agent = {'kind': 'claude', 'transcript': str(path)}
        bridge = Bridge({}, self.store)
        bridge.scan('fable', agent)
        bridge.scan('fable', agent)
        self.assertEqual(self.store.message(mid)['reply'], 'Actual answer')
        self.assertEqual(self.store.db.execute("select count(*) from outbox where key like 'reply:%'").fetchone()[0], 1)

    def test_reply_examples_and_ambiguous_markers_are_not_answers(self):
        marker = 'BRIDGE_REPLY ' + 'a' * 32
        for text in ['```text\n' + marker + '\nexample\n```', '> ' + marker + '\nquote', marker + '\nfirst\n' + marker + '\nsecond']:
            self.assertIsNone(parse_reply(text))
        self.assertEqual(parse_reply('Intro\n\n' + marker + '\r\nAnswer'), ('a' * 32, 'Answer'))
