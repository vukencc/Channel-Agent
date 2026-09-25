"""重试判定逻辑测试（不需要网络）。

重点覆盖那个网关偶发的「空 body 422」：它该重试，
而带 body 的 422（真正的参数错误）不该重试。
"""
import httpx
from openai import APIConnectionError, APITimeoutError, APIStatusError

from core.llm import _is_transient


def _status_error(status_code: int, body: str) -> APIStatusError:
    request = httpx.Request("POST", "https://example.test/chat/completions")
    response = httpx.Response(status_code, request=request, text=body)
    return APIStatusError("err", response=response, body=None)


def test_empty_422_is_transient():
    assert _is_transient(_status_error(422, "")) is True


def test_422_with_body_is_not_transient():
    assert _is_transient(_status_error(422, '{"error":"bad schema"}')) is False


def test_server_errors_are_transient():
    assert _is_transient(_status_error(500, "boom")) is True
    assert _is_transient(_status_error(503, "unavailable")) is True


def test_client_errors_are_not_transient():
    assert _is_transient(_status_error(400, "bad request")) is False
    assert _is_transient(_status_error(401, "unauthorized")) is False


def test_connection_errors_are_transient():
    request = httpx.Request("POST", "https://example.test/chat/completions")
    assert _is_transient(APIConnectionError(request=request)) is True
    assert _is_transient(APITimeoutError(request=request)) is True


def test_plain_exception_is_not_transient():
    assert _is_transient(ValueError("nope")) is False
