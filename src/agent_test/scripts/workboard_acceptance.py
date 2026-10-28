"""固定外部验收；测试源码不提供给被测 Agent，仅在其隔离工作区执行。"""
import argparse
import json
from pathlib import Path
import shlex
import threading


CHECKS = r'''
import json, os, subprocess, tempfile, unittest
try:
    from workboard import Board
except Exception as exc:
    print(json.dumps({'import_error': type(exc).__name__, 'detail': str(exc)[:400]}))
    raise SystemExit(1)

class Acceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.temp.name, 'board.db')
        self.board = Board(self.db)
        self.project = self.board.create_project('Acceptance')
        self.pid = self.project['id']

    def tearDown(self):
        self.board.close()
        self.temp.cleanup()

    def task(self, title='First', **kw):
        return self.board.create_task(self.pid, title, **kw)

    def test_project(self): self.assertEqual(self.project['name'], 'Acceptance')
    def test_integer_id(self): self.assertIsInstance(self.task()['id'], int)
    def test_default_status(self): self.assertEqual(self.task()['status'], 'todo')
    def test_default_priority(self): self.assertEqual(self.task()['priority'], 'normal')
    def test_empty_project(self):
        with self.assertRaises(ValueError): self.board.create_project(' ')
    def test_empty_title(self):
        with self.assertRaises(ValueError): self.task(' ')
    def test_bad_priority(self):
        with self.assertRaises(ValueError): self.task(priority='urgent')
    def test_bad_project(self):
        with self.assertRaises(ValueError): self.board.create_task(999999, 'bad')
    def test_quote(self): self.assertEqual(self.task("O'Reilly")['title'], "O'Reilly")
    def test_literal_search(self):
        self.task('100% complete'); self.task('100X complete')
        self.assertEqual(len(self.board.search(self.pid, '%')), 1)
    def test_update_title(self):
        t=self.task(); self.assertEqual(self.board.update_task(t['id'], title='Changed')['title'], 'Changed')
    def test_bad_status(self):
        with self.assertRaises(ValueError): self.board.update_task(self.task()['id'], status='missing')
    def test_update_atomic(self):
        t=self.task()
        with self.assertRaises(ValueError): self.board.update_task(t['id'], title='Changed', status='missing')
        self.assertEqual(self.board.get_task(t['id'])['title'], 'First')
    def test_negative_offset(self):
        with self.assertRaises(ValueError): self.board.list_tasks(self.pid, offset=-1)
    def test_negative_limit(self):
        with self.assertRaises(ValueError): self.board.list_tasks(self.pid, limit=-1)
    def test_page(self):
        self.task('A'); self.task('B'); self.assertEqual(len(self.board.list_tasks(self.pid, limit=1, offset=1)), 1)
    def test_dependency(self):
        a=self.task(); b=self.task('B', depends_on=[a['id']]); self.assertIn(a['id'],b['depends_on'])
    def test_dependency_cross_project(self):
        other=self.board.create_project('Other'); a=self.board.create_task(other['id'],'other')
        with self.assertRaises(ValueError): self.task(depends_on=[a['id']])
    def test_dependency_missing(self):
        with self.assertRaises(ValueError): self.task(depends_on=[999999])
    def test_dependency_self(self):
        a=self.task()
        with self.assertRaises(ValueError): self.board.update_task(a['id'],depends_on=[a['id']])
    def test_dependency_cycle(self):
        a=self.task(); b=self.task('B',depends_on=[a['id']])
        with self.assertRaises(ValueError): self.board.update_task(a['id'],depends_on=[b['id']])
    def test_done_blocked(self):
        a=self.task(); b=self.task('B',depends_on=[a['id']])
        with self.assertRaises(ValueError): self.board.update_task(b['id'],status='done')
    def test_done_allowed(self):
        a=self.task(); b=self.task('B',depends_on=[a['id']]); self.board.update_task(a['id'],status='done')
        self.assertEqual(self.board.update_task(b['id'],status='done')['status'],'done')
    def test_comments(self):
        a=self.task(); self.board.add_comment(a['id'],'comment'); self.assertEqual(self.board.comments(a['id'])[0]['text'],'comment')
    def test_labels(self):
        a=self.task(); self.board.add_label(a['id'],'tag'); self.board.add_label(a['id'],'tag')
        self.assertEqual(self.board.get_task(a['id'])['labels'].count('tag'),1)
    def test_history(self):
        a=self.task(); self.board.update_task(a['id'],title='Changed'); self.assertTrue(self.board.history(a['id']))
    def test_summary(self):
        self.task(); self.assertEqual(self.board.summary(self.pid)['todo'],1)
    def test_persistence(self):
        a=self.task(); self.board.add_comment(a['id'],'keep'); self.board.add_label(a['id'],'keep'); self.board.close(); self.board=Board(self.db)
        self.assertEqual(self.board.get_task(a['id'])['title'],'First'); self.assertTrue(self.board.comments(a['id'])); self.assertIn('keep',self.board.get_task(a['id'])['labels'])
    def test_roundtrip(self):
        a=self.task(); self.task('B',depends_on=[a['id']]); new=self.board.import_project(self.board.export_project(self.pid))
        ts=self.board.list_tasks(new['id']); self.assertEqual(len(ts),2)
        self.assertTrue(all(t['id']!=a['id'] for t in ts)); self.assertTrue(any(t['depends_on'] for t in ts))
    def test_bad_import(self):
        self.task(); before=self.board.export_project(self.pid)
        with self.assertRaises(ValueError): self.board.import_project({'project':{'name':'Bad'},'tasks':[{'title':''}]})
        self.assertEqual(self.board.export_project(self.pid),before)
    def test_cli_init(self):
        result=subprocess.run(['python3','-m','workboard','--db',self.db,'init'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr); json.loads(result.stdout)
    def test_cli_error(self):
        result=subprocess.run(['python3','-m','workboard','--db',self.db,'task-add','999999','Bad'],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0); self.assertTrue(result.stderr)

suite=unittest.defaultTestLoader.loadTestsFromTestCase(Acceptance)
result=unittest.TextTestRunner(verbosity=2).run(suite)
print(json.dumps({'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors)}))
raise SystemExit(not result.wasSuccessful())
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    import tempfile
    if not root.is_relative_to(Path(tempfile.gettempdir()).resolve()) or not root.name.startswith('agent-project-validation-'):
        parser.error('仅可验收隔离实验目录')
    from agent_core import config
    from agent_core.tools.command import run_command
    from agent_core.tools.sandbox import ToolContext, tool_context
    config.TOOL_MAX_OUTPUT = 20000
    verified = 0
    failed = False
    for workspace in (root / 'work').iterdir():
        if not workspace.is_dir() or workspace.is_symlink():
            continue
        context = ToolContext(workspace, root / 'acceptance_audit.jsonl', lambda *args: True,
                              threading.Event(), permission_policy='standard')
        with tool_context(context):
            output = run_command('python3 -c ' + shlex.quote(CHECKS), reason='独立验收生成项目，不联网')
            own = run_command('python3 -m unittest discover -s tests -v', reason='复核生成项目自带测试，不联网')
        (root / 'acceptance.txt').write_text(output + '\n\nPROJECT TESTS\n' + own, encoding='utf-8')
        print(output[-1800:])
        print(own[-500:])
        verified += 1
        # 拦截、超时、取消与缺少退出码均不能视为验收成功。
        failed = failed or any(value.splitlines()[0:1] != ['[退出码] 0'] for value in (output, own))
    return int(failed or verified == 0)


if __name__ == '__main__':
    raise SystemExit(main())
