"""Arena 传输路由：诊断横幅脱敏、环境代理回退候选、路由命名。"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from ctf_ext.harness.arena import (ArenaClient, ArenaProtocolError,
                                   _env_safe_value, describe_transport)


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
