"""Safety and evidence tests for the opt-in real-model validation harness."""

from pathlib import Path
import uuid

import pytest

from agent_test.scripts.real_multiagent_validation import (
    isolate_experiment,
    summarize_runs,
    summarize_trace,
    validate_new_root,
)


def test_new_experiment_root_accepts_only_unused_system_temp_child(tmp_path):
    name = f"agent-real-multi-validation-{uuid.uuid4().hex}"
    assert validate_new_root(tmp_path / name, temp_root=tmp_path) == tmp_path / name

    existing = tmp_path / f"agent-real-multi-validation-{uuid.uuid4().hex}"
    existing.mkdir()
    with pytest.raises(ValueError, match="new"):
        validate_new_root(existing, temp_root=tmp_path)
    with pytest.raises(ValueError, match="directly"):
        validate_new_root(tmp_path / "nested" / name, temp_root=tmp_path)
    with pytest.raises(ValueError, match="new"):
        validate_new_root(tmp_path / "unsafe", temp_root=tmp_path)


def test_new_experiment_root_rejects_symlink(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / f"agent-real-multi-validation-{uuid.uuid4().hex}"
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="new"):
        validate_new_root(linked, temp_root=tmp_path)


def test_isolate_experiment_redirects_runtime_paths(tmp_path, monkeypatch):
    class Settings:
        pass

    settings = Settings()
    settings.TOOL_ROOTS = {"host": {"path": Path("/host")}}
    settings.RAG_SOURCES = {"host": {"path": Path("/host")}}
    root = tmp_path / "experiment"
    root.mkdir()
    isolate_experiment(settings, root)
    assert settings.AGENT_STATE_DIR == root / "state"
    assert settings.SANDBOX_DIR == root / "work"
    assert settings.RAG_CACHE_DIR == root / "rag-cache"
    assert settings.DOC_DIR == root / "knowledge"
    assert settings.AUDIT_LOG == root / "audit.jsonl"
    assert settings.TOOL_ROOTS == settings.RAG_SOURCES == {}
    assert settings.DEBUG and settings.ENABLE_AGENT_TASKS and settings.ENABLE_SESSION_BUDGETS
    assert settings.MAX_TOOL_ROUNDS == 0


def test_usage_report_aggregates_real_metric_fields_without_content():
    record = {"run_history": [{"turn_id": "first", "model_calls": [{"input_tokens": 30,
        "output_tokens": 7, "tokens_source": "provider", "estimated_cost_usd": 0.02,
        "api_key": "secret"}], "tools": [{"name": "run_command", "source": "executed",
        "result": "private output"}]}], "last_run": {"turn_id": "second", "model_calls": [
        {"input_tokens": 10, "output_tokens": 2, "tokens_source": "estimate"}], "tools": []}}
    summary = summarize_runs(record)
    assert summary["rounds"] == 2
    assert summary["model_calls"] == 2
    assert summary["provider_usage_calls"] == 1
    assert summary["input_tokens"] == 40
    assert summary["unknown_cost_calls"] == 1
    assert summary["tool_sources"] == {"executed": 1}
    assert "secret" not in str(summary) and "private output" not in str(summary)


def test_completed_run_is_not_counted_twice_when_last_run_is_retained():
    run = {"turn_id": "same", "model_calls": [{"input_tokens": 42,
           "output_tokens": 5, "tokens_source": "provider"}],
           "tools": [{"name": "write_file", "source": "executed", "execution_progress": True}]}
    summary = summarize_runs({"run_history": [run], "last_run": run})
    assert summary["rounds"] == summary["model_calls"] == summary["tool_calls"] == 1
    assert summary["input_tokens"] == 42
    assert summary["execution_progress_calls"] == 1


def test_trace_summary_counts_actual_communication_and_compaction():
    timeline = [{"kind": "message.sent"}, {"kind": "message.read"},
                {"kind": "context.compacted"},
                {"kind": "turn.stopped", "reason": "completed"}]
    summary = summarize_trace(timeline)
    assert summary["messages_sent"] == summary["messages_read"] == 1
    assert summary["compactions"] == 1
    assert summary["terminal_reasons"] == {"completed": 1}
