"""控制台入口：`ai-agent-startup` / `python -m agent_frontend.main`。"""
import sys


def main() -> None:
    try:
        # 导入配置即完成 .env 加载；配置错误在启动阶段以可读信息退出。
        from agent_core import config  # noqa: F401
        from agent_frontend.cli.app import main as cli_main
    except ValueError as exc:
        print(f'配置错误：{exc}', file=sys.stderr)
        raise SystemExit(2) from None
    cli_main()


if __name__ == '__main__':
    main()
