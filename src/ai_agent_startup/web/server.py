"""显式启动 WebUI；仅监听本机，单进程持有状态锁。"""
import argparse
import ipaddress
import os
from pathlib import Path


def main():
    from ai_agent_startup import config
    parser = argparse.ArgumentParser(description='Agent Workspace 本地 WebUI')
    parser.add_argument('--host', default=os.getenv('WEB_UI_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.getenv('WEB_UI_PORT', '8765')))
    parser.add_argument('--state-dir', type=Path)
    args = parser.parse_args()
    try:
        local = args.host == 'localhost' or ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        local = False
    if not local or args.host not in {'localhost', '127.0.0.1', '::1'} or not 1 <= args.port <= 65535:
        parser.error('WebUI host 必须是回环地址，port 必须在 1..65535 内')
    try:
        import uvicorn
        from ai_agent_startup.web.app import create_app
    except ImportError:
        parser.error('请先运行 uv sync --locked --extra web 安装可选 WebUI 依赖')
    config.validate_runtime_config()
    hostname = '[' + args.host + ']' if ':' in args.host else args.host
    print(f'WebUI：http://{hostname}:{args.port}', flush=True)
    uvicorn.run(create_app(state_dir=args.state_dir), host=args.host, port=args.port,
                workers=1, access_log=False, proxy_headers=False, timeout_graceful_shutdown=30)


if __name__ == '__main__':
    main()
