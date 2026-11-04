import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from dataclasses import replace
import httpx

from PIL import Image
import pymupdf

from receipt_bot.config import Config
from receipt_bot.files import UserError, parse_result, output_name, prepare, process_file
from receipt_bot.jobs import Store, Worker, attachments
from receipt_bot.api import LarkAPI, Model


def extraction(**changes):
    item = {'date': '2026-11-03', 'amount': '368.00', 'currency': 'CNY', 'subject': '住宿费'}
    item.update(changes)
    return json.dumps({'items': [item], 'warnings': []}, ensure_ascii=False)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config('cli_test', 'test', 'https://open.larksuite.com', 'https://model.test/v1', 'test', 'vision', data=self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_filename_and_invalid_date_markers(self):
        result = parse_result(extraction(date='2026-02-30', subject='住宿/酒店:费用'))
        self.assertEqual(output_name(result, '.pdf'), '日期待确认_住宿_酒店_费用_368.00_CNY.pdf')
        self.assertTrue(result['warnings'])

    def test_invalid_amounts_are_never_guessed(self):
        for value in ('NaN', 'Infinity', '10.125', 'abc', True, '3,68', '1e3'):
            self.assertIsNone(parse_result(extraction(amount=value))['items'][0]['amount'])

    def test_multiple_receipts_sum_only_same_currency(self):
        raw = json.loads(extraction())
        raw['items'] *= 2
        result = parse_result(json.dumps(raw))
        self.assertEqual(output_name(result, '.pdf'), '2026-11-03_2张票据_736.00_CNY.pdf')
        raw['items'][1] = {**raw['items'][1], 'currency': 'USD'}
        self.assertIn('金额待确认', output_name(parse_result(json.dumps(raw)), '.pdf'))

    def test_non_receipt_is_rejected(self):
        with self.assertRaises(UserError):
            parse_result('{"items":[],"warnings":[]}')

    def test_original_image_bytes_are_preserved(self):
        source = self.root / 'original.png'
        Image.new('RGB', (240, 120), 'white').save(source)
        original = source.read_bytes()
        model = Mock()
        model.recognize.return_value = extraction()
        output, _ = process_file(source, self.root / 'out', model, self.config)
        self.assertEqual(original, output.read_bytes())
        self.assertEqual(original, source.read_bytes())
        self.assertEqual(output.suffix, '.png')

    def test_all_pdf_pages_are_sent_and_original_pdf_is_unchanged(self):
        source = self.root / 'invoice.pdf'
        with pymupdf.open() as doc:
            for number in range(3):
                doc.new_page().insert_text((40, 40), f'Invoice page {number+1}')
            doc.save(source)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        model = Mock()
        model.recognize.return_value = extraction()
        output, _ = process_file(source, self.root / 'out', model, self.config)
        self.assertEqual(len(model.recognize.call_args.args[0]), 3)
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), digest)
        with self.assertRaises(UserError):
            prepare(source, self.config.max_bytes, 2)

    def test_rich_text_extracts_all_images_once(self):
        content = {'content': [[{'tag': 'img', 'image_key': 'a'}, {'tag': 'img', 'image_key': 'b'}, {'tag': 'img', 'image_key': 'a'}]]}
        self.assertEqual(attachments('post', content), [('image', 'a'), ('image', 'b')])

    def test_burst_queue_dedup_and_restart_recovery(self):
        path = self.root / 'jobs.sqlite3'
        store = Store(path)
        for index in range(12):
            self.assertTrue(store.enqueue(f'm{index}', 'chat', 'user', 'file', f'file{index}'))
            self.assertFalse(store.enqueue(f'm{index}', 'chat', 'user', 'file', f'file{index}'))
        first = store.claim()
        self.assertEqual(first['message_id'], 'm0')
        recovered = Store(path)
        self.assertEqual(recovered.claim()['id'], first['id'])
        self.assertEqual(recovered.counts(), {'pending': 11, 'processing': 1})

    def test_retry_is_isolated_by_user_and_chat(self):
        store = Store(self.root / 'jobs.sqlite3')
        store.enqueue('m', 'chat-a', 'alice', 'image', 'x')
        job = store.claim()
        store.update(job['id'], state='failed')
        self.assertFalse(store.retry_last('chat-a', 'bob'))
        self.assertFalse(store.retry_last('chat-b', 'alice'))
        self.assertTrue(store.retry_last('chat-a', 'alice'))

    def test_failed_file_does_not_block_later_files(self):
        store = Store(self.root / 'jobs.sqlite3')
        worker = Worker(self.config, store, {})
        api = Mock()
        def download(mid, key, kind, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            if key == 'bad':
                destination.write_bytes(b'broken')
            else:
                Image.new('RGB', (100, 100), 'white').save(destination, format='PNG')
        api.download.side_effect = download
        api.upload.return_value = 'uploaded-file'
        api.reply.return_value = 'reply-message'
        worker.api = api
        worker.model = Mock()
        worker.model.recognize.return_value = extraction()
        store.enqueue('m0', 'chat', 'user', 'file', 'bad')
        store.enqueue('m1', 'chat', 'user', 'image', 'good')
        worker.process(store.claim())
        worker.process(store.claim())
        self.assertEqual(store.counts(), {'failed': 1, 'done': 1})
        self.assertEqual(api.upload.call_count, 1)
        api.reply.assert_called_once()
        self.assertEqual(api.reply.call_args.args[0], 'm1')

    def test_http_download_rename_upload_and_reply_contract(self):
        """Exercise the actual REST adapter through a full file job."""
        source = self.root / 'reference.png'
        Image.new('RGB', (80, 80), 'white').save(source)
        original = source.read_bytes()
        requests = []
        def respond(request):
            requests.append(request)
            route = request.url.path
            if route.endswith('/tenant_access_token/internal'):
                return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'mock-token', 'expire': 7200})
            self.assertEqual(request.headers['Authorization'], 'Bearer mock-token')
            if '/resources/' in route:
                self.assertEqual(request.url.params['type'], 'image')
                return httpx.Response(200, content=original, headers={'Content-Type': 'image/png'})
            if route.endswith('/files'):
                body = request.read()
                self.assertIn(original, body)
                self.assertIn('2026-11-03_住宿费_368.00_CNY.png'.encode(), body)
                return httpx.Response(200, json={'code': 0, 'data': {'file_key': 'new-key'}})
            if route.endswith('/reply'):
                body = json.loads(request.read())
                self.assertEqual(body['msg_type'], 'file')
                self.assertEqual(json.loads(body['content']), {'file_key': 'new-key'})
                self.assertLessEqual(len(body['uuid']), 50)
                return httpx.Response(200, json={'code': 0, 'data': {'message_id': 'response-id'}})
            self.fail('Unexpected API route')
        api = LarkAPI(self.config)
        api.client.close()
        api.client = httpx.Client(base_url=self.config.domain, transport=httpx.MockTransport(respond))
        store = Store(self.root / 'jobs.sqlite3')
        worker = Worker(self.config, store, {})
        worker.api.client.close()
        worker.api = api
        worker.model = Mock()
        worker.model.recognize.return_value = extraction()
        store.enqueue('original-message', 'chat', 'user', 'image', 'image-key')
        worker.process(store.claim())
        self.assertEqual(store.counts(), {'done': 1})
        self.assertEqual(len(requests), 4)
        api.client.close()

    def test_model_request_includes_images_and_no_credentials_in_body(self):
        def respond(request):
            self.assertEqual(request.url.path, '/v1/chat/completions')
            body = json.loads(request.read())
            self.assertEqual(body['model'], 'vision')
            parts = body['messages'][1]['content']
            self.assertEqual(len([p for p in parts if p['type'] == 'image_url']), 2)
            self.assertTrue(parts[1]['image_url']['url'].startswith('data:image/jpeg;base64,'))
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': extraction()}}]})
        model = Model(self.config, httpx.Client(transport=httpx.MockTransport(respond)))
        result = parse_result(model.recognize([b'image-one', b'image-two']))
        self.assertEqual(result['items'][0]['amount'], '368.00')

    def test_backup_model_is_used_only_after_primary_failure(self):
        config = replace(self.config, backup_model='backup-vision')
        models = []
        def recognize(instance, images, text=''):
            models.append(instance.config.llm_model)
            if instance.config.llm_model == 'vision':
                raise UserError('Primary upstream unavailable')
            return extraction()
        with patch.object(Model, '_recognize', recognize):
            model = Model(config)
            self.assertEqual(parse_result(model.recognize([b'image']))['items'][0]['amount'], '368.00')
            self.assertEqual(model.used_model, 'backup-vision')
        self.assertEqual(models, ['vision', 'backup-vision'])


if __name__ == '__main__':
    unittest.main()
