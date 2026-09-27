"""Append-only transcript formatting with bounded pages and no discarded history."""
import json


class Transcript:
    PAGE_LINES = 180
    LINE_CHARS = 160

    def __init__(self):
        self.messages = None
        self.count = 0
        self.lines = []
        self.tool_names = {}
        self.follow = True
        self.start = 0

    @classmethod
    def text_lines(cls, text):
        result = []
        for line in str(text).split('\n'):
            if line:
                result.extend(line[i:i + cls.LINE_CHARS] for i in range(0, len(line), cls.LINE_CHARS))
            else:
                result.append('')
        return result

    def sync(self, messages):
        if messages is not self.messages or len(messages) < self.count:
            self.messages, self.count, self.lines, self.tool_names = messages, 0, [], {}
        for number in range(self.count, len(messages)):
            message = messages[number]
            role = message['role']
            if role == 'system':
                continue
            label = {'user': '你', 'assistant': 'Agent', 'tool': '工具结果'}.get(role, role)
            if role == 'tool':
                label += ' · ' + self.tool_names.get(message.get('tool_call_id'), '')
            self.lines += ['', f'━━ {label} · 消息 {number} ━━']
            self.lines += self.text_lines(message.get('content') or '')
            for call in message.get('tool_calls', []):
                name = call['function']['name']
                self.tool_names[call['id']] = name
                self.lines += ['┌ 工具调用：' + name]
                try:
                    arguments = json.loads(call['function']['arguments'])
                    if isinstance(arguments, dict):
                        for key, value in arguments.items():
                            self.lines += self.text_lines(f'{key}: {value}')
                    else:
                        self.lines += self.text_lines(arguments)
                except (ValueError, KeyError):
                    self.lines += self.text_lines(call['function'].get('arguments', ''))
        self.count = len(messages)

    def page(self, partial='', extra=''):
        tail = (['', '━━ Agent · 生成中 ━━'] + self.text_lines(partial)) if partial else []
        if extra:
            tail += ['', *self.text_lines(extra)]
        total = len(self.lines) + len(tail)
        if self.follow:
            self.start = max(0, total - self.PAGE_LINES)
        self.start = max(0, min(self.start, max(0, total - self.PAGE_LINES)))
        end = min(total, self.start + self.PAGE_LINES)
        body = self.lines[self.start:min(end, len(self.lines))]
        if end > len(self.lines):
            body += tail[max(0, self.start - len(self.lines)):end - len(self.lines)]
        return '\n'.join(body), (self.start, end, total)

    def older(self):
        self.follow = False
        self.start = max(0, self.start - self.PAGE_LINES + 10)

    def newer(self):
        self.start += self.PAGE_LINES - 10

    def top(self):
        self.follow, self.start = False, 0

    def bottom(self):
        self.follow = True
