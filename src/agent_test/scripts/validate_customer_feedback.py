"""客服Flow独立只读验收；静态证据检查不代替人工业务语义复核。"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

EXPECTED_SHA = '81f8c1d4c9842951a441b6e27c655b8f485597947afc5c39d525a230f7b1a08e'


def pair_messages(messages: list[dict]) -> tuple[list[dict], list[str]]:
    """按实际顺序核验每个调用有且只有一个随后出现的工具结果。"""
    calls, pending, finished, errors = [], {}, set(), []
    for message in messages:
        if not isinstance(message, dict):
            errors.append('invalid_message')
            continue
        for call in message.get('tool_calls', []):
            try:
                ident, function = call['id'], call['function']
                arguments = function['arguments']
                arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
                if not isinstance(ident, str) or not ident or not isinstance(arguments, dict):
                    raise ValueError('invalid_call')
                if ident in pending or ident in finished:
                    errors.append('duplicate_call:' + ident)
                    continue
                row = {'id': ident, 'name': function['name'], 'arguments': arguments}
                calls.append(row)
                pending[ident] = row
            except (KeyError, ValueError, TypeError):
                errors.append('invalid_call')
        if message.get('role') == 'tool':
            ident = message.get('tool_call_id')
            if ident not in pending:
                errors.append('orphan_or_duplicate_result:' + str(ident))
                continue
            pending.pop(ident)['result'] = message.get('content', '')
            finished.add(ident)
    errors.extend('missing_result:' + ident for ident in pending)
    return calls, errors

def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def native_citation(citation: dict, linked: list[dict], knowledge: Path) -> dict:
    """先证明原生来源/偏移/quote真实返回，再映射原KB的稳定ID。"""
    source, start, end, quote = (citation.get(k) for k in ('source', 'start', 'end', 'quote'))
    result = {'matched_call_ids': [], 'document_ids': [], 'clause_ids': []}
    if not (isinstance(source, str) and Path(source).name == source and '/' not in source and '\\' not in source and type(start) is int and type(end) is int and 0 <= start < end and isinstance(quote, str) and quote):
        return result
    for call in linked:
        body = str(call.get('result', ''))
        for match in re.finditer(r'\[\d+\] 来源: ([^\n]+?) \((\d+):(\d+); [^\n]*\)\n(.*?)(?=\n\n\[\d+\] 来源: |\Z)', body, re.S):
            if match[1] == source and (int(match[2]), int(match[3])) == (start, end) and quote in match[4]:
                result['matched_call_ids'].append(call['id'])
    path = knowledge / source
    if not result['matched_call_ids'] or not path.is_file():
        return result
    if path.is_symlink():
        return {'matched_call_ids': [], 'document_ids': [], 'clause_ids': []}
    text = path.read_text(encoding='utf-8')
    if end > len(text) or quote not in text[start:end]:
        result['matched_call_ids'] = []
        return result
    # KB只用于将已经验证的来源位置映射ID，不能据此补造未返回的quote。
    absolute_quote_start = start + text[start:end].index(quote)
    docs = re.findall(r'DOC-[A-Z]+', text[:absolute_quote_start + len(quote)])
    clauses = list(re.finditer(r'^## ((?:REF|INV|PLN|SEA|ACC|CAN|EXP|SUP)-\d{2})\b', text, re.M))
    prior = [m[1] for m in clauses if m.start() <= absolute_quote_start]
    result['document_ids'] = list(dict.fromkeys(docs[:1]))
    result['clause_ids'] = prior[-1:]
    result['clause_ids'] += [m[1] for m in clauses if absolute_quote_start < m.start() < absolute_quote_start + len(quote)]
    return result


def validate(*, workspace: Path, expected_path: Path, feedback_path: Path,
             knowledge: Path, runtime_knowledge: Path, summary_path: Path,
             rag_evidence_path: Path, state_dir: Path, tool_records: Path,
             node_roles: dict[str, str] | None = None,
             expected_sha256: str = EXPECTED_SHA,
             execution_type: str = 'real_model_execution') -> dict:
    """只读验收；真实模型与受控变异的来源由调用者明确记录。"""
    roles = node_roles or {n: n for n in ('classify', 'technical', 'service', 'operations', 'human')}
    checks: list[dict] = []

    def check(name: str, passed: bool, detail: object = None) -> None:
        checks.append(dict(name=name, passed=bool(passed), detail=detail))

    def read(path: Path) -> object:
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            check('read:' + path.name, False, type(exc).__name__ + ': ' + str(exc))
            return {}

    exp = read(expected_path)
    if not isinstance(exp, dict) or not isinstance(exp.get('items'), list):
        return {'all_static_checks_passed': False, 'checks': checks, 'failed_checks': max(1, len(checks))}
    expected = {x['id']: x for x in exp['items']}
    check('expected_frozen_sha256', digest(expected_path) == expected_sha256,
          digest(expected_path))
    out = workspace / 'outputs'
    groups: dict[str, dict] = {}
    target_ids = {
        'classification': set(expected),
        'technical': {i for i, e in expected.items() if e['category'] == 'technical'},
        'operations': {i for i, e in expected.items() if e['category'] == 'technical'},
        'service': {i for i, e in expected.items() if e['category'] == 'service'},
        'human_queue': {i for i, e in expected.items() if e['human_escalation'] is True},
    }
    for name, ids in target_ids.items():
        value = read(out / (name + '.json'))
        rows = value.get('items', []) if isinstance(value, dict) else []
        check(name + ':items_list', isinstance(rows, list))
        rows = rows if isinstance(rows, list) else []
        row_ids = [x.get('id') for x in rows if isinstance(x, dict)]
        check(name + ':unique_exact_ids', len(rows) == len(ids) and len(row_ids) == len(set(row_ids)) and set(row_ids) == ids,
              {'expected': sorted(ids), 'actual': row_ids})
        groups[name] = {x['id']: x for x in rows if isinstance(x, dict) and 'id' in x}
        if name == 'classification':
            if 'counts' in value:
                check('classification:declared_counts', value['counts'] == exp['category_counts'], value['counts'])
    for ident, item in groups['classification'].items():
        e = expected.get(ident, {})
        check(ident + ':category', item.get('category') == e.get('category'), item.get('category'))
        check(ident + ':automation_flag_boolean', type(item.get('automation_failed')) is bool)
        if e.get('category') == 'service':
            check(ident + ':automation_failed', item.get('automation_failed') is e['human_escalation'])
    check('category_counts', Counter(x.get('category') for x in groups['classification'].values()) == exp['category_counts'])
    for name in ('technical', 'operations'):
        for ident, item in groups[name].items():
            check(ident + ':' + name + ':priority', item.get('severity') == (expected.get(ident, {}).get('ops') or {}).get('priority'), item.get('severity'))
            fields = ('symptoms', 'hypotheses', 'next_checks', 'missing_info', 'customer_acknowledgement') if name == 'technical' else ('acceptance_summary', 'next_actions', 'customer_update')
            for field in fields:
                check(ident + ':' + name + ':' + field, bool(item.get(field)))
            if name == 'operations':
                check(ident + ':ops_simulation', item.get('status') == 'triaged_simulation')
    evidence = read(rag_evidence_path)
    evidence = evidence if isinstance(evidence, list) else []
    used_evidence: dict[str, str] = {}
    citation_details: list[dict] = []
    for ident, item in groups['service'].items():
        e = expected.get(ident, {})
        human = e.get('human_escalation')
        check(ident + ':service_decision', item.get('decision') == ('handoff_human' if human else 'initial_reply'))
        check(ident + ':reply_nonempty', bool(item.get('reply')))
        if human:
            check(ident + ':handoff_reason', bool(item.get('handoff_reason')))
            check(ident + ':human_no_automatic_rag', item.get('queries', []) == [] and item.get('citations', []) == [])
            continue
        queries = item.get('queries', [])
        check(ident + ':actual_queries', isinstance(queries, list) and bool(queries))
        linked = [r for r in evidence if r.get('arguments', {}).get('query') in queries]
        check(ident + ':all_queries_actual', isinstance(queries, list) and all(any(r.get('arguments', {}).get('query') == q for r in linked) for q in queries))
        check(ident + ':bounded_rag', 1 <= len(linked) <= 3)
        check(ident + ':actual_rag_calls', bool(linked), [r.get('id') for r in linked])
        successful = [r for r in linked if re.search(r'命中 [1-9]\d*/\d+ 个段落', str(r.get('result', ''))) and '[1] 来源:' in str(r.get('result', ''))]
        check(ident + ':rag_success_nonempty', bool(successful))
        for r in linked:
            check(ident + ':rag_source:' + r.get('id', ''), r.get('arguments', {}).get('source') == 'support')
            check(ident + ':rag_modes:' + r.get('id', ''), all(r.get('arguments', {}).get(k) == 'normal' for k in ('strictness', 'breadth')))
            prior = used_evidence.get(r.get('id', ''))
            check(ident + ':independent_rag:' + r.get('id', ''), prior in (None, ident), prior)
            used_evidence[r.get('id', '')] = ident
        citations = item.get('citations', [])
        check(ident + ':citations_nonempty', isinstance(citations, list) and bool(citations))
        citations = citations if isinstance(citations, list) else []
        cited_docs, cited_clauses = set(), set()
        for c in citations:
            check(ident + ':native_citation_schema', isinstance(c, dict) and all(k in c for k in ('source', 'start', 'end', 'quote')))
            if not isinstance(c, dict):
                continue
            if 'source' in c:
                mapped = native_citation(c, successful, knowledge)
                check(ident + ':native_citation:' + str(c.get('source')), bool(mapped['matched_call_ids']), {'citation': c, 'mapped': mapped})
                cited_docs.update(mapped['document_ids'])
                cited_clauses.update(mapped['clause_ids'])
                citation_details.append({'feedback_id': ident, 'citation': c, 'native_mapping': mapped})
                continue
            # 兼容解析历史证据用于定位，但缺少原生来源的引用已在上面明确失败。
            quote, doc, clause = c.get('quote'), c.get('document_id'), c.get('clause_id')
            matches = [r['id'] for r in successful if any(all(isinstance(v, str) and bool(v) and v in chunk for v in (quote, doc, clause)) for chunk in re.split(r'\[\d+\] 来源:', str(r.get('result', '')))[1:])]
            check(ident + ':literal_quote:' + str(clause), bool(matches), {'citation': c, 'matching_call_ids': matches})
            citation_details.append({'feedback_id': ident, 'citation': c, 'matching_call_ids': matches})
            cited_docs.add(c.get('document_id'))
            cited_clauses.add(c.get('clause_id'))
        check(ident + ':required_docs', set(e.get('required_doc_ids', [])) <= cited_docs)
        check(ident + ':required_clauses', set(e.get('required_clause_ids', [])) <= cited_clauses)
    for ident, item in groups['human_queue'].items():
        check(ident + ':human_simulation', item.get('status') == 'queued_simulation')
        for field in ('case_summary', 'previous_attempts', 'acknowledgement'):
            check(ident + ':human:' + field, bool(item.get(field)))
    summary = read(summary_path)
    check('flow_completed', summary.get('status') == 'completed', summary.get('status'))
    sessions = summary.get('sessions', [])
    check('five_nodes', len(sessions) == 5)
    allowed_tools = {'read_file', 'list_files', 'create_file', 'update_file', 'edit_file', 'append_file', 'rag_search'}
    for session in sessions:
        check(session['node'] + ':paired', session.get('paired') is True)
        check(session['node'] + ':completed', session.get('status') == 'completed')
        check(session['node'] + ':handoffs', all(session.get('incoming_handoffs', {}).values()))
        check(session['node'] + ':only_file_rag_tools', set(session.get('tools', [])) <= allowed_tools, sorted(set(session.get('tools', []))))
    # 汇总只作交叉验证，真实工具与结果配对必须从会话读取。
    actual_rag = []
    seen_call_ids = set()
    expected_nodes = set(roles.values())
    check('exact_node_roles', len(roles) == 5 and len(expected_nodes) == 5 and {s.get('node') for s in sessions} == expected_nodes)
    check('distinct_sessions', len({s.get('session_id') for s in sessions}) == 5)
    for session in sessions:
        node = session.get('node', '')
        ident = session.get('session_id', '')
        check(node + ':safe_session_id', isinstance(ident, str) and bool(ident) and Path(ident).name == ident and '/' not in ident and chr(92) not in ident)
        if not isinstance(ident, str) or Path(ident).name != ident or '/' in ident or chr(92) in ident:
            continue
        try:
            messages = [json.loads(line) for line in (state_dir / ident / 'messages.jsonl').read_text().splitlines() if line.strip()]
            calls, errors = pair_messages(messages)
        except (OSError, ValueError, TypeError) as exc:
            calls, errors = [], [type(exc).__name__]
        check(node + ':actual_tool_pairing', not errors, errors)
        check(node + ':actual_counts', len(calls) == session.get('tool_calls') == session.get('tool_results'))
        check(node + ':actual_tools', [c['name'] for c in calls] == session.get('tools'))
        exported = read(tool_records / (node + '-tools-private.json'))
        check(node + ':export_matches_state', exported == calls)
        check(node + ':actual_allowed_tools', all(c['name'] in allowed_tools for c in calls))
        check(node + ':actual_workspace', session.get('workspace_path') == str(workspace))
        check(node + ':preserved_system_prompt', session.get('system_prompt_preserved') is True)
        for call in calls:
            check(node + ':global_call_id:' + call['id'], call['id'] not in seen_call_ids)
            seen_call_ids.add(call['id'])
            if call['name'] == 'rag_search':
                check(node + ':rag_only_service:' + call['id'], node == roles.get('service'))
                actual_rag.append({k: call.get(k) for k in ('id', 'arguments', 'result')})
            if call['name'] in {'create_file', 'update_file', 'edit_file', 'append_file'}:
                check(node + ':write_business_output:' + call['id'], call['arguments'].get('path') in {'outputs/' + n + '.json' for n in target_ids})
                output_role = {'classify': 'classification', 'technical': 'technical', 'service': 'service', 'operations': 'operations', 'human': 'human_queue'}
                owned = next((v for r, v in output_role.items() if roles.get(r) == node), None)
                check(node + ':write_own_output:' + call['id'], call['arguments'].get('path') == 'outputs/' + str(owned) + '.json')
    check('rag_evidence_matches_actual_calls', evidence == actual_rag)
    check('no_unassigned_extra_rag', {r.get('id') for r in evidence} == set(used_evidence))
    expected_parents = {'classify': set(), 'technical': {'classify'}, 'service': {'classify'}, 'operations': {'technical'}, 'human': {'service'}}
    for role, parents in expected_parents.items():
        session = next((s for s in sessions if s.get('node') == roles.get(role)), {})
        check(role + ':exact_handoffs', session.get('incoming_handoffs') == {roles[p]: True for p in parents})
    identities = [s.get('project_identity') for s in sessions]
    check('same_project_identity', bool(identities) and all(isinstance(i, list) and len(i) == 2 and i == identities[0] for i in identities))
    check('feedback_unchanged', (workspace / 'feedback.json').is_file() and digest(workspace / 'feedback.json') == digest(feedback_path))
    files = {str(p.relative_to(workspace)) for p in workspace.rglob('*') if p.is_file()}
    check('workspace_no_symlinks', not any(p.is_symlink() for p in workspace.rglob('*')))
    check('workspace_only_business_json', files <= {'feedback.json'} | {'outputs/' + n + '.json' for n in target_ids}, sorted(files))
    check('knowledge_exact_copy_files', {p.name for p in knowledge.iterdir()} == {p.name for p in runtime_knowledge.iterdir()})
    for source in (knowledge).glob('*.md'):
        check('knowledge_unchanged:' + source.name, (runtime_knowledge / source.name).is_file() and digest(source) == digest(runtime_knowledge / source.name))
    return {'execution_type': execution_type, 'validation_type': 'static_independent_validation', 'input_contract_version': 1,
            'expected_sha256': digest(expected_path), 'checks': checks,
            'passed_checks': sum(x['passed'] for x in checks), 'failed_checks': sum(not x['passed'] for x in checks),
            'all_static_checks_passed': all(x['passed'] for x in checks), 'citations': citation_details,
            'semantic_review_required': True,
            'business_passed': None,
            'semantic_review_pending': [{'id': i, 'required_facts': e.get('required_facts', []), 'ops': e.get('ops'), 'routing_rule': e.get('routing_rule'), 'optional_policy_notes': e.get('optional_policy_notes')} for i, e in expected.items()],
            'limits': ['逐字引用与同一工具结果关联检查不替代逐条语义审查', '无真实外发/危险写入需结合完整工具参数与审计另行核验']}


def main() -> int:
    """显式输入全部证据路径；报告只写调用者指定的新文件。"""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('workspace', 'expected', 'feedback', 'knowledge', 'runtime-knowledge', 'summary', 'rag-evidence', 'state-dir', 'tool-records', 'report'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--node-roles', type=Path, help='角色到实际节点ID的JSON映射')
    parser.add_argument('--execution-type', choices=('real_model_execution', 'recorded_replay', 'controlled_validator_fixture'), default='real_model_execution')
    args = parser.parse_args()
    if args.report.exists():
        parser.error('报告文件已存在，请指定新文件，保留原证据')
    try:
        result = validate(workspace=args.workspace, expected_path=args.expected, feedback_path=args.feedback,
                          knowledge=args.knowledge, runtime_knowledge=args.runtime_knowledge,
                          summary_path=args.summary, rag_evidence_path=args.rag_evidence,
                          state_dir=args.state_dir, tool_records=args.tool_records,
                          node_roles=json.loads(args.node_roles.read_text()) if args.node_roles else None,
                          execution_type=args.execution_type)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        result = {'all_static_checks_passed': False, 'business_passed': None,
                  'failure': 'invalid_evidence', 'error_type': type(exc).__name__}
    with args.report.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({'report': str(args.report), 'passed': result.get('passed_checks', 0),
                      'failed': result.get('failed_checks', 1), 'semantic_review_required': True}))
    return 0 if result['all_static_checks_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
