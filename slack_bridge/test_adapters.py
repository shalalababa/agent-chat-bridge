import json
import unittest
from unittest.mock import patch

from adapters import Unavailable, readiness, slack_response, dispatch
import struct


class ReadinessTests(unittest.TestCase):
    def test_response_endpoint_rejects_untrusted_urls_before_network(self):
        for url in ['http://hooks.slack.com/actions/x', 'https://hooks.slack.com.evil.test/x', 'https://localhost/x', 'https://user@hooks.slack.com/x']:
            with self.assertRaises(ValueError):
                slack_response(url, {'replace_original': True})

    def test_claude_busy_session_accepts_native_queue_delivery(self):
        agent = {'kind': 'claude', 'session': 'exact-session'}
        for status in ['idle', 'busy']:
            owner = {'pid': 123, 'status': status}
            with patch('adapters.claude_owner', return_value=owner):
                self.assertEqual(readiness(agent, 'origin'), owner)
        with patch('adapters.claude_owner', side_effect=Unavailable('offline')):
            with self.assertRaises(Unavailable):
                readiness(agent, 'origin')

    def test_claude_dispatch_uses_next_priority_and_checks_socket_owner(self):
        owner = {'pid': 123, 'messagingSocketPath': '/tmp/test.sock'}
        with patch('adapters.claude_owner', return_value=owner), patch('adapters.socket.socket') as factory:
            client = factory.return_value.__enter__.return_value
            client.getsockopt.return_value = struct.pack('3i', 123, 1000, 1000)
            dispatch({'kind': 'claude', 'session': 'exact'}, 'hello', 'origin', owner)
            frame = json.loads(client.sendall.call_args.args[0])
            self.assertEqual(frame['priority'], 'next')
            self.assertEqual(frame['session_id'], 'exact')
            client.sendall.reset_mock()
            client.getsockopt.return_value = struct.pack('3i', 456, 1000, 1000)
            with self.assertRaises(Unavailable):
                dispatch({'kind': 'claude', 'session': 'exact'}, 'hello', 'origin', owner)
            client.sendall.assert_not_called()

    def test_missing_backend_is_distinct_from_busy(self):
        with patch('adapters.codex_call', side_effect=RuntimeError('Desktop Codex app-tools configuration not found')):
            with self.assertRaisesRegex(Unavailable, 'WSL backend connection missing'):
                readiness({'kind': 'codex', 'session': 'exact'}, 'origin')

    def test_plugin_launch_configuration(self):
        from adapters import normalize_app_tools
        settings = {'plugin_path': 'C:/app/tools', 'env': {'CODEX_MCP_NODE_PATH': 'C:/node.exe'}}
        self.assertEqual(normalize_app_tools(settings)['args'], ['C:/node.exe', 'C:/app/tools/server.mjs'])
        legacy = {'args': ['node.exe', 'server.mjs']}
        self.assertEqual(normalize_app_tools(legacy), legacy)
        with self.assertRaises(ValueError):
            normalize_app_tools({'plugin_path': 'C:/app/tools'})

    def test_saved_or_active_codex_session_accepts_native_delivery(self):
        agent = {'kind': 'codex', 'session': 'id'}
        for state in ['idle', 'notLoaded', 'active', 'error']:
            reply = {'content': [{'type': 'text', 'text': json.dumps({'thread': {'status': {'type': state}}})}]}
            with patch('adapters.codex_call', return_value=reply):
                if state == 'error':
                    with self.assertRaises(Unavailable):
                        readiness(agent, 'origin')
                else:
                    self.assertIsNone(readiness(agent, 'origin'))


if __name__ == '__main__':
    unittest.main()
