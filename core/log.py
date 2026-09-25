"""统一的日志配置。

注意区分两类输出：
- 用户可见的对话内容（AI 的回答）→ 走 stdout，用 print；
- 程序自身的诊断信息（工具调用、重试、错误）→ 走 logging。

只对本项目自己的 logger（core.*、tools.*）开启 DEBUG，
第三方库（openai/httpx/httpcore 等）一律压到 WARNING，避免刷屏。
"""
import logging

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"

# 本项目自己的包前缀
_APP_PREFIXES = ("core", "tools")

# 这些库日志极啰嗦，即使调试也不放行
_NOISY = ("httpx", "httpcore", "openai", "asyncio", "urllib3", "hpack")


def setup_logging(debug: bool = False) -> None:
    """在程序入口调用一次。"""
    logging.basicConfig(level=logging.INFO, format=_FORMAT, datefmt=_DATEFMT)

    app_level = logging.DEBUG if debug else logging.INFO
    for name in _APP_PREFIXES:
        logging.getLogger(name).setLevel(app_level)

    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """各模块用它拿 logger，例如 get_logger(__name__)。"""
    return logging.getLogger(name)
