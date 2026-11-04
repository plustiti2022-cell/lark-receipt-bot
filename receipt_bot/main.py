import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import threading

from .api import LarkAPI, Model
from .config import Config, ROOT
from .files import process_file
from .jobs import Store, Worker


def setup_logging(config):
    (ROOT / 'logs').mkdir(exist_ok=True)
    class Redact(logging.Filter):
        def filter(self, record):
            value = record.getMessage()
            for secret in (config.app_secret, config.llm_key, config.backup_key):
                if secret:
                    value = value.replace(secret, '[REDACTED]')
            import re
            value = re.sub(r'(wss://[^\s?]+)\?\S+', r'\1?[REDACTED]', value)
            record.msg, record.args = value, ()
            return True
    handlers = [logging.StreamHandler(), RotatingFileHandler(ROOT / 'logs' / 'bot.log', maxBytes=2_000_000, backupCount=3, encoding='utf-8')]
    for handler in handlers:
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        handler.addFilter(Redact())
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)


def serve(config):
    runtime = {'service': 'lark-receipt-bot', 'pid': os.getpid(), 'started_at': datetime.now(timezone.utc).isoformat(),
               'worker_alive': False, 'processing': None, 'last_error': None}
    ws = None
    store = None
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ('/', '/health'):
                self.send_error(404)
                return
            conn = getattr(ws, '_conn', None)
            state = getattr(conn, 'state', None)
            connected = getattr(state, 'name', '') == 'OPEN'
            payload = {**runtime, 'lark_connected': connected,
                       'model': config.llm_model, 'backup_model': config.backup_model,
                       'jobs': store.counts() if store else {}}
            data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *_):
            pass
    # Bind first: do not recover jobs or start workers when another instance is running.
    try:
        server = ThreadingHTTPServer(('127.0.0.1', config.health_port), Handler)
    except OSError:
        raise SystemExit(f'Local port {config.health_port} is occupied. Check the running bot before starting another instance.')
    store = Store(config.data / 'jobs.sqlite3')
    worker = Worker(config, store, runtime)
    threading.Thread(target=server.serve_forever, name='health', daemon=True).start()
    worker.start()
    LOG = logging.getLogger('receipt_bot')
    import lark_oapi as lark
    # SDK's INFO logging includes the WebSocket credential query. Keep it suppressed.
    from lark_oapi.core.log import logger as sdk_logger
    sdk_logger.setLevel(logging.ERROR)
    def receive(data):
        worker.receive(json.loads(lark.JSON.marshal(data.event)))
    dispatcher = lark.EventDispatcherHandler.builder('', '').register_p2_im_message_receive_v1(receive).build()
    ws = lark.ws.Client(config.app_id, config.app_secret, domain=config.domain,
                        event_handler=dispatcher, log_level=lark.LogLevel.ERROR)
    LOG.info('Local service ready at http://127.0.0.1:%d/health; connecting to Lark', config.health_port)
    try:
        ws.start()
    except KeyboardInterrupt:
        LOG.info('Stopping local bot')
    finally:
        worker.stop_event.set()
        server.shutdown()


def main():
    parser = argparse.ArgumentParser(description='Lark receipt renaming bot')
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('check', help='Check configuration and Lark authentication')
    local = sub.add_parser('process', help='Recognize and rename a local file without sending messages')
    local.add_argument('file', type=Path)
    local.add_argument('--output', type=Path, default=ROOT / 'data' / 'local-output')
    args = parser.parse_args()
    config = Config.load()
    setup_logging(config)
    if args.command == 'check':
        LarkAPI(config).authenticate()
        print(json.dumps({'lark_authenticated': True, 'domain': config.domain, 'model': config.llm_model}, ensure_ascii=False))
    elif args.command == 'process':
        target, result = process_file(args.file, args.output, Model(config), config)
        print(json.dumps({'file': str(target), 'result': result}, ensure_ascii=False))
    else:
        serve(config)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        from .files import UserError
        print(str(exc) if isinstance(exc, (UserError, ValueError)) else 'Startup failed (' + type(exc).__name__ + ').', file=sys.stderr)
        sys.exit(1)
