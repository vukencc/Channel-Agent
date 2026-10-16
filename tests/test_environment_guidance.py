"""Shared file workflow guidance must lead agents through real environment checks."""

from ai_agent_startup.core.prompts import FILE_WORKFLOW_GUIDE, file_workflow_guide


def test_environment_guidance_is_shared_with_restored_sessions():
    guide = file_workflow_guide()
    assert guide == FILE_WORKFLOW_GUIDE
    assert 'inspect_environment' in guide
    assert 'prepare_project_environment' in guide


def test_environment_guidance_distinguishes_preparation_from_validation():
    guide = file_workflow_guide()
    assert 'build' in guide.lower() or '构建' in guide
    assert 'test' in guide.lower() or '测试' in guide
    assert '静态' in guide
    assert '真实' in guide or '实际' in guide


def test_environment_guidance_reports_execution_limits_truthfully():
    guide = file_workflow_guide()
    assert '沙箱' in guide or '隔离' in guide
    assert '资源' in guide or '配额' in guide
    assert '拒绝' in guide or '取消' in guide
