"""有限的安全文本排版；不产生链接、图片或可加载的资源。"""
from __future__ import annotations

import html
import re


def _inline(text: str) -> str:
    # 先分离代码，再逐段转义。用户内容从不进入 HTML 属性或原始标记。
    parts = re.split(r'(`[^`\n]+`)', text)
    result = []
    for part in parts:
        if len(part) > 1 and part.startswith('`') and part.endswith('`'):
            result.append('<code style="font-family:monospace;color:#acd4ef;background-color:#29313c">'
                          + html.escape(part[1:-1]) + '</code>')
        else:
            escaped = html.escape(part)
            result.append(re.sub(r'\*\*([^*\n]+)\*\*', r'<strong>\1</strong>', escaped))
    return ''.join(result)


def render_safe_text(content: str) -> str:
    """支持段落、标题、列表与代码；流式未闭合围栏仍完整显示代码。"""
    blocks: list[str] = []
    paragraph: list[str] = []
    listing: list[str] = []
    code: list[str] | None = None
    fence = ''
    language = ''

    def flush() -> None:
        if paragraph:
            blocks.append('<p style="margin:0 0 12px;line-height:155%;white-space:pre-wrap">' + '<br/>'.join(_inline(line) for line in paragraph) + '</p>')
            paragraph.clear()
        if listing:
            blocks.append('<ul style="margin:3px 0 14px;padding-left:22px">'
                          + ''.join('<li>' + _inline(line) + '</li>' for line in listing) + '</ul>')
            listing.clear()

    def flush_code() -> None:
        label = '<p style="color:#8796aa;font-size:11px;margin:0 0 8px">' + html.escape(language) + '</p>' if language else ''
        blocks.append('<table width="100%" cellspacing="0" cellpadding="13" bgcolor="#171c24"><tr><td>'
                      + label + '<pre style="font-family:monospace;font-size:13px;color:#d7e2f0;margin:0;white-space:pre-wrap">'
                      + html.escape('\n'.join(code or [])) + '</pre></td></tr></table><p style="margin:0;font-size:5px">&nbsp;</p>')

    for line in str(content).split('\n'):
        marker = re.match(r'^\s{0,3}(`{3,}|~{3,})(.*)$', line)
        if code is not None:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                flush_code()
                code = None
            else:
                code.append(line)
            continue
        if marker:
            flush()
            fence, language, code = marker[1], marker[2].strip(), []
            continue
        heading = re.match(r'^\s{0,3}#{1,6}\s+(.+)$', line)
        bullet = re.match(r'^\s*(?:[-*+]\s+|\d+[.)]\s+)(.+)$', line)
        if heading:
            flush()
            blocks.append('<h3 style="font-size:17px;font-weight:600;margin:18px 0 10px;color:#f0f3f8">'
                          + _inline(heading[1]) + '</h3>')
        elif bullet:
            if paragraph:
                flush()
            listing.append(bullet[1])
        elif not line.strip():
            flush()
        else:
            if listing:
                flush()
            paragraph.append(line)
    if code is not None:
        flush_code()
    flush()
    return ''.join(blocks)


def message_html(name: str, content: str, *, kind: str = 'assistant') -> str:
    """所有动态文本均由转义器生成，颜色与结构只取自本地常量。"""
    label = html.escape(str(name))
    formatted = render_safe_text(str(content))
    if kind == 'user':
        panel = ('<table align="right" width="88%" cellspacing="0" cellpadding="16" bgcolor="#28323f">'
                 '<tr><td><p style="color:#aec8ef;font-size:11px;font-weight:600;margin:0 0 9px">'
                 + label + '</p>' + formatted + '</td></tr></table>')
    elif kind in {'tool', 'detail'}:
        panel = ('<table width="100%" cellspacing="0" cellpadding="13" bgcolor="#22272f"><tr><td>'
                 '<p style="color:#96a4b7;font-size:11px;font-weight:600;margin:0 0 9px">'
                 + label + '</p>' + formatted + '</td></tr></table>')
    else:
        panel = ('<p style="color:#8fcbb8;font-size:12px;font-weight:600;margin:0 0 12px">'
                 + label + '</p>' + formatted)
    return ('<table width="100%" cellspacing="0" cellpadding="0"><tr><td>' + panel
            + '</td></tr><tr><td height="24"></td></tr></table>')
