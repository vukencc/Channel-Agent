"""控制台入口：`ai-agent-startup` / `python -m ai_agent_startup.main`。"""
import sys


def main() -> None:
    try:
        # 导入配置即完成 .env 加载；配置错误在启动阶段以可读信息退出。
        from ai_agent_startup import config  # noqa: F401
        from ai_agent_startup.core.cli import main as cli_main
    except ValueError as exc:
        print(f'配置错误：{exc}', file=sys.stderr)
        raise SystemExit(2) from None
    cli_main()


if __name__ == '__main__':
    main()
