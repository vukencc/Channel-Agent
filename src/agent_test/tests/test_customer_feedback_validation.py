"""独立验收器受控变异回归；不调用模型，不代表真实业务产物。"""
import hashlib
import json
from pathlib import Path

import pytest

from agent_test.scripts.validate_customer_feedback import native_citation, pair_messages
from agent_test.scripts.validate_customer_feedback import validate


def test_pairs_require_actual_unique_results(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "one", "function": {"name": "read_file", "arguments": '{"path":"feedback.json"}'}}]},
        {"role": "tool", "tool_call_id": "one", "content": "真实受控文件结果"},
    ]
    calls, errors = pair_messages(messages)
    assert not errors
    assert calls == [{"id": "one", "name": "read_file", "arguments": {"path": "feedback.json"}, "result": "真实受控文件结果"}]
    for mutation in (messages[:1], messages + [messages[1]], messages + [messages[0]], [messages[1], messages[0]]):
        assert pair_messages(mutation)[1]


@pytest.mark.parametrize("change", [{"start": False}, {"start": 1}, {"source": "../policy.md"}, {"source": "policy\\\\other.md"}, {"quote": "不存在"}])
def test_native_citation_rejects_mutations(tmp_path, change):
    text = "DOC-REFUND\n## REF-01 退款\n可提交审核，不保证退款。"
    (tmp_path / "policy.md").write_text(text)
    citation = {"source": "policy.md", "start": 0, "end": len(text), "quote": "可提交审核"}
    linked = [{"id": "rag-one", "result": f"[1] 来源: policy.md (0:{len(text)}; rerank=1)\n{text}"}]
    mapped = native_citation(citation, linked, tmp_path)
    assert mapped["matched_call_ids"] == ["rag-one"]
    assert mapped["clause_ids"] == ["REF-01"]
    assert not native_citation({**citation, **change}, linked, tmp_path)["matched_call_ids"]
    assert not native_citation(citation, [], tmp_path)["matched_call_ids"]


@pytest.fixture
def controlled_case(tmp_path, monkeypatch):
    """受控验收夹具仅测试校验逻辑，绝不作为模型执行证明。"""
    monkeypatch.chdir(tmp_path)
    fixtures = Path(__file__).resolve().parents[1] / 'scripts/cases/customer_feedback'
    expected = json.loads((fixtures / 'expected.json').read_text())
    workspace = tmp_path / 'workspace'
    outputs = workspace / 'outputs'
    outputs.mkdir(parents=True)
    (workspace / 'feedback.json').write_bytes((fixtures / 'feedback.json').read_bytes())
    state = tmp_path / 'state'
    records = tmp_path / 'records'
    records.mkdir()
    runtime_knowledge = tmp_path / 'knowledge'
    runtime_knowledge.mkdir()
    chunks, citations = [], []
    for source in sorted((fixtures / 'knowledge').glob('*.md')):
        text = source.read_text()
        (runtime_knowledge / source.name).write_bytes(source.read_bytes())
        chunks.append(f'[{len(chunks) + 1}] 来源: {source.name} (0:{len(text)}; rerank=1)\n{text}')
        citations.append({'source': source.name, 'start': 0, 'end': len(text), 'quote': text})
    result = '命中 8/8 个段落\n\n' + '\n\n'.join(chunks)
    groups = {n: [] for n in ('classification', 'technical', 'service', 'operations', 'human_queue')}
    rag = []
    for item in expected['items']:
        ident = item['id']
        groups['classification'].append({'id': ident, 'category': item['category'], 'automation_failed': item['human_escalation'] is True})
        if item['category'] == 'technical':
            groups['technical'].append({'id': ident, 'severity': item['ops']['priority'], **{k: ['受控值'] for k in ('symptoms', 'hypotheses', 'next_checks', 'missing_info', 'customer_acknowledgement')}})
            groups['operations'].append({'id': ident, 'severity': item['ops']['priority'], 'status': 'triaged_simulation', **{k: ['受控值'] for k in ('acceptance_summary', 'next_actions', 'customer_update')}})
        if item['category'] == 'service':
            human = item['human_escalation']
            groups['service'].append({'id': ident, 'decision': 'handoff_human' if human else 'initial_reply', 'reply': '受控验收夹具，不证明语义', 'handoff_reason': '受控值', 'queries': [] if human else [ident], 'citations': [] if human else citations})
            if not human:
                rag.append({'id': ident, 'arguments': {'query': ident, 'source': 'support', 'strictness': 'normal', 'breadth': 'normal'}, 'result': result})
            else:
                groups['human_queue'].append({'id': ident, 'status': 'queued_simulation', **{k: ['受控值'] for k in ('case_summary', 'previous_attempts', 'acknowledgement')}})
    def write(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False))
    for name, items in groups.items():
        write(outputs / (name + '.json'), {'items': items})
    parents = {'classify': {}, 'technical': {'classify': True}, 'service': {'classify': True}, 'operations': {'technical': True}, 'human': {'service': True}}
    sessions = []
    for node in parents:
        calls = [{'name': 'rag_search', **r} for r in rag] if node == 'service' else []
        messages = []
        for call in calls:
            messages.extend([{'role': 'assistant', 'tool_calls': [{'id': call['id'], 'function': {'name': call['name'], 'arguments': json.dumps(call['arguments'])}}]}, {'role': 'tool', 'tool_call_id': call['id'], 'content': call['result']}])
        folder = state / node
        folder.mkdir(parents=True)
        (folder / 'messages.jsonl').write_text('\n'.join(json.dumps(m) for m in messages))
        write(records / (node + '-tools-private.json'), calls)
        sessions.append({'node': node, 'session_id': node, 'status': 'completed', 'paired': True, 'tool_calls': len(calls), 'tool_results': len(calls), 'tools': [c['name'] for c in calls], 'workspace_path': str(workspace), 'project_identity': [1, 2], 'system_prompt_preserved': True, 'incoming_handoffs': parents[node]})
    write(tmp_path / 'summary.json', {'status': 'completed', 'sessions': sessions})
    write(tmp_path / 'rag.json', rag)
    return dict(workspace=workspace, expected_path=fixtures / 'expected.json', feedback_path=fixtures / 'feedback.json', knowledge=fixtures / 'knowledge', runtime_knowledge=runtime_knowledge, summary_path=tmp_path / 'summary.json', rag_evidence_path=tmp_path / 'rag.json', state_dir=state, tool_records=records, execution_type='controlled_validator_fixture')


def test_controlled_case_retains_pending_semantics(controlled_case):
    report = validate(**controlled_case)
    assert report['all_static_checks_passed'], [c for c in report['checks'] if not c['passed']]
    assert report['business_passed'] is None
    assert len(report['semantic_review_pending']) == 30
    assert report['execution_type'] == 'controlled_validator_fixture'


@pytest.mark.parametrize('newline', [b'\n', b'\r\n'])
def test_expected_fingerprint_accepts_checkout_line_endings(controlled_case, tmp_path, newline):
    """同一冻结内容的 LF/CRLF 检出形式均须通过完整受控验收。"""
    canonical = controlled_case['expected_path'].read_bytes().replace(b'\r\n', b'\n')
    assert hashlib.sha256(canonical).hexdigest() == '465a0869b2bfbca983d60601733b855293425166c29dff95411832b99e94c219'
    expected_path = tmp_path / 'expected.json'
    expected_path.write_bytes(canonical.replace(b'\n', newline))
    report = validate(**{**controlled_case, 'expected_path': expected_path})
    assert report['all_static_checks_passed'], [c for c in report['checks'] if not c['passed']]
    assert report['expected_sha256'] == hashlib.sha256(expected_path.read_bytes()).hexdigest()
    assert report['expected_canonical_sha256'] == hashlib.sha256(canonical).hexdigest()
    assert report['business_passed'] is None


@pytest.mark.parametrize('mutation', ['business_content', 'space', 'bare_cr', 'bom', 'final_newline'])
def test_expected_fingerprint_rejects_other_byte_changes(controlled_case, tmp_path, mutation):
    """仅归一 CRLF；业务内容、格式和其他字节变化均不能绕过冻结。"""
    canonical = controlled_case['expected_path'].read_bytes().replace(b'\r\n', b'\n')
    if mutation == 'business_content':
        changed = canonical.replace(b'"technical"', b'"service"', 1)
    elif mutation == 'space':
        changed = b' ' + canonical
    elif mutation == 'bare_cr':
        changed = canonical.replace(b'\n', b'\r', 1)
    elif mutation == 'bom':
        # JSON 字符串中的 BOM 仍是合法 JSON，必须由冻结校验拒绝。
        changed = b'{"marker": "\xef\xbb\xbf",' + canonical.lstrip()[1:]
    else:
        changed = canonical.rstrip(b'\n') if canonical.endswith(b'\n') else canonical + b'\n'
    assert changed != canonical
    expected_path = tmp_path / 'mutated-expected.json'
    expected_path.write_bytes(changed)
    report = validate(**{**controlled_case, 'expected_path': expected_path})
    assert not report['all_static_checks_passed']
    assert any(c['name'] == 'expected_frozen_sha256' and not c['passed'] for c in report['checks'])


@pytest.mark.parametrize(('node', 'path', 'allowed'), [
    ('classify', 'outputs/classification.json', True),
    ('technical', 'outputs/technical.json', True),
    ('service', 'outputs/service.json', True),
    ('operations', 'outputs/operations.json', True),
    ('human', 'outputs/human_queue.json', True),
    ('classify', 'outputs/technical.json', False),
    ('service', 'outputs/human_queue.json', False),
    ('human', 'feedback.json', False),
    ('operations', '../outputs/operations.json', False),
    ('technical', None, False),
])
def test_controlled_update_file_respects_node_output_ownership(controlled_case, node, path, allowed):
    """受控工具记录验证真实update_file参数契约；不实际调用文件工具或模型。"""
    messages_path = controlled_case['state_dir'] / node / 'messages.jsonl'
    messages = [json.loads(line) for line in messages_path.read_text().splitlines()]
    arguments = {'content': '受控验收夹具', 'reason': '受控验收契约回归'}
    if path is not None:
        arguments['path'] = path
    messages.extend([
        {'role': 'assistant', 'tool_calls': [{'id': 'update-owned', 'function': {'name': 'update_file', 'arguments': json.dumps(arguments)}}]},
        {'role': 'tool', 'tool_call_id': 'update-owned', 'content': '受控工具结果，不是实际执行证明'},
    ])
    messages_path.write_text('\n'.join(json.dumps(message) for message in messages))
    calls, errors = pair_messages(messages)
    assert not errors
    (controlled_case['tool_records'] / (node + '-tools-private.json')).write_text(json.dumps(calls))
    summary_path = controlled_case['summary_path']
    summary = json.loads(summary_path.read_text())
    session = next(session for session in summary['sessions'] if session['node'] == node)
    session.update(tool_calls=len(calls), tool_results=len(calls), tools=[call['name'] for call in calls])
    summary_path.write_text(json.dumps(summary))
    report = validate(**controlled_case)
    checks = {check['name']: check['passed'] for check in report['checks']}
    assert checks[node + ':write_own_output:update-owned'] is allowed
    assert report['all_static_checks_passed'] is allowed


@pytest.mark.parametrize('status', ['failed', 'cancelled', 'timeout', 'running'])
def test_completed_nodes_never_mask_failed_cancelled_timeout_run(controlled_case, status):
    path = controlled_case['summary_path']
    summary = json.loads(path.read_text())
    summary['status'] = status
    path.write_text(json.dumps(summary))
    report = validate(**controlled_case)
    assert not report['all_static_checks_passed']
    assert any(c['name'] == 'flow_completed' and not c['passed'] for c in report['checks'])


@pytest.mark.parametrize('mutation', ['missing_result', 'extra_rag', 'command', 'delegate', 'permission_denied', 'fabricated_query', 'reused_query', 'human_rag', 'wrong_priority', 'input_changed', 'duplicate_id'])
def test_controlled_business_and_evidence_mutations_fail(controlled_case, mutation):
    outputs = controlled_case['workspace'] / 'outputs'
    if mutation in {'missing_result', 'extra_rag', 'command', 'delegate', 'permission_denied'}:
        path = controlled_case['state_dir'] / 'service/messages.jsonl'
        messages = [json.loads(line) for line in path.read_text().splitlines()]
        if mutation == 'missing_result':
            messages.pop()
        elif mutation == 'permission_denied':
            messages[1]['content'] = '[已拦截] 当前选定权限禁止该工具'
        else:
            name = {'extra_rag': 'rag_search', 'command': 'run_command', 'delegate': 'spawn_agent'}[mutation]
            messages.extend([{'role': 'assistant', 'tool_calls': [{'id': 'extra', 'function': {'name': name, 'arguments': '{}'}}]}, {'role': 'tool', 'tool_call_id': 'extra', 'content': '受控值'}])
        path.write_text('\n'.join(json.dumps(m) for m in messages))
        if mutation != 'missing_result':
            # 同步所有导出，保证不能仅靠摘要与记录不一致发现这些越界。
            calls, errors = pair_messages(messages)
            assert not errors
            (controlled_case['tool_records'] / 'service-tools-private.json').write_text(json.dumps(calls))
            summary_path = controlled_case['summary_path']
            summary = json.loads(summary_path.read_text())
            session = next(s for s in summary['sessions'] if s['node'] == 'service')
            session.update(tool_calls=len(calls), tool_results=len(calls), tools=[c['name'] for c in calls])
            summary_path.write_text(json.dumps(summary))
            controlled_case['rag_evidence_path'].write_text(json.dumps([{k: c[k] for k in ('id', 'arguments', 'result')} for c in calls if c['name'] == 'rag_search']))
    elif mutation == 'input_changed':
        (controlled_case['workspace'] / 'feedback.json').write_text('{}')
    else:
        name = 'technical' if mutation == 'wrong_priority' else 'classification' if mutation == 'duplicate_id' else 'service'
        path = outputs / (name + '.json')
        data = json.loads(path.read_text())
        if mutation == 'wrong_priority':
            data['items'][0]['severity'] = 'P0'
        elif mutation == 'duplicate_id':
            data['items'].append(data['items'][0])
        elif mutation == 'human_rag':
            next(i for i in data['items'] if i['decision'] == 'handoff_human')['queries'] = ['未执行政策检索']
        elif mutation == 'reused_query':
            replies = [i for i in data['items'] if i['decision'] == 'initial_reply']
            replies[1]['queries'] = replies[0]['queries']
        else:
            next(i for i in data['items'] if i['decision'] == 'initial_reply')['queries'].append('未实际检索')
        path.write_text(json.dumps(data))
    assert not validate(**controlled_case)['all_static_checks_passed']


def test_cli_bad_input_creates_failure_report(tmp_path, monkeypatch):
    from agent_test.scripts.validate_customer_feedback import main
    import sys

    path = tmp_path / 'absent'
    report = tmp_path / 'report.json'
    argv = ['validate-customer-feedback']
    for flag in ('workspace', 'expected', 'feedback', 'knowledge', 'runtime-knowledge', 'summary', 'rag-evidence', 'state-dir', 'tool-records'):
        argv.extend(['--' + flag, str(path)])
    argv.extend(['--report', str(report), '--execution-type', 'controlled_validator_fixture'])
    monkeypatch.setattr(sys, 'argv', argv)
    assert main() == 1
    assert json.loads(report.read_text())['all_static_checks_passed'] is False
    before = report.read_bytes()
    with pytest.raises(SystemExit):
        main()
    assert report.read_bytes() == before
