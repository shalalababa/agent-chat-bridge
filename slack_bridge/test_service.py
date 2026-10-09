import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from service import ensure_running


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.health = Path(self.tmp.name) / 'health.json'
        self.addCleanup(self.tmp.cleanup)

    def heartbeat(self, pid=123, age=0, connected=True):
        self.health.write_text(json.dumps({'pid': pid, 'updated': time.time()-age, 'slack_connected': connected}))

    def test_live_service_is_not_restarted(self):
        self.heartbeat()
        with patch('service.service_state', return_value=('active',123)), patch('service.control') as control:
            ensure_running(self.health, timeout=0)
            control.assert_not_called()

    def test_stopped_service_starts_and_requires_new_pid_heartbeat(self):
        self.heartbeat(pid=7)
        def start(action):
            self.assertEqual(action,'start')
            self.heartbeat()
        with patch('service.service_state', side_effect=[('inactive',0),('active',123)]), patch('service.control', side_effect=start) as control:
            ensure_running(self.health, timeout=0)
            control.assert_called_once_with('start')

    def test_stale_wrong_pid_and_disconnected_are_not_live(self):
        for options in [{'age':60},{'pid':7},{'connected':False}]:
            self.heartbeat(**options)
            with patch('service.service_state',return_value=('active',123)), patch('service.control'):
                with self.assertRaisesRegex(RuntimeError,'not queued'):
                    ensure_running(self.health,timeout=0)

    def test_start_failure_is_reported(self):
        with patch('service.service_state',return_value=('failed',0)), patch('service.control',side_effect=RuntimeError('cannot start')):
            with self.assertRaisesRegex(RuntimeError,'cannot start'):
                ensure_running(self.health,timeout=0)

    def test_readiness_error_automatically_runs_start_and_rechecks(self):
        self.heartbeat(connected=False)
        def recover(action):
            self.assertEqual(action, 'start')
            self.heartbeat()
        with patch('service.service_state', return_value=('active',123)), patch('service.control', side_effect=recover) as control:
            ensure_running(self.health, timeout=0)
            control.assert_called_once_with('start')

    def test_failed_recovery_is_bounded_and_preserves_manual_hint(self):
        self.heartbeat(connected=False)
        with patch('service.service_state', return_value=('active',123)), patch('service.control') as control:
            with self.assertRaisesRegex(RuntimeError, 'systemctl --user start agent-bridge.service'):
                ensure_running(self.health, timeout=0)
            control.assert_called_once_with('start')
