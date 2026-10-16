"""用公开小依赖实测环境准备；工作区、审计、下载缓存全部放临时目录。"""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import shutil
from threading import Event
import time

from ai_agent_startup import config
from ai_agent_startup.tools.command import run_command
from ai_agent_startup.tools.environment import inspect_environment, prepare_project_environment
from ai_agent_startup.tools.sandbox import ToolContext, tool_context


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offline-artifacts', type=Path,
                        help='预先校验来源的 is-number tgz 和 idna wheel 所在目录；本脚本不在宿主下载/执行代码')
    args = parser.parse_args()
    # 仅覆盖本进程配置；不修改 .env、用户会话或现有工作区。
    node_prefix = Path.home() / '.nvm/versions/node/v24.21.0'
    uv = Path.home() / '.local/bin/uv'
    if not (node_prefix / 'bin/node').is_file() or not uv.is_file():
        raise SystemExit('本机验证所需的已安装 Linux Node/uv 不存在；未下载或回退宿主执行')
    config.COMMAND_RUNTIME_PATHS = {'node': str(node_prefix), 'uv': str(uv)}
    config.COMMAND_NODE_MEMORY_MB = 2048
    config.ENABLE_ENVIRONMENT_PREPARE = True
    config.COMMAND_NETWORK = 'allowlist'
    config.COMMAND_NETWORK_ALLOWLIST = ['registry.npmjs.org', 'pypi.org', 'files.pythonhosted.org']
    report = {'kind': 'real_sandbox_environment_smoke', 'time': time.time(),
              'mode': 'offline_artifacts' if args.offline_artifacts else 'network_allowlist',
              'public_dependencies': ['is-number@7.0.0', 'idna==3.10'],
              'limits': {name: getattr(config, name) for name in
                         ('COMMAND_MEMORY_MB', 'COMMAND_NODE_MEMORY_MB', 'COMMAND_CPU_SECONDS',
                          'COMMAND_FILE_MB', 'COMMAND_TIMEOUT', 'COMMAND_NETWORK_MAX_BYTES',
                          'COMMAND_NETWORK_MAX_CONNECTIONS', 'COMMAND_NETWORK_TIMEOUT')},
              'stages': {}}
    with tempfile.TemporaryDirectory(prefix='agent-environment-validation-') as directory:
        base = Path(directory)
        root = base / 'work'
        root.mkdir()
        config.AGENT_STATE_DIR = base / 'state'
        config.AUDIT_LOG = base / 'audit.jsonl'
        approvals = []
        context = ToolContext(root=root, audit_path=config.AUDIT_LOG, cancelled=Event(),
            permission_policy='standard', confirm=lambda prompt, timeout: approvals.append(prompt) or True)
        with tool_context(context):
            report['inspection'] = json.loads(inspect_environment())
            report['stages']['node_default_limit'] = run_command('node -e "console.log(1+2)"')
            js = root / 'node-project'
            js.mkdir()
            (js / 'package.json').write_text(json.dumps({'name': 'environment-smoke',
                'version': '1.0.0', 'private': True, 'scripts': {'test': 'node --test test.cjs',
                'build': 'node build.cjs'}, 'dependencies': {'is-number': '7.0.0'}}))
            if args.offline_artifacts:
                inputs = js / 'inputs'
                inputs.mkdir()
                shutil.copyfile(args.offline_artifacts / 'is-number-7.0.0.tgz', inputs / 'is-number-7.0.0.tgz')
                package = json.loads((js / 'package.json').read_text())
                package['dependencies']['is-number'] = 'file:inputs/is-number-7.0.0.tgz'
                (js / 'package.json').write_text(json.dumps(package))
            (js / 'test.cjs').write_text('''const test = require('node:test');
const assert = require('node:assert/strict');
const isNumber = require('is-number');
test('public package accepts finite numbers and numeric strings', () => {
 assert.equal(isNumber(42), true); assert.equal(isNumber('42'), true);
});
test('public package rejects objects and infinity', () => {
 assert.equal(isNumber({}), false); assert.equal(isNumber(Infinity), false);
});
''')
            (js / 'build.cjs').write_text("const fs=require('node:fs');fs.mkdirSync('dist',{recursive:true});fs.copyFileSync('test.cjs','dist/test.cjs');console.log('build completed');")
            report['stages']['npm_install'] = prepare_project_environment('npm', project_dir='node-project', network=not bool(args.offline_artifacts),
                reason='在临时项目实际下载公开小依赖并验证沙箱安装能力')
            report['stages']['npm_build_test'] = run_command('cd node-project && npm run build && npm test', runtime_profile='node')
            py = root / 'python-project'
            py.mkdir()
            (py / 'requirements.txt').write_text('idna==3.10\n')
            if args.offline_artifacts:
                inputs = py / 'inputs'
                inputs.mkdir()
                shutil.copyfile(args.offline_artifacts / 'idna-3.10-py3-none-any.whl', inputs / 'idna-3.10-py3-none-any.whl')
                (py / 'requirements.txt').write_text('inputs/idna-3.10-py3-none-any.whl\n')
                report['artifact_sha256'] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in [js / 'inputs/is-number-7.0.0.tgz', inputs / 'idna-3.10-py3-none-any.whl']}
            (py / 'test_idna.py').write_text('''import unittest
import idna
class PublicPackageTests(unittest.TestCase):
    def test_public_unicode_domain_roundtrip(self):
        self.assertEqual(idna.encode('bücher.de'), b'xn--bcher-kva.de')
        self.assertEqual(idna.decode(b'xn--bcher-kva.de'), 'bücher.de')
    def test_invalid_domain_is_rejected(self):
        with self.assertRaises(idna.IDNAError):
            idna.encode('invalid domain.example')
''', encoding='utf-8')
            report['stages']['uv_install'] = prepare_project_environment('uv', project_dir='python-project', network=not bool(args.offline_artifacts),
                reason='在临时受管虚拟环境实际下载公开wheel并运行其功能测试')
            report['stages']['python_test'] = run_command('cd python-project && .agent-env/python/bin/python -m unittest discover -v')
            report['approval_count'] = len(approvals)
            audit_records = [json.loads(line) for line in config.AUDIT_LOG.read_text().splitlines()]
            report['audit_events'] = [row['event'] for row in audit_records]
            report['network_events'] = [{key: row[key] for key in ('event', 'host', 'error', 'connected', 'peer') if key in row}
                                        for row in audit_records if row['event'].startswith('network_')]
    checks = ('npm_install', 'npm_build_test', 'uv_install', 'python_test')
    report['passed'] = all(report['stages'][name].startswith('[退出码] 0\n') for name in checks)
    output_dir = config.PROJECT_ROOT / '.cache/reports'
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / ('environment-validation-' + str(time.time_ns()) + '.json')
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': report['passed'], 'report': str(output), 'stages': report['stages']}, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
