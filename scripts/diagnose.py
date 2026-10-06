"""Bounded engineering probes. --live explicitly uses the configured paid API.

Run from the repository root: uv run python -m scripts.diagnose --live --runs 3
Outputs remain in ignored .cache/reports; real user sessions are never modified.
"""
import argparse
import asyncio
import json
import logging
import tempfile
import time
import copy
import hashlib
import shutil
from pathlib import Path

from ai_agent_startup import config
from ai_agent_startup.core import llm
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore


async def run_live(output: Path, runs: int, effort: str, thinking: str, replay: Path | None = None):
    config.REASONING_EFFORT = effort
    config.THINKING_MODE = thinking
    config.RAG_ASSESS = False
    rows = []
    source = SessionStore.read_record(replay) if replay else None
    with tempfile.TemporaryDirectory(prefix='agent-diagnose-') as temporary:
        root = Path(temporary)
        store = SessionStore(root / 'state', root / 'crud_tests')
        manager = SessionManager(store)
        async def exercise(label, prompt, filename):
            session = manager.create(label)
            if source:
                store.directory(source['id'])  # Validate ID before reading the source workspace.
                user_index = max(i for i, m in enumerate(source['messages']) if m['role'] == 'user')
                session.record['messages'] = copy.deepcopy(source['messages'][:user_index])
                prompt = source['messages'][user_index]['content']
                shutil.copytree(config.SANDBOX_DIR / source['id'], store.workspace(session.id),
                                dirs_exist_ok=True, symlinks=True)
            workspace = store.workspace(session.id)
            before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in workspace.glob('*.html') if not p.is_symlink()}
            manager.submit(session, prompt)
            latencies = []
            async def service():
                while session.busy:
                    start = time.monotonic()
                    if session.confirmation:
                        manager.decide(session, True)
                    await asyncio.sleep(.01)
                    latencies.append(time.monotonic() - start)
            helper = asyncio.create_task(service())
            try:
                await asyncio.wait_for(asyncio.shield(session.task), 240 if source else 120)
            except TimeoutError:
                manager.cancel(session)
                await session.task
            await helper
            path = store.workspace_path(session.id) / filename
            content = path.read_text() if path.exists() else ''
            row = {'task': label, 'status': session.record['status'],
                   'error': session.record.get('error'), 'file_chars': len(content),
                   'heartbeat_max_ms': round(max(latencies, default=0)*1000, 2),
                   **session.record.get('last_run', {})}
            if filename.endswith('.html'):
                row['html_present'] = '<html' in content.lower() and '<style' in content.lower()
            else:
                row['file_content_correct'] = content.strip() == 'hello'
            if source:
                row.pop('file_chars', None)
                row.pop('html_present', None)
                row['files'] = {p.name: {'chars': len(p.read_text()),
                    'changed': hashlib.sha256(p.read_bytes()).hexdigest() != before.get(p.name),
                    'has_closing_html': '</html>' in p.read_text().lower()}
                    for p in workspace.glob('*.html') if not p.is_symlink()}
                shutil.copytree(workspace, output / label, dirs_exist_ok=True, symlinks=True)
                (output / label / 'session.json').write_text(json.dumps(session.record, ensure_ascii=False, indent=2))
            # Keep generated artifacts separate from benchmark inputs and user workspaces.
            if not source:
                artifact = output / (label + '-' + filename)
                artifact.write_text(content)
            rows.append(row)
            (output / 'agent-live.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2))
            print(json.dumps(row, ensure_ascii=False), flush=True)
        try:
            for number in range(runs):
                await exercise(f'drawing-{number}',
                    '用HTML+CSS画一幅晓山瑞希和宵崎奏在田野间骑自行车的画面，保存为 index.html。', 'index.html')
            if not source:
                await asyncio.gather(*(exercise(f'concurrent-crud-{i}',
                    '请把 hello 保存到 hello.txt，再用工具读回验证。', 'hello.txt') for i in range(2)))
        finally:
            await manager.shutdown()
            store.close()
            if llm._client:
                await llm._client.close()
                llm._client = None
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='allow requests to configured model service')
    parser.add_argument('--runs', type=int, choices=range(1, 6), default=3)
    parser.add_argument('--effort', default='low')
    parser.add_argument('--thinking', choices=['auto', 'enabled', 'disabled'], default='auto')
    parser.add_argument('--read-timeout', type=float)
    parser.add_argument('--replay-session', type=Path, help='copy this session and its workspace; replay its last user request without changing originals')
    parser.add_argument('--output', type=Path, default=Path('.cache/reports/system-diagnostics/live'))
    args = parser.parse_args()
    if not args.live:
        parser.error('live probes require --live; run pytest for offline fault injection')
    logging.basicConfig(level=logging.WARNING)
    if args.read_timeout is not None:
        config.SESSION_TIMEOUT = args.read_timeout
    args.output.mkdir(parents=True, exist_ok=True)
    asyncio.run(run_live(args.output, args.runs, args.effort, args.thinking, args.replay_session))


if __name__ == '__main__':
    main()
