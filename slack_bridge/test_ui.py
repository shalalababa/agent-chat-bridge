import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from catalog import Catalog
from core import Store
from registry import Registry
from slack_ui import SlackUI


class UITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = Store(root / 'queue.db')
        path = root / 'config.json'
        path.write_text(json.dumps({'team': 'T', 'users': ['U'], 'agents': {'astra': {'kind': 'codex', 'session': 's1', 'transcript': '/tmp/t', 'label': 'My session'}}}))
        self.registry = Registry(path, self.store)
        self.ui = SlackUI(self.registry, Catalog(), lambda: None)

    async def asyncTearDown(self):
        if self.ui.tasks:
            await asyncio.gather(*self.ui.tasks, return_exceptions=True)
        self.store.db.close()
        self.tmp.cleanup()

    async def test_untrusted_user_cannot_pause(self):
        r = self.ui.handle({'type': 'slash_commands', 'payload': {'team_id': 'T', 'user_id': 'outsider', 'command': '/agents', 'text': 'pause astra'}})
        self.assertIn('restricted', r['text'])
        self.assertFalse(self.registry.load()['agents']['astra']['paused'])

    async def test_owner_pause_and_resume(self):
        with patch('slack_ui.slack', return_value={'ok': True}):
            for command, paused in [('pause astra', True), ('resume astra', False)]:
                self.ui.handle({'type': 'slash_commands', 'payload': {'team_id': 'T', 'user_id': 'U', 'command': '/agents', 'text': command}})
                self.assertEqual(self.registry.load()['agents']['astra']['paused'], paused)
            await asyncio.gather(*self.ui.tasks)

    async def test_retried_interaction_does_not_apply_twice(self):
        envelope = {'envelope_id': 'same-envelope', 'type': 'slash_commands', 'payload': {'team_id': 'T', 'user_id': 'U', 'command': '/agents', 'text': 'pause astra'}}
        with patch('slack_ui.slack', return_value={'ok': True}):
            first = self.ui.handle(envelope)
            revision = self.registry.load()['revision']
            self.assertEqual(self.ui.handle(envelope), first)
            self.assertEqual(self.registry.load()['revision'], revision)
            await asyncio.gather(*self.ui.tasks)

    async def test_edit_form_preserves_session_selection(self):
        view = self.ui.edit_view('astra')
        picker = next(b for b in view['blocks'] if b.get('block_id') == 'session')['element']
        self.assertEqual(picker['initial_option']['value'], 'codex:s1')
        self.assertEqual(json.loads(view['private_metadata'])['old'], 'astra')

    async def test_refresh_updates_the_clicked_slash_command_card(self):
        payload = {'user': {'id': 'U'}, 'actions': [{'action_id': 'refresh'}],
                   'response_url': 'https://hooks.slack.com/actions/test', 'container': {'type': 'message', 'is_ephemeral': True}}
        with patch.object(self.ui.catalog, 'refresh'), patch('slack_ui.slack', return_value={'ok': True}), patch('slack_ui.slack_response', create=True) as response:
            await self.ui.act(payload)
        response.assert_called_once()
        self.assertEqual(response.call_args.args[0], payload['response_url'])
        self.assertTrue(response.call_args.args[1]['replace_original'])
        self.assertIn('blocks', response.call_args.args[1])

    async def test_home_refresh_does_not_require_a_response_url(self):
        payload = {'user': {'id': 'U'}, 'actions': [{'action_id': 'refresh'}], 'view': {'type': 'home'}}
        with patch.object(self.ui.catalog, 'refresh'), patch('slack_ui.slack', return_value={'ok': True}) as api:
            await self.ui.act(payload)
        self.assertEqual(api.call_args.args[0], 'views.publish')

    async def test_save_submission_updates_registry_and_modal(self):
        cfg = self.registry.load()
        payload = {'user': {'id': 'U'}, 'view': {'id': 'V', 'callback_id': 'agent_save', 'private_metadata': json.dumps({'old': 'astra', 'revision': cfg['revision']}),
                   'state': {'values': {'alias': {'value': {'value': 'reviewer'}}, 'session': {'session_picker': {'selected_option': {'value': 'codex:s1', 'text': {'type': 'plain_text', 'text': 'My session'}}}}, 'pasted': {'value': {'value': None}}}}}}
        with patch.object(self.ui.catalog, 'resolve', return_value=cfg['agents']['astra']), patch('slack_ui.slack', return_value={'ok': True}) as api:
            await self.ui.submitted(payload)
        self.assertIn('reviewer', self.registry.load()['agents'])
        self.assertNotIn('astra', self.registry.load()['agents'])
        self.assertEqual(api.call_args_list[0].args[0], 'views.update')

    async def test_form_error_preserves_user_input(self):
        cfg = self.registry.load()
        payload = {'user': {'id': 'U'}, 'view': {'id': 'V', 'callback_id': 'agent_save', 'private_metadata': json.dumps({'old': None, 'revision': cfg['revision']}),
                   'state': {'values': {'alias': {'value': {'value': 'new_agent'}}, 'session': {'session_picker': {'selected_option': None}}, 'pasted': {'value': {'value': 'bad'}}}}}}
        with patch('slack_ui.slack', return_value={'ok': True}) as api:
            await self.ui.submitted(payload)
        replacement = api.call_args_list[0].args[1]['view']
        alias = next(b for b in replacement['blocks'] if b.get('block_id') == 'alias')
        self.assertEqual(alias['element']['initial_value'], 'new_agent')
        self.assertEqual(replacement['callback_id'], 'agent_save')

    async def test_both_slack_event_types_deduplicate(self):
        from bridge import Bridge
        cfg = {'team': 'T', 'users': ['U'], 'channels': ['C'], 'bot': 'B', 'agents': {'astra': {}}}
        bridge = Bridge(cfg, self.store)
        for event_type, event_id in [('app_mention', 'event1'), ('message', 'event2')]:
            bridge.receive({'team_id': 'T', 'event_id': event_id, 'event': {'type': event_type, 'user': 'U', 'channel': 'C', 'ts': '1.0', 'text': '<@B> astra hello'}})
        self.assertEqual(self.store.db.execute('select count(*) from messages').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
