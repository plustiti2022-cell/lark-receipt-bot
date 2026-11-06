"""Manage a macOS launchd service without installing global files."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
LABEL = 'local.lark-receipt-bot.' + hashlib.sha256(str(ROOT).encode()).hexdigest()[:12]
DOMAIN = f'gui/{os.getuid()}'
TARGET = DOMAIN + '/' + LABEL
PLIST = ROOT / 'data' / 'service.plist'


def health(port):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f'http://127.0.0.1:{port}/health', timeout=2) as response:
        result = json.load(response)
    if result.get('service') != 'lark-receipt-bot':
        raise RuntimeError('Health port belongs to another service.')
    return result


def loaded():
    return subprocess.run(['launchctl', 'print', TARGET], capture_output=True).returncode == 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['start', 'stop', 'status'])
    args = parser.parse_args()
    if sys.platform != 'darwin':
        raise SystemExit('These scripts require macOS; use start.ps1 on Windows.')
    if args.action == 'stop':
        if loaded():
            subprocess.run(['launchctl', 'bootout', TARGET], check=True)
        print('Bot stopped.')
        return
    from receipt_bot.config import Config
    config = Config.load()
    if args.action == 'status':
        print(json.dumps(health(config.health_port), ensure_ascii=False, indent=2))
        return
    if not loaded():
        from receipt_bot.api import LarkAPI
        api = LarkAPI(config)
        try:
            api.authenticate()
        finally:
            api.client.close()
        # Refuse to launch alongside another bot or an occupied health port.
        import socket
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', config.health_port))
        (ROOT / 'data').mkdir(exist_ok=True)
        (ROOT / 'logs').mkdir(exist_ok=True)
        with PLIST.open('wb') as handle:
            plistlib.dump({
                'Label': LABEL,
                'ProgramArguments': [str(ROOT / '.venv/bin/python'), '-u', '-m', 'receipt_bot.main'],
                'WorkingDirectory': str(ROOT),
                'RunAtLoad': True,
                'KeepAlive': True,
                'ThrottleInterval': 15,
                'StandardOutPath': str(ROOT / 'logs/stdout.log'),
                'StandardErrorPath': str(ROOT / 'logs/stderr.log'),
                'EnvironmentVariables': {'PYTHONUNBUFFERED': '1'},
            }, handle)
        subprocess.run(['launchctl', 'bootstrap', DOMAIN, str(PLIST)], check=True)
    for _ in range(30):
        try:
            status = health(config.health_port)
            if status.get('lark_connected') and status.get('worker_alive'):
                print(json.dumps(status, ensure_ascii=False, indent=2))
                return
        except (OSError, ValueError, RuntimeError):
            pass
        time.sleep(1)
    raise SystemExit('Service registered, but Lark connection is not verified. Check logs/stderr.log and logs/bot.log.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        from receipt_bot.files import UserError
        message = str(exc) if isinstance(exc, (UserError, ValueError)) else type(exc).__name__
        raise SystemExit('Service action failed: ' + message)
