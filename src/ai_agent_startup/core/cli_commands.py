"""Shared command metadata, slash completion, and searchable CLI selectors."""
from dataclasses import dataclass

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.layout import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import Frame, TextArea

from ai_agent_startup import config


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    arguments: str = ''
    aliases: tuple[str, ...] = ()


COMMANDS = (
    Command('/new', '新建会话并启动独立 Agent', '[名称]'),
    Command('/sessions', '搜索并恢复已有会话（包含隐藏会话）', '', ('/resume',)),
    Command('/switch', '通过唯一 ID 前缀切换会话', 'ID前缀'),
    Command('/models', '搜索并选择模型预设'),
    Command('/model', '设置模型预设；default 恢复默认', '[名称|default]'),
    Command('/permissions', '搜索并选择权限模式'),
    Command('/policy', '设置权限模式；default 恢复环境默认', '[smart|full_access|standard|readonly|trusted|default]'),
    Command('/tools', '选择工具子集；all 全部，none 无工具', '[名称,...|all|none]'),
    Command('/platform', '查看平台能力与隔离验证方式'),
    Command('/usage', '查看保留窗口统计（启用可观测命令后）'),
    Command('/trace', '查看有界脱敏指标（启用可观测命令后）', '[轮次]'),
    Command('/image', '发送工作区图片；仍需确认上传', '路径 | 问题'),
    Command('/tasks', '查看或取消当前会话创建的子任务', '[cancel ID]'),
    Command('/plan', '只读查看当前任务计划、依赖、阻塞与执行证据（启用计划后）', '[分页游标]'),
    Command('/config', '查看或设置会话预算；default 恢复全局默认', '[JSON|预设|default]'),
    Command('/branch', '复制截至指定消息的历史到新会话（启用分支后）', '[编号]'),
    Command('/resend', '在新分支编辑并重发 user 消息', '编号 新文本'),
    Command('/retry', '在新分支重发最后一个用户问题'),
    Command('/rename', '重命名当前会话', '名称'),
    Command('/prompt', '空闲时设置当前 Agent 的系统指令', '指令'),
    Command('/remember', '保存当前会话的长期记忆', '内容'),
    Command('/memory', '查看记忆；管理 scope/add/search/edit/rm 使用参数', '[scope 范围|add JSON|search JSON|edit ID JSON|rm ID|candidates]'),
    Command('/forget', '清空记忆（需要确认）'),
    Command('/export', '导出对话与记忆文件', '[md|json]'),
    Command('/stop', '停止当前任务'),
    Command('/close', '隐藏当前会话；/sessions 可恢复，文件保留'),
    Command('/delete', '将选定会话数据移入回收区（需要明确确认）', '[ID前缀]', ('/del',)),
    Command('/cleanup', '预览并确认清理缓存、日志或会话记录', '[cache|logs|sessions|all]'),
    Command('/details', '显示或隐藏工具参数、结果与思考详情', '[on|off]'),
    Command('/yes', '批准当前会话的待确认操作'),
    Command('/no', '拒绝当前会话的待确认操作'),
    Command('/older', '上一页完整历史'),
    Command('/newer', '下一页完整历史'),
    Command('/top', '最早历史'),
    Command('/bottom', '跟随最新回复'),
    Command('/where', '查看当前文件工作区与会话记录位置'),
    Command('/sidebar', '隐藏或显示会话侧栏'),
    Command('/help', '显示命令帮助'),
    Command('/quit', '停止后台任务、保存并退出'),
)

POLICY_LABELS = {
    'smart': 'Smart', 'full_access': 'Full Access', 'standard': 'Standard',
    'readonly': 'Read Only', 'trusted': 'Trusted',
}
POLICY_DESCRIPTIONS = {
    'smart': '按本地确定性风险判断工作区操作；高风险或未知风险仍需确认',
    'full_access': '自动批准工具确认；会话删除仍须明确确认',
    'standard': '沿用工具的逐次确认要求',
    'readonly': '只允许读取；拒绝写入与命令执行',
    'trusted': '仅预先确认规则匹配可自动批准',
}


def help_text() -> str:
    shortcuts = 'Enter 发送 · Tab 补全 · Ctrl+P 命令面板 · ↑/↓ 选择 · Esc 返回\nAlt+Enter 换行 · Ctrl+N 新建 · Ctrl+←/→ 切换 · Ctrl+C 停止 · Ctrl+Q 退出'
    rows = [f'{command.name} {command.arguments}\n  {command.description}' +
            (f'（别名 {", ".join(command.aliases)}）' if command.aliases else '')
            for command in COMMANDS]
    return shortcuts + '\n' + '\n'.join(rows) + '\n以 // 开头可发送以 / 开头的普通消息。'


class CommandCompleter(Completer):
    def __init__(self, cli):
        self.cli = cli

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor
        if not text.startswith('/') or text.startswith('//') or '\n' in text:
            return
        command, separator, argument = text.partition(' ')
        if not separator:
            for item in COMMANDS:
                for name in (item.name, *item.aliases):
                    if name.startswith(command):
                        yield Completion(name, start_position=-len(command),
                                         display_meta=(item.arguments + ' · ' if item.arguments else '') + item.description)
            return
        rows = []
        if command in {'/policy', '/permissions'}:
            rows = [(name, POLICY_LABELS.get(name, '恢复环境默认'))
                    for name in (*POLICY_LABELS, 'default', 'full', 'full access')]
        elif command in {'/model', '/models'}:
            rows = [(name, '模型预设') for name in ('default', *config.MODEL_PROFILES)]
        elif command in {'/switch', '/sessions', '/resume', '/delete', '/del'}:
            rows = [(identifier, session.record['title'])
                    for identifier, session in self.cli.manager.sessions.items()]
        elif command == '/export':
            rows = [('md', 'Markdown'), ('json', 'JSON')]
        elif command == '/tools':
            from ai_agent_startup.core.llm import TOOL_SCHEMAS
            rows = [(name, '工具') for name in ('all', 'none', *(row['function']['name'] for row in TOOL_SCHEMAS))]
        elif command == '/config':
            rows = [('default', '恢复全局默认')]
        elif command == '/cleanup':
            rows = [('cache', '清理 RAG 缓存'), ('logs', '清空日志并保留维护审计'),
                    ('sessions', '归档所有活动会话记录'), ('all', '清理上述全部内容')]
        elif command == '/details':
            rows = [('on', '显示执行详情'), ('off', '隐藏执行详情')]
        elif command == '/memory':
            rows = [(name, '记忆管理') for name in ('scope', 'add', 'search', 'edit', 'rm', 'candidates')]
            if argument.startswith('scope '):
                rows = [('scope ' + name, '记忆范围') for name in ('session', 'project', 'global', 'default')]
        for name, description in rows:
            if name.startswith(argument):
                yield Completion(name, start_position=-len(argument), display_meta=description)


@dataclass(frozen=True)
class PaletteItem:
    value: str
    title: str
    description: str
    arguments: str = ''


class CommandPalette:
    """The query owns focus only while open; opening never edits the prompt."""
    def __init__(self, cli):
        self.cli = cli
        self.visible = False
        self.mode = 'commands'
        self.owner = None
        self.index = 0
        self.query = TextArea(height=1, multiline=False, prompt='搜索 › ')
        self.query.buffer.on_text_changed += self._changed
        self.container = Frame(HSplit([
            self.query,
            Window(FormattedTextControl(self.rows), height=9, wrap_lines=False),
            Window(FormattedTextControl('↑/↓ 选择 · Enter 确定 · Esc 返回\n参数命令先填入输入框；/help 查看完整用法'), height=2),
        ]), title=lambda: {'commands': '命令面板', 'sessions': '会话 / 恢复',
                          'models': '模型预设', 'permissions': '权限模式'}[self.mode])

    def _changed(self, buffer):
        self.index = 0
        if self.cli.app:
            self.cli.app.invalidate()

    def open(self, mode='commands', query=''):
        self.mode = mode
        self.owner = self.cli.current
        self.query.buffer.set_document(Document(query, len(query)))
        self.index = 0
        self.visible = True
        self.cli.input.buffer.cancel_completion()
        self.cli.app.layout.focus(self.query)
        self.cli.app.invalidate()

    def matches(self):
        if self.mode == 'commands':
            rows = [PaletteItem(row.name, row.name, row.description, row.arguments) for row in COMMANDS]
        elif self.mode == 'sessions':
            rows = [PaletteItem(identifier, session.record['title'], identifier +
                                (' · 隐藏，可恢复' if identifier in self.cli.hidden else ''))
                    for identifier, session in self.cli.manager.sessions.items()]
        elif self.mode == 'models':
            rows = [PaletteItem(name, name, '恢复默认模型' if name == 'default' else '模型预设')
                    for name in ('default', *config.MODEL_PROFILES)]
        else:
            rows = [PaletteItem(name, POLICY_LABELS[name], POLICY_DESCRIPTIONS[name]) for name in POLICY_LABELS]
            rows.append(PaletteItem('default', 'Default', '恢复环境默认权限模式'))
        words = self.query.text.casefold().split()
        return [row for row in rows if all(word in f'{row.value} {row.title} {row.description} {row.arguments}'.casefold() for word in words)]

    def rows(self):
        matches = self.matches()
        if not matches:
            return [('', '没有匹配项')]
        self.index = min(self.index, len(matches) - 1)
        start = max(0, self.index - 3)
        result = []
        for index, row in enumerate(matches[start:start + 7], start):
            style = 'class:selected' if index == self.index else ''
            result.append((style, f'{">" if index == self.index else " "} {row.title} {row.arguments}\n  {row.description}\n'))
        return result

    def move(self, step):
        matches = self.matches()
        if matches:
            self.index = (self.index + step) % len(matches)
        self.cli.app.invalidate()

    def close(self):
        self.visible = False
        self.cli.app.layout.focus(self.cli.input)
        self.cli.app.invalidate()

    def choose(self):
        matches = self.matches()
        if not matches:
            return
        row = matches[min(self.index, len(matches) - 1)]
        mode, owner = self.mode, self.owner
        self.close()
        if mode == 'sessions':
            self.cli.select(row.value)
        elif mode in {'models', 'permissions'}:
            if self.cli.current != owner:
                self.cli.notice = '当前会话已改变，请重新打开选择器'
                self.cli.render(force=True)
                return
            self.cli.handle(('/model ' if mode == 'models' else '/policy ') + row.value)
        elif row.arguments:
            self.cli.prepare_command(row.value + ' ')
        else:
            self.cli.handle(row.value)
