"""启动多会话终端，配置错误以可读信息退出。"""
import sys

if __name__ == '__main__':
    try:
        import config
        from core.cli import main
    except ValueError as exc:
        print(f'配置错误：{exc}', file=sys.stderr)
        raise SystemExit(2) from None
    main()
