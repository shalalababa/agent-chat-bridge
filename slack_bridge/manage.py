"""Local command equivalent of the in-Slack agent manager."""
import argparse
import json
import sys

from bridge import CONFIG, DB
from catalog import Catalog
from core import Store
from registry import Registry


def main():
    parser = argparse.ArgumentParser(prog='agent-bridge')
    parser.add_argument('action', choices=['list', 'discover', 'add', 'edit', 'pause', 'resume', 'remove'])
    parser.add_argument('alias', nargs='?')
    parser.add_argument('--session', help='codex:SESSION_ID or claude:SESSION_ID')
    parser.add_argument('--name', help='New alias when editing')
    args = parser.parse_args()
    store = Store(DB)
    registry = Registry(CONFIG, store)
    cfg = registry.load()
    if args.action == 'list':
        for alias, agent in cfg['agents'].items():
            print(f'@{alias}\t{agent["kind"].title()}\t{"Paused" if agent.get("paused") else "Enabled"}\t{agent.get("label", agent["session"])}')
        return
    if args.action == 'discover':
        catalog = Catalog()
        catalog.refresh(cfg)
        for key, session in catalog.sessions.items():
            print(key, session['label'], session.get('status', 'unknown'))
        return
    if not args.alias:
        parser.error('An agent alias is required')
    args.alias = args.alias.lstrip('@').lower()
    if args.action in ('pause', 'resume'):
        registry.pause(args.alias, args.action == 'pause')
    elif args.action == 'remove':
        registry.remove(args.alias)
    else:
        old = args.alias if args.action == 'edit' else None
        if old and old not in cfg['agents']:
            parser.error('Unknown agent')
        if args.session:
            catalog = Catalog()
            catalog.refresh(cfg)
            agent = catalog.resolve(args.session)
        elif old:
            agent = cfg['agents'][old]
        else:
            parser.error('add requires --session')
        registry.save(args.name or args.alias, agent, old, cfg['revision'])
    print('Saved. The running bridge reloads changes automatically.')


if __name__ == '__main__':
    try:
        main()
    except ValueError as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
