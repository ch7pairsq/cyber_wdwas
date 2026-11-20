"""DeepSeek 官方 API 通道（OpenAI 兼容）的离线回归；不访问网络、不调用真实模型。

2026-11-18 用户决定预留官方通道接口；2026-11-20 用户确认切换为默认主通道
（模型 ID：deepseek-flash / deepseek-v4-pro，密钥改用官方 key）。
本文件验证：线上格式翻译、响应解析、白名单、思考参数映射、传输安全姿态与配置校验。
"""
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

from ctf_ext.llm.openai_compat import (ALLOWED_MODELS, DEFAULT_BASE_URL, DEFAULT_MODEL,
                                       PROVIDER_NAME, DeepSeekOfficialProvider)
from ctf_ext.llm.provider import ProviderConfigError, ProviderError, ProviderTimeout, RateLimited
from ctf_ext.config import validate_config


class TransportError(Exception):
    pass


class TransportTimeout(TransportError):
    pass


class FakeResponse:
    def __init__(self, status=200, payload=None, chunks=None):
        self.status_code = status
        self.payload = payload if payload is not None else {
            "model": DEFAULT_MODEL,
            "choices": [{"message": {"role": "assistant", "content": '{"ok":true}'},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}}
        self.chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size):
        return iter(self.chunks if self.chunks is not None else [json.dumps(self.payload).encode()])

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, result):
        self.result = result
        self.trust_env = True
        self.calls = []
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self):
        self.closed = True


def invoke(result=None, **kwargs):
    session = FakeSession(result if result is not None else FakeResponse())
    fake = SimpleNamespace(Session=lambda: session, Timeout=TransportTimeout, RequestException=TransportError)
    with patch.dict(sys.modules, {"requests": fake}):
        response = DeepSeekOfficialProvider(api_key="test-key-not-a-real-secret").complete(
            [{"role": "user", "content": "离线测试"}], system="只用提供的题目", json_mode=True, **kwargs)
    return response, session


def expect(exception, fn):
    try:
        fn()
    except exception as error:
        return error
    raise AssertionError("expected " + exception.__name__)


def config():
    return {"concurrency": {"max_workers": 1, "max_project_workers": 1},
            "storage": {"db_path": "test.db"},
            "harness": {"protocol": "mock", "competition_locked": False}}


def test_chat_completions_payload_headers_and_usage():
    response, session = invoke()
    url, kwargs = session.calls[0]
    assert url == DEFAULT_BASE_URL + "/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer test-key-not-a-real-secret"
    assert "anthropic-version" not in kwargs["headers"]
    assert kwargs["json"]["model"] == DEFAULT_MODEL and kwargs["json"]["stream"] is False
    assert kwargs["json"]["messages"][0]["role"] == "system"
    assert "JSON" in kwargs["json"]["messages"][0]["content"]
    assert kwargs["json"]["messages"][1] == {"role": "user", "content": "离线测试"}
    assert "response_format" not in kwargs["json"]
    assert kwargs["allow_redirects"] is False and kwargs["verify"] is True
    assert session.trust_env is False and session.closed and session.result.closed
    assert response.usage == {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}
    assert response.stop_reason == "stop"


def test_rejects_other_endpoints_and_models():
    for url in (DEFAULT_BASE_URL + ".evil", DEFAULT_BASE_URL + "/v1", "http://api.deepseek.com/"):
        expect(ProviderConfigError, lambda url=url: DeepSeekOfficialProvider(api_key="x", base_url=url))
    for model in ("deepseek-chat", "deepseek-reasoner", "gpt-4o", "other"):
        expect(ProviderConfigError, lambda model=model: DeepSeekOfficialProvider(api_key="x", model=model))
    # 官方双模型均应可构造（默认 flash；换挡 pro 走 model_override）。
    assert DeepSeekOfficialProvider(api_key="x", model="deepseek-v4-pro")._model == "deepseek-v4-pro"


def test_thinking_budget_maps_to_official_fields():
    # 攻坚档（>=8000）：thinking enabled + reasoning_effort=high（官方示例口径）。
    _, session = invoke(thinking_budget=8000)
    payload = session.calls[0][1]["json"]
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "high"
    # 轻思考档：只开 thinking，不指定 effort（由官方默认）。
    _, session = invoke(thinking_budget=4000)
    payload = session.calls[0][1]["json"]
    assert payload["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in payload
    # 侦察档（0/未传）：不下发思考字段。
    _, session = invoke()
    assert "thinking" not in session.calls[0][1]["json"]
    assert "reasoning_effort" not in session.calls[0][1]["json"]


def test_missing_key_fails_before_transport():
    with patch.dict(os.environ, {}, clear=True):
        expect(ProviderConfigError, lambda: DeepSeekOfficialProvider().complete([]))


def test_redirect_and_error_body_are_not_followed_or_logged():
    for status in (301, 302, 307, 401, 500):
        error = expect(ProviderError, lambda: invoke(FakeResponse(status=status, payload={"secret": "DO_NOT_LOG"})))
        assert "DO_NOT_LOG" not in str(error)


def test_rate_limit_and_timeout_classification():
    expect(RateLimited, lambda: invoke(FakeResponse(status=429)))
    error = expect(ProviderTimeout, lambda: invoke(TransportTimeout("SECRET")))
    assert "SECRET" not in str(error)


def test_transport_failure_does_not_expose_secret():
    error = expect(ProviderError, lambda: invoke(TransportError("SECRET")))
    assert "SECRET" not in str(error)


def test_tool_definitions_sent_in_openai_format_and_calls_parsed_back():
    definition = {"name": "file_info", "description": "identify file",
                  "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}
    response, session = invoke(tools=[definition])
    wire = session.calls[0][1]["json"]["tools"]
    assert wire == [{"type": "function", "function": {"name": "file_info",
                     "description": "identify file",
                     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]
    assert response.text == '{"ok":true}'

    call = FakeResponse(payload={"model": DEFAULT_MODEL, "choices": [{"message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "t1", "type": "function",
                        "function": {"name": "file_info", "arguments": '{"path": "a"}'}}]},
        "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2}})
    response, _ = invoke(call, tools=[definition])
    assert response.text == ""
    assert response.tool_calls == [{"id": "t1", "name": "file_info", "input": {"path": "a"}}]
    assert response.stop_reason == "tool_calls"


def test_internal_anthropic_style_messages_are_translated():
    messages = [
        {"role": "user", "content": "题目描述"},
        {"role": "assistant", "content": [{"type": "text", "text": "先看文件"},
                                          {"type": "tool_use", "id": "t1", "name": "file_info",
                                           "input": {"path": "a"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                      "content": "status=ok", "is_error": False},
                                     {"type": "text", "text": "继续"}]},
    ]
    session = FakeSession(FakeResponse())
    fake = SimpleNamespace(Session=lambda: session, Timeout=TransportTimeout, RequestException=TransportError)
    with patch.dict(sys.modules, {"requests": fake}):
        DeepSeekOfficialProvider(api_key="k").complete(messages, json_mode=True)
    sent = session.calls[0][1]["json"]["messages"]
    # system 在最前，其次 user、assistant（tool_calls 内联），tool 结果独立成行，文本随后。
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "tool", "user"]
    assert sent[2]["tool_calls"][0]["function"]["arguments"] == '{"path": "a"}'
    assert sent[3] == {"role": "tool", "tool_call_id": "t1", "content": "status=ok"}
    assert sent[4]["content"] == "继续"


def test_unpermitted_and_foreign_responses_are_rejected():
    definition = {"name": "file_info", "description": "identify file",
                  "input_schema": {"type": "object"}}
    undeclared = FakeResponse(payload={"model": DEFAULT_MODEL, "choices": [{"message": {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "t9", "type": "function",
                        "function": {"name": "web_search", "arguments": "{}"}}]},
        "finish_reason": "tool_calls"}]})
    expect(ProviderError, lambda: invoke(undeclared, tools=[definition]))
    foreign = FakeResponse(payload={"model": "gpt-4o",
                                    "choices": [{"message": {"role": "assistant", "content": "x"},
                                                 "finish_reason": "stop"}]})
    expect(ProviderError, lambda: invoke(foreign))
    empty = FakeResponse(payload={"model": DEFAULT_MODEL,
                                  "choices": [{"message": {"role": "assistant", "content": None,
                                                           "reasoning_content": "思考"},
                                               "finish_reason": "stop"}]})
    expect(ProviderError, lambda: invoke(empty))


def test_search_tool_definitions_are_rejected_before_transport():
    definition = {"name": "web_search", "description": "forbidden", "input_schema": {"type": "object"}}
    expect(ProviderConfigError, lambda: invoke(tools=[definition]))


def test_invalid_and_oversized_response_rejected():
    expect(ProviderError, lambda: invoke(FakeResponse(chunks=[b"not json"])))
    expect(ProviderError, lambda: invoke(FakeResponse(chunks=[b"x" * (2 * 1024 * 1024 + 1)])))
    expect(ProviderError, lambda: invoke(FakeResponse(payload={"model": DEFAULT_MODEL, "choices": []})))


def test_repr_never_contains_key():
    assert "SECRET" not in repr(DeepSeekOfficialProvider(api_key="SECRET"))


def test_config_validation_accepts_official_channel_and_rejects_mismatch():
    official = dict(config(), model={"provider": PROVIDER_NAME})
    assert not validate_config(official)
    # 官方双模型均可作为主模型/换挡模型。
    assert not validate_config(dict(config(), model={"provider": PROVIDER_NAME, "model": "deepseek-v4-pro"}))
    assert not validate_config(dict(config(), model={"provider": PROVIDER_NAME, "pro_model": "deepseek-v4-pro"}))
    assert not validate_config(dict(config(), model={"provider": PROVIDER_NAME,
                                                     "pro_model": "deepseek-v4-pro", "pro_from_stage": 2}))
    for override in ({"model": {"provider": PROVIDER_NAME, "base_url": "https://api.deepseek.com.evil"}},
                     {"model": {"provider": PROVIDER_NAME, "model": "deepseek-chat"}},
                     {"model": {"provider": PROVIDER_NAME, "model": "other"}},
                     {"model": {"provider": PROVIDER_NAME, "pro_model": "deepseek-chat"}},
                     {"model": {"provider": PROVIDER_NAME, "pro_model": "gpt-4o"}},
                     {"model": {"provider": PROVIDER_NAME, "pro_from_stage": -1}},
                     {"model": {"provider": PROVIDER_NAME, "pro_from_stage": "2"}},
                     {"model": {"provider": PROVIDER_NAME, "pro_from_stage": True}},
                     {"model": {"provider": "external"}}):
        assert validate_config(dict(config(), **override))
    # 公司网关通道口径不变：官方端点/模型仍按错配拒绝。
    assert validate_config(dict(config(), model={"base_url": "https://api.deepseek.com"}))
    assert validate_config(dict(config(), model={"model": "deepseek-chat"}))
    for bad_model in ({"model": {"provider": PROVIDER_NAME, "key_cache_file": 1}},
                      {"model": {"key_cache_file": 1}}):
        assert validate_config(dict(config(), **bad_model))


def test_cli_make_provider_selects_official_channel():
    from ctf_ext.__main__ import _make_provider
    with patch.dict(os.environ, {"BAIZHI_API_KEY": "injected-test-key"}, clear=True):
        provider = _make_provider({"model": {"provider": PROVIDER_NAME}})
        assert isinstance(provider, DeepSeekOfficialProvider)
        assert provider._api_key == "injected-test-key"
        assert provider._model == DEFAULT_MODEL
        # 双模型换挡：model_override 构造同端点同凭据的 Pro 实例，仅模型 ID 不同。
        pro = _make_provider({"model": {"provider": PROVIDER_NAME, "model": DEFAULT_MODEL}},
                             model_override="deepseek-v4-pro")
        assert isinstance(pro, DeepSeekOfficialProvider)
        assert pro._model == "deepseek-v4-pro" and pro._api_key == provider._api_key
        assert pro is not provider
    expect(SystemExit, lambda: _make_provider({"model": {"provider": "external"}}))
