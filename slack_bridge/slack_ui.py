"""Slack-native agent manager. Modal opens never wait for discovery RPCs."""
import asyncio
import json
import time

from adapters import Unavailable, readiness, slack, slack_response


def plain(text):
    return {'type': 'plain_text', 'text': str(text)[:2900]}


def section(text):
    return {'type': 'section', 'text': plain(text)}


def button(label, action, value='', **extra):
    return {'type': 'button', 'text': plain(label), 'action_id': action, 'value': value or action, **extra}


def notice(title, text):
    return {'type': 'modal', 'title': plain(title), 'close': plain('Done'), 'blocks': [section(text)]}


class SlackUI:
    def __init__(self, registry, catalog, reload_config):
        self.registry, self.catalog, self.reload = registry, catalog, reload_config
        self.tasks = set()
        registry.store.db.execute('create table if not exists ui_receipts (id text primary key, response text)')
        registry.store.db.commit()

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        def report(done):
            if not done.cancelled() and done.exception():
                print(json.dumps({'event': 'slack_ui_error', 'error_type': type(done.exception()).__name__}), flush=True)
        task.add_done_callback(report)

    def authorized(self, payload):
        cfg = self.registry.load()
        team = payload.get('team_id') or payload.get('team', {}).get('id')
        user = payload.get('user_id') or payload.get('user', {}).get('id')
        return team == cfg['team'] and user in cfg['users']

    def blocks(self):
        cfg = self.registry.load()
        names = list(cfg['agents'])[:2] or ['reviewer']
        example = f'Send @{names[0]} hello'
        if len(names) > 1:
            example += f' or @{names[0]} @{names[1]} review this'
        blocks = [ {'type': 'header', 'text': plain('Your agents')},
                   section(example + ' in an enabled channel. These are plain-text aliases.'),
                   {'type': 'actions', 'elements': [button('Add agent', 'add', style='primary'), button('Refresh', 'refresh')]}]
        for alias, agent in list(cfg['agents'].items())[:25]:
            live = self.catalog.sessions.get(agent['kind'] + ':' + agent['session'], {})
            status = 'Paused' if agent.get('paused') else {'idle': 'Available', 'notLoaded': 'Available', 'active': 'Busy', 'busy': 'Busy', 'offline': 'Offline'}.get(live.get('status'), 'Not checked')
            pending = self.registry.pending(alias, ['queued', 'sending', 'sent', 'uncertain'])
            title = agent.get('label') or live.get('label') or agent['session'][:8]
            received = self.registry.pending(alias, ['received'])
            problem = self.registry.store.db.execute(
                "select error from messages where target=? and status in ('queued','sent','uncertain') and error is not null order by updated desc limit 1", (alias,)).fetchone()
            detail = (f'\n{pending} pending delivery(s)' if pending else '')
            detail += (f'\n{received} received; no reply yet' if received else '')
            if problem:
                detail += '\n' + problem[0][:250]
            blocks += [{'type': 'divider'}, section(f'@{alias} · {status}\n{agent["kind"].title()} · {title}' + detail)]
            value = json.dumps({'alias': alias, 'id': agent['id']})
            blocks.append({'type': 'actions', 'block_id': 'agent_' + alias, 'elements': [
                button('Edit', 'edit', value), button('Resume' if agent.get('paused') else 'Pause', 'resume' if agent.get('paused') else 'pause', value),
                button('Check connection', 'check', value), button('Remove', 'remove', value)]})
        if not cfg['agents']:
            blocks.append(section('No agents registered yet. Add an existing Codex or Claude session to begin.'))
        if len(cfg['agents']) > 25:
            blocks.append(section('Showing the first 25 agents. Use /agents edit NAME to manage another agent.'))
        blocks.append({'type': 'context', 'elements': [plain('Pause affects bridge delivery only. Removing a registration leaves its conversation intact.')]})
        if self.catalog.updated:
            blocks.append({'type': 'context', 'elements': [plain('Refreshed at ' + time.strftime('%H:%M:%S %Z', time.localtime(self.catalog.updated)))]})
        return blocks

    async def publish(self, user, refresh=False):
        if refresh:
            await asyncio.to_thread(self.catalog.refresh, self.registry.load())
        await asyncio.to_thread(slack, 'views.publish', {'user_id': user, 'view': {'type': 'home', 'blocks': self.blocks()}})

    async def update_source(self, payload):
        if payload.get('response_url'):
            await asyncio.to_thread(slack_response, payload['response_url'],
                                    {'replace_original': True, 'text': 'Agent Bridge manager', 'blocks': self.blocks()})
        elif payload.get('container', {}).get('type') == 'message' and not payload['container'].get('is_ephemeral', True):
            await asyncio.to_thread(slack, 'chat.update', {'channel': payload['container']['channel_id'],
                                                          'ts': payload['container']['message_ts'], 'text': 'Agent Bridge manager', 'blocks': self.blocks()})

    async def refresh_manager(self, payload):
        await asyncio.to_thread(self.catalog.refresh, self.registry.load())
        await self.update_source(payload)
        await self.publish(payload.get('user_id') or payload['user']['id'])

    def edit_view(self, old=None, values=None, error=None):
        cfg = self.registry.load()
        agent = cfg['agents'].get(old, {})
        values = values or {}
        metadata = {'old': old, 'revision': cfg['revision']}
        alias = values.get('alias', old or '')
        selected = values.get('selected')
        if not selected and agent:
            selected = {'text': plain((agent['kind'].title() + ' · ' + agent.get('label', old))[:75]), 'value': agent['kind'] + ':' + agent['session']}
        alias_element = {'type': 'plain_text_input', 'action_id': 'value', 'placeholder': plain('e.g. reviewer'), 'max_length': 32}
        if alias:
            alias_element['initial_value'] = alias
        picker = {'type': 'external_select', 'action_id': 'session_picker', 'placeholder': plain('Choose an existing session'), 'min_query_length': 0}
        if selected:
            picker['initial_option'] = selected
        pasted = {'type': 'plain_text_input', 'action_id': 'value', 'placeholder': plain('codex:SESSION_ID or claude:SESSION_ID')}
        if values.get('pasted'):
            pasted['initial_value'] = values['pasted']
        blocks = []
        if error:
            blocks.append(section('Could not save: ' + error))
        blocks += [
            {'type': 'input', 'block_id': 'alias', 'label': plain('Agent alias'), 'element': alias_element},
            {'type': 'input', 'block_id': 'session', 'optional': True, 'label': plain('Existing session'), 'element': picker},
            {'type': 'input', 'block_id': 'pasted', 'optional': True, 'label': plain('Or enter a session ID'), 'element': pasted},
            {'type': 'context', 'elements': [plain('The list shows recent local Codex tasks and live Claude sessions. A pasted ID overrides the selection. No new conversation will be created.')]},
        ]
        return {'type': 'modal', 'callback_id': 'agent_save', 'private_metadata': json.dumps(metadata), 'title': plain('Edit agent' if old else 'Add agent'), 'submit': plain('Save'), 'close': plain('Cancel'), 'blocks': blocks}

    def remove_view(self, alias):
        cfg = self.registry.load()
        queued = self.registry.pending(alias, ['queued'])
        text = f'Remove @{alias} from Agent Bridge? Its underlying conversation will remain open and unchanged.'
        if queued:
            text += f' Its {queued} unsent message(s) will be cancelled.'
        return {'type': 'modal', 'callback_id': 'agent_remove', 'private_metadata': json.dumps({'alias': alias, 'revision': cfg['revision']}),
                'title': plain('Remove agent'), 'submit': plain('Remove'), 'close': plain('Cancel'), 'blocks': [section(text)]}

    async def open_view(self, trigger, view):
        return await asyncio.to_thread(slack, 'views.open', {'trigger_id': trigger, 'view': view})

    async def check(self, trigger, alias):
        opened = await self.open_view(trigger, notice('Check connection', f'Checking @{alias}…'))
        cfg = self.registry.load()
        try:
            await asyncio.to_thread(readiness, cfg['agents'][alias], cfg['origin_thread'])
            text = f'@{alias}: the exact registered session is available to receive a message. No agent turn was started.'
        except Unavailable as error:
            text = f'@{alias}: {error}. No agent turn was started.'
        await asyncio.to_thread(slack, 'views.update', {'view_id': opened['view']['id'], 'view': notice('Connection result', text)})

    async def act(self, payload):
        user = payload['user']['id']
        action = payload['actions'][0]
        name = action['action_id']
        if name == 'add':
            await self.open_view(payload['trigger_id'], self.edit_view())
        elif name == 'refresh':
            await self.refresh_manager(payload)
        else:
            value = json.loads(action['value'])
            alias = value['alias']
            current = self.registry.load()['agents'].get(alias)
            if not current or current['id'] != value['id']:
                await self.open_view(payload['trigger_id'], notice('Agent changed', 'This registration changed. Refresh the agent list and try again.'))
                return
            if name == 'edit':
                await self.open_view(payload['trigger_id'], self.edit_view(alias))
            elif name == 'remove':
                await self.open_view(payload['trigger_id'], self.remove_view(alias))
            elif name == 'check':
                await self.check(payload['trigger_id'], alias)
            elif name in ('pause', 'resume'):
                self.registry.pause(alias, name == 'pause')
                self.reload()
                await self.update_source(payload)
                await self.publish(user)

    async def submitted(self, payload):
        view = payload['view']
        meta = json.loads(view['private_metadata'])
        values = {}
        try:
            if view['callback_id'] == 'agent_save':
                state = view['state']['values']
                values = {'alias': state['alias']['value']['value'] or '', 'selected': state['session']['session_picker'].get('selected_option'), 'pasted': state['pasted']['value'].get('value') or ''}
                key = values['pasted'].strip() or (values['selected'] or {}).get('value', '')
                agent = await asyncio.to_thread(self.catalog.resolve, key)
                self.registry.save(values['alias'], agent, meta.get('old'), meta['revision'])
                message = f'@{values["alias"].strip().lower()} is ready. Its existing session is now registered with Agent Bridge.'
            else:
                self.registry.remove(meta['alias'], meta['revision'])
                message = f'@{meta["alias"]} was removed from Agent Bridge. Its conversation was not closed.'
            self.reload()
            replacement = notice('Saved', message)
        except ValueError as error:
            replacement = (self.edit_view(meta.get('old'), values, str(error)) if view['callback_id'] == 'agent_save' else notice('Not removed', str(error)))
        except Exception:
            replacement = notice('Could not save', 'The local bridge could not save this change. Refresh the manager and try again.')
        await asyncio.to_thread(slack, 'views.update', {'view_id': view['id'], 'view': replacement})
        await self.publish(payload['user']['id'])

    def handle(self, envelope):
        eid = envelope.get('envelope_id')
        if eid:
            previous = self.registry.store.db.execute('select response from ui_receipts where id=?', (eid,)).fetchone()
            if previous:
                return json.loads(previous[0])
        result = self._handle(envelope)
        if eid:
            with self.registry.store.db:
                self.registry.store.db.execute('insert or ignore into ui_receipts values (?,?)', (eid, json.dumps(result)))
        return result

    def _handle(self, envelope):
        payload = envelope.get('payload', {})
        if not self.authorized(payload):
            return {'text': 'This Agent Bridge manager is restricted to its configured owner.', 'response_type': 'ephemeral'}
        if envelope['type'] == 'slash_commands':
            if payload.get('command') != '/agents':
                return {'text': 'Unknown command.', 'response_type': 'ephemeral'}
            parts = payload.get('text', '').strip().split()
            command = parts[0].lower() if parts else 'list'
            alias = parts[1].lower().lstrip('@') if len(parts) > 1 else None
            cfg = self.registry.load()
            if command in ('list', 'help'):
                self.spawn(self.refresh_manager(payload))
                return {'response_type': 'ephemeral', 'text': 'Agent Bridge manager', 'blocks': self.blocks()}
            if command == 'add':
                self.spawn(self.open_view(payload['trigger_id'], self.edit_view()))
                return {}
            if alias not in cfg['agents']:
                return {'response_type': 'ephemeral', 'text': 'Use /agents list, add, edit NAME, pause NAME, resume NAME, or remove NAME.'}
            if command in ('edit', 'remove'):
                view = self.edit_view(alias) if command == 'edit' else self.remove_view(alias)
                self.spawn(self.open_view(payload['trigger_id'], view))
                return {}
            if command in ('pause', 'resume'):
                self.registry.pause(alias, command == 'pause')
                self.reload()
                self.spawn(self.publish(payload['user_id']))
                return {'response_type': 'ephemeral', 'text': f'@{alias} is now {"paused" if command == "pause" else "enabled"}.'}
            return {'response_type': 'ephemeral', 'text': 'Unknown action. Use /agents list.'}
        kind = payload.get('type')
        if kind == 'block_suggestion':
            return {'options': self.catalog.options(payload.get('value', ''))}
        if kind == 'block_actions':
            if payload.get('actions', [{}])[0].get('action_id') in ('add', 'refresh', 'edit', 'remove', 'pause', 'resume', 'check'):
                self.spawn(self.act(payload))
            return {}
        if kind == 'view_submission' and payload['view']['callback_id'] in ('agent_save', 'agent_remove'):
            self.spawn(self.submitted(payload))
            return {'response_action': 'update', 'view': notice('Saving', 'Saving your agent settings…')}
        return {}
