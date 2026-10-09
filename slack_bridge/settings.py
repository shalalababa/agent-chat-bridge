"""Local file locations. Every path can be overridden with an environment variable."""
import os
from pathlib import Path


def _path(variable, default):
    return Path(os.environ.get(variable) or default).expanduser()


HOME = Path.home()
# Non-secret settings: team, owner users, channels, bot id, origin thread, agents.
CONFIG = _path('AGENT_BRIDGE_CONFIG', HOME / '.config/agent-bridge/config.json')
# Slack tokens (SLACK_BOT_TOKEN, SLACK_APP_TOKEN) in KEY=value lines, mode 0600.
CREDENTIALS = _path('AGENT_BRIDGE_SLACK_ENV', CONFIG.parent / 'slack.env')
# Queue database, heartbeat and daemon lock.
STATE = _path('AGENT_BRIDGE_STATE', HOME / '.local/state/agent-bridge')
# Claude Code keeps live session registrations in CLAUDE_CONFIG_DIR/sessions
# and transcripts in CLAUDE_CONFIG_DIR/projects.
CLAUDE_HOME = _path('CLAUDE_CONFIG_DIR', HOME / '.claude')
# Codex rollout transcripts. Under WSL with the Windows desktop app this is
# usually /mnt/c/Users/<you>/.codex/sessions.
CODEX_SESSIONS = _path('CODEX_SESSIONS_DIR', HOME / '.codex/sessions')
# systemd user unit that runs `bridge.py run`.
SERVICE = os.environ.get('AGENT_BRIDGE_SERVICE') or 'agent-bridge.service'
