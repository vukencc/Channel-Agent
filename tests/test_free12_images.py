import asyncio
import json
import threading
from contextlib import contextmanager

import pytest
from PIL import Image

from ai_agent_startup import config
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_IMAGE_INPUT', True, raising=False)
    monkeypatch.setattr(config, 'VISION_MODELS', ['vision-test'], raising=False)
    monkeypatch.setattr(config, 'MODEL', 'vision-test')
    store = SessionStore(tmp_path / 'state', tmp_path / 'work')
    yield store
    store.close()


def test_image_normalization_denies_boundary_bad_format_and_large_pixels(setup, monkeypatch):
    from ai_agent_startup.core.images import prepare_image
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
    from ai_agent_startup.core.images import submit_image, expand_images
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
    from ai_agent_startup.core.images import submit_image
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
    from ai_agent_startup.core.context import build_model_history, ContextBudgetError
    monkeypatch.setattr(config, 'IMAGE_TOKEN_BUDGET', 20000, raising=False)
    messages = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'see', '_attachments': [{}]}]
    with pytest.raises(ContextBudgetError):
        build_model_history(messages, schemas=[])


def test_image_payload_reaches_sdk_and_branch_preserves_attachment(setup, monkeypatch):
    from types import SimpleNamespace
    from ai_agent_startup.core import llm
    from ai_agent_startup.core.images import save_attachment, image_scope, prepare_image
    from ai_agent_startup.core.branches import fork_record
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


def test_retry_image_turn_copies_attachment_into_new_branch(setup, monkeypatch):
    from ai_agent_startup.core.images import submit_image, expand_images
    monkeypatch.setattr(config, 'ENABLE_SESSION_BRANCHES', True)
    seen = []
    async def model(history, **kwargs):
        payload = expand_images(history, {'model': config.MODEL})
        seen.append(payload[-1]['content'])
        return {'role': 'assistant', 'content': '测试桩'}
    async def run():
        manager = SessionManager(setup, model=model, confirmation_handler=lambda *_: True)
        parent = manager.create()
        await manager.flush()
        Image.new('RGB', (16, 16)).save(setup.workspace(parent.id) / 'image.png')
        await submit_image(manager, parent, 'image.png', 'see image')
        await parent.task
        child = await manager.resend(parent, 1)
        await child.task
        assert isinstance(seen[-1], list)
        assert seen[-1][1]['type'] == 'image_url'
        assert '_attachments' in child.record['messages'][-2]
        await manager.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('command_jobs_enabled', [False, True])
def test_image_preparation_waits_for_workspace_writer(setup, monkeypatch, command_jobs_enabled):
    from ai_agent_startup.core import images

    monkeypatch.setattr(config, 'ENABLE_COMMAND_JOBS', command_jobs_enabled)
    attempted, prepared = threading.Event(), threading.Event()

    def fake_prepare(root, path):
        prepared.set()
        return b'jpeg data'

    monkeypatch.setattr(images, 'prepare_image', fake_prepare)

    async def model(*args, **kwargs):
        pytest.fail('denied image should not invoke model')

    async def run():
        manager = SessionManager(setup, model=model, confirmation_handler=lambda *_: False)
        release, entered = threading.Event(), threading.Event()
        holder = None
        try:
            session = manager.create()
            await manager.flush()
            root = manager.workspace(session)
            owner = manager.command_jobs if command_jobs_enabled else manager.workspaces
            original_guard = owner.guard

            @contextmanager
            def observed_guard(path, cancelled):
                attempted.set()
                with original_guard(path, cancelled):
                    yield

            monkeypatch.setattr(owner, 'guard', observed_guard)

            def hold_writer():
                with original_guard(root, threading.Event()):
                    entered.set()
                    release.wait(timeout=5)

            holder = threading.Thread(target=hold_writer, daemon=True)
            holder.start()
            assert await asyncio.to_thread(entered.wait, 2)
            upload = asyncio.create_task(images.submit_image(manager, session, 'photo.png', 'describe'))
            assert await asyncio.to_thread(attempted.wait, 2)
            assert not prepared.is_set()
            assert not upload.done()
            release.set()
            assert await asyncio.wait_for(upload, 2) is False
            assert prepared.is_set()
        finally:
            release.set()
            if holder is not None:
                await asyncio.to_thread(holder.join, 2)
            await manager.shutdown()

    asyncio.run(run())
