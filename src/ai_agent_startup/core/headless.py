"""单次无 TTY 执行入口；复用完整会话循环，默认拒绝所有待确认操作。"""
import asyncio
import os
import logging

from ai_agent_startup import config
from ai_agent_startup.core.llm import call_model
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.session_service import open_session


def redact_result(value):
    secrets = [config.API_KEY, config.EMBEDDING_MODEL_API_KEY, config.WEB_SEARCH_API_KEY]
    secrets += [os.getenv(endpoint['api_key_env'], '') for endpoint in config.MODEL_FALLBACKS if endpoint.get('api_key_env')]
    secrets += [os.getenv('TAVILY_API_KEY', '')]
    def clean(item):
        if isinstance(item, str):
            for secret in secrets:
                if secret and len(secret) >= 8:
                    item = item.replace(secret, '[已隐藏配置密钥]')
            return item
        if isinstance(item, dict):
            return {key: clean(child) for key, child in item.items()}
        if isinstance(item, list):
            return [clean(child) for child in item]
        return item
    return clean(value)


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact_result(super().format(record))


async def run_headless(store, prompt: str, *, session_id: str | None = None,
                       policy: str = 'standard', model=call_model) -> dict:
    if not prompt.strip():
        raise ValueError('prompt 不能为空')
    manager = SessionManager(store, model=model, confirmation_handler=lambda *_: False,
                             permission_override=policy)
    try:
        if session_id:
            matches = [session for key, session in manager.sessions.items() if key.startswith(session_id)]
            if len(matches) != 1:
                raise ValueError('session 必须是唯一的已保存会话 ID 或前缀')
            session = matches[0]
        else:
            session = open_session(manager, 'Headless 会话')
        start = len(session.record['messages'])
        manager.submit(session, prompt)
        try:
            await asyncio.shield(session.task)
        except asyncio.CancelledError:
            manager.cancel(session)
            await asyncio.gather(session.task, return_exceptions=True)
        await manager.flush()
        record = session.record
        status = record['status']
        return redact_result({'version': 1, 'session_id': session.id,
            'turn_id': record.get('last_run', {}).get('turn_id'), 'status': status,
            'exit_code': {'idle': 0, 'checkpoint': 3, 'cancelled': 130}.get(status, 4),
            'messages': record['messages'][start:], 'metrics': record.get('last_run', {}),
            'error': record.get('error', ''), 'permission_policy': policy})
    finally:
        await manager.shutdown()
