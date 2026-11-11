"""独立动态客服验收器的受控 fixture；无模型请求，无真实执行证明。"""
import json
from pathlib import Path

import pytest

from agent_test.scripts.validate_dynamic_feedback import command_json, validate


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def wrapped(value):
    # command._execute 只在 stderr 非空时追加该段；Python print 保留末尾换行。
    return '[退出码] 0\n[stdout]\n' + json.dumps(value, ensure_ascii=False) + '\n'


@pytest.mark.parametrize('suffix', ['', '\n'])
def test_command_json_accepts_actual_zero_exit_without_stderr(suffix):
    assert command_json('[退出码] 0\n[stdout]\n{"id":"CF-002"}' + suffix) == {'id': 'CF-002'}


@pytest.mark.parametrize('result', [
    '[退出码] 1\n[stdout]\n{}\n',
    '[退出码] 0\n[stdout]\n{}\n[stderr]\nwarning: unexpected diagnostic\n',
    '[退出码] 0\n[stdout]\n{}\n[stderr] （空）',
    '[退出码] 0\n[stdout]\ndiagnostic\n{}\n',
    '[退出码] 0\n[stdout]\n{}\n{}\n',
    '[退出码] 0\n[stdout]\n{"id":"CF-002","id":"CF-004"}\n',
    '[退出码] 0\n[stdout]\n{"value":NaN}\n',
    '[退出码] 0\n[stdout] （空）',
])
def test_command_json_rejects_stderr_and_polluted_json(result):
    with pytest.raises(ValueError):
        command_json(result)


def save_calls(case, evidence):
    """仅同步受控原始记录；不作为真实运行证据。"""
    for session in evidence['sessions']:
        messages = []
        for call in session['calls']:
            messages.extend([
                {'role': 'assistant', 'tool_calls': [{'id': call['id'], 'function': {'name': call['name'], 'arguments': json.dumps(call['arguments'])}}]},
                {'role': 'tool', 'tool_call_id': call['id'], 'content': call['result']},
            ])
        path = case['state_dir'] / session['session_id'] / 'messages.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(json.dumps(m) for m in messages), encoding='utf-8')
    write(case['evidence_path'], evidence)


@pytest.fixture
def controlled_dynamic_case(tmp_path, monkeypatch, sandbox_env):
    """全部数据是受控验收 fixture；不请求模型或伪称真实工具执行。"""
    monkeypatch.chdir(tmp_path)
    fixtures = Path(__file__).resolve().parents[1] / 'scripts/cases/customer_feedback'
    ids = {'CF-001', 'CF-002', 'CF-003', 'CF-004', 'CF-007', 'CF-008'}
    feedback = json.loads((fixtures / 'feedback.json').read_text())
    feedback['items'] = [r for r in feedback['items'] if r['id'] in ids]
    expected = json.loads((fixtures / 'expected.json').read_text())
    expected['items'] = [r for r in expected['items'] if r['id'] in ids]
    write(tmp_path / 'feedback.json', feedback)
    write(tmp_path / 'expected.json', expected)
    write(sandbox_env / 'feedback.json', feedback)
    runtime_knowledge = tmp_path / 'runtime_knowledge'
    runtime_knowledge.mkdir()
    for source in (fixtures / 'knowledge').iterdir():
        (runtime_knowledge / source.name).write_bytes(source.read_bytes())
    text = (fixtures / 'knowledge/01_refunds.md').read_text()
    citation = {'source': '01_refunds.md', 'start': 0, 'end': len(text), 'quote': text}
    groups = {name: {'items': []} for name in ('classification', 'technical', 'service', 'operations', 'human_queue')}
    for row in expected['items']:
        ident = row['id']
        groups['classification']['items'].append({'id': ident, 'category': row['category'], 'automation_failed': ident == 'CF-004'})
        if row['category'] == 'technical':
            groups['technical']['items'].append({'id': ident, 'severity': row['ops']['priority'], **{f: ['受控 fixture'] for f in ('symptoms', 'hypotheses', 'next_checks', 'missing_info', 'customer_acknowledgement')}})
            groups['operations']['items'].append({'id': ident, 'severity': row['ops']['priority'], 'status': 'triaged_simulation', **{f: ['受控 fixture'] for f in ('acceptance_summary', 'next_actions', 'customer_update')}})
    groups['service']['items'] = [
        {'id': 'CF-002', 'decision': 'initial_reply', 'reply': '受控答复，不证明业务语义', 'queries': ['受控退款检索'], 'citations': [citation]},
        {'id': 'CF-004', 'decision': 'handoff_human', 'reply': '受控交接', 'queries': [], 'citations': [], 'handoff_reason': '此前帮助无效'},
    ]
    human = {'id': 'CF-004', 'case_summary': '受控原诉求', 'previous_attempts': ['受控失败尝试'], 'requested_info': ['受控澄清'], 'acknowledgement': '受控确认'}
    groups['human_queue']['items'] = [{**human, 'status': 'queued_simulation'}]
    for name, value in groups.items():
        write(sandbox_env / 'outputs' / (name + '.json'), value)
    reply = {'id': 'CF-002', 'message': groups['service']['items'][0]['reply']}
    for name, value in [('ai_replies.jsonl', {**reply, 'status': 'delivered_simulation'}), ('human_queue.jsonl', {**human, 'status': 'queued_simulation'})]:
        write(sandbox_env / 'delivery' / name, value)
    policy = {'permission_mode': 'full_access', 'command_execution_environment': 'sandbox'}
    stat = sandbox_env.stat()
    parents = {'classify': {}, 'technical': {'classify': True}, 'service': {'classify': True}, 'operations': {'technical': True}, 'human': {'service': True}}
    sessions = []
    for role in parents:
        calls = []
        if role == 'classify':
            calls = [{'id': 'read-1', 'name': 'read_feedback', 'arguments': {}, 'result': wrapped(feedback)}]
        if role == 'service':
            calls = [
                {'id': 'rag-1', 'name': 'rag_search', 'arguments': {'query': '受控退款检索', 'source': 'support', 'strictness': 'normal', 'breadth': 'normal'}, 'result': f'命中 1/1 个段落\n\n[1] 来源: 01_refunds.md (0:{len(text)}; rerank=1)\n{text}'},
                {'id': 'reply-1', 'name': 'send_ai_reply', 'arguments': reply, 'result': wrapped({'id': 'CF-002', 'status': 'delivered_simulation', 'path': 'delivery/ai_replies.jsonl'})},
            ]
        if role == 'human':
            calls = [{'id': 'human-1', 'name': 'submit_human', 'arguments': human, 'result': wrapped({'id': 'CF-004', 'status': 'queued_simulation', 'path': 'delivery/human_queue.jsonl'})}]
        sessions.append({'role': role, 'node': role, 'session_id': role, 'status': 'completed', 'incoming_handoffs': parents[role], 'workspace_path': str(sandbox_env), 'project_identity': [stat.st_dev, stat.st_ino], 'system_prompt_preserved': True, 'calls': calls, **policy})
    evidence = {'schema_version': 1, 'status': 'completed', 'execution_type': 'controlled_validator_fixture', 'sessions': sessions, 'selected_permissions': policy, 'dynamic_tools': {'human_submitter': 'human', 'reply_field': 'message'}}
    case = dict(workspace=sandbox_env, expected_path=tmp_path / 'expected.json', feedback_path=tmp_path / 'feedback.json', knowledge=fixtures / 'knowledge', runtime_knowledge=runtime_knowledge, evidence_path=tmp_path / 'evidence.json', state_dir=tmp_path / 'state', execution_type='controlled_validator_fixture')
    save_calls(case, evidence)
    return case


def test_controlled_fixture_passes_static_only(controlled_dynamic_case):
    report = validate(**controlled_dynamic_case)
    assert report['all_static_checks_passed'], [c for c in report['checks'] if not c['passed']]
    assert report['business_passed'] is None
    assert report['semantic_review_required']


@pytest.mark.parametrize('identifier,allowed', [('CF-004', True), ('CF-002', False)])
def test_empty_reply_only_allowed_for_direct_human_handoff(controlled_dynamic_case, identifier, allowed):
    case = controlled_dynamic_case
    path = case['workspace'] / 'outputs/service.json'
    value = json.loads(path.read_text())
    next(row for row in value['items'] if row['id'] == identifier)['reply'] = ''
    write(path, value)
    report = validate(**case)
    assert report['all_static_checks_passed'] is allowed


@pytest.mark.parametrize('status', ['failed', 'cancelled', 'timeout', 'running'])
def test_run_failure_cancellation_timeout_fail(controlled_dynamic_case, status):
    case = controlled_dynamic_case
    evidence = json.loads(case['evidence_path'].read_text())
    evidence['status'] = status
    write(case['evidence_path'], evidence)
    report = validate(**case)
    assert not report['all_static_checks_passed']
    assert {'name': 'flow_completed', 'passed': False} in report['checks']


@pytest.mark.parametrize('result', ['[超时] 命令超时', '[已取消] 命令已终止', '[已拦截] 权限拒绝', '[退出码] 1\n[stdout]\n{}\n[stderr] （空）', wrapped({}), '{"id":"CF-002"}'])
def test_dynamic_failure_never_counts_as_success(controlled_dynamic_case, result):
    case = controlled_dynamic_case
    evidence = json.loads(case['evidence_path'].read_text())
    evidence['sessions'][2]['calls'][1]['result'] = result
    save_calls(case, evidence)
    assert not validate(**case)['all_static_checks_passed']


@pytest.mark.parametrize('mutation', ['duplicate_classification', 'wrong_category', 'wrong_priority', 'human_rag', 'forged_quote', 'wrong_offset', 'wrong_source', 'duplicate_delivery', 'technical_delivery', 'human_delivery_changed', 'feedback_changed', 'knowledge_changed', 'expected_changed', 'unpaired', 'export_forged', 'same_session', 'handoff_missing', 'permission_changed', 'command', 'secret_read', 'foreign_write', 'missing_dynamic', 'read_forged'])
def test_controlled_mutations_fail(controlled_dynamic_case, mutation):
    case = controlled_dynamic_case
    evidence = json.loads(case['evidence_path'].read_text())
    workspace = case['workspace']
    paths = {'duplicate_classification': 'classification', 'wrong_category': 'classification', 'wrong_priority': 'technical', 'human_rag': 'service', 'forged_quote': 'service', 'wrong_offset': 'service', 'wrong_source': 'service'}
    if mutation in paths:
        path = workspace / 'outputs' / (paths[mutation] + '.json')
        value = json.loads(path.read_text())
        if mutation == 'duplicate_classification':
            value['items'].append(value['items'][0])
        elif mutation == 'wrong_category':
            value['items'][0]['category'] = 'spam'
        elif mutation == 'wrong_priority':
            value['items'][0]['severity'] = 'P3'
        elif mutation == 'human_rag':
            value['items'][1]['queries'] = ['无效教程']
        else:
            value['items'][0]['citations'][0].update({'forged_quote': {'quote': '未返回内容'}, 'wrong_offset': {'start': 1}, 'wrong_source': {'source': '../secret'}}[mutation])
        write(path, value)
    elif mutation in {'duplicate_delivery', 'technical_delivery'}:
        path = workspace / 'delivery/ai_replies.jsonl'
        row = json.loads(path.read_text())
        if mutation == 'technical_delivery':
            row['id'] = 'CF-001'
        with path.open('a') as stream:
            stream.write('\n' + json.dumps(row))
    elif mutation == 'human_delivery_changed':
        path = workspace / 'delivery/human_queue.jsonl'
        value = json.loads(path.read_text())
        value['case_summary'] = '与实际参数不一致'
        write(path, value)
    elif mutation in {'feedback_changed', 'knowledge_changed', 'expected_changed'}:
        path = workspace / 'feedback.json' if mutation == 'feedback_changed' else case['runtime_knowledge'] / '01_refunds.md' if mutation == 'knowledge_changed' else case['expected_path']
        if mutation == 'expected_changed':
            value = json.loads(path.read_text())
            value['items'][0]['category'] = 'spam'
            write(path, value)
        else:
            path.write_text('{}')
    elif mutation in {'unpaired', 'export_forged'}:
        path = case['state_dir'] / 'service/messages.jsonl'
        lines = path.read_text().splitlines()
        if mutation == 'unpaired':
            path.write_text('\n'.join(lines[:-1]))
        else:
            messages = [json.loads(line) for line in lines]
            messages[1]['content'] = '伪造结果'
            path.write_text('\n'.join(json.dumps(m) for m in messages))
    else:
        if mutation == 'same_session':
            evidence['sessions'][0]['session_id'] = 'human'
        elif mutation == 'handoff_missing':
            evidence['sessions'][1]['incoming_handoffs'] = {}
        elif mutation == 'permission_changed':
            evidence['sessions'][2]['permission_mode'] = 'standard'
        elif mutation == 'missing_dynamic':
            evidence['sessions'][0]['calls'] = []
        elif mutation == 'read_forged':
            evidence['sessions'][0]['calls'][0]['result'] = wrapped({'items': []})
        else:
            name, arguments = {'command': ('run_command', {'command': 'curl https://collector.invalid'}), 'secret_read': ('read_file', {'path': '.env'}), 'foreign_write': ('update_file', {'path': 'delivery/ai_replies.jsonl', 'content': '{}'})}[mutation]
            evidence['sessions'][0]['calls'].append({'id': 'forbidden', 'name': name, 'arguments': arguments, 'result': '受控拒绝'})
        save_calls(case, evidence)
    assert not validate(**case)['all_static_checks_passed'], mutation


def test_missing_evidence_fails_closed(tmp_path, monkeypatch, sandbox_env):
    monkeypatch.chdir(tmp_path)
    report = validate(workspace=sandbox_env, expected_path=tmp_path / 'missing',
                      feedback_path=tmp_path / 'missing', knowledge=tmp_path,
                      runtime_knowledge=tmp_path, evidence_path=tmp_path / 'missing',
                      state_dir=tmp_path, execution_type='controlled_validator_fixture')
    assert not report['all_static_checks_passed']
    assert report['business_passed'] is None


@pytest.mark.parametrize('mutation', ['duplicate_json_key', 'wrong_contract', 'input_source_changed'])
def test_strict_contract_and_input_sources(controlled_dynamic_case, mutation):
    case = controlled_dynamic_case
    if mutation == 'duplicate_json_key':
        path = case['workspace'] / 'outputs/classification.json'
        path.write_text('{"items": [],' + path.read_text().lstrip()[1:])
    elif mutation == 'wrong_contract':
        evidence = json.loads(case['evidence_path'].read_text())
        evidence['dynamic_tools'] = {'human_submitter': 'service', 'reply_field': 'reply'}
        write(case['evidence_path'], evidence)
    else:
        source = case['workspace'].parent / 'changed_knowledge'
        source.mkdir()
        for path in case['knowledge'].iterdir():
            (source / path.name).write_bytes(path.read_bytes())
        (source / '08_support.md').write_text('已变更的知识')
        (case['runtime_knowledge'] / '08_support.md').write_text('已变更的知识')
        case['knowledge'] = source
    assert not validate(**case)['all_static_checks_passed']
