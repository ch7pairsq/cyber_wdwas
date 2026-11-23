"""v36 arena_http：SOCKS5 被拒（REP=2）时降级到 HTTP_PROXY 明文转发。

第五场实测：靶机已 available，SOCKS5 CONNECT 仍一律 REP=2（平台规则拒绝），
模型侧 arena_http 7 次全 DENY；而 requests 走 HTTP_PROXY 的明文绝对 URI
转发可达。这里用本地假代理验证降级路径与请求行形态（禁止 CONNECT）。
"""
from __future__ import annotations

import json
import os
import socket
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from ctf_ext.harness.arena import ArenaClient, ArenaProtocolError

SCRIPT = Path(__file__).resolve().parents[2] / "ctf_ext" / "gateway" / "arena_http.py"


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _start(handler):
    server = _Server(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


class _SocksRefuse(socketserver.BaseRequestHandler):
    """SOCKS5 握手成功、CONNECT 回 REP=2（复现平台侧拒绝）。"""

    def handle(self):
        self.request.recv(3)  # VER NMETHODS METHODS
        self.request.sendall(b"\x05\x00")
        head = self.request.recv(4)  # VER CMD RSV ATYP
        if len(head) < 4:
            return
        if head[3] == 3:
            size = self.request.recv(1)[0]
            self.request.recv(size + 2)
        elif head[3] == 1:
            self.request.recv(6)
        self.request.sendall(b"\x05\x02\x00\x01\x00\x00\x00\x00\x00\x00")


class _HttpProxy(socketserver.BaseRequestHandler):
    """记录请求行，回固定响应；用于验证绝对 URI 与 route=http-proxy。"""

    def handle(self):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            data += chunk
        self.server.request_line = data.split(b"\r\n", 1)[0].decode("latin-1")
        body = b"hello-via-proxy"
        self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: "
                             + str(len(body)).encode()
                             + b"\r\nConnection: close\r\n\r\n" + body)


def _env(**overrides):
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
    env.update(overrides)
    return env


def test_socks_refused_falls_back_to_http_proxy_absolute_uri():
    _, socks_port = _start(_SocksRefuse)
    proxy, proxy_port = _start(_HttpProxy)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--host", "127.0.0.1", "--port", "1337",
         "--path", "/x", "--timeout", "3"],
        capture_output=True, text=True, timeout=20,
        env=_env(ARENA_PROXY="socks5://127.0.0.1:{}".format(socks_port),
                 ARENA_NETWORK_CIDR="127.0.0.0/8",
                 ARENA_BASE_URL="",
                 HTTP_PROXY="http://127.0.0.1:{}".format(proxy_port)))
    proxy.shutdown()
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["route"] == "http-proxy"
    assert "hello-via-proxy" in payload["body"]
    # HTTP 代理只接受绝对 URI；CONNECT 被平台禁止，绝不发 CONNECT。
    assert proxy.request_line == "GET http://127.0.0.1:1337/x HTTP/1.1"
    assert "socks5" in proc.stderr  # 诊断里保留 SOCKS5 失败原因


def test_socks_refused_without_http_proxy_denies():
    _, socks_port = _start(_SocksRefuse)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--host", "127.0.0.1", "--port", "1337",
         "--timeout", "2"],
        capture_output=True, text=True, timeout=20,
        env=_env(ARENA_PROXY="socks5://127.0.0.1:{}".format(socks_port),
                 ARENA_NETWORK_CIDR="127.0.0.0/8",
                 ARENA_BASE_URL=""))
    assert proc.returncode == 2
    assert "DENY" in proc.stderr and "socks5 connect failed" in proc.stderr
    assert "no fallback" in proc.stderr


def test_arena_client_logs_http_error_body(monkeypatch, capsys):
    """409 的响应体必须落 stderr：第五场风暴里只打了状态码，无法定责。"""
    client = ArenaClient("http://10.1.2.3:8090", "token",
                         proxy="socks5://127.0.0.1:1", timeout=1)
    monkeypatch.setattr(client, "_do_request",
                        lambda *args, **kwargs: (409, b'{"error":"environment_limit"}'))
    with pytest.raises(ArenaProtocolError):
        client.request("POST", "/openapi/v1/challenges/start",
                       query={"unique_code": "u"})
    err = capsys.readouterr().err
    assert "HTTP 409" in err and "environment_limit" in err

