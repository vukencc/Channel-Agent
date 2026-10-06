"""Configuration URL failures should be reported before SDK construction."""

import asyncio
import time
from urllib.parse import urlparse

import pytest

from ai_agent_startup import config
from ai_agent_startup.core import llm


PROXY_NAMES = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
)
NO_PROXY_NAMES = ("NO_PROXY", "no_proxy")


@pytest.fixture
def valid_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "API_KEY", "test-key")
    monkeypatch.setattr(config, "BASE_URL", "https://host.test/v1")
    monkeypatch.setattr(config, "MODEL", "test-model")
    monkeypatch.setattr(config, "MODEL_FALLBACKS", [])
    monkeypatch.setattr(config, "EMBEDDING_MODEL_SOURCE", "LOCAL")
    monkeypatch.setattr(config, "EMBEDDING_MODEL_URL", None)
    monkeypatch.setattr(config, "DOC_DIR", tmp_path)
    monkeypatch.setattr(config, "SANDBOX_DIR", tmp_path / "sandbox")
    for name in (*PROXY_NAMES, *NO_PROXY_NAMES):
        monkeypatch.delenv(name, raising=False)


def test_primary_newline_reports_exact_position_without_url(valid_runtime, monkeypatch):
    url = "https://host.test\n/v1"
    assert url.index("\n") == 17
    assert urlparse(url).hostname == "host.test"
    monkeypatch.setattr(config, "BASE_URL", url)

    with pytest.raises(ValueError) as caught:
        config.validate_runtime_config()

    message = str(caught.value)
    assert "BASE_URL" in message
    assert "17" in message
    assert "U+000A" in message
    assert url not in message


@pytest.mark.parametrize("url", [
    "https://user:private-token@host.test\r/v1",
    "https://host.test\t/v1",
    "https://host.test\x7f/v1",
], ids=["carriage-return", "tab", "delete"])
def test_fallback_control_reports_safe_diagnostic(valid_runtime, monkeypatch, url):
    monkeypatch.setattr(config, "MODEL_FALLBACKS", [
        {"model": "fallback", "base_url": url, "api_key_env": "FALLBACK_TEST_KEY"},
    ])
    monkeypatch.setenv("FALLBACK_TEST_KEY", "test-key")

    with pytest.raises(ValueError) as caught:
        config.validate_runtime_config()

    message = str(caught.value)
    assert "MODEL_FALLBACKS" in message
    assert url not in message
    assert "private-token" not in message


@pytest.mark.parametrize("codepoint", [*range(32), 127], ids=lambda value: f"U+{value:04X}")
def test_http_url_rejects_every_ascii_control(codepoint):
    url = f"https://host.test{chr(codepoint)}/v1"
    with pytest.raises(ValueError) as caught:
        config.validate_http_url(url, "BASE_URL")
    message = str(caught.value)
    assert "BASE_URL" in message
    assert f"U+{codepoint:04X}" in message
    assert str(url.index(chr(codepoint))) in message
    assert url not in message


@pytest.mark.parametrize("url", [
    "ftp://host.test/v1",
    "https://host.test:invalid/v1",
    "https://[::1/v1",
], ids=["unsupported-scheme", "invalid-port", "unclosed-ipv6"])
def test_http_url_rejects_unsupported_or_malformed_address(url):
    with pytest.raises(ValueError) as caught:
        config.validate_http_url(url, "BASE_URL")
    assert "BASE_URL" in str(caught.value)
    assert url not in str(caught.value)


def test_primary_malformed_port_fails_runtime_validation(valid_runtime, monkeypatch):
    monkeypatch.setattr(config, "BASE_URL", "https://host.test:invalid/v1")
    with pytest.raises(ValueError, match="BASE_URL"):
        config.validate_runtime_config()


@pytest.mark.parametrize("function,endpoint", [
    ("get_client", None),
    ("endpoint_client", {"base_url": "https://user:private-token@host.test\n/v1"}),
])
def test_client_rejects_bad_url_before_sdk_construction(
    valid_runtime, monkeypatch, function, endpoint,
):
    monkeypatch.setattr(llm, "_client", None)
    monkeypatch.setattr(llm, "_fallback_clients", {})
    if function == "get_client":
        monkeypatch.setattr(config, "BASE_URL", "https://user:private-token@host.test\n/v1")
    monkeypatch.setattr(llm, "AsyncOpenAI", lambda **kwargs: pytest.fail("SDK constructed"))

    with pytest.raises(ValueError) as caught:
        getattr(llm, function)(endpoint) if endpoint is not None else llm.get_client()

    message = str(caught.value)
    assert "BASE_URL" in message or "MODEL_FALLBACKS" in message
    assert "private-token" not in message
    assert "https://" not in message


@pytest.mark.parametrize("name", PROXY_NAMES)
def test_proxy_newline_fails_runtime_validation(valid_runtime, monkeypatch, name):
    monkeypatch.setenv(name, "http://proxy.test\n:8080")
    with pytest.raises(ValueError) as caught:
        config.validate_runtime_config()
    assert name in str(caught.value)
    assert "http://proxy.test" not in str(caught.value)


@pytest.mark.parametrize("function,endpoint", [
    ("get_client", None),
    ("endpoint_client", {"base_url": "https://fallback.test/v1"}),
])
def test_proxy_newline_fails_before_client_construction(
    valid_runtime, monkeypatch, function, endpoint,
):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test\n:8080")
    monkeypatch.setattr(llm, "_client", None)
    monkeypatch.setattr(llm, "_fallback_clients", {})
    monkeypatch.setattr(llm, "AsyncOpenAI", lambda **kwargs: pytest.fail("SDK constructed"))
    with pytest.raises(ValueError, match="HTTPS_PROXY"):
        getattr(llm, function)(endpoint) if endpoint is not None else llm.get_client()


def test_embedding_url_is_checked_only_for_api_source(valid_runtime, monkeypatch):
    monkeypatch.setattr(config, "EMBEDDING_MODEL_URL", "https://host.test\n/embeddings")
    config.validate_runtime_config()

    monkeypatch.setattr(config, "EMBEDDING_MODEL_SOURCE", "API")
    with pytest.raises(ValueError, match="EMBEDDING_MODEL_URL"):
        config.validate_runtime_config()


def test_normal_url_constructs_real_client_without_network(valid_runtime, monkeypatch):
    monkeypatch.setattr(llm, "_client", None)
    client = llm.get_client()
    try:
        assert str(client.base_url) == "https://host.test/v1/"
    finally:
        asyncio.run(client.close())


@pytest.mark.parametrize("scheme", ["http", "https", "socks5", "socks5h"])
def test_supported_proxy_schemes_are_accepted(valid_runtime, monkeypatch, scheme):
    monkeypatch.setenv("HTTPS_PROXY", f"{scheme}://proxy.test:8080")
    config.validate_runtime_config()


def test_unsupported_proxy_scheme_is_rejected(valid_runtime, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "ftp://proxy.test:8080")
    with pytest.raises(ValueError, match="HTTPS_PROXY"):
        config.validate_runtime_config()


@pytest.mark.parametrize("name", NO_PROXY_NAMES)
def test_no_proxy_control_reports_safe_position(valid_runtime, monkeypatch, name):
    monkeypatch.setenv(name, "host.test,private-token\n.example")
    with pytest.raises(ValueError) as caught:
        config.validate_runtime_config()
    message = str(caught.value)
    assert name in message
    assert "U+000A" in message
    assert "23" in message
    assert "private-token" not in message


@pytest.mark.parametrize("bad_name,good_name", [
    ("https_proxy", "HTTPS_PROXY"),
    ("no_proxy", "NO_PROXY"),
])
def test_invalid_lowercase_proxy_overrides_valid_uppercase(
    valid_runtime, monkeypatch, bad_name, good_name,
):
    good_value = "host.test" if "NO_PROXY" in good_name.upper() else "http://proxy.test:8080"
    bad_value = "private-token\n.test" if "NO_PROXY" in bad_name.upper() else "http://proxy.test\n:8080"
    monkeypatch.setenv(good_name, good_value)
    monkeypatch.setenv(bad_name, bad_value)
    with pytest.raises(ValueError) as caught:
        config.validate_runtime_config()
    assert bad_name in str(caught.value)
    assert "private-token" not in str(caught.value)


@pytest.mark.parametrize("bad_name,good_name", [
    ("HTTPS_PROXY", "https_proxy"),
    ("NO_PROXY", "no_proxy"),
])
def test_valid_lowercase_proxy_overrides_invalid_uppercase(
    valid_runtime, monkeypatch, bad_name, good_name,
):
    bad_value = "private-token\n.test" if bad_name == "NO_PROXY" else "http://proxy.test\n:8080"
    good_value = "host.test" if good_name == "no_proxy" else "http://proxy.test:8080"
    monkeypatch.setenv(bad_name, bad_value)
    monkeypatch.setenv(good_name, good_value)
    config.validate_runtime_config()


@pytest.mark.parametrize("upper,lower", [
    ("HTTPS_PROXY", "https_proxy"),
    ("NO_PROXY", "no_proxy"),
])
def test_empty_lowercase_proxy_disables_invalid_uppercase(
    valid_runtime, monkeypatch, upper, lower,
):
    monkeypatch.setenv(upper, "private-token\n.test")
    monkeypatch.setenv(lower, "")
    config.validate_runtime_config()


def test_schemeless_proxy_is_accepted_by_runtime_and_client(valid_runtime, monkeypatch):
    """省略方案的代理由 HTTPX 按 HTTP 代理处理。"""
    monkeypatch.setenv("HTTPS_PROXY", "proxy.test:8080")
    config.validate_runtime_config()
    monkeypatch.setattr(llm, "_client", None)
    client = llm.get_client()
    try:
        assert str(client.base_url) == "https://host.test/v1/"
    finally:
        asyncio.run(client.close())


def test_no_proxy_wildcard_skips_invalid_proxy_urls(valid_runtime, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("HTTPS_PROXY", "private-token\n.test")
    monkeypatch.setenv("ALL_PROXY", "ftp://bad-proxy.test")
    config.validate_runtime_config()
    monkeypatch.setattr(llm, "_client", None)
    client = llm.get_client()
    try:
        assert str(client.base_url) == "https://host.test/v1/"
    finally:
        asyncio.run(client.close())


def test_no_proxy_list_accepts_boundary_whitespace(valid_runtime, monkeypatch):
    monkeypatch.setenv("NO_PROXY", " host.test , example.test ")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:8080")
    config.validate_runtime_config()


def test_no_proxy_list_wildcard_skips_invalid_proxy_urls(valid_runtime, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "host.test, * ,example.test")
    monkeypatch.setenv("HTTPS_PROXY", "private-token\n.test")
    config.validate_runtime_config()


@pytest.mark.parametrize("bad_name,bad_value", [
    ("BASE_URL", "https://host.test\n/v1"),
    ("NO_PROXY", "host.test,private-token\n.example"),
], ids=["primary-url", "proxy-bypass"])
def test_web_first_message_reports_configuration_error_and_releases_model_slot(
    valid_runtime, monkeypatch, tmp_path, bad_name, bad_value,
):
    """浏览器首轮失败应保留安全诊断，并归还模型与预算占位。"""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from ai_agent_startup.core.storage import SessionStore
    from ai_agent_startup.web.app import create_app

    monkeypatch.setattr(config, "ENABLE_AGENT_TASKS", False)
    monkeypatch.setattr(config, "MEMORY_AUTO_EXTRACT", False)
    monkeypatch.setattr(config, "RAG_ASSESS", False)
    monkeypatch.setattr(config, "MODEL_REQUESTS_PER_MINUTE", 10)
    monkeypatch.setattr(llm, "_client", None)
    monkeypatch.setattr(llm, "_fallback_clients", {})
    monkeypatch.setattr(llm, "AsyncOpenAI", lambda **kwargs: pytest.fail("SDK constructed"))
    if bad_name == "BASE_URL":
        monkeypatch.setattr(config, bad_name, bad_value)
    else:
        monkeypatch.setenv(bad_name, bad_value)

    store = SessionStore(tmp_path / "state", tmp_path / "workspace")
    try:
        app = create_app(store=store, token="local-test-token", model=llm.call_model)
        with TestClient(app, base_url="http://localhost", headers={
            "Authorization": "Bearer local-test-token",
        }) as client:
            created = client.post("/api/sessions", json={"title": "test"})
            assert created.status_code == 201
            identifier = created.json()["id"]
            sent = client.post(f"/api/sessions/{identifier}/messages", json={"text": "hello"})
            assert sent.status_code == 202
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                detail = client.get(f"/api/sessions/{identifier}").json()
                if detail["status"] == "error" and not detail["busy"]:
                    break
                time.sleep(0.01)
            else:
                pytest.fail("Web 会话未报告配置错误")

            assert bad_name in detail["error"]
            assert "U+000A" in detail["error"]
            assert "private-token" not in detail["error"]
            assert bad_value not in detail["error"]
            assert client.get("/api/sessions").json()["runtime"]["active_model_calls"] == 0
            assert app.state.manager.budget_ledger is not None
            assert app.state.manager.budget_ledger.state["pending"] == {}
    finally:
        store.close()
