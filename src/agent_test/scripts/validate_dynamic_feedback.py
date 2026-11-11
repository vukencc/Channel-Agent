"""六条动态客服 Flow 的独立只读验收；不将静态证据冒充业务语义通过。"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

from agent_test.scripts.validate_customer_feedback import EXPECTED_SHA, digest, native_citation, pair_messages

IDS = {'CF-001', 'CF-002', 'CF-003', 'CF-004', 'CF-007', 'CF-008'}
ROLES = {'classify', 'technical', 'service', 'operations', 'human'}
OUTPUTS = {'classify': 'classification', 'technical': 'technical', 'service': 'service',
           'operations': 'operations', 'human': 'human_queue'}
PARENTS = {'classify': set(), 'technical': {'classify'}, 'service': {'classify'},
           'operations': {'technical'}, 'human': {'service'}}


def strict_json(text: str) -> object:
    """拒绝重复键和非有限常量，避免证据含有互相矛盾的值。"""
    def pairs(rows: list[tuple]) -> dict:
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError('duplicate_json_key')
            result[key] = value
        return result

    def invalid(value: str) -> None:
        raise ValueError('nonfinite_json_value')

    value = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)
    json.dumps(value, allow_nan=False)
    return value


def command_json(result: object) -> object:
    """接受退出零的纯 JSON stdout；任何 stderr（含 warning）均不算严格成功。"""
    if not isinstance(result, str):
        raise ValueError('missing_command_result')
    prefix = '[退出码] 0\n[stdout]\n'
    if not result.startswith(prefix):
        raise ValueError('unsuccessful_command_result')
    # command._execute 无 stderr 时不追加段落。让严格 JSON 解码拒绝额外
    # stdout、stderr/诊断段及重复键，不从混杂输出中截取看似成功的对象。
    return strict_json(result[len(prefix):])


def validate(*, workspace: Path, expected_path: Path, feedback_path: Path,
             knowledge: Path, runtime_knowledge: Path, evidence_path: Path,
             state_dir: Path, execution_type: str = 'real_model_execution') -> dict:
    """公开证据与原始会话逐一对照；expected 只读冻结原三十条中的六条。"""
    checks: list[dict] = []

    def check(name: str, passed: bool) -> None:
        # 报告不复制对话、工具正文或凭据。
        checks.append({'name': name, 'passed': bool(passed)})

    def read(path: Path) -> object:
        return strict_json(path.read_text(encoding='utf-8'))

    def run() -> None:
        fixture_dir = Path(__file__).parent / 'cases/customer_feedback'
        frozen = (fixture_dir / 'expected.json').read_bytes().replace(b'\r\n', b'\n')
        check('expected_frozen_sha256', hashlib.sha256(frozen).hexdigest() == EXPECTED_SHA)
        original = {x['id']: x for x in json.loads(frozen)['items'] if x['id'] in IDS}
        supplied = read(expected_path)['items']
        expected = {x['id']: x for x in supplied}
        check('expected_exact_subset', set(expected) == IDS)
        check('expected_subset_unchanged', len(supplied) == 6 and expected == original)
        feedback = read(feedback_path)
        source = read(Path(__file__).parent / 'cases/customer_feedback/feedback.json')
        source_items = [x for x in source['items'] if x['id'] in IDS]
        check('feedback_frozen_subset', feedback.get('items') == source_items)
        check('feedback_unchanged', digest(workspace / 'feedback.json') == digest(feedback_path))
        evidence = read(evidence_path)
        check('evidence_schema', evidence.get('schema_version') == 1)
        check('execution_type', execution_type in {'real_model_execution', 'recorded_replay', 'controlled_validator_fixture'} and evidence.get('execution_type') == execution_type)
        check('flow_completed', evidence.get('status') == 'completed')
        sessions = evidence['sessions']
        check('five_roles', len(sessions) == 5 and {s.get('role') for s in sessions} == ROLES)
        check('distinct_nodes', len({s.get('node') for s in sessions}) == 5)
        check('distinct_sessions', len({s.get('session_id') for s in sessions}) == 5)
        by_role = {s['role']: s for s in sessions}
        policy = evidence['selected_permissions']
        check('selected_permission_values', policy == {'permission_mode': 'full_access', 'command_execution_environment': 'sandbox'})
        contract = evidence['dynamic_tools']
        submitter, reply_field = contract['human_submitter'], contract['reply_field']
        check('dynamic_contract', contract == {'human_submitter': 'human', 'reply_field': 'message'})
        identities = [s.get('project_identity') for s in sessions]
        stat = workspace.stat()
        check('same_project_identity', all(i == [stat.st_dev, stat.st_ino] for i in identities))
        calls_by_role = {}
        all_ids = set()
        for role, session in by_role.items():
            check(role + ':completed', session.get('status') == 'completed')
            check(role + ':workspace', session.get('workspace_path') == str(workspace))
            check(role + ':system_prompt_preserved', session.get('system_prompt_preserved') is True)
            check(role + ':selected_permissions', all(session.get(k) == v for k, v in policy.items()))
            check(role + ':handoffs', session.get('incoming_handoffs') == {by_role[p]['node']: True for p in PARENTS[role]})
            sid = session['session_id']
            if not isinstance(sid, str) or not sid or sid in {'.', '..'} or '/' in sid or '\\' in sid:
                check(role + ':safe_session_id', False)
                continue
            messages = [strict_json(line) for line in (state_dir / sid / 'messages.jsonl').read_text().splitlines() if line.strip()]
            for message in messages:
                for call in message.get('tool_calls', []):
                    arguments = call['function']['arguments']
                    if isinstance(arguments, str):
                        strict_json(arguments)
            calls, errors = pair_messages(messages)
            check(role + ':actual_tool_pairing', not errors)
            check(role + ':export_matches_raw_state', calls == session['calls'])
            calls_by_role[role] = calls
            allowed = {'read_file', 'list_files', 'create_file', 'update_file', 'edit_file', 'append_file', 'read_feedback'}
            if role == 'service':
                allowed |= {'rag_search', 'send_ai_reply'}
            if role == submitter:
                allowed.add('submit_human')
            for call in calls:
                ident, name, arguments = call['id'], call['name'], call['arguments']
                check(role + ':unique_call:' + ident, ident not in all_ids)
                all_ids.add(ident)
                check(role + ':allowed_tool:' + ident, name in allowed)
                if name in {'read_file', 'list_files'}:
                    path = arguments.get('path', '.')
                    permitted = {'.', 'outputs', 'delivery', 'feedback.json'} | {'outputs/' + n + '.json' for n in OUTPUTS.values()} | {'delivery/ai_replies.jsonl', 'delivery/human_queue.jsonl'}
                    check(role + ':bounded_read:' + ident, path in permitted)
                if name in {'create_file', 'update_file', 'edit_file', 'append_file'}:
                    check(role + ':own_output_only:' + ident, arguments.get('path') == 'outputs/' + OUTPUTS[role] + '.json')
                if name in {'read_feedback', 'send_ai_reply', 'submit_human'}:
                    try:
                        body = command_json(call.get('result'))
                    except (ValueError, TypeError):
                        body = None
                    check(role + ':dynamic_success:' + ident, body is not None)
                    if name == 'read_feedback':
                        check(role + ':feedback_result:' + ident, body == feedback and arguments == {})
                    elif name in {'send_ai_reply', 'submit_human'}:
                        filename = 'ai_replies.jsonl' if name == 'send_ai_reply' else 'human_queue.jsonl'
                        status = 'delivered_simulation' if name == 'send_ai_reply' else 'queued_simulation'
                        check(role + ':dynamic_receipt:' + ident, body == {'id': arguments.get('id'), 'status': status, 'path': 'delivery/' + filename})
        groups = {}
        targets = {'classification': IDS, 'technical': {'CF-001', 'CF-007'}, 'operations': {'CF-001', 'CF-007'}, 'service': {'CF-002', 'CF-004'}, 'human_queue': {'CF-004'}}
        for name, ids in targets.items():
            value = read(workspace / 'outputs' / (name + '.json'))
            rows = value['items']
            check(name + ':unique_exact_ids', isinstance(rows, list) and len(rows) == len(ids) and Counter(r['id'] for r in rows) == Counter(ids))
            groups[name] = {r['id']: r for r in rows}
            if 'outputs' in evidence:
                check(name + ':export_matches_disk', evidence['outputs'].get(name) == value)
        for ident, row in groups['classification'].items():
            check(ident + ':category', row.get('category') == expected[ident]['category'])
            check(ident + ':automation_flag', type(row.get('automation_failed')) is bool and row['automation_failed'] == (ident == 'CF-004'))
        for name in ('technical', 'operations'):
            fields = ('symptoms', 'hypotheses', 'next_checks', 'missing_info', 'customer_acknowledgement') if name == 'technical' else ('acceptance_summary', 'next_actions', 'customer_update')
            for ident, row in groups[name].items():
                check(ident + ':' + name + ':priority', row.get('severity') == expected[ident]['ops']['priority'])
                check(ident + ':' + name + ':required_fields', all(bool(row.get(f)) for f in fields))
                if name == 'operations':
                    check(ident + ':ops_simulation', row.get('status') == 'triaged_simulation')
        service = groups['service']
        for ident, row in service.items():
            # 直接转人工不再自动作答；非空确认由人工节点的 acknowledgement 验收。
            check(ident + ':reply_contract', isinstance(row.get('reply'), str) and (
                row.get('decision') == 'handoff_human' or bool(row['reply'].strip())))
            check(ident + ':decision', row.get('decision') == ('initial_reply' if ident == 'CF-002' else 'handoff_human'))
        failed = service['CF-004']
        check('CF-004:immediate_human_no_rag', bool(failed.get('handoff_reason')) and failed.get('queries', []) == [] and failed.get('citations', []) == [])
        rag = [c for c in calls_by_role['service'] if c['name'] == 'rag_search']
        queries = service['CF-002'].get('queries', [])
        check('CF-002:bounded_actual_queries', isinstance(queries, list) and bool(queries) and 1 <= len(rag) <= 3 and set(queries) == {c['arguments'].get('query') for c in rag})
        for call in rag:
            check('rag:normal_support:' + call['id'], call['arguments'].get('source') == 'support' and all(call['arguments'].get(k) == 'normal' for k in ('strictness', 'breadth')))
            check('rag:successful:' + call['id'], bool(re.search(r'命中 [1-9]\d*/\d+ 个段落', str(call.get('result', '')))))
        clauses, docs = set(), set()
        citations = service['CF-002'].get('citations', [])
        check('CF-002:citations_nonempty', isinstance(citations, list) and bool(citations))
        for index, citation in enumerate(citations):
            mapped = native_citation(citation, rag, knowledge)
            check('CF-002:native_citation:' + str(index), bool(mapped['matched_call_ids']))
            clauses.update(mapped['clause_ids'])
            docs.update(mapped['document_ids'])
        check('CF-002:required_clauses', {'REF-01', 'REF-02'} <= clauses)
        check('CF-002:required_docs', {'DOC-REFUND'} <= docs)
        human = groups['human_queue']['CF-004']
        check('CF-004:human_simulation', human.get('status') == 'queued_simulation')
        check('CF-004:human_fields', all(bool(human.get(f)) for f in ('case_summary', 'previous_attempts', 'acknowledgement')))
        dynamic_calls = [c for calls in calls_by_role.values() for c in calls]
        check('read_feedback:actually_called', any(c['name'] == 'read_feedback' for c in dynamic_calls))
        for tool, filename, ids in [('send_ai_reply', 'ai_replies.jsonl', {'CF-002'}), ('submit_human', 'human_queue.jsonl', {'CF-004'})]:
            rows = [strict_json(line) for line in (workspace / 'delivery' / filename).read_text().splitlines() if line.strip()]
            calls = [c for c in dynamic_calls if c['name'] == tool]
            check(tool + ':exactly_once_calls', Counter(c['arguments'].get('id') for c in calls) == Counter(ids))
            check(tool + ':exactly_once_delivery', Counter(r.get('id') for r in rows) == Counter(ids))
            if tool == 'send_ai_reply':
                check(tool + ':reply_matches_output', len(rows) == len(calls) == 1 and rows[0].get(reply_field) == calls[0]['arguments'].get(reply_field) == service['CF-002']['reply'])
            status = 'delivered_simulation' if tool == 'send_ai_reply' else 'queued_simulation'
            check(tool + ':delivery_matches_arguments', len(rows) == len(calls) == 1 and rows[0] == {**calls[0]['arguments'], 'status': status})
            if tool == 'submit_human':
                check(tool + ':handoff_matches_output', len(calls) == 1 and all(calls[0]['arguments'].get(f) == human.get(f) for f in ('case_summary', 'previous_attempts', 'acknowledgement')))
        allowed_files = {'feedback.json'} | {'outputs/' + n + '.json' for n in OUTPUTS.values()} | {'delivery/ai_replies.jsonl', 'delivery/human_queue.jsonl'}
        check('workspace_only_business_files', {p.relative_to(workspace).as_posix() for p in workspace.rglob('*') if p.is_file()} <= allowed_files)
        check('workspace_no_symlinks', not any(p.is_symlink() for p in workspace.rglob('*')))
        check('knowledge_exact_files', {p.name for p in knowledge.iterdir()} == {p.name for p in runtime_knowledge.iterdir()})
        check('knowledge_frozen_files', {p.name for p in knowledge.iterdir()} == {p.name for p in (fixture_dir / 'knowledge').iterdir()})
        for path in knowledge.iterdir():
            check('knowledge_unchanged:' + path.name, path.is_file() and not path.is_symlink() and not (runtime_knowledge / path.name).is_symlink() and digest(path) == digest(runtime_knowledge / path.name))
            original_path = fixture_dir / 'knowledge' / path.name
            check('knowledge_original:' + path.name, original_path.is_file() and path.read_bytes().replace(b'\r\n', b'\n') == original_path.read_bytes().replace(b'\r\n', b'\n'))

    try:
        run()
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        check('invalid_or_missing_evidence', False)
    return {'execution_type': execution_type, 'validation_type': 'static_independent_validation',
            'checks': checks, 'passed_checks': sum(c['passed'] for c in checks),
            'failed_checks': sum(not c['passed'] for c in checks),
            'all_static_checks_passed': bool(checks) and all(c['passed'] for c in checks),
            'business_passed': None, 'semantic_review_required': True,
            'limits': ['静态关联不代替逐条业务语义审查', 'delivery 是本地模拟队列，不证明外部送达']}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('workspace', 'expected', 'feedback', 'knowledge', 'runtime-knowledge', 'evidence', 'state-dir', 'report'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--execution-type', choices=('real_model_execution', 'recorded_replay', 'controlled_validator_fixture'), default='real_model_execution')
    args = parser.parse_args()
    if args.report.exists():
        parser.error('报告已存在，必须保留原证据')
    report = validate(workspace=args.workspace, expected_path=args.expected, feedback_path=args.feedback,
                      knowledge=args.knowledge, runtime_knowledge=args.runtime_knowledge,
                      evidence_path=args.evidence, state_dir=args.state_dir, execution_type=args.execution_type)
    with args.report.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({k: report[k] for k in ('passed_checks', 'failed_checks', 'all_static_checks_passed')}))
    return 0 if report['all_static_checks_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
