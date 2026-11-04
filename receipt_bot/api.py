import json
from dataclasses import replace
import logging
from pathlib import Path
import time
import uuid

import httpx

from .files import PROMPT, UserError, image_data


class Model:
    def __init__(self, config, client=None):
        self.config = config
        self.client = client
        self.used_model = None

    def recognize(self, images, text=''):
        try:
            result = self._recognize(images, text)
            self.used_model = self.config.llm_model
            return result
        except UserError:
            if not self.config.backup_model or self.config.backup_model == self.config.llm_model:
                raise
            logging.getLogger('receipt_bot').warning('Primary model unavailable; using configured backup model')
            config = replace(self.config, llm_model=self.config.backup_model,
                             llm_url=self.config.backup_url or self.config.llm_url,
                             llm_key=self.config.backup_key or self.config.llm_key,
                             backup_model='')
            backup = Model(config)
            result = backup.recognize(images, text)
            self.used_model = config.llm_model
            return result

    def _recognize(self, images, text=''):
        content = [{'type': 'text', 'text': '请识别以下票据文件。' + ('\nPDF 提取文字（仅作参考）：\n' + text if text else '')}]
        content += [{'type': 'image_url', 'image_url': {'url': image_data(data)}} for data in images]
        payload = {'model': self.config.llm_model, 'messages': [
            {'role': 'system', 'content': PROMPT}, {'role': 'user', 'content': content}],
            'stream': False, 'max_tokens': 4096}
        try:
            with self.client or httpx.Client(timeout=self.config.timeout) as client:
                for attempt in range(3):
                    response = client.post(self.config.llm_url + '/chat/completions',
                        headers={'Authorization': 'Bearer ' + self.config.llm_key}, json=payload)
                    if response.status_code != 429 and response.status_code < 500:
                        break
                    if attempt < 2:
                        time.sleep(2 ** attempt)
            if response.status_code in (401, 403):
                raise UserError(f'模型接口鉴权失败（HTTP {response.status_code}），请检查 LLM_API_KEY 是否有效并有权使用该模型。')
            if response.status_code != 200:
                raise UserError(f'识别模型接口返回 HTTP {response.status_code}，请检查模型配置、额度或图片支持情况。')
            data = response.json()
            choices = data.get('choices') or []
            if not choices:
                raise UserError('识别模型没有返回结果，请稍后重试。')
            if choices[0].get('finish_reason') == 'length':
                raise UserError('模型识别结果超过长度限制，请拆分票据后重新发送。')
            result = choices[0].get('message', {}).get('content')
            if not isinstance(result, str) or not result.strip():
                raise UserError('模型返回了空内容，请检查模型是否支持图片识别。')
            return result
        except httpx.TimeoutException:
            raise UserError('模型识别超时，请稍后重试。') from None
        except httpx.HTTPError:
            raise UserError('无法连接模型接口，请检查本机网络。') from None
        except (ValueError, KeyError, TypeError, AttributeError):
            raise UserError('模型接口响应格式异常，请检查 API 配置。') from None


class LarkAPI:
    def __init__(self, config):
        self.config = config
        self.client = httpx.Client(base_url=config.domain, timeout=60)
        self.token = ''
        self.expires = 0

    def authenticate(self):
        if time.time() < self.expires:
            return self.token
        response = self.client.post('/open-apis/auth/v3/tenant_access_token/internal', json={
            'app_id': self.config.app_id, 'app_secret': self.config.app_secret})
        data = self._check(response)
        if not data.get('tenant_access_token'):
            raise UserError('Lark 未返回应用访问凭证，请核对 App ID 和 App Secret。')
        self.token = data['tenant_access_token']
        self.expires = time.time() + max(1, int(data.get('expire', 7200)) - 120)
        return self.token

    @staticmethod
    def _check(response):
        if response.status_code >= 400:
            raise UserError(f'Lark 接口返回 HTTP {response.status_code}，请检查网络或应用权限。')
        try:
            data = response.json()
        except ValueError:
            raise UserError('Lark 接口返回了异常响应。') from None
        if data.get('code', 0) != 0:
            # Do not echo remote error messages, which can contain request credentials.
            code = data.get('code')
            raise UserError(f'Lark 接口错误码 {code}。请核对应用权限是否已开通并发布、用户是否在应用可用范围内。')
        return data

    def headers(self):
        return {'Authorization': 'Bearer ' + self.authenticate()}

    def download(self, message_id, file_key, kind, destination: Path):
        from urllib.parse import quote
        route = '/open-apis/im/v1/messages/' + quote(message_id, safe='') + '/resources/' + quote(file_key, safe='')
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_suffix('.part')
        try:
            with self.client.stream('GET', route, params={'type': kind}, headers=self.headers()) as response:
                if response.status_code != 200 or 'application/json' in response.headers.get('Content-Type', ''):
                    response.read()
                    self._check(response)
                    raise UserError('无法下载附件，请检查消息读取权限。')
                size = 0
                with temp.open('wb') as handle:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > self.config.max_bytes:
                            raise UserError(f'文件超过 {self.config.max_bytes // 1024 // 1024} MB，请拆分后重发。')
                        handle.write(chunk)
            if size == 0:
                raise UserError('下载到的文件为空，请重新发送。')
            temp.replace(destination)
        finally:
            temp.unlink(missing_ok=True)

    def upload(self, path: Path):
        with path.open('rb') as handle:
            response = self.client.post('/open-apis/im/v1/files', headers=self.headers(),
                data={'file_type': 'pdf' if path.suffix.lower() == '.pdf' else 'stream', 'file_name': path.name},
                files={'file': (path.name, handle, 'application/octet-stream')})
        return self._check(response)['data']['file_key']

    def reply(self, message_id, kind, content, phase):
        from urllib.parse import quote
        # Stable UUID protects short-lived retries from creating duplicate replies.
        identity = str(uuid.uuid5(uuid.NAMESPACE_URL, message_id + ':' + phase))
        route = '/open-apis/im/v1/messages/' + quote(message_id, safe='') + '/reply'
        payload = {'msg_type': kind, 'content': json.dumps(content, ensure_ascii=False), 'uuid': identity}
        for attempt in range(3):
            try:
                response = self.client.post(route, headers=self.headers(), json=payload)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                return self._check(response)['data'].get('message_id', '')
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == 2:
                    raise UserError('文件回传暂时失败，请稍后发送“重试”。') from None
                time.sleep(2 ** attempt)

    def text(self, message_id, value, phase='notice'):
        return self.reply(message_id, 'text', {'text': value}, phase)
