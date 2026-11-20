"""模型传输 TLS 信任与连接层重试的离线回归；不访问网络、不调用真实模型。

2026-11-20 平台实跑暴露两个传输层问题：
- SSL：出口为 TLS-MITM 型（direct 路由也被透明拦截）时，trust_env=False +
  verify=True 只认 certifi，平台注入的 SSL_CERT_FILE 等变量全部失效 →
  全部意图 SSLError 死循环烧光预算。修复为合并信任（certifi 公共 CA +
  平台注入 CA 文件 → verify=<bundle 路径>），证书链校验完整保留；
  无注入时行为不变（verify=True）。
- 韧性：连接类快速失败（SSL/连接拒绝）无重试，一次抖动烧掉整个意图。
  修复为退避重试 3 次（5/15/30s）；黑洞路由类超时保持快速判死。
"""
import json
import os
import ssl
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import certifi

from ctf_ext.llm import provider as provider_mod
from ctf_ext.llm.deepseek import DeepSeekProvider
from ctf_ext.llm.openai_compat import DeepSeekOfficialProvider, DEFAULT_MODEL
from ctf_ext.llm.provider import (ProviderError, ProviderTimeout,
                                  TRANSPORT_RETRY_DELAYS, platform_ca_bundle)

CALLS = [{"role": "user", "content": "离线测试"}]


class TransportError(Exception):
    pass


class TransportTimeout(TransportError):
    pass


class FakeResponse:
    """官方通道响应桩：OpenAI Chat Completions 形状。"""
    def __init__(self):
        self.status_code = 200
        self.payload = {"model": DEFAULT_MODEL,
                        "choices": [{"message": {"role": "assistant", "content": '{"ok":true}'},
                                     "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}}
        self.closed = False

    def iter_content(self, chunk_size):
        return iter([json.dumps(self.payload).encode()])

    def close(self):
        self.closed = True


class GatewayResponse:
    """公司网关响应桩：Anthropic Messages 形状。"""
    def __init__(self):
        self.status_code = 200
        self.payload = {"model": DEFAULT_MODEL, "content": [{"type": "text", "text": '{"ok":true}'}],
                        "usage": {"input_tokens": 10, "output_tokens": 3}}
        self.closed = False

    def iter_content(self, chunk_size):
        return iter([json.dumps(self.payload).encode()])

    def close(self):
        self.closed = True


class SeqSession:
    """按序返回结果/异常的会话桩：记录每次 post 的 URL 与 kwargs。"""
    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.trust_env = True
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        self.closed = True


def _fake_requests(session):
    return SimpleNamespace(Session=lambda: session, Timeout=TransportTimeout,
                           RequestException=TransportError, ConnectionError=TransportError)


def _reset_cache():
    """清空 CA bundle 缓存：每个用例前后复位，避免环境快照串扰。"""
    provider_mod._ca_bundle_cache = None


def _self_signed_cert(tmp_path, name):
    """生成真实自签证书（伪造 PEM 块会被 OpenSSL load_verify_locations 拒绝）。"""
    crt = tmp_path / (name + ".crt")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(tmp_path / (name + ".key")), "-out", str(crt),
                    "-days", "2", "-subj", "/CN=ctf-ext-test-" + name],
                   check=True, capture_output=True)
    return str(crt)


def _ca_count(cafile):
    """OpenSSL 可加载的 CA 数量：verify= 真实消费 bundle 的前提。"""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cafile=cafile)
    return len(ctx.get_ca_certs())


def test_no_injected_ca_bundle_returns_true():
    with patch.dict(os.environ, {}, clear=True):
        _reset_cache()
        assert platform_ca_bundle() is True
    _reset_cache()


def test_non_pem_or_missing_file_returns_true(tmp_path):
    fake = tmp_path / "not-pem"
    fake.write_bytes(b"hello world")
    with patch.dict(os.environ, {"SSL_CERT_FILE": str(fake),
                                 "REQUESTS_CA_BUNDLE": str(tmp_path / "missing"),
                                 "CURL_CA_BUNDLE": str(tmp_path),  # 目录，isfile=False
                                 "NODE_EXTRA_CA_CERTS": ""}, clear=True):
        _reset_cache()
        assert platform_ca_bundle() is True
    _reset_cache()


def test_bundle_merges_certifi_and_injected_files_and_loads_in_openssl(tmp_path):
    first = _self_signed_cert(tmp_path, "first")
    second = _self_signed_cert(tmp_path, "second")
    with patch.dict(os.environ, {"SSL_CERT_FILE": first,
                                 "NODE_EXTRA_CA_CERTS": second}, clear=True):
        _reset_cache()
        bundle = platform_ca_bundle()
    try:
        assert bundle is not True and os.path.isfile(bundle)
        with open(bundle, "rb") as fh:
            content = fh.read()
        base = certifi.contents().encode("ascii")
        assert content.startswith(base)
        assert (content.count(b"-----BEGIN CERTIFICATE-----")
                == base.count(b"-----BEGIN CERTIFICATE-----") + 2)
        # 合并 bundle 能被 OpenSSL 整体加载（verify= 真实消费的前提），
        # 且 CA 数量 >= certifi 基线 + 注入数。
        assert _ca_count(bundle) >= _ca_count(certifi.where()) + 2
    finally:
        _reset_cache()


def test_bundle_cached_per_environment_snapshot(tmp_path):
    ca = _self_signed_cert(tmp_path, "cached")
    with patch.dict(os.environ, {"SSL_CERT_FILE": ca}, clear=True):
        _reset_cache()
        first = platform_ca_bundle()
        second = platform_ca_bundle()
        assert first is not True and first == second
        # 环境快照变化（指向不存在文件）→ 重新判定
        with patch.dict(os.environ, {"SSL_CERT_FILE": str(tmp_path / "changed.pem")}):
            assert platform_ca_bundle() is True
    _reset_cache()


def test_official_verify_true_without_injection():
    session = SeqSession([FakeResponse()])
    with patch.dict(os.environ, {}, clear=True), \
            patch.dict(sys.modules, {"requests": _fake_requests(session)}):
        _reset_cache()
        response = DeepSeekOfficialProvider(api_key="test-key-not-a-real-secret").complete(
            CALLS, json_mode=True)
    assert response.text == '{"ok":true}'
    assert session.calls[0][1]["verify"] is True


def test_official_verify_uses_bundle_when_injected(tmp_path):
    ca = _self_signed_cert(tmp_path, "official")
    session = SeqSession([FakeResponse()])
    with patch.dict(os.environ, {"SSL_CERT_FILE": ca}, clear=True), \
            patch.dict(sys.modules, {"requests": _fake_requests(session)}):
        _reset_cache()
        response = DeepSeekOfficialProvider(api_key="test-key-not-a-real-secret").complete(
            CALLS, json_mode=True)
    verify = session.calls[0][1]["verify"]
    assert response.text == '{"ok":true}'
    assert verify is not True and os.path.isfile(verify)
    with open(verify, "rb") as fh:
        assert fh.read().startswith(certifi.contents().encode("ascii"))
    assert _ca_count(verify) >= _ca_count(certifi.where()) + 1


def test_gateway_verify_uses_bundle_when_injected(tmp_path):
    ca = _self_signed_cert(tmp_path, "gateway")
    session = SeqSession([GatewayResponse()])
    with patch.dict(os.environ, {"SSL_CERT_FILE": ca}, clear=True), \
            patch.dict(sys.modules, {"requests": _fake_requests(session)}):
        _reset_cache()
        response = DeepSeekProvider(api_key="test-key-not-a-real-secret").complete(
            CALLS, json_mode=True)
    verify = session.calls[0][1]["verify"]
    assert response.text == '{"ok":true}'
    assert verify is not True and os.path.isfile(verify)
    assert _ca_count(verify) >= _ca_count(certifi.where()) + 1


def test_official_connection_failures_retried_then_success():
    # SSL/连接拒绝类快速失败：整轮路由（direct→proxy）失败后退避重试成功
    sleeps = []
    session = SeqSession([TransportError("ssl direct"), TransportError("ssl proxy"),
                          FakeResponse()])
    provider = DeepSeekOfficialProvider(api_key="test-key-not-a-real-secret", timeout=5)
    with patch("ctf_ext.llm.openai_compat.time.sleep", side_effect=sleeps.append), \
            patch.dict(os.environ, {"HTTPS_PROXY": "http://10.55.0.9:18082"}, clear=True), \
            patch.dict(sys.modules, {"requests": _fake_requests(session)}):
        _reset_cache()
        response = provider.complete(CALLS, json_mode=True)
    assert response.text == '{"ok":true}'
    assert [kw.get("proxies") for _, kw in session.calls] == [
        None, {"http": "http://10.55.0.9:18082", "https": "http://10.55.0.9:18082"}, None]
    assert sleeps == [TRANSPORT_RETRY_DELAYS[0]] and provider._proxy_route == "direct"


def test_official_retry_exhaustion_maps_to_provider_error():
    sleeps = []
    session = SeqSession([TransportError("ssl")] * 4)
    provider = DeepSeekOfficialProvider(api_key="test-key-not-a-real-secret", timeout=5)
    with patch("ctf_ext.llm.openai_compat.time.sleep", side_effect=sleeps.append), \
            patch.dict(os.environ, {}, clear=True), \
            patch.dict(sys.modules, {"requests": _fake_requests(session)}):
        _reset_cache()
        try:
            provider.complete(CALLS, json_mode=True)
            raise AssertionError("expected ProviderError")
        except ProviderError as error:
            assert "transport failed" in str(error)
    # 无代理 env：每轮仅 direct 一条路由；1+3 次尝试后耗尽
    assert len(session.calls) == 4 and sleeps == list(TRANSPORT_RETRY_DELAYS)


def test_official_timeout_still_fails_fast_without_retry():
    # 黑洞路由类超时是持续状态：双路由各试一次即快速判死，不重试
    sleeps = []
    session = SeqSession([TransportTimeout("t1"), TransportTimeout("t2")])
    provider = DeepSeekOfficialProvider(api_key="test-key-not-a-real-secret", timeout=5)
    with patch("ctf_ext.llm.openai_compat.time.sleep", side_effect=sleeps.append), \
            patch.dict(os.environ, {"HTTPS_PROXY": "http://10.55.0.9:18082"}, clear=True), \
            patch.dict(sys.modules, {"requests": _fake_requests(session)}):
        _reset_cache()
        try:
            provider.complete(CALLS, json_mode=True)
            raise AssertionError("expected ProviderTimeout")
        except ProviderTimeout:
            pass
    assert len(session.calls) == 2 and sleeps == []


def test_gateway_connection_failures_retried_then_success():
    sleeps = []
    session = SeqSession([TransportError("ssl"), GatewayResponse()])
    with patch("ctf_ext.llm.deepseek.time.sleep", side_effect=sleeps.append), \
            patch.dict(os.environ, {}, clear=True), \
            patch.dict(sys.modules, {"requests": _fake_requests(session)}):
        _reset_cache()
        response = DeepSeekProvider(api_key="test-key-not-a-real-secret").complete(
            CALLS, json_mode=True)
    assert response.text == '{"ok":true}'
    assert len(session.calls) == 2 and sleeps == [TRANSPORT_RETRY_DELAYS[0]]


def test_gateway_retry_exhaustion_maps_to_provider_error():
    sleeps = []
    session = SeqSession([TransportError("ssl")] * 4)
    with patch("ctf_ext.llm.deepseek.time.sleep", side_effect=sleeps.append), \
            patch.dict(os.environ, {}, clear=True), \
            patch.dict(sys.modules, {"requests": _fake_requests(session)}):
        _reset_cache()
        try:
            DeepSeekProvider(api_key="test-key-not-a-real-secret").complete(CALLS, json_mode=True)
            raise AssertionError("expected ProviderError")
        except ProviderError as error:
            assert "transport failed" in str(error)
    assert len(session.calls) == 4 and sleeps == list(TRANSPORT_RETRY_DELAYS)


def test_gateway_timeout_still_fails_fast_without_retry():
    sleeps = []
    session = SeqSession([TransportTimeout("t1")])
    with patch("ctf_ext.llm.deepseek.time.sleep", side_effect=sleeps.append), \
            patch.dict(os.environ, {}, clear=True), \
            patch.dict(sys.modules, {"requests": _fake_requests(session)}):
        _reset_cache()
        try:
            DeepSeekProvider(api_key="test-key-not-a-real-secret").complete(CALLS, json_mode=True)
            raise AssertionError("expected ProviderTimeout")
        except ProviderTimeout:
            pass
    assert len(session.calls) == 1 and sleeps == []
