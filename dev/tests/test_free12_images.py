import asyncio
import json

import pytest
from PIL import Image

import config
from core.sessions import SessionManager
from core.storage import SessionStore


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_IMAGE_INPUT', True, raising=False)
    monkeypatch.setattr(config, 'VISION_MODELS', ['vision-test'], raising=False)
    monkeypatch.setattr(config, 'MODEL', 'vision-test')
    store = SessionStore(tmp_path / 'state', tmp_path / 'work')
    yield store
    store.close()


def test_image_normalization_denies_boundary_bad_format_and_large_pixels(setup, monkeypatch):
    from core.images import prepare_image
    root = setup.workspace('a' * 32)
    Image.new('RGB', (32, 16)).save(root / 'ok.png')
    data = prepare_image(root, 'ok.png')
    assert data.startswith(b'\xff\xd8')
    with pytest.raises(ValueError):
        prepare_image(root, '../outside.png')
    (root / 'bad.png').write_text('not an image')
    with pytest.raises(ValueError):
        prepare_image(root, 'bad.png')
    monkeypatch.setattr(config, 'IMAGE_MAX_PIXELS', 16)
    with pytest.raises(ValueError):
        prepare_image(root, 'ok.png')


def test_confirmed_image_persists_reference_and_expands_only_for_approved_endpoint(setup):
    from core.images import submit_image, expand_images
    requests = []
    async def model(history, **kwargs):
        requests.append(expand_images(history, {'model': config.MODEL}))
        with pytest.raises(ValueError):
            expand_images(history, {'model': 'another-model'})
        return {'role': 'assistant', 'content': '协议测试桩；非真实视觉识别'}
    async def run():
        manager = SessionManager(setup, model=model, confirmation_handler=lambda *_: True)
        session = manager.create()
        await manager.flush()
        Image.new('RGB', (32, 16)).save(setup.workspace(session.id) / 'image.png')
        await submit_image(manager, session, 'image.png', 'describe')
        await session.task
        assert session.record['status'] == 'idle'
        assert requests[0][-1]['content'][1]['image_url']['url'].startswith('data:image/jpeg;base64,')
        metadata = session.record['messages'][-2]
        assert metadata['content'] == 'describe' and '_attachments' in metadata
        assert 'base64' not in (setup.directory(session.id) / 'messages.jsonl').read_text()
        await manager.shutdown()
    asyncio.run(run())


def test_image_denied_or_unsupported_never_submits_model(setup, monkeypatch):
    from core.images import submit_image
    async def model(*args, **kwargs):
        pytest.fail('拒绝上传不得调用模型')
    async def run():
        manager = SessionManager(setup, model=model, confirmation_handler=lambda *_: False)
        session = manager.create()
        await manager.flush()
        Image.new('RGB', (16, 16)).save(setup.workspace(session.id) / 'image.png')
        assert await submit_image(manager, session, 'image.png', 'describe') is False
        assert len(session.record['messages']) == 1
        assert not (setup.directory(session.id) / 'attachments').exists()
        monkeypatch.setattr(config, 'VISION_MODELS', [])
        with pytest.raises(ValueError, match='视觉'):
            await submit_image(manager, session, 'image.png', 'describe')
        await manager.shutdown()
    asyncio.run(run())


def test_image_tokens_are_counted_without_base64(setup, monkeypatch):
    from core.context import build_model_history, ContextBudgetError
    monkeypatch.setattr(config, 'IMAGE_TOKEN_BUDGET', 20000, raising=False)
    messages = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'see', '_attachments': [{}]}]
    with pytest.raises(ContextBudgetError):
        build_model_history(messages, schemas=[])


def test_image_payload_reaches_sdk_and_branch_preserves_attachment(setup, monkeypatch):
    from types import SimpleNamespace
    from core import llm
    from core.images import save_attachment, image_scope, prepare_image
    from core.branches import fork_record
    monkeypatch.setattr(config, 'ENABLE_SESSION_BRANCHES', True)
    captured = []
    async def create(**kwargs):
        captured.append(kwargs)
        return object()
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client.with_options = lambda **kwargs: client
    monkeypatch.setattr(llm, 'endpoint_client', lambda endpoint: client)
    record = setup.create('vision', 'system')
    root = setup.workspace(record['id'])
    Image.new('RGB', (16, 16)).save(root / 'image.png')
    data = prepare_image(root, 'image.png')
    ref = save_attachment(setup.directory(record['id']) / 'attachments', data, config.MODEL)
    record['messages'].append({'role': 'user', 'content': 'see', '_attachments': [ref]})
    setup.save(record)
    child = fork_record(setup, record)
    assert (setup.directory(child['id']) / 'attachments' / (ref['sha256'] + '.jpg')).read_bytes() == data
    with image_scope(setup.directory(child['id']) / 'attachments'):
        asyncio.run(llm._open_stream(child['messages']))
    assert '_attachments' not in captured[0]['messages'][-1]
    assert captured[0]['messages'][-1]['content'][1]['type'] == 'image_url'
    assert '图片附件引用' in setup.export(record, 'md').read_text()
