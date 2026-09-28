"""在新文件中创建会话快照；原消息日志、工作区和记忆不改写。"""
import copy
import json
import shutil

import config


def validate_prefix(messages: list[dict]):
    if not messages or messages[0].get('role') != 'system':
        raise ValueError('分支必须包含系统消息')
    pending = set()
    for message in messages:
        role = message.get('role')
        if role == 'tool':
            identifier = message.get('tool_call_id')
            if identifier not in pending:
                raise ValueError('工具调用与结果配对不完整，不能从此处分支')
            pending.remove(identifier)
        else:
            if pending:
                raise ValueError('工具调用与结果配对不完整，不能从此处分支')
            calls = message.get('tool_calls', [])
            if calls:
                identifiers = [call.get('id') for call in calls]
                if (role != 'assistant' or any(not isinstance(value, str) or not value for value in identifiers)
                        or len(set(identifiers)) != len(identifiers)):
                    raise ValueError('工具调用与结果配对无效')
                pending.update(identifiers)
    if pending:
        raise ValueError('截断点位于工具配对中间；请选择结果返回后的消息')


def fork_record(store, source: dict, through: int | None = None, *, attachment_refs=()) -> dict:
    from core.storage import LazyRecord
    if not config.ENABLE_SESSION_BRANCHES:
        raise ValueError('请显式开启 ENABLE_SESSION_BRANCHES')
    if isinstance(source, LazyRecord) and source.loader is not None:
        # 创建快照不触发旧会话中断恢复写入。
        source = store.read_record(store.directory(source['id']) / 'session.json')
    messages = source['messages']
    through = len(messages) - 1 if through is None else through
    if type(through) is not int or not 0 <= through < len(messages):
        raise ValueError('分支消息编号超出范围（system=0）')
    prefix = copy.deepcopy(messages[:through + 1])
    validate_prefix(prefix)
    memory = store.memory(source['id'], source.get('memory_namespace'))
    if len(memory) > config.MEMORY_MAX_CHARS * 8:
        raise ValueError('记忆文件超过有界快照大小，请先显式整理')
    record = store.new_record(source['title'] + ' · 分支', prefix[0]['content'])
    prefix[0]['content'] += (f'\n[分支说明] 来自会话 {source["id"]} 的消息 0..{through}。'
        '新分支工作区为空；历史工具结果来自原工作区，不代表这里存在相同文件。'
        '请先核查当前文件，不重放原会话已完成的操作。原会话保持不变。')
    record['messages'] = prefix
    record['branch'] = {'parent_id': source['id'], 'through': through}
    record['memory_namespace'] = 'session'
    for field in ('model_profile', 'tool_names', 'permission_policy', 'budget_overrides'):
        if field in source:
            record[field] = copy.deepcopy(source[field])
    if len(json.dumps(record, ensure_ascii=False).encode()) > config.SESSION_MAX_MB * 1024 ** 2:
        raise ValueError('分支超过 SESSION_MAX_MB，请选择更早的截断点')
    directory = store.directory(record['id'])
    directory.mkdir(mode=0o700)  # 新随机 ID，绝不覆盖已有目录。
    try:
        from core.images import read_attachment, save_attachment
        for message in [*prefix, {'_attachments': attachment_refs}]:
            for reference in message.get('_attachments', []):
                data = read_attachment(store.directory(source['id']) / 'attachments', reference)
                save_attachment(directory / 'attachments', data, reference['model'])
        store.save(record)
        store.atomic_write(store.memory_path(record['id'], 'session'), memory)
        store._audit_memory(record['id'], 'session_branch', 'session', '')
        return record
    except BaseException:
        # 仅清理本函数新建且尚未发布给管理器的目录。
        shutil.rmtree(directory)
        raise
