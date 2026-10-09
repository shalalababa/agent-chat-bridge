"""Ensure the existing user service is live before accepting a peer send."""
import json
import subprocess
import time

from settings import SERVICE, STATE

ACTIVATE_HINT = f'In WSL, run: systemctl --user start {SERVICE}; then retry.'
HEALTH = STATE / 'health.json'


def service_state():
    result = subprocess.run(['systemctl', '--user', 'show', SERVICE,
                             '--property=ActiveState', '--property=MainPID'],
                            capture_output=True, text=True, timeout=10, check=True)
    fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    return fields.get('ActiveState'), int(fields.get('MainPID', '0'))


def control(action):
    # systemd serializes concurrent starts and owns the single service process.
    subprocess.run(['systemctl', '--user', action, SERVICE],
                   capture_output=True, text=True, timeout=15, check=True)


def _check_running(health_path, timeout):
    try:
        state, pid = service_state()
        if state not in ('active', 'activating'):
            control('start')
            state, pid = service_state()
        deadline = time.monotonic() + timeout
        while True:
            try:
                health = json.loads(health_path.read_text())
                age = time.time() - health['updated']
                if (state == 'active' and pid > 0 and health.get('pid') == pid
                        and 0 <= age < 15 and health.get('slack_connected') is True):
                    return
            except (OSError, ValueError, KeyError, TypeError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError('Bridge is not ready (service, heartbeat, or Slack connection). Message not queued. ' + ACTIVATE_HINT)
            time.sleep(0.5)
            state, pid = service_state()
    except (subprocess.SubprocessError, OSError) as error:
        raise RuntimeError('Could not activate or check Agent Bridge. Message not queued. ' + ACTIVATE_HINT) from error


def ensure_running(health_path=HEALTH, timeout=15):
    try:
        _check_running(health_path, timeout)
        return
    except RuntimeError:
        # One automatic recovery attempt. Starting is idempotent and does not
        # interrupt a healthy daemon or resend any previously accepted message.
        pass
    try:
        control('start')
        _check_running(health_path, timeout)
    except (RuntimeError, subprocess.SubprocessError, OSError) as error:
        raise RuntimeError('Automatic bridge activation failed. Message not queued. '
                           + ACTIVATE_HINT + ' Cause: ' + str(error)) from error
