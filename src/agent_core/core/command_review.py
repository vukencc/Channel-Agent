"""仅供框架传递已绑定命令的动态工具确认展示信息。"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandReview:
    command: str
    name: str
    network: bool
    code: str
    arguments_json: str


_review = ContextVar('dynamic_command_review', default=None)


@contextmanager
def command_review(value: CommandReview):
    token = _review.set(value)
    try:
        yield
    finally:
        _review.reset(token)


def bound_review(command: str, network: bool | None = None) -> CommandReview | None:
    value = _review.get()
    if value is None or value.command != command or (network is not None and value.network != network):
        return None
    return value


def review_text(command: str, network: bool | None = None) -> str | None:
    value = bound_review(command, network)
    return format_review(value) if value is not None else None


def format_review(value: CommandReview) -> str:
    return (f'节点动态工具：{value.name}\n'
            f'请求联网：{"是" if value.network else "否"}\n'
            f'参数 JSON：\n{value.arguments_json}\n'
            f'Python 源码：\n{value.code}\n'
            f'原始执行命令：\n{value.command}')
