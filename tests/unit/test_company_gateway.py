"""公司网关协议与参赛约束的离线回归；不访问网络、不调用真实模型。"""
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

from ctf_ext.llm.deepseek import DeepSeekProvider, DEFAULT_BASE_URL, DEFAULT_MODEL
from ctf_ext.llm.provider import ProviderConfigError, ProviderError, ProviderTimeout, RateLimited
from ctf_ext.gateway.policy import PolicyConfig, PolicyEngine, DENY
from ctf_ext.gateway.tools import ToolRegistry, ToolSpec
from ctf_ext.config import validate_config


class TransportError(Exception):
    pass


class TransportTimeout(TransportError):
    pass


class FakeResponse:
    def __init__(self, status=200, payload=None, chunks=None):
        self.status_code = status
        self.payload = payload if payload is not None else {
            "model": DEFAULT_MODEL, "content": [{"type": "text", "text": '{"ok":true}'}],
            "usage": {"input_tokens": 10, "output_tokens": 3, "cache_read_input_tokens": 2}}
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
        response = DeepSeekProvider(api_key="test-key-not-a-real-secret").complete(
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


def test_company_messages_payload_and_usage():
    response, session = invoke()
    url, kwargs = session.calls[0]
    assert url == DEFAULT_BASE_URL + "/v1/messages"
    assert kwargs["headers"]["anthropic-version"] == "2023-06-01"
    assert kwargs["headers"]["x-api-key"] == "test-key-not-a-real-secret"
    assert kwargs["json"]["model"] == DEFAULT_MODEL
    assert "system" in kwargs["json"] and "tools" not in kwargs["json"]
    assert "response_format" not in kwargs["json"]
    assert kwargs["allow_redirects"] is False and kwargs["verify"] is True
    assert session.trust_env is False and session.closed and session.result.closed
    assert response.usage["total_tokens"] == 15


def test_rejects_other_gateway_and_models():
    for url in ("https://api.deepseek.com", DEFAULT_BASE_URL + ".evil", DEFAULT_BASE_URL + "?next=x"):
        expect(ProviderConfigError, lambda: DeepSeekProvider(api_key="x", base_url=url))
    expect(ProviderConfigError, lambda: DeepSeekProvider(api_key="x", model="other-model"))


def test_missing_key_fails_before_transport():
    with patch.dict(os.environ, {}, clear=True):
        expect(ProviderConfigError, lambda: DeepSeekProvider().complete([]))


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


def test_tool_calls_and_model_substitution_are_rejected():
    expect(ProviderError, lambda: invoke(FakeResponse(payload={"content": [{"type": "tool_use", "name": "web_search"}]})))
    expect(ProviderError, lambda: invoke(FakeResponse(payload={"model": "other", "content": [{"type": "text", "text": "x"}]})))


def test_declared_local_tools_are_sent_and_tool_use_is_parsed():
    definition = {"name": "file_info", "description": "identify file",
                  "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}
    response, session = invoke(tools=[definition])
    assert session.calls[0][1]["json"]["tools"] == [definition]
    assert response.text == '{"ok":true}'

    call = FakeResponse(payload={"model": DEFAULT_MODEL, "content": [{"type": "tool_use", "id": "t1",
                             "name": "file_info", "input": {"path": "a"}}],
                             "usage": {"input_tokens": 4, "output_tokens": 2}})
    response, _ = invoke(call, tools=[definition])
    assert response.tool_calls == [{"id": "t1", "name": "file_info", "input": {"path": "a"}}]


def test_search_tool_definitions_are_rejected_before_transport():
    definition = {"name": "web_search", "description": "forbidden", "input_schema": {"type": "object"}}
    expect(ProviderConfigError, lambda: invoke(tools=[definition]))


def test_invalid_and_oversized_response_rejected():
    expect(ProviderError, lambda: invoke(FakeResponse(chunks=[b"not json"])))
    expect(ProviderError, lambda: invoke(FakeResponse(chunks=[b"x" * (2 * 1024 * 1024 + 1)])))
    expect(ProviderError, lambda: invoke(FakeResponse(payload={"content": []})))


def test_repr_never_contains_key():
    assert "SECRET" not in repr(DeepSeekProvider(api_key="SECRET"))


def test_cli_uses_configured_key_environment():
    from ctf_ext.__main__ import _make_provider
    with patch.dict(os.environ, {"COMPANY_KEY_FOR_TEST": "injected-test-key"}):
        provider = _make_provider({"model": {"api_key_env": "COMPANY_KEY_FOR_TEST"}})
        assert provider._api_key == "injected-test-key"
    expect(SystemExit, lambda: _make_provider({"model": {"provider": "external"}}))


def test_installers_denied_even_if_allowlisted():
    policy = PolicyEngine(PolicyConfig(command_allowlist=("pip", "pip3", "uv", "apt-get", "python", "python3")))
    for argv in (["pip", "install", "x"], ["uv", "pip", "install", "x"], ["apt-get", "install", "x"],
                 ["python", "-m", "pip", "install", "x"], ["python3", "-mpip", "install", "x"],
                 ["python3", "-m", "ensurepip"]):
        decision = policy.decide_command(argv)
        assert decision.action == DENY and decision.reason_code == "RUNTIME_INSTALL_FORBIDDEN"
    assert policy.decide_command(["python3", "-c", "print(1)"]).action == "allow"


def test_search_and_generic_network_tools_denied():
    policy = PolicyEngine(PolicyConfig(command_allowlist=("python3",)))
    registry = ToolRegistry(policy)
    for name in ("web_search", "browser_research", "web-fetch", "brave_search"):
        registry.register(ToolSpec(name=name))
        assert registry.execute(name, ["python3", "-c", "raise AssertionError"]).error_code == "WEB_SEARCH_FORBIDDEN"
    registry.register(ToolSpec(name="remote_data", risk_level="network"))
    assert registry.execute("remote_data", ["python3", "-c", "raise AssertionError"]).error_code == "GENERIC_NETWORK_TOOL_FORBIDDEN"


def test_config_rejects_gateway_model_search_and_install_overrides():
    assert not validate_config(config())
    for override in ({"model": {"base_url": "https://api.deepseek.com"}},
                     {"model": {"model": "other"}}, {"model": {"api_key": "never-store"}},
                     {"policy": {"web_search": True}}, {"policy": {"runtime_install": True}},
                     {"app": {"mode": "competition"}}):
        assert validate_config(dict(config(), **override))
