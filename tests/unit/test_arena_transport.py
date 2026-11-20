"""Arena 传输路由：诊断横幅脱敏、环境代理回退候选、路由命名。"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from ctf_ext.harness.arena import (ArenaClient, ArenaProtocolError,
                                   _env_safe_value, describe_transport,
                                   resolve_arena_base_url, resolve_arena_token,
                                   setup_platform_network_env)


def test_env_safe_value_masks_credentials_but_keeps_host():
    assert _env_safe_value("BAIZHI_API_KEY", "sk-secret") == "<set len=9>"
    assert _env_safe_value("ARENA_SOLVER_TOKEN", "t") == "<set len=1>"
    # SOCKS5 用户口令不进横幅，只剩 host:port
    assert (_env_safe_value("PROXY", "socks5://user:pw@10.0.0.5:1080")
            == "socks5://10.0.0.5:1080")
    assert _env_safe_value("PROXY", "") == "<unset>"


def test_describe_transport_never_leaks_token_or_proxy_password():
    env = {"ARENA_SOLVER_TOKEN": "tk_super_secret",
           "ARENA_BASE_URL": "http://10.1.2.3:8090",
           "ALL_PROXY": "socks5://u:p@10.9.9.9:1080"}
    with patch.dict(os.environ, env, clear=True):
        banner = describe_transport("http://10.1.2.3:8090",
                                    ["socks5://u:p@10.9.9.9:1080"])
    assert "tk_super_secret" not in banner
    assert "u:p@" not in banner
    assert "socks5://10.9.9.9:1080" in banner
    assert "http://10.1.2.3:8090" in banner


def test_fallback_values_dedupes_and_ignores_unsupported_schemes():
    env = {"ALL_PROXY": "socks5://10.0.0.1:1080",
           "HTTPS_PROXY": "socks5://10.0.0.1:1080",
           "HTTP_PROXY": "ftp://10.0.0.2:21",
           "NO_PROXY": "localhost"}
    with patch.dict(os.environ, env, clear=True):
        client = ArenaClient("http://10.1.2.3:8090", "token", timeout=1)
        assert client.fallback_values() == ["socks5://10.0.0.1:1080"]
        assert [r[0] for r in client._fallback_routes()] == ["socks"]


def test_route_naming_hides_proxy_credentials():
    client = ArenaClient("http://10.1.2.3:8090", "token", timeout=1,
                         proxy="socks5://u:p@10.0.0.1:1080")
    assert client._route_name(("socks", client._proxy)) == "socks5://10.0.0.1:1080"
    assert client._route_name(("http", "http://u:p@10.0.0.2:3128")) == "http://10.0.0.2:3128"
    assert client._route_name(None) == "direct"


def test_explicit_proxy_must_be_socks5():
    with pytest.raises(ArenaProtocolError):
        ArenaClient("http://10.1.2.3:8090", "token", proxy="http://10.0.0.1:8080")


def test_socks_route_uses_route_proxy_when_self_proxy_unset():
    # 回归：回退路由的 SOCKS5 代理元组必须传给 _socks5_connect，
    # 不能依赖 self._proxy（否则显式代理为空时抛 TypeError）。
    client = ArenaClient("http://10.9.9.9:8090", "token", timeout=0.5)
    with pytest.raises((OSError, ArenaProtocolError)):
        client._do_request(("socks", ("127.0.0.1", 1, b"", b"")), "GET", "/x",
                           None, None, {"Authorization": "Bearer token"})


def test_resolve_base_url_prefers_env_then_gateway_then_k8s():
    with patch.dict(os.environ, {"ARENA_BASE_URL": "http://a:1"}, clear=True):
        assert resolve_arena_base_url("http://cfg:9") == ("http://a:1", "ARENA_BASE_URL")
    with patch.dict(os.environ, {"ARENA_GATEWAY_URL": "http://g:2/"}, clear=True):
        assert resolve_arena_base_url("http://cfg:9") == ("http://g:2", "ARENA_GATEWAY_URL")
    # 平台真实形态：ARENA_BASE_URL 不注入，k8s service 变量指向 model-proxy
    k8s = {"MODEL_PROXY_X_SERVICE_HOST": "1.2.3.4",
           "MODEL_PROXY_X_SERVICE_PORT_CHALLENGES": "8090",
           "MODEL_PROXY_X_SERVICE_PORT_SOCKS": "18081",
           "KUBERNETES_SERVICE_HOST": "10.96.0.1",
           "KUBERNETES_SERVICE_PORT_HTTPS": "443"}
    with patch.dict(os.environ, k8s, clear=True):
        url, source = resolve_arena_base_url("")
        assert url == "http://1.2.3.4:8090"
        assert "k8s service" in source and "challenges" in source
        client = ArenaClient(url, "t", timeout=0.2)
        # socks 命名端口进入回退候选
        assert client.fallback_values() == ["socks5://1.2.3.4:18081"]
    with patch.dict(os.environ, {}, clear=True):
        assert resolve_arena_base_url("http://cfg:9") == ("http://cfg:9", "config")
        assert resolve_arena_base_url("") == ("", "none")


def test_resolve_token_env_first_then_token_files(tmp_path):
    gateway = tmp_path / "gateway.token"
    gateway.write_text("  gw-secret-value \n")
    with patch.dict(os.environ, {"ARENA_GATEWAY_TOKEN_FILE": str(gateway)}, clear=True):
        assert resolve_arena_token() == ("gw-secret-value", "ARENA_GATEWAY_TOKEN_FILE")
    with patch.dict(os.environ, {"ARENA_SOLVER_TOKEN": "env-token-123456"}, clear=True):
        assert resolve_arena_token() == ("env-token-123456", "env ARENA_SOLVER_TOKEN")
    with patch.dict(os.environ, {}, clear=True):
        assert resolve_arena_token() == ("", "none")


def test_client_token_candidates_from_files(tmp_path):
    gateway = tmp_path / "gw.token"
    gateway.write_text("file-token-value\n")
    env = {"ARENA_SOLVER_TOKEN": "env-token-123456789",
           "ARENA_GATEWAY_TOKEN_FILE": str(gateway)}
    with patch.dict(os.environ, env, clear=True):
        client = ArenaClient("http://a:1", "env-token-123456789", timeout=0.2)
    assert [v for _, v in client._tokens] == ["env-token-123456789", "file-token-value"]
    assert client.token_source() == "env ARENA_SOLVER_TOKEN"


def test_resolve_base_url_accepts_official_benchmark_env():
    # 官方注入契约（2026-11-20）：API 根 = BENCHMARK_BASE_URL
    with patch.dict(os.environ, {"BENCHMARK_BASE_URL": "http://10.201.0.9:8090/"},
                    clear=True):
        assert resolve_arena_base_url("") == ("http://10.201.0.9:8090",
                                              "BENCHMARK_BASE_URL")


def test_setup_platform_network_env_exports_tunnel_idempotently():
    k8s = {"MODEL_PROXY_X_SERVICE_HOST": "10.55.0.9",
           "MODEL_PROXY_X_SERVICE_PORT_CHALLENGES": "8090",
           "MODEL_PROXY_X_SERVICE_PORT_SOCKS": "18081",
           "MODEL_PROXY_X_SERVICE_PORT_HTTP_PROXY": "18082",
           "NO_PROXY": "localhost,model-proxy-x"}
    with patch.dict(os.environ, k8s, clear=True):
        actions = setup_platform_network_env()
        assert os.environ["ARENA_PROXY"] == "socks5://10.55.0.9:18081"
        assert os.environ["HTTPS_PROXY"] == "http://10.55.0.9:18082"
        assert os.environ["HTTP_PROXY"] == "http://10.55.0.9:18082"
        assert os.environ["https_proxy"] == "http://10.55.0.9:18082"
        assert any("ARENA_PROXY" in a for a in actions)
        # 幂等：已有值不覆盖
        os.environ["ARENA_PROXY"] = "socks5://custom:1"
        assert setup_platform_network_env() == []
        assert os.environ["ARENA_PROXY"] == "socks5://custom:1"
