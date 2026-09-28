"""显式预先确认规则；命令只匹配单个简单调用的 argv 前缀。"""
from pathlib import PurePath
import shlex
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PermissionRule(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tool: Literal['create_file', 'update_file', 'edit_file', 'append_file', 'delete_file', 'mkdir', 'copy', 'move', 'run_command']
    path_prefix: str | None = None
    command_prefix: list[str] | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def check_scope(self):
        if self.tool == 'run_command':
            if self.path_prefix is not None or not self.command_prefix or any(not value.strip() for value in self.command_prefix):
                raise ValueError('命令规则必须提供非空 argv 前缀，不能按文件路径授权命令')
        else:
            path = PurePath(self.path_prefix or '')
            if (not self.path_prefix or path.is_absolute() or '..' in path.parts
                    or self.path_prefix.startswith('@') or self.command_prefix is not None):
                raise ValueError('文件规则必须提供工作区相对路径前缀')
        return self


def simple_command_tokens(command: str) -> list[str] | None:
    # Shell 插值、重定向、控制符和多行都回退逐次确认，不能套用前缀规则。
    if any(char in command for char in ('$', '`', '\n', '\r', '\x00')):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|<>()')
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    if not tokens or any(token and all(char in ';&|<>()' for char in token) for token in tokens):
        return None
    return tokens
