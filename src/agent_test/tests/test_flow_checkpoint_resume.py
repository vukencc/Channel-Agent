"""项目 Flow 检查点恢复的受控离线回归。"""
import asyncio
import copy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path

import pytest

from agent_core import config
from agent_core.core.flows import FlowService
from agent_core.core.llm import ModelConnectionError, TOOL_SCHEMAS
from agent_core.core.projects import ProjectService
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore
from agent_core.core.tool_settings import selected_schemas
from test_flow_draft_review_runtime import assistant, call, review_payload, runtime_store
from test_flow_receipt_handoff_runtime import ticket_tool


@pytest.mark.parametrize('legacy', [False, True])
def test_resume_same_run_skips_completed_nodes(tmp_path, monkeypatch, sandbox_env, legacy):
    monkeypatch.setattr(config, 'FLOW_AUTO_ARCHIVE', False)
    store = SessionStore(tmp_path / 'state', tmp_path / 'private')
    calls = []
    async def model(history, *, session_id, emit):
        session = manager.sessions[session_id]
        node = session.record['flow_node_id']
        calls.append((node, session_id))
        if node == 'b' and sum(name == 'b' for name, _ in calls) <= 2:
            raise ModelConnectionError('offline fixture')
        return {'role': 'assistant', 'content': '完成'}
    async def run():
        nonlocal manager
        manager = SessionManager(store, model=model)
        flows = FlowService(manager)
        projects = ProjectService(manager, flows)
        try:
            template = flows.create()
            template.update(nodes=[{'id': n, 'title': n, 'prompt': n} for n in ('a', 'b', 'c')],
                            edges=[{'source': 'a', 'target': 'b'}, {'source': 'b', 'target': 'c'}])
            template = flows.save(template)
            project = projects.create('项目', str(sandbox_env), flow_id=template['id'])
            fid = project['flow_id']
            flows.start(fid, input_text='输入')
            await asyncio.wait_for(flows.tasks[fid], 5)
            if legacy:
                legacy_run = flows.records[fid]['run']
                legacy_run.pop('definition_digest')
                legacy_run.pop('project_revision')
                for row in legacy_run['nodes'].values():
                    row.pop('session_configuration_digest', None)
                manager.sessions[legacy_run['nodes']['b']['session_id']].record.pop('flow_project_id')
                flows._write(flows.records[fid])
            before = flows.get(fid)['run']
            flows._resume_plan(flows.records[fid], before['id'])
            assert before['status'] == 'failed' and before['resume_available']
            first = copy.deepcopy(before['nodes']['a'])
            sid = before['nodes']['b']['session_id']
            session = manager.sessions[sid]
            with pytest.raises(ValueError):
                flows.resume(fid, run_id='wrong')
            original_turn = session.record['last_run']['turn_id']
            session.record['last_run']['turn_id'] = 'changed'
            assert not flows.get(fid)['run']['resume_available']
            session.record['last_run']['turn_id'] = original_turn
            session.record['budget_overrides'] = {'TOOL_TIMEOUT': 1}
            assert not flows.get(fid)['run']['resume_available']
            session.record.pop('budget_overrides')
            original_reason = session.record['last_run']['stop_reason']
            session.record['last_run']['stop_reason'] = 'context_budget'
            assert not flows.get(fid)['run']['resume_available']
            session.record['last_run']['stop_reason'] = original_reason
            session.cancelled.set()
            assert not flows.get(fid)['run']['resume_available']
            session.cancelled.clear()
            if not legacy:
                node = flows.records[fid]['nodes'][1]
                original_prompt = node['prompt']
                node['prompt'] = 'altered'
                assert not flows.get(fid)['run']['resume_available']
                node['prompt'] = original_prompt
            original_project = projects.records[project['id']]
            original_project['revision'] += 1
            assert not flows._snapshot(flows.records[fid])['run']['resume_available']
            original_project['revision'] -= 1
            with monkeypatch.context() as patch:
                patch.setattr(flows, '_write', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('disk')))
                with pytest.raises(OSError):
                    flows.resume(fid, run_id=before['id'])
            assert len(calls) == 2 and flows.get(fid)['run']['status'] == 'failed'
            flows.resume(fid, run_id=before['id'])
            with pytest.raises(ValueError):
                flows.resume(fid, run_id=before['id'])
            await asyncio.wait_for(flows.tasks[fid], 5)
            mid = flows.get(fid)['run']
            assert mid['status'] == 'failed' and mid['resume_available']
            assert mid['nodes']['c']['status'] == 'blocked'
            flows.resume(fid, run_id=before['id'])
            await asyncio.wait_for(flows.tasks[fid], 5)
            after = flows.get(fid)['run']
            assert after['id'] == before['id'] and after['status'] == 'completed'
            assert after['nodes']['a'] == first
            assert [name for name, _ in calls] == ['a', 'b', 'b', 'b', 'c']
            assert calls[2][1] == calls[3][1] == sid
            assert len(after['resume_attempts']) == 2
        finally:
            await manager.shutdown()
    manager = None
    try:
        asyncio.run(run())
    finally:
        store.close()


def test_start_releases_reservation_if_run_metadata_fails(tmp_path, monkeypatch, sandbox_env):
    store = SessionStore(tmp_path / 'state', tmp_path / 'private')
    async def run():
        manager = SessionManager(store)
        flows = FlowService(manager)
        projects = ProjectService(manager, flows)
        try:
            template = flows.create()
            template['nodes'] = [{'id': 'a', 'title': 'a', 'prompt': 'a'}]
            template = flows.save(template)
            project = projects.create('项目', str(sandbox_env), flow_id=template['id'])
            monkeypatch.setattr(flows, '_new_run', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('disk')))
            with pytest.raises(OSError):
                flows.start(project['flow_id'])
            assert not manager.project_reservations
            assert project['flow_id'] not in flows.tasks
        finally:
            await manager.shutdown()
    try:
        asyncio.run(run())
    finally:
        store.close()


@pytest.mark.parametrize('failure', [TimeoutError, ModelConnectionError])
@pytest.mark.parametrize('checkpoint_stage, outcome', [
    ('pending', 'blocked'),
    ('pending', 'restaged'),
    ('unidentified', 'restaged'),
    ('signed', 'blocked'),
    ('signed', 'verified'),
    ('signed', 'duplicate'),
    ('signed_selector', 'duplicate'),
    ('signed', 'reader_verified'),
    ('signed', 'reader_wrong'),
    ('signed', 'reader_other_data'),
    ('signed', 'reader_other_path'),
    ('signed', 'unknown_writer'),
])
def test_resume_keeps_dynamic_action_handoff_constraints(
        runtime_store, monkeypatch, failure, checkpoint_stage, outcome):
    """断线后，未提交的草稿与已签发的编号仍约束同一 Flow 的交接。"""
    store, project = runtime_store
    monkeypatch.setattr(config, 'FLOW_AUTO_ARCHIVE', False)
    monkeypatch.setattr(config, 'MODEL_RECOVERY_LIMIT', 0)
    writes = []
    phases = []
    resume_calls = 0
    resumed = False
    action = {'feedback_id': 'CF-01', 'diagnosis': '页面显示空白'}
    definition = ticket_tool()
    signed = checkpoint_stage in {'signed', 'signed_selector'}
    if checkpoint_stage == 'signed_selector':
        action = {'id': 'CF-01'}
        definition['parameters'] = {'type': 'object', 'properties': {'id': {'type': 'string'}},
                                    'required': ['id'], 'additionalProperties': False}
    if checkpoint_stage == 'unidentified':
        action = {'customer': 'CF-01', 'diagnosis': '页面显示空白'}
        definition['parameters']['properties']['customer'] = definition['parameters']['properties'].pop('feedback_id')
        definition['parameters']['required'] = ['customer', 'diagnosis']
    contract = {'fields': {'submitted_ticket_ids': {'type': 'array'}}}
    readers = []
    if outcome in {'reader_verified', 'reader_wrong', 'reader_other_data', 'reader_other_path', 'unknown_writer'}:
        path = Path(__file__).resolve().parents[1] / 'scripts/cases/customer_feedback_dynamic.template.json'
        template = json.loads(path.read_text(encoding='utf-8'))
        configured_reader = next(tool for node in template['nodes'] for tool in node.get('custom_tools', ())
                                 if tool['name'] == 'read_feedback')
        input_path = project / 'feedback.json'
        input_data = {'items': []}
        if outcome == 'reader_other_data':
            input_path = project / 'deliveries.json'
            input_data = {'deliveries': []}
            configured_reader['code'] = chr(10).join((
                'import json as codec, os as platform',
                'def main(options):',
                "    source_name = platform.path.join('.', 'deliveries.json')",
                "    with open(source_name, 'r', encoding='utf-8') as handle:",
                '        manifest = codec.load(handle)',
                "    if not isinstance(manifest, dict) or not isinstance(manifest.get('deliveries'), list):",
                "        raise ValueError('invalid delivery manifest')",
                '    return manifest',
            ))
        elif outcome == 'reader_other_path':
            input_path = project / 'inventory.json'
            input_data = {'inventory': []}
            configured_reader['code'] = chr(10).join((
                'def main(options):',
                '    import json as codec',
                '    from pathlib import Path as LocalPath',
                "    inventory = codec.loads(LocalPath('inventory.json').read_text(encoding='utf-8'))",
                '    return inventory',
            ))
        if outcome == 'unknown_writer':
            configured_reader['code'] = ('def main(arguments):' + chr(10)
                + "    with open('read-marker.txt', 'w') as stream:" + chr(10)
                + "        stream.write('side effect')" + chr(10) + "    return {'items': []}" + chr(10))
        readers.append(configured_reader)
        input_path.write_text(json.dumps(input_data), encoding='utf-8')
        if outcome in {'reader_verified', 'reader_wrong'}:
            (project / 'json.py').write_text("open('shadow-marker.txt', 'w').close()" + chr(10)
                                            + "raise RuntimeError('unexpected workspace import')" + chr(10),
                                            encoding='utf-8')
        if os.name == 'nt' and os.environ.get('RUN_WINDOWS_WSL_TESTS') != '1':
            from agent_core.core.custom_tools import CustomTool

            original_run = CustomTool.run

            def controlled_native_run(tool, arguments):
                if tool.name != 'read_feedback':
                    return original_run(tool, arguments)
                assert tool.definition == configured_reader and arguments == '{}'
                if outcome == 'unknown_writer':
                    (project / 'read-marker.txt').write_text('side effect', encoding='utf-8')
                result = input_path.read_text(encoding='utf-8')
                return chr(10).join(('[退出码] 0', '[stdout]', result))

            monkeypatch.setattr(CustomTool, 'run', controlled_native_run)

    async def model(history, *, session_id, emit):
        nonlocal resume_calls
        session = manager.sessions[session_id]
        if session.record['flow_node_id'] == 'next':
            return assistant('下游已收到交接')
        payload = review_payload(history)
        if payload is not None:
            return assistant('{"status":"pass"}')
        schemas = selected_schemas(TOOL_SCHEMAS)
        names = {row['function']['name'] for row in schemas}
        phases.append(names)
        if len(phases) == 1:
            preview = next(row['function']['name'] for row in schemas
                           if 'submit_ticket' in row['function'].get('description', ''))
            return assistant(calls=[call('preview', preview, **action)])
        if len(phases) == 2 and signed:
            assert names == {'submit_ticket'}
            return assistant(calls=[call('commit', 'submit_ticket', **action)])
        if not resumed:
            raise failure('offline fixture')
        resume_calls += 1
        if resume_calls == 1:
            assert names != {'submit_ticket'}, '续跑不得恢复模型的旧批准'
            if outcome in {'restaged', 'duplicate'}:
                preview = next(row['function']['name'] for row in schemas
                               if 'submit_ticket' in row['function'].get('description', ''))
                return assistant(calls=[call('new-preview', preview, **action)])
        if outcome == 'restaged' and resume_calls == 2:
            assert names == {'submit_ticket'}
            reordered = dict(reversed(action.items()))
            return assistant(calls=[call('new-commit', 'submit_ticket', **reordered)])
        if readers and resume_calls == 1:
            assert 'draft_read_feedback' in names and 'read_feedback' not in names
            return assistant(calls=[call('read-preview', 'draft_read_feedback')])
        if readers and resume_calls == 2:
            assert names == {'read_feedback'}
            return assistant(calls=[call('read-commit', 'read_feedback')])
        if outcome == 'duplicate' and names == {'submit_ticket'}:
            return assistant(calls=[call('duplicate-commit', 'submit_ticket', **action)])
        if outcome in {'blocked', 'reader_wrong'}:
            if not names and signed:
                return assistant('{"set":{"submitted_ticket_ids":["WRONG"]}}')
            return assistant('{"submitted_ticket_ids":["WRONG"]}')
        return assistant('{"submitted_ticket_ids":["ISSUED-1"]}')

    async def run():
        nonlocal manager, resumed
        def setup():
            manager = SessionManager(store, model=model)
            flows = FlowService(manager)
            projects = ProjectService(manager, flows)
            original_tool = manager._tool

            async def write_ticket(session, tool_call, *, evidence=None):
                if tool_call['function']['name'] != 'submit_ticket':
                    return await original_tool(session, tool_call, evidence=evidence)
                ledger = project / 'tickets.json'
                writes.append(json.loads(tool_call['function']['arguments']))
                ledger.write_text(json.dumps(writes), encoding='utf-8')
                evidence['source'] = 'executed'
                return '[退出码] 0\n[stdout]\n{"ok":true,"ticket":{"ticket_id":"ISSUED-1"}}'

            monkeypatch.setattr(manager, '_tool', write_ticket)
            return manager, flows, projects

        manager, flows, projects = setup()
        try:
            record = projects.create('办理续跑', str(project), permission_policy='full_access')
            fid = record['flow_id']
            flow = flows.get(fid)
            flow.update(nodes=[
                {'id': 'work', 'title': '办理',
                 'prompt': ('调用 read_feedback 读取反馈。' + chr(10) if readers else '') + '提交工单，输出签发编号。',
                 'y': 0.0, 'tool_names': ['submit_ticket', *(tool['name'] for tool in readers)],
                 'custom_tools': [definition, *readers],
                 'handoff_contract': contract},
                {'id': 'next', 'title': '下游', 'prompt': '核对交接', 'x': 120.0, 'y': 0.0, 'tool_names': []},
            ], edges=[{'source': 'work', 'target': 'next'}])
            flows.save(flow)
            # 模拟旧版缺省 x 原样落盘，y 仍是调用者提供的浮点零。
            flows.records[fid]['nodes'][0]['x'] = 0
            flows.start(fid)
            await asyncio.wait_for(flows.tasks[fid], 5)
            before = flows.get(fid)['run']
            assert before['resume_available'], before['error']
            assert len(writes) == (signed)
            sid = before['nodes']['work']['session_id']
            session = manager.sessions[sid]
            expected_checkpoint = copy.deepcopy(session.record['_flow_action_checkpoint'])
            exported = json.loads(store.export(session.record, 'json').read_text(encoding='utf-8'))
            assert '_flow_action_checkpoint' not in exported
            await manager.shutdown()
            manager, flows, projects = setup()
            flows._resume_plan(flows.records[fid], before['id'])
            assert flows.get(fid)['run']['resume_available']
            node = flows.records[fid]['nodes'][0]
            assert type(node['x']) is int and type(node['y']) is float
            node['x'] = 1.0
            assert not flows.get(fid)['run']['resume_available']
            node['x'] = 0
            session_record = manager.sessions[sid].record
            assert session_record['_flow_action_checkpoint'] == json.loads(json.dumps(expected_checkpoint))
            if checkpoint_stage == 'unidentified':
                action_state = session_record['_flow_action_checkpoint']['actions']
                pending = action_state['pending']
                assert len(pending) == 1 and pending[0][1].startswith('arguments:')
                action_state['pending'] = [['submit_ticket', hashlib.sha256(b'{}').hexdigest()]]
                assert not flows.get(fid)['run']['resume_available']
                with pytest.raises(ValueError, match='无法区分未标识动作'):
                    flows.resume(fid, run_id=before['id'])
                action_state['pending'] = pending
            resumed = True
            flows.resume(fid, run_id=before['id'])
            await asyncio.wait_for(flows.tasks[fid], 5)
            after = flows.get(fid)['run']
            assert after['id'] == before['id']
            assert after['nodes']['work']['session_id'] == sid
            session = manager.sessions[sid]
            assert '_flow_action_checkpoint' not in session.record
            if readers:
                reads = session.record['last_run']['flow_required_reads']
                assert reads['completed'] == ['read_feedback']
                metric = next(row for row in session.record['last_run']['tools'] if row['tool_call_id'] == 'read-commit')
                assert (metric.get('receipt_reader') is True) == (outcome != 'unknown_writer')
                assert not (project / 'shadow-marker.txt').exists()
            if outcome in {'blocked', 'reader_wrong', 'unknown_writer'}:
                assert after['nodes']['next']['status'] == 'blocked'
                if outcome == 'reader_wrong':
                    assert resume_calls == 4, '保留已签发回执后，错误编号仍应触发一次真实编号修正请求'
                if outcome == 'unknown_writer':
                    assert (project / 'read-marker.txt').exists()
                expected = 'flow_handoff_receipts' if signed else 'flow_action_expired'
                assert session.record['last_run']['stop_reason'] == expected
                assert len(writes) == (signed)
            else:
                assert after['nodes']['next']['status'] == 'completed', after['nodes']['work']['error']
                assert len(writes) == 1
                if outcome == 'duplicate':
                    assert [event['status'] for event in session.record['last_run']['action_preflights']] == ['already_completed']
        finally:
            await manager.shutdown()

    manager = None
    asyncio.run(run())


@pytest.mark.parametrize('case', [
    'success', 'legacy', 'twice', 'semantic', 'semantic_revoked', 'semantic_tampered', 'failed', 'unpaired', 'tampered', 'duplicate', 'duplicate_after_resume',
])
def test_resume_requires_only_reads_without_verifiable_completion(runtime_store, monkeypatch, case):
    store, project = runtime_store
    monkeypatch.setattr(config, 'FLOW_AUTO_ARCHIVE', False)
    monkeypatch.setattr(config, 'MODEL_RECOVERY_LIMIT', 0)
    if case == 'twice':
        monkeypatch.setattr(config, 'RUN_HISTORY_LIMIT', 1)
    complete = case in {'success', 'legacy', 'twice', 'semantic'}
    semantic = case.startswith('semantic')
    feedback = {'items': [{'id': 'CF-01', 'text': '页面无法提交，请帮忙排查。提示显示 [输出已截断]。'}]} if semantic else {'items': []}
    if case == 'legacy':
        feedback = {'items': [{'id': 'CF-01', 'text': '原文包含 [输出已截断] 以及已截断）'}]}
    (project / 'feedback.json').write_text(json.dumps(feedback, ensure_ascii=False), encoding='utf-8')
    (project / 'unrelated.txt').write_text('先前的无关证据。', encoding='utf-8')
    observed_review = []
    # 纯读但有独立变量，不匹配特殊只读代码的结构。运行结果仍能满足必做读取。
    reader = {
        'name': 'read_feedback', 'description': '只读取当前工作区反馈记录。',
        'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
        'code': chr(10).join(('def main(arguments):', '    import json',
                              "    with open('feedback.json', 'r', encoding='utf-8') as stream:",
                              '        data = json.load(stream)',
                              "    return {'items': data['items']}")),
    }
    if case == 'failed':
        reader['code'] = 'def main(arguments):' + chr(10) + "    return {'ok': False, 'error': '读取失败'}"
    if os.name == 'nt' and os.environ.get('RUN_WINDOWS_WSL_TESTS') != '1':
        from agent_core.core.custom_tools import CustomTool

        original_run = CustomTool.run

        def controlled_native_run(tool, arguments):
            if tool.name != 'read_feedback':
                return original_run(tool, arguments)
            assert tool.definition['code'] == reader['code'] and arguments == '{}'
            value = ('{"ok":false,"error":"读取失败"}' if case == 'failed'
                     else (project / 'feedback.json').read_text(encoding='utf-8'))
            return chr(10).join(('[退出码] 0', '[stdout]', value))

        monkeypatch.setattr(CustomTool, 'run', controlled_native_run)
    initial_calls = 0
    resumed_calls = 0
    resumed = False

    async def model(history, *, session_id, emit):
        nonlocal initial_calls, resumed_calls
        session = manager.sessions[session_id]
        if session.record['flow_node_id'] == 'next':
            return assistant('已收到交接')
        names = {row['function']['name'] for row in selected_schemas(TOOL_SCHEMAS)}
        payload = review_payload(history)
        if semantic and payload is not None:
            observed_review.append(payload)
            assert case == 'semantic', '撤销授权或篡改请求后不能交付旧回执'
            assert len(payload['sources']) == 1
            source = payload['sources'][0]
            assert source['kind'] == 'custom_result' and source['scope'] == 'tool_report'
            assert source['tool'] == 'read_feedback' and json.loads(source['text']) == feedback
            assert '先前的无关证据' not in source['text']
            return assistant('{"status":"pass"}')
        if not resumed:
            initial_calls += 1
            if semantic and initial_calls == 1:
                return assistant(calls=[call('unrelated-read', 'read_file', path='unrelated.txt')])
            phase = initial_calls - semantic
            if phase == 1:
                assert 'draft_read_feedback' in names
                return assistant(calls=[call('initial-draft', 'draft_read_feedback')])
            if phase == 2:
                assert names == {'read_feedback'}
                return assistant(calls=[call('initial-read', 'read_feedback')])
            if case == 'duplicate' and initial_calls == 3:
                return assistant(calls=[call('initial-read', 'read_file', path='feedback.json')])
            raise TimeoutError('受控离线超时')
        resumed_calls += 1
        if case == 'twice' and resumed_calls == 1:
            raise ModelConnectionError('受控第二次断线')
        if case == 'duplicate_after_resume' and resumed_calls == 1:
            return assistant(calls=[call('initial-read', 'read_file', path='feedback.json')])
        if resumed_calls == 2 and case == 'success':
            assert 'draft_read_feedback' in names
            return assistant(calls=[call('unnecessary-redraft', 'draft_read_feedback')])
        if semantic:
            if case == 'semantic_revoked':
                manager.permission_override = 'readonly'
            if case == 'semantic_tampered':
                calls = [entry for row in session.record['messages'] for entry in row.get('tool_calls', ())]
                original = next(entry for entry in calls if entry['id'] == 'initial-read')
                original['function']['arguments'] = '{"path":"unrelated.txt"}'
            return assistant('{"read_checked":true,"notes":"客户明确要求排查无法提交的问题。"}')
        return assistant('{"read_checked":true}')

    async def run():
        nonlocal manager, resumed
        def setup():
            loaded_manager = SessionManager(store, model=model)
            flows = FlowService(loaded_manager)
            return loaded_manager, flows, ProjectService(loaded_manager, flows)

        manager, service, projects = setup()
        try:
            project_record = projects.create('必做读取续跑', str(project), permission_policy='full_access')
            fid = project_record['flow_id']
            flow = service.get(fid)
            flow.update(nodes=[
                {'id': 'work', 'title': '读取', 'prompt': '调用 read_feedback 读取工作区反馈。',
                 'tool_names': ['read_file', 'read_feedback'], 'custom_tools': [reader],
                 'handoff_contract': {'fields': {'read_checked': {'type': 'boolean'},
                                                 **({'notes': {'type': 'string'}} if semantic else {})}}},
                {'id': 'next', 'title': '下游', 'prompt': '核对交接', 'tool_names': []},
            ], edges=[{'source': 'work', 'target': 'next'}])
            service.save(flow)
            service.start(fid)
            await service.tasks[fid]
            before = service.get(fid)['run']
            assert before['resume_available']
            sid = before['nodes']['work']['session_id']
            if case == 'legacy':
                original = manager.sessions[sid]
                original.record['_flow_action_checkpoint'].pop('required_reads')
                await manager.save(original)
            await manager.shutdown()
            manager, service, projects = setup()
            assert service.get(fid)['run']['resume_available']
            record = manager.sessions[sid].record
            if case in {'unpaired', 'tampered'}:
                message = next(row for row in record['messages'] if row.get('tool_call_id') == 'initial-read')
                if case == 'unpaired':
                    message['_event_id'] = 'unpaired'
                else:
                    message['content'] = message['content'].replace('"items": []', '"items": [1]').replace('"items":[]', '"items":[1]')
            if case == 'success':
                node = service.records[fid]['nodes'][0]
                field = node['handoff_contract']['fields']['read_checked']
                field['type'] = 'integer'
                assert not service.get(fid)['run']['resume_available']
                field['type'] = 'boolean'
                binding = manager.workspaces.owner_binding(manager.sessions[sid])
                manager.workspaces.bindings[sid] = replace(binding, identity=(0, 0))
                assert not service.get(fid)['run']['resume_available']
                manager.workspaces.bindings[sid] = binding
            resumed = True
            for _ in range(2 if case == 'twice' else 1):
                service.resume(fid, run_id=before['id'])
                await service.tasks[fid]
            after = service.get(fid)['run']
            if complete:
                assert after['status'] == 'completed', after['nodes']['work']['error']
                assert after['nodes']['next']['session_id'] and resumed_calls == (2 if case == 'twice' else 1)
                assert record['last_run']['flow_required_reads']['completed'] == ['read_feedback']
                if case == 'semantic':
                    assert len(observed_review) == 1
            else:
                assert after['nodes']['next']['status'] == 'blocked'
                if semantic:
                    assert not observed_review
                else:
                    assert record['last_run']['flow_required_reads']['completed'] == []
        finally:
            await service.shutdown()
            await manager.shutdown()

    manager = None
    asyncio.run(run())
