import hashlib
from contextlib import contextmanager
import json
import logging
import queue
from pathlib import Path
import shutil
import sqlite3
import threading
import time

from .api import LarkAPI, Model
from .files import UserError, process_file, receipt_summary

LOG = logging.getLogger('receipt_bot')
HELP = '请直接发送票据图片或 PDF。我会识别日期、金额、事项，按新名称返回原文件。\n识别不清的字段会标记“待确认”；一个 PDF 含多张票据时保留整份 PDF。\n处理失败可发送“重试”；请留意结果中的待确认提示。'


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, message_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                sender TEXT NOT NULL, kind TEXT NOT NULL, resource TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL, updated REAL NOT NULL,
                result TEXT, file_key TEXT, reply_id TEXT, error TEXT)''')
            db.execute('CREATE TABLE IF NOT EXISTS controls (id TEXT PRIMARY KEY, created REAL NOT NULL)')
            # The listening port prevents a second local instance from owning this database.
            db.execute("UPDATE jobs SET state='pending' WHERE state='processing'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, message_id, chat_id, sender, kind, resource):
        identity = hashlib.sha256((message_id + ':' + resource).encode()).hexdigest()[:24]
        with self.connect() as db:
            cursor = db.execute('INSERT OR IGNORE INTO jobs(id,message_id,chat_id,sender,kind,resource,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                (identity, message_id, chat_id, sender, kind, resource, time.time(), time.time()))
            return cursor.rowcount == 1

    def claim(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM jobs WHERE state='pending' ORDER BY created LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE jobs SET state='processing', updated=? WHERE id=?", (time.time(), row['id']))
                return dict(row)

    def update(self, identity, **values):
        allowed = {'state', 'result', 'file_key', 'reply_id', 'error'}
        if not set(values) <= allowed:
            raise ValueError('Unsupported job field')
        values['updated'] = time.time()
        with self.connect() as db:
            db.execute('UPDATE jobs SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?', [*values.values(), identity])

    def accept_control(self, message_id):
        with self.connect() as db:
            return db.execute('INSERT OR IGNORE INTO controls VALUES(?,?)', (message_id, time.time())).rowcount == 1

    def retry_last(self, chat_id, sender):
        with self.connect() as db:
            row = db.execute("SELECT id FROM jobs WHERE state='failed' AND chat_id=? AND sender=? ORDER BY updated DESC LIMIT 1", (chat_id, sender)).fetchone()
            if row:
                db.execute("UPDATE jobs SET state='pending',error=NULL,updated=? WHERE id=?", (time.time(), row['id']))
                return True
            return False

    def counts(self):
        with self.connect() as db:
            return dict(db.execute('SELECT state,COUNT(*) FROM jobs GROUP BY state').fetchall())

    def cleanup(self, root, hours):
        cutoff = time.time() - hours * 3600
        with self.connect() as db:
            rows = db.execute("SELECT id FROM jobs WHERE state IN ('done','failed') AND updated<?", (cutoff,)).fetchall()
            for row in rows:
                # id is our SHA256-derived directory name, never a user-controlled path.
                path = root / row['id']
                if path.parent.resolve() == root.resolve() and path.is_dir():
                    shutil.rmtree(path)
                db.execute('DELETE FROM jobs WHERE id=?', (row['id'],))
            db.execute('DELETE FROM controls WHERE created<?', (cutoff,))


def attachments(kind, content):
    if kind == 'image' and content.get('image_key'):
        return [('image', content['image_key'])]
    if kind == 'file' and content.get('file_key'):
        return [('file', content['file_key'])]
    found = []
    def walk(value):
        if isinstance(value, dict):
            if value.get('tag') == 'img' and value.get('image_key'):
                pair = ('image', value['image_key'])
                if pair not in found:
                    found.append(pair)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    if kind == 'post':
        walk(content)
    return found


class Worker(threading.Thread):
    def __init__(self, config, store, runtime):
        super().__init__(name='receipt-worker', daemon=True)
        self.config, self.store, self.runtime = config, store, runtime
        self.stop_event = threading.Event()
        self.controls = queue.Queue(maxsize=100)
        self.api, self.model = LarkAPI(config), Model(config)

    def receive(self, event):
        # This callback only commits to the local queue; all API calls happen off the event loop.
        sender = event.get('sender', {})
        if sender.get('sender_type') != 'user':
            return
        message = event.get('message', {})
        mid, chat = message.get('message_id'), message.get('chat_id')
        uid = sender.get('sender_id', {}).get('open_id', '')
        if not mid or not chat or not uid:
            return
        try:
            content = json.loads(message.get('content', '{}'))
        except (ValueError, TypeError):
            return
        if not isinstance(content, dict):
            return
        refs = attachments(message.get('message_type'), content)
        if refs:
            for kind, key in refs:
                self.store.enqueue(mid, chat, uid, kind, key)
            LOG.info('Attachment event queued (%d resources)', len(refs))
        elif message.get('message_type') == 'text' and self.store.accept_control(mid):
            try:
                self.controls.put_nowait((mid, chat, uid, str(content.get('text', '')).strip()))
            except queue.Full:
                LOG.warning('Control queue is full')

    def run(self):
        last_cleanup = 0
        while not self.stop_event.is_set():
            self.runtime['worker_alive'] = True
            try:
                if time.time() - last_cleanup > 600:
                    self.store.cleanup(self.config.data / 'work', self.config.retention_hours)
                    last_cleanup = time.time()
                try:
                    mid, chat, uid, text = self.controls.get_nowait()
                    if text.lower() in ('retry', '重试'):
                        queued = self.store.retry_last(chat, uid)
                        self.api.text(mid, '已重新排队。' if queued else '没有找到你的失败任务，请重新发送票据。')
                    else:
                        self.api.text(mid, HELP)
                except queue.Empty:
                    pass
                job = self.store.claim()
                if job:
                    self.process(job)
                else:
                    self.stop_event.wait(.5)
            except Exception as exc:
                LOG.error('Worker loop error (%s)', type(exc).__name__)
                self.runtime['last_error'] = '后台任务循环异常，请检查运行日志。'
                self.stop_event.wait(2)
        self.runtime['worker_alive'] = False

    def process(self, job):
        identity = job['id']
        folder = self.config.data / 'work' / identity
        source = folder / 'source'
        try:
            self.runtime['processing'] = identity
            if not source.exists():
                self.api.download(job['message_id'], job['resource'], job['kind'], source)
            if job['result']:
                result = json.loads(job['result'])
                from .files import output_name, prepare
                _, _, extension = prepare(source, self.config.max_bytes, self.config.max_pages)
                target = folder / output_name(result, extension)
                if not target.exists():
                    shutil.copyfile(source, target)
            else:
                target, result = process_file(source, folder, self.model, self.config)
                self.runtime['last_used_model'] = self.model.used_model
                self.store.update(identity, result=json.dumps(result, ensure_ascii=False))
            key = job['file_key'] or self.api.upload(target)
            self.store.update(identity, file_key=key)
            reply_id = self.api.reply(job['message_id'], 'file', {'file_key': key}, identity + ':file')
            self.store.update(identity, reply_id=reply_id, state='done', error=None)
            self.runtime['last_error'] = None
            LOG.info('Receipt completed: %s (%d invoices)', identity, len(result['items']))
            # File delivery is the required result; a summary failure must not retry the file.
            try:
                summary = receipt_summary(result)
                if summary:
                    self.api.text(job['message_id'], summary, identity + ':summary')
            except Exception:
                LOG.warning('Optional summary delivery failed: %s', identity)
        except Exception as exc:
            error = str(exc) if isinstance(exc, UserError) else '处理失败，请检查网络或发送“重试”。'
            self.store.update(identity, state='failed', error=error)
            self.runtime['last_error'] = error
            LOG.error('Receipt failed: %s (%s)', identity, type(exc).__name__)
            try:
                self.api.text(job['message_id'], error, identity + ':error')
            except Exception:
                LOG.error('Could not deliver error notice: %s', identity)
        finally:
            self.runtime['processing'] = None
