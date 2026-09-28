"""Full-screen terminal client; sessions continue running while focus changes."""
import argparse
import asyncio
import logging
import json
import sys
from contextlib import redirect_stdout
import time
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, VSplit, Layout, Window, ConditionalContainer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.filters import Condition
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea
from prompt_toolkit.patch_stdout import patch_stdout

import config
from core import llm
from core.sessions import SessionManager
from core.storage import SessionStore
from core.transcript import Transcript

HELP = '''Enter 发送 · Alt+Enter 换行 · Ctrl+N 新建 · Ctrl+←/→ 切换 · Ctrl+C 停止 · Ctrl+Q 退出
/new [名称]          新建会话并启动独立 Agent
/switch ID前缀       切换会话（也可点击左侧）
/model 名称          切换预设模型参数；default 恢复默认；无参数列出预设
/tools 名称,...      选择会话工具；all 全部，none 无工具；无参数查看当前子集
/config [JSON|预设]  查看或设置会话预算；default 恢复全局默认
/policy 档位         readonly/standard/trusted；default 恢复环境默认；无参数查看
/memory scope 范围   启用管理后选择 session/project/global/default；add/search/edit 使用 JSON
/branch [编号]       启用分支后复制截至该消息的历史到新会话；system=0，默认末尾
/resend 编号 新文本  在新分支编辑并重发 user 消息；/retry 重发最后一个用户问题
/rename 名称         重命名当前会话
/prompt 指令         设置当前 Agent 的系统指令（空闲时）
/remember 内容       保存当前会话的长期记忆
/memory              查看记忆文件
/forget              清空记忆（需要确认）
/export [md|json]    导出对话与记忆文件
/stop                停止当前任务
/close               隐藏当前会话（文件保留，重启可恢复）
/yes 或 /no          只批准或拒绝当前会话的待确认操作
/older /newer        上一页 / 下一页完整历史
/top /bottom         最早历史 / 跟随最新回复
/where               查看当前文件工作区
/sidebar             隐藏或显示会话侧栏
/help                显示帮助
/quit                停止后台任务、保存并退出
以 // 开头可发送以 / 开头的普通消息。记忆和上下文仅在当前会话内使用。'''

LABELS = {'idle': '就绪', 'queued': '排队', 'running': '运行', 'confirming': '待确认',
          'stopping': '停止中', 'cancelled': '已停止', 'error': '错误', 'interrupted': '中断恢复',
          'checkpoint': '阶段保存·可继续'}


class AgentCLI:
    def __init__(self, store: SessionStore, *, input=None, output=None):
        self.store = store
        self.manager = SessionManager(store, self.refresh)
        self.hidden = set()
        self.notice = 'F1 查看帮助 · /where 查看工作区 · PageUp 浏览历史 · Ctrl+End 跟随最新'
        self.pending_forget = None
        self.app = None
        self.transcript = Transcript()
        self.page_info = (0, 0, 0)
        self._refresh_handle = None
        self._view_key = None
        self.sidebar_visible = True
        self._ui_jobs = set()
        self._disk_lock = asyncio.Lock()
        self.input = TextArea(height=2, prompt='› ', multiline=True, wrap_lines=True)
        self.chat = TextArea(read_only=True, scrollbar=True, wrap_lines=True, focusable=True)
        self.sidebar = Window(FormattedTextControl(self.session_list), width=22)
        self.current = next(reversed(self.manager.sessions), None)
        if self.current is None:
            self.current = self.manager.create().id
        if store.errors:
            self.notice = '以下会话文件无法读取，已保留原文件：\n' + '\n'.join(store.errors)
        bindings = KeyBindings()

        @bindings.add('enter', filter=Condition(lambda: self.app.layout.has_focus(self.input)))
        def send(event):
            value = self.input.text.strip()
            self.input.text = ''
            self.handle(value)

        @bindings.add('escape', 'enter')
        def newline(event):
            self.input.buffer.insert_text('\n')

        @bindings.add('c-n')
        def new(event):
            self.handle('/new')

        @bindings.add('c-left')
        def previous(event):
            self.cycle(-1)

        @bindings.add('c-right')
        def following(event):
            self.cycle(1)

        @bindings.add('c-c')
        def stop(event):
            self.handle('/stop')

        @bindings.add('c-q')
        def quit(event):
            self.app.exit()

        @bindings.add('f1')
        def help(event):
            self.handle('/help')

        @bindings.add('c-y')
        def approve(event):
            self.handle('/yes')

        @bindings.add('c-r')
        def reject(event):
            self.handle('/no')

        @bindings.add('pageup')
        def pageup(event):
            self.transcript.follow = False
            if self.chat.buffer.document.cursor_position_row < 10:
                self.transcript.older()
                self.render(force=True, anchor='end')
            else:
                self.chat.buffer.cursor_up(count=10)

        @bindings.add('pagedown')
        def pagedown(event):
            if self.chat.buffer.document.cursor_position_row >= self.chat.buffer.document.line_count - 11:
                self.transcript.newer()
                self.render(force=True, anchor='start')
            else:
                self.chat.buffer.cursor_down(count=10)

        @bindings.add('c-home')
        def top(event):
            self.handle('/top')

        @bindings.add('c-end')
        def bottom(event):
            self.handle('/bottom')

        @bindings.add('f2')
        def sidebar(event):
            self.handle('/sidebar')

        @bindings.add('tab')
        def focus(event):
            self.app.layout.focus(self.chat if self.app.layout.has_focus(self.input) else self.input)

        root = HSplit([
            Window(FormattedTextControl(lambda: [('class:header',
                f' Agent CLI | {sum(s.busy for s in self.manager.sessions.values())} 个任务运行中 | 文件：{self.store.workspace_root.name}/{self.current[:8]}… | F1 帮助')]), height=1),
            VSplit([ConditionalContainer(Frame(self.sidebar, title='会话 / Agents'),
                                          Condition(lambda: self.sidebar_visible)),
                    Frame(self.chat, title=self.conversation_title)]),
            ConditionalContainer(Frame(Window(FormattedTextControl(self.confirmation_text), height=5, wrap_lines=True),
                                       title='当前会话需要确认 · Ctrl+Y 同意 / Ctrl+R 拒绝'),
                                 Condition(lambda: bool(self.active.confirmation) or self.pending_forget == self.current)),
            Frame(self.input, title='输入消息或 /命令'),
            Window(FormattedTextControl(self.status_text), height=1),
        ])
        self.app = Application(layout=Layout(root, focused_element=self.input), key_bindings=bindings,
                               full_screen=True, mouse_support=True, input=input, output=output,
                               min_redraw_interval=0.08, max_render_postpone_time=0.05, refresh_interval=1.0,
                               style=Style.from_dict({'header': 'bg:#1f2937 #e5e7eb bold',
                                                      'selected': '#22d3ee bold', 'status': '#94a3b8'}))
        self.app.ttimeoutlen = 0.05
        self.render(force=True)

    @property
    def active(self):
        return self.manager.sessions[self.current]

    def session_list(self):
        result = []
        for identifier, session in self.manager.sessions.items():
            if identifier in self.hidden:
                continue
            def click(event, key=identifier):
                from prompt_toolkit.mouse_events import MouseEventType
                if event.event_type == MouseEventType.MOUSE_UP:
                    self.select(key)
            status = LABELS.get(session.record['status'], session.record['status'])
            parent = session.record.get('branch', {}).get('parent_id')
            lineage = f' · ↳{parent[:6]}' if parent else ''
            result.append(('class:selected' if identifier == self.current else '',
                           f'{">" if identifier == self.current else " "} {session.record["title"][:14]}\n  {identifier[:8]} · {status}{lineage}\n', click))
        return result

    def confirmation_text(self):
        if self.pending_forget == self.current:
            return '确认清空当前会话的 memory.md？对话历史和文件工作区将保留。'
        return self.active.confirmation['prompt'] if self.active.confirmation else ''

    def status_text(self):
        s = self.active
        elapsed = f' 本轮 {time.monotonic() - s.started_at:.0f}s' if s.busy else ''
        if s.busy and s.phase.startswith(('模型', '生成工具', '等待模型')):
            idle = time.monotonic() - s.last_model_event_at
            if s.last_model_event_at and idle >= 5:
                elapsed += f' · 等待后续数据 {idle:.0f}s / 空闲上限 {config.SESSION_TIMEOUT:g}s'
        if config.ENABLE_SESSION_BUDGETS:
            remaining = s.record.get('last_run', {}).get('budget', {}).get('rounds_remaining')
            if remaining is not None:
                elapsed += f' · 剩余模型轮次 {remaining}'
        return [('class:status', f'{s.id[:8]} | {LABELS.get(s.record["status"], "")} {s.phase}{elapsed} | 上下文 {len(s.record["messages"])} 条 | Ctrl+Q 退出')]

    def select(self, identifier):
        self.transcript = Transcript()
        self._view_key = None
        self.current = identifier
        self.hidden.discard(identifier)
        self.notice = ''
        self.render(force=True)
        self.app.layout.focus(self.input)

    def cycle(self, step):
        ids = [key for key in self.manager.sessions if key not in self.hidden]
        self.select(ids[(ids.index(self.current) + step) % len(ids)])

    def conversation_title(self):
        start, end, total = self.page_info
        following = '跟随最新' if self.transcript.follow else '浏览历史（Ctrl+End 返回最新）'
        return f'对话 · {start + 1 if total else 0}–{end}/{total} 行 · {following}'

    def refresh(self):
        """Coalesce token/status notifications; never rebuild text in token callbacks."""
        if not getattr(self, 'app', None) or not self.current:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # Headless callers can explicitly render; real UI runs on asyncio.
        if self._refresh_handle is None:
            self._refresh_handle = loop.call_later(0.08, self.render)

    def render(self, force=False, anchor=None):
        if self._refresh_handle is not None:
            self._refresh_handle.cancel()
            self._refresh_handle = None
        if not self.app or not self.current:
            return
        session = self.active
        # Mouse/scrollbar users browsing earlier lines should not be dragged down.
        if (not force and self.chat.text and
                self.chat.buffer.cursor_position < len(self.chat.text)):
            self.transcript.follow = False
        key = (self.current, len(session.record['messages']), session.partial,
               session.record.get('assessment'), session.record.get('error'), self.notice,
               self.transcript.start if not self.transcript.follow else -1)
        if force or key != self._view_key:
            self.transcript.sync(session.record['messages'])
            extra = '\n'.join(filter(None, [session.record.get('assessment'), session.record.get('error'), self.notice]))
            text, self.page_info = self.transcript.page(session.partial, extra)
            if self.chat.text != text:
                position = (len(text) if self.transcript.follow or anchor == 'end' else
                            0 if anchor == 'start' else min(self.chat.buffer.cursor_position, len(text)))
                self.chat.buffer.set_document(Document(text, position), bypass_readonly=True)
            self._view_key = key
        self.app.invalidate()

    def disk_job(self, operation, message):
        """File export/memory operations must not stall typing on slow filesystems."""
        owner = self.current
        async def run():
            async with self._disk_lock:
                try:
                    await self.manager.flush()
                    result = await asyncio.to_thread(operation)
                    note = message(result)
                except (OSError, ValueError) as exc:
                    note = str(exc)
                self.notice = note if self.current == owner else f'[{owner[:8]}] {note}'
                self.refresh()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            result = operation()
            self.notice = message(result)
            return
        self.notice = '正在处理文件…'
        task = asyncio.create_task(run())
        self._ui_jobs.add(task)
        task.add_done_callback(self._ui_jobs.discard)

    def memory_command(self, session, argument):
        """显式管理用户记忆；修改/删除仍由当前会话的确认流程处理。"""
        from core.memory import MemoryPatch, MemoryQuery
        verb, _, payload = argument.partition(' ')
        payload = payload.strip()
        namespace = session.record.get('memory_namespace')
        if not config.ENABLE_MEMORY_MANAGEMENT and argument != 'scope default':
            raise ValueError('请先显式开启 ENABLE_MEMORY_MANAGEMENT')
        if verb == 'scope':
            if (session.confirmation or self.pending_forget == session.id
                    or getattr(self, 'pending_memory_remove', (None,))[0] == session.id
                    or getattr(self, 'pending_memory_edit', (None,))[0] == session.id):
                raise ValueError('请先处理当前待确认操作，再切换记忆空间')
            if payload:
                self.manager.set_memory_namespace(session, None if payload == 'default' else payload)
            self.notice = '记忆空间：' + str(session.record.get('memory_namespace') or 'default')
        elif verb == 'search':
            values = json.loads(payload) if payload.startswith('{') else {'query': payload}
            query = MemoryQuery.model_validate(values).model_dump()
            self.disk_job(lambda: self.store.search_memory(session.id, namespace=namespace, **query),
                          lambda rows: json.dumps(rows, ensure_ascii=False, indent=2))
        elif verb == 'add':
            values = MemoryPatch.model_validate_json(payload).model_dump(mode='json', exclude_unset=True)
            if not values.get('text'):
                raise ValueError('add JSON 必须包含非空 text')
            self.disk_job(lambda: self.store.remember(session.id, namespace=namespace, **values),
                          lambda _: '记忆已保存，使用 /memory 查看条目 ID。')
        elif verb == 'edit':
            if session.confirmation:
                raise ValueError('请先处理工具确认')
            entry_id, _, raw = payload.partition(' ')
            values = MemoryPatch.model_validate_json(raw).model_dump(mode='json', exclude_unset=True)
            if not values:
                raise ValueError('edit JSON 不能为空')
            self.pending_memory_edit = (session.id, entry_id, values, namespace)
            self.notice = f'确认修改记忆 {entry_id}：{json.dumps(values, ensure_ascii=False)}？输入 /yes 或 /no。'

    def branch_command(self, session, command, argument):
        if not config.ENABLE_SESSION_BRANCHES:
            raise ValueError('请先显式开启 ENABLE_SESSION_BRANCHES')
        if command == '/branch':
            index = int(argument) if argument else None
            text = None
        elif command == '/resend':
            number, _, text = argument.partition(' ')
            index = int(number)
            if not text.strip():
                raise ValueError('请输入重发的新文本')
        else:
            index = next((i for i in range(len(session.record['messages']) - 1, 0, -1)
                          if session.record['messages'][i]['role'] == 'user'), None)
            if index is None:
                raise ValueError('没有可以重发的用户消息')
            text = None
        try:
            asyncio.get_running_loop()
            synchronous = False
        except RuntimeError:
            synchronous = True
        async def create_branch():
            try:
                child = (await self.manager.fork_session(session, index) if command == '/branch'
                         else await self.manager.resend(session, index, text))
                if self.current == session.id:
                    self.select(child.id)
                self.notice = f'新分支 {child.id[:8]}；原会话未修改，新工作区为空。'
                if synchronous and child.task:
                    await child.task
            except (ValueError, OSError) as exc:
                self.notice = str(exc)
            self.refresh()
        self.notice = '正在创建独立会话快照…'
        if synchronous:
            asyncio.run(create_branch())
        else:
            task = asyncio.create_task(create_branch())
            self._ui_jobs.add(task)
            task.add_done_callback(self._ui_jobs.discard)

    def handle(self, value):
        if not value:
            return
        try:
            session = self.active
            namespace = session.record.get('memory_namespace')
            if value.startswith('//') or not value.startswith('/'):
                if session.confirmation:
                    raise ValueError('当前会话正在等待确认，请 /yes 或 /no；也可切换其他会话')
                self.notice = ''
                self.transcript.bottom()
                self.manager.submit(session, value[1:] if value.startswith('//') else value)
            else:
                command, _, argument = value.partition(' ')
                argument = argument.strip()
                if command == '/new':
                    self.select(self.manager.create(argument or '新会话').id)
                elif command in {'/branch', '/resend', '/retry'}:
                    self.branch_command(session, command, argument)
                elif command == '/model':
                    if argument:
                        self.manager.set_model_profile(session, None if argument == 'default' else argument)
                    self.notice = '当前模型预设：' + str(session.record.get('model_profile') or 'default') + '\n可选：' + ', '.join(config.MODEL_PROFILES)
                elif command == '/tools':
                    if argument:
                        names = None if argument == 'all' else [] if argument == 'none' else argument.replace(',', ' ').split()
                        self.manager.set_tool_names(session, names)
                    names = session.record.get('tool_names', config.MODEL_TOOL_NAMES)
                    self.notice = '当前工具子集：' + ('全部' if names is None else ', '.join(names) or '无工具')
                elif command == '/config':
                    from core.session_limits import effective_limits
                    if argument:
                        self.manager.set_limits(session, json.loads(argument) if argument.startswith('{') else argument)
                    self.notice = json.dumps(effective_limits(session.record), ensure_ascii=False, indent=2)
                elif command == '/policy':
                    if argument:
                        self.manager.set_permission_policy(session, None if argument == 'default' else argument)
                    policy = session.record.get('permission_policy') or config.TOOL_PERMISSION_POLICY
                    self.notice = f'当前权限策略：{policy}；预先确认规则 {len(config.TOOL_PERMISSION_RULES)} 条。trusted 未匹配规则时仍需逐次确认。'
                elif command == '/switch':
                    matches = [key for key in self.manager.sessions if key.startswith(argument)]
                    if not argument or len(matches) != 1:
                        raise ValueError('请指定唯一的会话 ID 前缀')
                    self.select(matches[0])
                elif command == '/rename':
                    if not argument:
                        raise ValueError('请输入名称')
                    session.record['title'] = argument
                    self.manager.save(session)
                elif command == '/prompt':
                    if session.busy or not argument:
                        raise ValueError('请在会话空闲时输入完整 Agent 指令')
                    session.record['messages'][0]['content'] = argument
                    self.manager.save(session)
                    self.notice = '已保存当前 Agent 指令。'
                elif command == '/remember':
                    if not argument:
                        raise ValueError('请输入要记住的内容')
                    self.disk_job(lambda: self.store.remember(session.id, argument, namespace=namespace),
                                  lambda _: '记忆已保存，下次模型请求时生效。')
                elif command == '/memory' and argument.partition(' ')[0] in {'scope', 'add', 'search', 'edit'}:
                    self.memory_command(session, argument)
                elif command == '/memory' and argument.startswith('rm '):
                    if session.confirmation:
                        raise ValueError('请先处理工具确认')
                    self.pending_memory_remove = (session.id, argument[3:].strip(), namespace)
                    self.notice = '确认删除指定记忆？输入 /yes 或 /no。'
                elif command == '/memory' and argument == 'candidates':
                    self.disk_job(lambda: (self.store.directory(session.id) / 'memory-candidates.json').read_text(),
                                  lambda text: '候选尚未注入；用 /remember 内容 明确采纳：\n' + text)
                elif command == '/memory':
                    self.disk_job(lambda: self.store.memory(session.id, namespace),
                                  lambda text: f'记忆文件：{self.store.memory_path(session.id, namespace)}\n' + (text or '（空）'))
                elif command == '/forget':
                    if session.confirmation:
                        raise ValueError('请先处理工具确认')
                    self.pending_forget = session.id
                    self.pending_forget_namespace = namespace
                elif command in {'/yes', '/no'}:
                    if session.confirmation:
                        self.manager.decide(session, command == '/yes')
                    elif getattr(self, 'pending_memory_edit', (None,))[0] == session.id:
                        _, entry_id, patch, edit_namespace = self.pending_memory_edit
                        self.pending_memory_edit = (None,)
                        if command == '/yes':
                            self.disk_job(lambda: self.store.edit_memory(session.id, entry_id, patch, namespace=edit_namespace),
                                          lambda _: '记忆条目已原子更新。')
                    elif getattr(self, 'pending_memory_remove', (None,))[0] == session.id:
                        _, entry_id, remove_namespace = self.pending_memory_remove
                        self.pending_memory_remove = (None,)
                        if command == '/yes':
                            self.disk_job(lambda: self.store.remove_memory(session.id, entry_id, remove_namespace), lambda _: '记忆条目已删除。')
                    elif self.pending_forget == session.id:
                        if command == '/yes':
                            forget_namespace = getattr(self, 'pending_forget_namespace', None)
                            self.disk_job(lambda: self.store.clear_memory(session.id, forget_namespace), lambda _: '记忆已清空。')
                        self.pending_forget = None
                    else:
                        raise ValueError('当前会话没有待确认操作')
                elif command == '/export':
                    self.disk_job(lambda: self.store.export_saved(session.id, argument or 'md'), lambda path: '已导出：' + str(path))
                elif command == '/stop':
                    self.manager.cancel(session)
                elif command == '/close':
                    if session.busy:
                        raise ValueError('请先停止或等待当前任务完成')
                    self.hidden.add(session.id)
                    available = [key for key in self.manager.sessions if key not in self.hidden]
                    self.select(available[-1] if available else self.manager.create().id)
                elif command == '/where':
                    self.notice = '文件工作区：' + str(self.store.workspace_path(session.id)) + '\n会话记录：' + str(self.store.directory(session.id))
                elif command == '/sidebar':
                    self.sidebar_visible = not self.sidebar_visible
                elif command in {'/older', '/newer', '/top', '/bottom'}:
                    {'/older': self.transcript.older, '/newer': self.transcript.newer,
                     '/top': self.transcript.top, '/bottom': self.transcript.bottom}[command]()
                    self.render(force=True, anchor='end' if command in {'/older', '/bottom'} else 'start')
                elif command == '/help':
                    self.notice = HELP
                elif command == '/quit':
                    self.app.exit()
                else:
                    raise ValueError('未知命令，输入 /help 查看可用命令')
        except (ValueError, OSError) as exc:
            self.notice = str(exc)
        self.render(force=True)

    async def run(self):
        try:
            with patch_stdout(raw=False):
                await self.app.run_async()
        finally:
            if self._refresh_handle is not None:
                self._refresh_handle.cancel()
                self._refresh_handle = None
            await self.manager.shutdown()
            if self._ui_jobs:
                await asyncio.gather(*list(self._ui_jobs), return_exceptions=True)
            await llm.close_clients()


def main():
    parser = argparse.ArgumentParser(description='多会话 Agent CLI：自动保存、独立工作区、文件记忆')
    parser.add_argument('--state-dir', type=Path, default=config.AGENT_STATE_DIR, help='持久化目录')
    parser.add_argument('--list', action='store_true', help='列出已保存会话后退出')
    parser.add_argument('--prompt', help='无 TTY 单次执行；默认拒绝待确认操作')
    parser.add_argument('--json', action='store_true', help='headless 输出单个 JSON 对象')
    parser.add_argument('--session', help='headless 继续已有会话 ID 或唯一前缀')
    parser.add_argument('--policy', choices=['readonly', 'standard', 'trusted'], help='headless 显式权限档位，默认 standard 并拒绝人工确认')
    args = parser.parse_args()
    if args.prompt is None and (args.json or args.session or args.policy):
        parser.error('--json/--session/--policy 需要 --prompt')
    if args.prompt is not None and (not args.prompt.strip() or args.list):
        parser.error('--prompt 不能为空且不能与 --list 同用')
    try:
        if not args.list:
            config.validate_runtime_config()
    except ValueError as exc:
        parser.exit(2, str(exc) + '\n')
    try:
        store = SessionStore(args.state_dir)
    except (OSError, RuntimeError) as exc:
        parser.exit(1, str(exc) + '\n')
    try:
        if args.list:
            for record in store.list_metadata():
                print(record['id'][:8], record['status'], record['title'])
            for error in store.errors:
                print('无法读取：', error)
            return
        if args.prompt is not None:
            from core.headless import run_headless, RedactingFormatter
            logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
            for handler in logging.getLogger().handlers:
                handler.setFormatter(RedactingFormatter('%(levelname)s %(name)s: %(message)s'))
            async def execute_once():
                try:
                    return await run_headless(store, args.prompt, session_id=args.session, policy=args.policy or 'standard')
                finally:
                    await llm.close_clients()
            try:
                # 第三方模型/解析器的诊断输出不得污染 JSON stdout。
                with redirect_stdout(sys.stderr):
                    result = asyncio.run(execute_once())
            except (ValueError, OSError) as exc:
                parser.exit(2, str(exc) + '\n')
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                answers = [row.get('content', '') for row in result['messages'] if row['role'] == 'assistant' and not row.get('tool_calls')]
                print('\n'.join(answers) or result['error'] or result['status'])
            raise SystemExit(result['exit_code'])
        # Keep debug/audit messages out of the interactive layout.
        logging.basicConfig(filename=store.root / 'cli.log', level=logging.DEBUG if config.DEBUG else logging.INFO,
                            format='%(asctime)s %(levelname)s %(name)s: %(message)s')
        for name in ('httpx', 'httpcore', 'openai'):
            logging.getLogger(name).setLevel(logging.WARNING)
        async def launch():
            cli = AgentCLI(store)
            await cli.run()
        asyncio.run(launch())
    finally:
        store.close()
