"""显式图片附件；保存引用，确认绑定模型/服务，仅请求发送时展开数据。"""
import asyncio
import base64
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import io
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import urlparse

from ai_agent_startup import config
from ai_agent_startup.tools.sandbox import ToolContext, tool_context, ask_permission, audit

_attachment_root = ContextVar('attachment_root', default=None)


def provider_identity(endpoint=None):
    url = (endpoint or {}).get('base_url') or config.BASE_URL or ''
    return hashlib.sha256(url.encode()).hexdigest()


def ensure_vision(model):
    if not config.ENABLE_IMAGE_INPUT:
        raise ValueError('图片输入未启用；可继续使用文本，或配置 ENABLE_IMAGE_INPUT')
    if model not in config.VISION_MODELS:
        raise ValueError('当前模型未配置视觉能力；请使用文本或显式选择已验证的 VISION_MODELS 模型')


def prepare_image(root: Path, relative_path: str) -> bytes:
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError:
        raise ValueError('图片输入需要 uv sync --locked --extra vision') from None
    candidate = Path(relative_path)
    target = (root / candidate).resolve()
    if candidate.is_absolute() or target == root.resolve() or not target.is_relative_to(root.resolve()):
        raise ValueError('图片路径必须位于当前会话工作区')
    descriptor = os.open(target, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('图片必须为普通文件')
        raw = stream.read(config.IMAGE_MAX_BYTES + 1)
    if len(raw) > config.IMAGE_MAX_BYTES:
        raise ValueError('图片文件超过 IMAGE_MAX_BYTES')
    try:
        with Image.open(io.BytesIO(raw), formats=['JPEG', 'PNG']) as image:
            if image.width * image.height > config.IMAGE_MAX_PIXELS:
                raise ValueError('图片像素超过 IMAGE_MAX_PIXELS')
            if getattr(image, 'n_frames', 1) != 1:
                raise ValueError('暂不支持动画图片')
            image.load()
            output = io.BytesIO()
            ImageOps.exif_transpose(image).convert('RGB').save(output, format='JPEG', quality=90)
            data = output.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError('图片解码失败；仅支持有效 PNG/JPEG') from exc
    if len(data) > config.IMAGE_MAX_BYTES:
        raise ValueError('标准化图片超过 IMAGE_MAX_BYTES')
    return data


def attachment_tokens(message):
    return len(message.get('_attachments', [])) * config.IMAGE_TOKEN_BUDGET


def read_attachment(root, reference):
    identifier = reference.get('sha256', '')
    if not re.fullmatch('[a-f0-9]{64}', identifier):
        raise ValueError('无效图片引用')
    root = Path(root)
    if root.is_symlink():
        raise ValueError('附件目录不能是符号链接')
    path = root / (identifier + '.jpg')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('附件必须为普通文件')
        data = stream.read(config.IMAGE_MAX_BYTES + 1)
    if len(data) > config.IMAGE_MAX_BYTES or hashlib.sha256(data).hexdigest() != identifier:
        raise ValueError('图片文件超限或摘要不匹配')
    return data


def save_attachment(root, data, model):
    root = Path(root)
    if root.is_symlink():
        raise ValueError('附件目录不能是符号链接')
    root.mkdir(exist_ok=True, mode=0o700)
    reference = {'sha256': hashlib.sha256(data).hexdigest(), 'model': model, 'provider': provider_identity()}
    target = root / (reference['sha256'] + '.jpg')
    if target.exists() or target.is_symlink():
        read_attachment(root, reference)
        return reference
    total = sum(path.stat().st_size for path in root.iterdir() if not path.is_symlink() and path.is_file())
    if total + len(data) > config.IMAGE_TOTAL_MB * 1024 * 1024:
        raise ValueError('附件总量超过 IMAGE_TOTAL_MB；不会自动删除旧附件')
    descriptor, temporary = tempfile.mkstemp(prefix='.image-', dir=root)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)  # 内容寻址目标不可覆盖。
    finally:
        os.unlink(temporary)
    return reference


@contextmanager
def image_scope(root):
    token = _attachment_root.set(root)
    try:
        yield
    finally:
        _attachment_root.reset(token)


def expand_images(history, endpoint):
    count = sum(len(message.get('_attachments', [])) for message in history)
    if not count:
        return history
    ensure_vision(endpoint['model'])
    if count > config.IMAGE_MAX_PER_REQUEST:
        raise ValueError('请求图片数超过 IMAGE_MAX_PER_REQUEST；请新建会话或缩小上下文')
    root = _attachment_root.get()
    if root is None:
        raise ValueError('图片请求缺少会话附件上下文')
    output = []
    for message in history:
        item = {key: value for key, value in message.items() if key != '_attachments'}
        references = message.get('_attachments', [])
        if references:
            if message['role'] != 'user':
                raise ValueError('图片只允许出现在用户消息中')
            parts = [{'type': 'text', 'text': message.get('content') or ''}]
            for reference in references:
                if reference.get('model') != endpoint['model'] or reference.get('provider') != provider_identity(endpoint):
                    raise ValueError('图片仅批准发送给原模型/服务；切换或备用服务需要重新确认')
                data = read_attachment(root, reference)
                parts.append({'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + base64.b64encode(data).decode('ascii')}})
            item['content'] = parts
        output.append(item)
    return output


async def _drained_thread(function, *args):
    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        await worker
        raise


async def submit_image(manager, session, path, prompt):
    model = config.MODEL_PROFILES.get(session.record.get('model_profile'), {}).get('model', config.MODEL)
    ensure_vision(model)
    if manager.closing or session.busy or not prompt.strip():
        raise ValueError('请在会话空闲时提供图片问题')
    task = asyncio.current_task()
    manager.fork_tasks.add(task)
    session.forking = True
    session.cancelled.clear()
    submitted = False
    try:
        await manager.flush()
        root = await _drained_thread(manager.store.workspace, session.id)
        data = await _drained_thread(prepare_image, root, path)
        loop = asyncio.get_running_loop()
        def approve():
            context = ToolContext(root, manager.store.directory(session.id) / 'audit.jsonl',
                lambda detail, timeout: manager._confirm(session, loop, detail, timeout), session.cancelled,
                permission_policy=manager.permission_override or session.record.get('permission_policy'))
            with tool_context(context):
                detail = f'{path}；标准化 JPEG {len(data)} 字节；sha256={hashlib.sha256(data).hexdigest()}；目标={urlparse(config.BASE_URL or "").hostname}/{model}。后续上下文可能向同一模型再次发送。'
                allowed = ask_permission('image_upload', detail, force_confirmation=True)
                audit('image_upload_decision', allowed=allowed, model=model)
                return allowed
        if not await _drained_thread(approve) or session.cancelled.is_set() or manager.closing:
            return False
        reference = await _drained_thread(save_attachment, manager.store.directory(session.id) / 'attachments', data, model)
        if session.cancelled.is_set() or manager.closing:
            return False
        session.forking = False
        manager.submit(session, prompt, attachments=[reference])
        submitted = True
        return True
    finally:
        session.forking = False
        manager.fork_tasks.discard(task)
        if not submitted and session.record['status'] in {'running', 'confirming', 'stopping'}:
            session.record['status'] = 'idle'
        manager.notify()
