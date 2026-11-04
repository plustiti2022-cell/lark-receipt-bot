from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    app_id: str
    app_secret: str
    domain: str
    llm_url: str
    llm_key: str
    llm_model: str
    timeout: float = 180
    max_bytes: int = 25 * 1024 * 1024
    max_pages: int = 20
    retention_hours: int = 24
    health_port: int = 8765
    backup_model: str = ''
    backup_url: str = ''
    backup_key: str = ''
    data: Path = ROOT / 'data'

    @classmethod
    def load(cls):
        values = {**dotenv_values(ROOT / '.env'), **os.environ}
        def get(key, default=''):
            return str(values.get(key) or default).strip()
        domain = get('LARK_DOMAIN', 'lark')
        domain = {'lark': 'https://open.larksuite.com', 'feishu': 'https://open.feishu.cn'}.get(domain, domain)
        if domain not in ('https://open.larksuite.com', 'https://open.feishu.cn'):
            raise ValueError('LARK_DOMAIN must be lark or feishu')
        config = cls(
            app_id=get('LARK_APP_ID'), app_secret=get('LARK_APP_SECRET'), domain=domain,
            llm_url=get('LLM_API_HOST').rstrip('/'), llm_key=get('LLM_API_KEY'),
            llm_model=get('LLM_MODEL'), timeout=float(get('LLM_TIMEOUT_SECONDS', '180')),
            max_bytes=int(get('MAX_FILE_MB', '25')) * 1024 * 1024,
            max_pages=int(get('MAX_PDF_PAGES', '20')),
            retention_hours=int(get('FILE_RETENTION_HOURS', '24')),
            health_port=int(get('HEALTH_PORT', '8765')),
            backup_model=get('LLM_MODEL_BACKUP'),
            backup_url=get('LLM_API_HOST_BACKUP', get('LLM_API_HOST')).rstrip('/'),
            backup_key=get('LLM_API_KEY_BACKUP', get('LLM_API_KEY')),
        )
        missing = [key for key, value in {
            'LARK_APP_ID': config.app_id, 'LARK_APP_SECRET': config.app_secret,
            'LLM_API_HOST': config.llm_url, 'LLM_API_KEY': config.llm_key,
            'LLM_MODEL': config.llm_model,
        }.items() if not value]
        if missing:
            raise ValueError('Missing configuration: ' + ', '.join(missing))
        if not config.llm_url.startswith(('https://', 'http://localhost:', 'http://127.0.0.1:')):
            raise ValueError('LLM_API_HOST must use HTTPS (or a local API)')
        return config
