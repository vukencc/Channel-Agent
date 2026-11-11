"""运行中的 Flow 只读视图。"""
from __future__ import annotations

import html
import json
from typing import Any

from PySide6.QtCore import Qt
from .flow import _status_text

from PySide6.QtWidgets import (QLabel, QListWidget, QListWidgetItem,
                               QTextEdit, QSplitter, QVBoxLayout, QWidget)


def _tool_lines(tools: list[dict[str, Any]], indent: str = '') -> list[str]:
    lines = []
    for tool in tools:
        kind = '（基础锁定）' if tool.get('required') else '（自定义）' if tool.get('custom') else ''
        description = str(tool.get('description') or '').strip() or '暂无描述'
        lines.append(f"{indent}{tool.get('name', '')}{kind}：{description}")
    return lines


class WorkingPanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._rows: list[dict[str, Any]] = []
        self._signature = ''
        self._selection: tuple[str, str] | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        heading = QLabel('正在运行的 Flow')
        heading.setObjectName('title')
        layout.addWidget(heading)
        self.empty = QLabel('当前没有运行中的 Flow')
        self.empty.setObjectName('muted')
        layout.addWidget(self.empty)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.listing = QListWidget()
        self.listing.setMouseTracking(True)
        self.listing.itemClicked.connect(self._select)
        self.listing.itemActivated.connect(self._select)
        self.splitter.addWidget(self.listing)
        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(12, 0, 0, 0)
        label = QLabel('节点详情')
        label.setObjectName('section')
        detail_layout.addWidget(label)
        self.details = QTextEdit()
        self.details.setAcceptRichText(False)
        self.details.setReadOnly(True)
        self.details.setPlaceholderText('选择节点查看详情')
        detail_layout.addWidget(self.details)
        self.splitter.addWidget(detail)
        self.splitter.setSizes([550, 350])
        layout.addWidget(self.splitter, 1)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.splitter.setOrientation(Qt.Orientation.Vertical if self.width() < 650 else Qt.Orientation.Horizontal)

    def set_runs(self, rows: list[dict[str, Any]]) -> None:
        signature = json.dumps(rows, ensure_ascii=False, sort_keys=True)
        if signature == self._signature:
            return
        self._signature = signature
        self._rows = rows
        scroll = self.listing.verticalScrollBar().value()
        self.listing.clear()
        selected = None
        for run in rows:
            header = QListWidgetItem(f"{run.get('title') or 'Flow'}  ·  {_status_text(run.get('status'))}")
            header.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.listing.addItem(header)
            for node in run.get('nodes') or []:
                key = (str(run.get('run_id')), str(node.get('id')))
                item = QListWidgetItem(f"    {node.get('title') or node.get('id')}  ·  {_status_text(node.get('status'))}")
                item.setData(Qt.ItemDataRole.UserRole, key)
                summary = node.get('task_summary') or node.get('task') or '暂无任务概要'
                model = node.get('model') or node.get('model_profile') or '未指定'
                item.setToolTip('<qt>' + html.escape(f"子 Agent：{node.get('active_subagent_count', 0)} 活跃 / {node.get('subagent_count', 0)} 总数\n任务：{summary}\n模型：{model}").replace('\n', '<br>') + '</qt>')
                self.listing.addItem(item)
                if key == self._selection:
                    selected = item
        self.empty.setVisible(not rows)
        self.splitter.setVisible(bool(rows))
        self.listing.verticalScrollBar().setValue(scroll)
        if selected:
            self.listing.setCurrentItem(selected)
            self._show_detail(self._selection)
        else:
            self._selection = None
            self.details.clear()

    def _select(self, item: QListWidgetItem) -> None:
        key = item.data(Qt.ItemDataRole.UserRole)
        if key:
            self._selection = tuple(key)
            self._show_detail(self._selection)

    def _show_detail(self, key: tuple[str, str]) -> None:
        for run in self._rows:
            if str(run.get('run_id')) != key[0]:
                continue
            for node in run.get('nodes') or []:
                if str(node.get('id')) != key[1]:
                    continue
                fields = [('Flow', run.get('title')), ('运行 ID', run.get('run_id')),
                          ('节点', node.get('title')), ('状态', _status_text(node.get('status'))),
                          ('任务概要', node.get('task_summary')), ('任务', node.get('task')),
                          ('模型', node.get('model')), ('模型配置', node.get('model_profile')),
                          ('思考模式', node.get('thinking_mode')), ('推理力度', node.get('reasoning_effort')),
                          ('阶段', node.get('phase')), ('模型调用数', node.get('model_calls')),
                          ('工具调用数', node.get('tool_calls')), ('开始时间', node.get('started_at')),
                          ('会话 ID', node.get('session_id')),
                          ('子 Agent 数', node.get('subagent_count')), ('活跃子 Agent 数', node.get('active_subagent_count')),
                          ]
                lines = []
                children = node.get('subagents') or []
                total = node.get('subagent_count') or 0
                if total > len(children):
                    lines.append(f'子 Agent 详情显示 {len(children)} / {total}，优先显示活跃项。')
                for label, value in fields:
                    if value is not None and value != '':
                        if isinstance(value, (dict, list)):
                            value = json.dumps(value, ensure_ascii=False, indent=2)
                        lines.append(f'{label}：{value}')
                if 'tools' in node:
                    lines.append('\n'.join(['可调用工具：', *(_tool_lines(node.get('tools') or [], '  ') or ['  无'])]))
                for child in children:
                    child_fields = [('状态', _status_text(child.get('status'))),
                                    ('任务概要', child.get('task_summary')), ('任务', child.get('task')),
                                    ('模型', child.get('model')), ('阶段', child.get('phase')),
                                    ('ID', child.get('id'))]
                    child_lines = [f"子 Agent：{child.get('title') or child.get('id') or '未命名'}"]
                    child_lines.extend(f'  {label}：{value}' for label, value in child_fields
                                       if value is not None and value != '')
                    if 'tools' in child:
                        child_lines.extend(['  可调用工具：', *(_tool_lines(child.get('tools') or [], '    ') or ['    无'])])
                    lines.append('\n'.join(child_lines))
                validation = node.get('handoff_validation')
                if validation:
                    result = ('格式校验通过（不代表业务验收通过）' if validation.get('status') == 'passed'
                              else '校验失败：' + str(validation.get('message') or '格式不符合契约'))
                    lines.append(f'结构化格式校验：{result}')
                scroll = self.details.verticalScrollBar().value()
                self.details.setPlainText('\n\n'.join(lines))
                self.details.verticalScrollBar().setValue(scroll)
                return
