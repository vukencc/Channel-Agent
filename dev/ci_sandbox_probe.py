"""CI 运行器能力探测；隔离不可用时非零退出，不降级执行。"""
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.command import run_command
from tools.sandbox import ToolContext, tool_context


def main():
    with tempfile.TemporaryDirectory(prefix='agent-ci-probe-') as temporary:
        root = Path(temporary)
        with tool_context(ToolContext(root / 'work', root / 'audit.jsonl', lambda *_: True, threading.Event())):
            result = run_command('/bin/true')
        if '[退出码] 0' not in result:
            print('CI 环境不支持所需 Bubblewrap 命名空间/prlimit；真实沙箱测试不能运行。\n' + result, file=sys.stderr)
            raise SystemExit(2)
        print('Bubblewrap + prlimit 实际隔离执行通过。')


if __name__ == '__main__':
    main()
