"""显式预先确认规则；命令只匹配单个简单调用的 argv 前缀。"""
from pathlib import Path, PurePath
import re
import shlex
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PERMISSION_POLICIES = frozenset({'readonly', 'standard', 'trusted', 'smart', 'full_access'})


class OperationRisk(BaseModel):
    """本地确定性风险结论；模型提供的理由不能降低风险。"""
    level: Literal['low', 'high', 'unknown']
    reason: str
    requires_confirmation: bool


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


def _workspace_targets(paths: list[str], *, write: bool = False) -> list[Path]:
    from ai_agent_startup.tools.sandbox import SandboxError, resolve_path, sandbox_root
    root = sandbox_root()
    targets = []
    for path in paths:
        if path.startswith('@') and not path.startswith('@workspace/'):
            raise SandboxError('Smart 仅自动批准当前工作区内的操作')
        target = resolve_path(path, write=write)
        if not target.is_relative_to(root):
            raise SandboxError('路径不属于当前工作区')
        targets.append(target)
    return targets


def _readonly_command(command: str) -> bool:
    if any(char in command for char in ';&|<>()*?[]~#\\'):
        return False
    tokens = simple_command_tokens(command)
    if not tokens:
        return False
    executable, *arguments = tokens
    short_options = {
        'pwd': 'LP', 'ls': 'laAhFprdRtS1', 'head': 'qv', 'tail': 'qv',
        'cat': 'AbEenstTv', 'wc': 'clmwL',
    }
    long_options = {
        'pwd': {'--logical', '--physical'},
        'ls': {'--all', '--almost-all', '--directory', '--human-readable', '--reverse'},
        'head': {'--quiet', '--silent', '--verbose'},
        'tail': {'--quiet', '--silent', '--verbose'},
        'cat': {'--number', '--number-nonblank', '--squeeze-blank'},
        'wc': {'--bytes', '--chars', '--lines', '--words', '--max-line-length'},
    }
    if executable not in short_options:
        return False
    paths = []
    options = True
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if options and value == '--':
            options = False
        elif options and executable in {'head', 'tail'} and value in {'-n', '-c', '--lines', '--bytes'}:
            index += 1
            if index >= len(arguments) or not re.fullmatch(r'[0-9]+', arguments[index]):
                return False
        elif options and executable in {'head', 'tail'} and re.fullmatch(r'(?:-[nc]|--(?:lines|bytes)=)[0-9]+', value):
            pass
        elif options and value.startswith('--'):
            if value not in long_options[executable]:
                return False
        elif options and value.startswith('-') and value != '-':
            if any(char not in short_options[executable] for char in value[1:]):
                return False
        elif value != '-':
            if executable == 'pwd' or value.startswith('@'):
                return False
            paths.append(value)
        index += 1
    _workspace_targets(paths or ['.'])
    return True


def evaluate_operation_risk(action: str, detail: str = '', *, paths: list[str] | None = None,
                            command: str | None = None) -> OperationRisk:
    """评估实际动作、路径及 argv；自然语言描述不提供授权。"""
    from ai_agent_startup.tools.sandbox import SandboxError

    if action in {'delete_file', 'move', 'update_file', 'edit_file'}:
        return OperationRisk(level='high', reason='删除、移动或覆盖现有内容需要确认', requires_confirmation=True)
    if action == 'web_search':
        return OperationRisk(level='high', reason='查询内容将发送至外部服务', requires_confirmation=True)
    try:
        if action == 'run_command':
            if command is not None and _readonly_command(command):
                return OperationRisk(level='low', reason='已验证的工作区简单只读命令', requires_confirmation=False)
            return OperationRisk(level='unknown', reason='命令不属于已验证的简单只读 argv', requires_confirmation=True)
        if action in {'create_file', 'mkdir', 'copy', 'append_file'} and paths and len(paths) == 1:
            target, = _workspace_targets(paths, write=True)
            safe = target.is_file() if action == 'append_file' else not target.exists() and not target.is_symlink()
            if safe:
                return OperationRisk(level='low', reason='已验证的工作区新建或追加操作', requires_confirmation=False)
    except (OSError, ValueError, SandboxError):
        return OperationRisk(level='unknown', reason='无法验证工作区路径或命令风险', requires_confirmation=True)
    return OperationRisk(level='unknown', reason='操作缺少可验证的低风险范围', requires_confirmation=True)
