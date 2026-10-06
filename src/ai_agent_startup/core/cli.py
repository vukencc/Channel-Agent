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
from prompt_toolkit.layout import HSplit, VSplit, Layout, Window, ConditionalContainer, FloatContainer, Float
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.filters import Condition
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea
from prompt_toolkit.patch_stdout import patch_stdout

from ai_agent_startup import config
from ai_agent_startup.core import llm
from ai_agent_startup.core.messages import is_user_request
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import LazyRecord, SessionStore
from ai_agent_startup.core.transcript import Transcript
from ai_agent_startup.core.cli_commands import CommandCompleter, CommandPalette, POLICY_LABELS, help_text

HELP = help_text()

LABELS = {'idle': '就绪', 'queued': '排队', 'running': '运行', 'confirming': '待确认',
          'stopping': '停止中', 'cancelled': '已停止', 'error': '错误', 'interrupted': '中断恢复',
          'checkpoint': '阶段保存·可继续'}


class AgentCLI:
    def __init__(self, store: SessionStore, *, input=None, output=None):
        self.store = store
        self.manager = SessionManager(store, self.refresh, cascade_deletions=True)
        self.hidden = set()
        self.notice = 'F1 查看帮助 · /where 查看工作区 · PageUp 浏览历史 · Ctrl+End 跟随最新'
        self.pending_forget = None
        self.pending_delete = None
        self.pending_cleanup = None
        self.show_details = False
        self._deleting = set()
        self._command_draft = None
        self.app = None
        self.transcript = Transcript(show_details=self.show_details)
        self.page_info = (0, 0, 0)
        self._refresh_handle = None
        self._view_key = None
        self._render_task = None
        self._render_dirty = False
        self._render_anchor = None
        self._render_closed = False
        self._drafts = {}
        self.sidebar_visible = True
        self._ui_jobs = set()
        self._delete_jobs = set()
        self._lifecycle_busy = False
        self._disk_lock = asyncio.Lock()
        self.input = TextArea(height=2, prompt='› ', multiline=True, wrap_lines=True,
                              completer=CommandCompleter(self), history=InMemoryHistory())
        self.palette = CommandPalette(self)
        self.chat = TextArea(read_only=True, scrollbar=True, wrap_lines=True, focusable=True)
        self.sidebar = Window(FormattedTextControl(self.session_list), width=22)
        self.current = next(reversed(self.manager.sessions), None)
        if self.current is None:
            from ai_agent_startup.core.session_service import open_session
            self.current = open_session(self.manager).id
        if store.errors:
            self.notice = '以下会话文件无法读取，已保留原文件：\n' + '\n'.join(store.errors)
        bindings = KeyBindings()

        @bindings.add('enter', eager=True, filter=Condition(lambda: not self.palette.visible and self.app.layout.has_focus(self.input)))
        def send(event):
            value = self.input.text.strip()
            # 命令可能切换会话，不能把命令本身存为旧会话草稿。
            command = value.startswith('/') and not value.startswith('//')
            saved_draft = self._command_draft if command else None
            self._command_draft = None
            if command:
                self.input.buffer.append_to_history()
                draft = saved_draft[1] if saved_draft and saved_draft[0] == self.current else ''
                self.input.buffer.set_document(Document(draft, len(draft)))
            accepted = self.handle(value)
            if accepted:
                if not command:
                    self.input.text = ''
            elif command:
                if saved_draft:
                    self._command_draft = saved_draft
                self.input.buffer.set_document(Document(value, len(value)))

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

        @bindings.add('c-p', eager=True)
        def commands(event):
            self.open_palette()

        @bindings.add('escape', eager=True, filter=Condition(lambda: self.palette.visible))
        def dismiss_palette(event):
            self.palette.close()

        @bindings.add('enter', eager=True, filter=Condition(lambda: self.palette.visible))
        def choose_palette(event):
            self.palette.choose()

        @bindings.add('up', eager=True, filter=Condition(lambda: self.palette.visible))
        def palette_previous(event):
            self.palette.move(-1)

        @bindings.add('down', eager=True, filter=Condition(lambda: self.palette.visible))
        def palette_next(event):
            self.palette.move(1)

        @bindings.add('tab', eager=True)
        def complete_or_focus(event):
            if self.palette.visible:
                self.palette.move(1)
                return
            buffer = self.input.buffer
            if (self.app.layout.has_focus(self.input)
                    and self.input.text.startswith('/') and not self.input.text.startswith('//')):
                if buffer.complete_state:
                    buffer.complete_next()
                else:
                    buffer.start_completion(select_first=True)
            else:
                self.app.layout.focus(self.chat if self.app.layout.has_focus(self.input) else self.input)

        root = HSplit([
            Window(FormattedTextControl(lambda: [('class:header',
                f' Agent CLI | {sum(s.busy for s in self.manager.sessions.values())} 个任务运行中 | '
                f'{sum(bool(s.confirmation) for s in self.manager.sessions.values())} 个待确认 | F1 帮助')]), height=1),
            VSplit([ConditionalContainer(Frame(self.sidebar, title='会话 / Agents'),
                                          Condition(lambda: self.sidebar_visible)),
                    Frame(self.chat, title=self.conversation_title)]),
            ConditionalContainer(Frame(Window(FormattedTextControl(self.confirmation_text), height=5, wrap_lines=True),
                                       title='当前会话需要确认 · Ctrl+Y 同意 / Ctrl+R 拒绝'),
                                 Condition(lambda: bool(self.active.confirmation) or self.pending_forget == self.current
                                           or self.pending_delete == self.current
                                           or self.pending_cleanup is not None and self.pending_cleanup[0] == self.current)),
            Window(FormattedTextControl(self.status_text), height=1),
            Frame(self.input, title='输入消息或 /命令 · Enter 发送 · Alt+Enter 换行'),
        ])
        root = FloatContainer(root, floats=[
            Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=7, scroll_offset=1)),
            Float(width=80, content=ConditionalContainer(self.palette.container,
                                                        Condition(lambda: self.palette.visible))),
        ])
        self.app = Application(layout=Layout(root, focused_element=self.input), key_bindings=bindings,
                               full_screen=True, mouse_support=True, input=input, output=output,
                               min_redraw_interval=0.08, max_render_postpone_time=0.05, refresh_interval=1.0,
                               style=Style.from_dict({'header': 'bg:#1f2937 #e5e7eb bold',
                                                      'selected': '#22d3ee bold', 'status': '#94a3b8',
                                                      'ready': '#86efac', 'working': '#67e8f9',
                                                      'queued': '#c4b5fd', 'waiting': '#fbbf24 bold', 'failed': '#f87171 bold'}))
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
            parent = session.record.get('delegated_from') or session.record.get('branch', {}).get('parent_id')
            lineage = f' · ↳{parent[:6]}' if parent else ''
            unread = self.manager.communication.pending_count(session)
            badge = f' · 未读{unread}' if unread else ''
            style = 'class:waiting' if session.confirmation else 'class:selected' if identifier == self.current else ''
            result.append((style,
                           f'{">" if identifier == self.current else " "} {session.record["title"][:14]}\n  {identifier[:8]} · {status}{lineage}{badge}\n', click))
        return result

    def confirmation_text(self):
        if self.pending_cleanup is not None and self.pending_cleanup[0] == self.current:
            selection = self.pending_cleanup[1]
            return ('确认清理：' + '、'.join(name for name, selected in selection.items() if selected) + '？\n'
                    '缓存和旧日志将清除；会话记录移入回收区。工作区、知识库、导出和共享记忆保留。\n'
                    '输入 /yes 或 /no；Full Access 仍须明确确认。')
        if self.pending_delete == self.current:
            count = len(self.manager.session_deletion_targets(self.active, cascade=True))
            return (f'确认删除会话 {self.active.record["title"]}（{self.current}）？\n'
                    f'该 Agent 及其委派后代共 {count} 个会话移入 .trash 回收区；工作区、导出和共享记忆保留。\n'
                    '输入 /yes 或 Ctrl+Y 明确同意；/no 或 Ctrl+R 取消。Full Access 不跳过此确认。')
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
        status = '待确认' if s.confirmation else LABELS.get(s.record['status'], '')
        state = s.record['status']
        style = ('waiting' if s.confirmation or state in {'confirming', 'stopping'} else 'queued' if state == 'queued' else
                 'failed' if state in {'error', 'interrupted'} else 'working' if state == 'running' else 'ready')
        loading = ' · 加载历史中（可继续编辑草稿）' if self._is_loading(s.record) else ''
        unread = self.manager.communication.pending_count(s)
        badge = f' · Agent 未读 {unread}' if unread else ''
        policy = self.manager.permission_override or s.record.get('permission_policy') or config.TOOL_PERMISSION_POLICY
        mode = POLICY_LABELS.get(policy, policy)
        return [(f'class:status class:{style}', f'{s.id[:8]} | {mode} | {status} {s.phase}{elapsed}{loading}{badge} | 上下文 {self._message_count(s.record)} 条 | Ctrl+Q 退出')]

    @staticmethod
    def _is_loading(record):
        return isinstance(record, LazyRecord) and record.loader is not None

    @staticmethod
    def _message_count(record):
        if isinstance(record, LazyRecord):
            if record.loader is not None:
                return dict.get(record, 'message_count', 0)
            return len(dict.__getitem__(record, 'messages'))
        return len(record['messages'])

    def select(self, identifier):
        if self.palette.visible:
            self.palette.close()
        if self.current:
            if self._command_draft and self._command_draft[0] == self.current:
                self._drafts[self.current] = self._command_draft[1]
                self._command_draft = None
            else:
                self._drafts[self.current] = self.input.text
        self.transcript = Transcript(show_details=self.show_details)
        self._view_key = None
        self.current = identifier
        self.page_info = (0, 0, 0)
        self.chat.buffer.set_document(Document('正在加载会话历史…'), bypass_readonly=True)
        draft = self._drafts.get(identifier, '')
        self.input.buffer.set_document(Document(draft, len(draft)))
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
        if self._render_closed or not getattr(self, 'app', None) or not self.current:
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
        if self._render_closed or not self.app or not self.current:
            return
        session = self.active
        # Mouse/scrollbar users browsing earlier lines should not be dragged down.
        if (not force and self.chat.text and
                self.chat.buffer.cursor_position < len(self.chat.text)):
            self.transcript.follow = False
        key = (self.current, self._message_count(session.record), session.partial,
               session.record.get('assessment'), session.record.get('error'), self.notice,
               self.transcript.start if not self.transcript.follow else -1)
        if force or key != self._view_key:
            extra = '\n'.join(filter(None, [session.record.get('assessment'), session.record.get('error'), self.notice]))
            # 已缓存页的导航只处理可见行，不等待新的后台任务。
            if (force and anchor is not None and self.transcript.count == key[1]
                    and (self._render_task is None or self._render_task.done())
                    and len(session.partial) < 8192):
                text, self.page_info = self.transcript.page(session.partial, extra)
                self._apply_page(text, anchor)
                self._view_key = key
                self.app.invalidate()
                return
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                self.transcript.sync(session.record['messages'])
                text, self.page_info = self.transcript.page(session.partial, extra)
                self._apply_page(text, anchor)
                self._view_key = key
            else:
                if self._render_task is not None and not self._render_task.done():
                    self._render_dirty = True
                    self._render_anchor = anchor
                else:
                    transcript = self.transcript
                    identifier = self.current
                    partial = session.partial

                    def prepare():
                        # 保留消息列表身份，避免每个流式刷新都重建全部历史。
                        transcript.sync(session.record['messages'])
                        if identifier not in self.manager.communication.latest:
                            self.manager.communication.read(identifier, 0, 1)

                    async def finish():
                        try:
                            await asyncio.to_thread(prepare)
                            if self.current == identifier and self.transcript is transcript:
                                text, self.page_info = transcript.page(partial, extra)
                                self._apply_page(text, anchor)
                                self._view_key = key
                        except (OSError, ValueError, KeyError, TypeError) as exc:
                            if self.current == identifier:
                                self.notice = f'历史加载失败：{exc}'
                                self._apply_page(self.notice, 'start')
                        finally:
                            if self._render_dirty:
                                next_anchor = self._render_anchor
                                self._render_dirty = False
                                self._render_anchor = None
                                asyncio.get_running_loop().call_soon(self.render, True, next_anchor)
                            self.app.invalidate()

                    self._render_task = asyncio.create_task(finish())
        self.app.invalidate()

    def _apply_page(self, text, anchor):
        if self.chat.text != text:
            position = (len(text) if self.transcript.follow or anchor == 'end' else
                        0 if anchor == 'start' else min(self.chat.buffer.cursor_position, len(text)))
            self.chat.buffer.set_document(Document(text, position), bypass_readonly=True)

    async def drain_render(self):
        """等待当前历史准备和合并后的后继任务。"""
        while self._render_task is not None:
            task = self._render_task
            await asyncio.shield(task)
            await asyncio.sleep(0)
            if self._render_task is task and not self._render_dirty:
                break

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

    def plan_command(self, session, argument=''):
        """只读计划查询不占模型槽，磁盘读取仍在线程完成。"""
        service = self.manager.task_plans
        if service is None:
            raise ValueError('任务计划未启用；设置 ENABLE_TASK_PLANS=true 后重启')
        async def run():
            try:
                plan = await service.get(session, cursor=argument or None)
                lines = [f'运行状态：{session.record["status"]} | 计划：{plan["status"]} | 验收：unverified']
                if plan['plan_id']:
                    lines.append(f'{plan["plan_id"][:8]} · revision={plan["revision"]} · {plan["goal"]}')
                for step in plan['tasks']:
                    lines.append(f'{step["key"]} [{step["status"]}] {step["title"]} · 依赖={step["depends_on"]}')
                    lines.append('  验收条件：' + step['acceptance'])
                    if step['block_reason']:
                        lines.append('  阻塞：' + step['block_reason'])
                    if step['execution_ref']:
                        ref = step['execution_ref']
                        lines.append(f'  执行：{ref["kind"]}/{ref["id"][:8]} · {ref["runtime_status"]} · 排空={ref["drained"]}')
                    if step['result']:
                        lines.append('  自报结果：' + step['result'])
                    if step['evidence_refs']:
                        lines.append('  证据：' + ', '.join(step['evidence_refs']))
                if plan['next_cursor']:
                    lines.append('下一页：/plan ' + plan['next_cursor'])
                note = '\n'.join(lines)
            except (OSError, ValueError, PermissionError) as exc:
                note = '计划读取失败：' + str(exc)
            self.notice = note if self.current == session.id else f'[{session.id[:8]}] {note}'
            self.refresh()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(run())
        else:
            task = asyncio.create_task(run())
            self._ui_jobs.add(task)
            task.add_done_callback(self._ui_jobs.discard)

    def memory_command(self, session, argument):
        """显式管理用户记忆；修改/删除仍由当前会话的确认流程处理。"""
        from ai_agent_startup.core.memory import MemoryPatch, MemoryQuery
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
                          if is_user_request(session.record['messages'][i])), None)
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

    def image_command(self, session, argument):
        from ai_agent_startup.core.images import submit_image
        path, separator, prompt = argument.partition('|')
        if not separator or not path.strip() or not prompt.strip():
            raise ValueError('用法：/image 工作区相对路径 | 问题')
        async def run():
            try:
                accepted = await submit_image(self.manager, session, path.strip(), prompt.strip())
                self.notice = '图片已加入会话。' if accepted else '已取消图片上传。'
            except (ValueError, OSError) as exc:
                self.notice = str(exc)
            self.refresh()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(run())
        else:
            task = asyncio.create_task(run())
            self._ui_jobs.add(task)
            task.add_done_callback(self._ui_jobs.discard)

    def open_palette(self, mode='commands', query=''):
        self.palette.open(mode, query)

    def prepare_command(self, value):
        """Keep a displaced message draft until the inserted command is submitted."""
        if self._command_draft is None:
            self._command_draft = (self.current, self.input.text)
        self.input.buffer.set_document(Document(value, len(value)))
        self.app.layout.focus(self.input)

    def request_delete(self, argument):
        if self._lifecycle_busy or self.pending_cleanup is not None:
            raise ValueError('请先完成清理或删除操作')
        if self._deleting:
            raise ValueError('已有会话正在移入回收区，请等待完成')
        matches = [key for key in self.manager.sessions if key.startswith(argument)] if argument else [self.current]
        if len(matches) != 1:
            raise ValueError('请指定唯一的会话 ID 前缀')
        target = self.manager.sessions[matches[0]]
        self.manager.check_session_deletion(target)
        if target.confirmation or self.pending_forget == target.id or any(
                getattr(self, name, (None,))[0] == target.id
                for name in ('pending_memory_remove', 'pending_memory_edit')):
            raise ValueError('请先处理选定会话的待确认操作')
        if self.pending_delete and self.pending_delete != target.id:
            raise ValueError('请先切换回待删除会话并用 /yes 或 /no 处理确认')
        if self.current != target.id:
            self.select(target.id)
        self.pending_delete = target.id
        self.notice = self.confirmation_text()

    def request_cleanup(self, argument):
        from ai_agent_startup.core.maintenance import maintenance_preview, maintenance_busy
        if self._lifecycle_busy:
            raise ValueError('正在清理或删除会话，请稍后操作')
        if self.pending_cleanup is not None or self.pending_delete or self.pending_forget or any(
                getattr(self, name, (None,))[0] is not None
                for name in ('pending_memory_remove', 'pending_memory_edit')):
            raise ValueError('请先处理已有的待确认操作')
        if any(session.confirmation for session in self.manager.sessions.values()):
            raise ValueError('请先处理已有的工具确认')
        if maintenance_busy(self.manager):
            raise ValueError('Agent 或工具仍在运行，请先停止并等待')
        names = set(argument.replace(',', ' ').split())
        if len(names) > 1 or names - {'cache', 'logs', 'sessions', 'all'}:
            raise ValueError('用法：/cleanup [cache|logs|sessions|all]')
        selection = {name: name in names or 'all' in names for name in ('cache', 'logs', 'sessions')}
        owner = self.current

        def displayed(preview):
            if names:
                for name, selected in selection.items():
                    if selected and preview[name].get('error'):
                        raise ValueError(preview[name]['error'])
                self.pending_cleanup = (owner, selection)
            return ('清理预览：' + json.dumps(preview, ensure_ascii=False) + '\n' +
                    ('请切换回确认会话后输入 /yes 或 /no。' if names else
                     '使用 /cleanup cache、logs、sessions 或 all 选择清理范围。'))
        self.disk_job(lambda: maintenance_preview(self.manager), displayed)

    def cleanup_command(self):
        from ai_agent_startup.core.maintenance import cleanup
        if self._lifecycle_busy:
            raise ValueError('正在清理或删除会话，请稍后操作')
        _, selection = self.pending_cleanup
        self._lifecycle_busy = True
        self.pending_cleanup = None

        async def run():
            try:
                current_job = asyncio.current_task()
                jobs = [job for job in self._ui_jobs if job is not current_job and not job.done()]
                await asyncio.gather(*jobs, return_exceptions=True)
                await self.drain_render()
                result = await cleanup(self.manager, **selection)
                deleted = set(result['sessions']['deleted_ids'])
                self.hidden.difference_update(deleted)
                for identifier in deleted:
                    self._drafts.pop(identifier, None)
                if self._command_draft and self._command_draft[0] in deleted:
                    self._command_draft = None
                if self.current in deleted:
                    self.current = None
                    from ai_agent_startup.core.session_service import open_session
                    self.select(open_session(self.manager).id)
                self.notice = '清理完成：' + json.dumps(result, ensure_ascii=False)
            except (ValueError, OSError) as exc:
                self.notice = str(exc)
            finally:
                self._lifecycle_busy = False
                if self.current not in self.manager.sessions:
                    self.current = None
                    if self.manager.sessions:
                        self.select(next(reversed(self.manager.sessions)))
                    else:
                        from ai_agent_startup.core.session_service import open_session
                        self.select(open_session(self.manager).id)
                self.refresh()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(run())
        else:
            job = asyncio.create_task(run())
            self._ui_jobs.add(job)
            job.add_done_callback(self._ui_jobs.discard)

    def delete_command(self, session):
        """Drain UI readers/writers before moving the explicitly approved session."""
        if self._lifecycle_busy:
            raise ValueError('正在清理或删除会话，请稍后操作')
        targets = self.manager.session_deletion_targets(session, cascade=True)
        for target in targets:
            self.manager.check_session_deletion(target)
        self._lifecycle_busy = True
        identifiers = {target.id for target in targets}
        self._deleting.update(identifiers)
        self.pending_delete = None
        self.notice = f'正在将会话 {session.id[:8]} 移入回收区…'

        async def remove():
            try:
                current_job = asyncio.current_task()
                while jobs := [job for job in self._ui_jobs
                               if job is not current_job and not job.done()]:
                    await asyncio.gather(*jobs, return_exceptions=True)
                await self.drain_render()
                archive = await self.manager.delete_session(session)
                self.hidden.difference_update(identifiers)
                for identifier in identifiers:
                    self._drafts.pop(identifier, None)
                if self._command_draft and self._command_draft[0] in identifiers:
                    self._command_draft = None
                if self.current in identifiers:
                    self.current = None
                    available = [key for key in self.manager.sessions if key not in self.hidden]
                    if not available:
                        available = list(self.manager.sessions)
                    if available:
                        self.select(available[-1])
                    else:
                        from ai_agent_startup.core.session_service import open_session
                        self.select(open_session(self.manager).id)
                self.notice = f'会话 {session.id[:8]} 已移入回收区：{archive}；工作区、导出文件和共享记忆保留。'
            except (ValueError, OSError) as exc:
                self.notice = str(exc)
            finally:
                self._lifecycle_busy = False
                self._deleting.difference_update(identifiers)
                self.refresh()

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(remove())
        else:
            task = asyncio.create_task(remove())
            self._ui_jobs.add(task)
            self._delete_jobs.add(task)
            task.add_done_callback(self._ui_jobs.discard)
            task.add_done_callback(self._delete_jobs.discard)

    def handle(self, value):
        if not value:
            return False
        accepted = True
        try:
            session = self.active
            command = value.partition(' ')[0]
            if self.manager.maintenance or self._lifecycle_busy:
                raise ValueError('正在清理数据，请稍后操作')
            if self.pending_cleanup is not None and self.pending_cleanup[0] == session.id and command not in {
                    '/yes', '/no', '/switch', '/sessions', '/resume', '/help', '/quit', '/where'}:
                raise ValueError('请先用 /yes 或 /no 处理清理确认')
            if self._is_loading(session.record) and command not in {
                    '/new', '/switch', '/close', '/stop', '/quit', '/help', '/where', '/sidebar',
                    '/older', '/newer', '/top', '/bottom', '/yes', '/no', '/sessions', '/resume',
                    '/models', '/permissions', '/delete', '/del'}:
                raise ValueError('历史仍在后台加载；草稿已保留，请加载完成后发送或切换其他会话')
            if session.id in self._deleting and command not in {'/new', '/switch', '/sessions', '/resume', '/help', '/quit'}:
                raise ValueError('会话正在移入回收区，请等待完成')
            if self.pending_delete == session.id and command not in {
                    '/yes', '/no', '/switch', '/sessions', '/resume', '/new', '/help', '/quit',
                    '/where', '/sidebar', '/older', '/newer', '/top', '/bottom', '/delete', '/del'}:
                raise ValueError('请先用 /yes 或 /no 处理当前会话的删除确认')
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
                if command in {'/sessions', '/resume'}:
                    self.open_palette('sessions', argument)
                elif command == '/models':
                    self.open_palette('models', argument)
                elif command == '/permissions':
                    self.open_palette('permissions', argument)
                elif command in {'/delete', '/del'}:
                    self.request_delete(argument)
                elif command == '/cleanup':
                    self.request_cleanup(argument)
                elif command == '/details':
                    if argument not in {'', 'on', 'off'}:
                        raise ValueError('用法：/details [on|off]')
                    self.show_details = not self.show_details if not argument else argument == 'on'
                    self.transcript = Transcript(show_details=self.show_details)
                    self._view_key = None
                    self.notice = '执行详情已显示。' if self.show_details else '执行详情已隐藏。'
                elif command == '/new':
                    from ai_agent_startup.core.session_service import open_session
                    self.select(open_session(self.manager, title=argument or '新会话').id)
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
                elif command == '/platform':
                    from ai_agent_startup.core.platform_info import platform_info
                    self.notice = json.dumps(platform_info(), ensure_ascii=False, indent=2)
                elif command in {'/usage', '/trace'}:
                    from ai_agent_startup.core.observability import usage_report, trace_report
                    report = usage_report(session.record) if command == '/usage' else trace_report(session.record, argument)
                    self.notice = json.dumps(report, ensure_ascii=False, indent=2)
                elif command == '/image':
                    self.image_command(session, argument)
                elif command == '/plan':
                    self.plan_command(session, argument)
                elif command == '/tasks':
                    queue = self.manager.agent_tasks
                    if queue is None:
                        raise ValueError('尚无已加载的子任务；可让 Agent 用 create_session 创建独立任务')
                    if argument:
                        verb, _, identifier = argument.partition(' ')
                        if verb != 'cancel':
                            raise ValueError('用法：/tasks 或 /tasks cancel ID')
                        queue.cancel(session.id, identifier.strip())
                    rows = [{'id': row['id'], 'child_id': row['child_id'], 'status': row['status'], 'error': row['error']} for row in queue.records.values() if row['owner'] == session.id][-20:]
                    self.notice = json.dumps(rows, ensure_ascii=False, indent=2)
                elif command == '/config':
                    from ai_agent_startup.core.session_limits import effective_limits
                    if argument:
                        self.manager.set_limits(session, json.loads(argument) if argument.startswith('{') else argument)
                    self.notice = json.dumps(effective_limits(session.record), ensure_ascii=False, indent=2)
                elif command == '/policy':
                    if argument:
                        argument = {'full': 'full_access', 'full access': 'full_access'}.get(argument, argument)
                        self.manager.set_permission_policy(session, None if argument == 'default' else argument)
                    policy = session.record.get('permission_policy') or config.TOOL_PERMISSION_POLICY
                    self.notice = f'当前权限模式：{POLICY_LABELS.get(policy, policy)} ({policy})；预先确认规则 {len(config.TOOL_PERMISSION_RULES)} 条。'
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
                    if self.pending_cleanup is not None and self.pending_cleanup[0] == session.id:
                        if command == '/yes':
                            self.cleanup_command()
                        else:
                            self.pending_cleanup = None
                            self.notice = '已取消数据清理。'
                    elif self.pending_delete == session.id:
                        if command == '/yes':
                            self.delete_command(session)
                        else:
                            self.pending_delete = None
                            self.notice = '已取消会话删除。'
                    elif session.confirmation:
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
                    if available:
                        self.select(available[-1])
                    else:
                        from ai_agent_startup.core.session_service import open_session
                        self.select(open_session(self.manager).id)
                elif command == '/where':
                    self.notice = '文件工作区：' + str(self.manager.workspace(session)) + '\n会话记录：' + str(self.store.directory(session.id))
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
            accepted = False
        self.render(force=True)
        return accepted

    async def run(self):
        try:
            with patch_stdout(raw=False):
                await self.app.run_async()
        finally:
            self._render_closed = True
            if self._refresh_handle is not None:
                self._refresh_handle.cancel()
                self._refresh_handle = None
            await self.drain_render()
            if self._delete_jobs:
                for session in list(self.manager.sessions.values()):
                    self.manager.cancel(session)
                for task in list(self.manager.fork_tasks):
                    task.cancel()
                await asyncio.gather(*list(self._delete_jobs), return_exceptions=True)
            await self.manager.shutdown()
            if self._ui_jobs:
                await asyncio.gather(*list(self._ui_jobs), return_exceptions=True)
            await llm.close_clients()


def main():
    parser = argparse.ArgumentParser(description='多会话 Agent CLI：自动保存、独立工作区、文件记忆')
    parser.add_argument('--state-dir', type=Path, default=config.AGENT_STATE_DIR, help='持久化目录')
    parser.add_argument('--platform', action='store_true', help='只显示平台能力，不创建状态或调用模型')
    parser.add_argument('--list', action='store_true', help='列出已保存会话后退出')
    parser.add_argument('--prompt', help='无 TTY 单次执行；默认拒绝待确认操作')
    parser.add_argument('--json', action='store_true', help='headless 输出单个 JSON 对象')
    parser.add_argument('--session', help='headless 继续已有会话 ID 或唯一前缀')
    parser.add_argument('--policy', choices=['readonly', 'standard', 'trusted', 'smart', 'full_access'], help='headless 显式权限档位，默认 standard 并拒绝人工确认')
    args = parser.parse_args()
    if args.platform:
        from ai_agent_startup.core.platform_info import platform_info
        print(json.dumps(platform_info(), ensure_ascii=False, indent=2))
        return
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
            from ai_agent_startup.core.headless import run_headless, RedactingFormatter
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
