"""直接父子会话的持久信箱；不唤醒模型、不跨越工具配对边界。"""
import json
import threading
import time

from ai_agent_startup import config
from ai_agent_startup.tools.sandbox import ToolContext, audit, tool_context


class SessionCommunication:
    def __init__(self, manager):
        self.manager = manager
        self.lock = threading.RLock()
        self.latest = {}

    def _session(self, identifier):
        self.manager.store.directory(identifier)
        session = self.manager.sessions.get(identifier)
        if session is None:
            raise PermissionError('会话不存在')
        return session

    def _path(self, identifier):
        self._session(identifier)
        path = self.manager.store.directory(identifier) / 'inbox.json'
        if path.is_symlink():
            raise PermissionError('信箱不能是符号链接')
        return path

    def _load(self, identifier):
        path = self._path(identifier)
        if not path.exists():
            self.latest[identifier] = 0
            return []
        if path.stat().st_size > config.SESSION_INBOX_MAX_BYTES:
            raise ValueError('信箱超过 SESSION_INBOX_MAX_BYTES，拒绝加载')
        messages = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(messages, list) or any(
                not isinstance(item, dict) or item.get('seq') != index + 1
                or item.get('recipient_id') != identifier or not isinstance(item.get('content'), str)
                for index, item in enumerate(messages))):
            raise ValueError('信箱格式无效')
        self.latest[identifier] = len(messages)
        return messages

    def pending_count(self, session):
        """界面仅读内存；重启后的计数由后台 read 初始化。"""
        return max(0, self.latest.get(session.id, 0) - session.record.get('inbox_cursor', 0))

    def send(self, sender_id, recipient_id, content):
        sender, recipient = self._session(sender_id), self._session(recipient_id)
        if sender_id == recipient_id or not (
                sender.record.get('delegated_from') == recipient_id
                or recipient.record.get('delegated_from') == sender_id):
            raise PermissionError('仅允许直接父子 Agent 通信')
        if self.manager.closing or sender.cancelled.is_set():
            raise ValueError('发送会话已取消或管理器正在关闭')
        if not isinstance(content, str) or not content.strip() or len(content) > config.SESSION_MESSAGE_MAX_CHARS:
            raise ValueError('消息为空或超过 SESSION_MESSAGE_MAX_CHARS')
        with self.lock:
            messages = self._load(recipient_id)
            item = {'seq': len(messages) + 1, 'sender_id': sender_id,
                    'recipient_id': recipient_id, 'content': content, 'created_at': time.time()}
            body = json.dumps([*messages, item], ensure_ascii=False)
            if len(body.encode('utf-8')) > config.SESSION_INBOX_MAX_BYTES:
                raise ValueError('信箱已满；不会丢弃历史或自动重试')
            self.manager.store.atomic_write(self._path(recipient_id), body)
            self.latest[recipient_id] = item['seq']
        # 直接内部调用同样保留发送方审计，不记录消息正文。
        from ai_agent_startup.tools.sandbox import _context
        context = _context.get()
        if context is None:
            context = ToolContext(self.manager.store.workspace_path(sender_id),
                self.manager.store.directory(sender_id) / 'audit.jsonl', lambda *_: False, sender.cancelled)
        with tool_context(context):
            audit('session_message_sent', recipient_id=recipient_id, seq=item['seq'], chars=len(content))
        return item

    def read(self, recipient_id, after_seq=0, limit=20, *, max_chars=None):
        if type(after_seq) is not int or after_seq < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('after_seq 必须非负，limit 必须在 1..100 之间')
        with self.lock:
            messages = self._load(recipient_id)
            page = []
            for item in messages[after_seq:after_seq + limit]:
                candidate = {'messages': [*page, item], 'next_seq': item['seq']}
                if max_chars is not None and len(json.dumps(candidate, ensure_ascii=False)) > max_chars:
                    if not page:
                        raise ValueError('单条信件超过 TOOL_MAX_OUTPUT；请提高输出上限后重试读取，信件已保留')
                    break
                page.append(item)
            return {'messages': page, 'next_seq': page[-1]['seq'] if page else after_seq}
