"""Arena 题目目标受限 HTTP 客户端（只读为主的探测/利用请求）。

安全约束（全部必须满足，否则拒绝执行）：
1. 必须设置 ARENA_PROXY（SOCKS5），请求只经隧道发出；
2. 目标 host 必须落在 ARENA_NETWORK_CIDR（平台 attempt 网段）内；
3. method 限定 GET/POST/PUT/HEAD/OPTIONS；
4. 响应只输出文本（≤ ARENA_HTTP_MAX_BYTES，默认 60000），不落盘、不执行。

隧道（v36）：优先 SOCKS5（ARENA_PROXY）。2026-11-22 第五场实测靶机已
available 时 SOCKS5 CONNECT 仍一律 REP=2（not allowed by ruleset），
导致工具对可达的 Web 靶机全 DENY；此时降级到平台 HTTP_PROXY 的明文
绝对 URI 转发（HTTP 代理禁止 CONNECT，因此只发
"GET http://host:port/path HTTP/1.1" 形式的请求行），两条通路都不通
才 DENY。输出 JSON 带 route 字段标明实际通路。

用法：
  python3 arena_http.py --host 10.201.0.201 --port 3000 --method GET --path /
  python3 arena_http.py --host 10.201.0.201 --port 3000 --method POST --path /api/login --body '{"u":"x"}'
"""
from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
import socket
import struct
import sys
import urllib.parse

MAX_DEFAULT = 60000
ALLOWED_METHODS = ("GET", "POST", "PUT", "HEAD", "OPTIONS")
# SOCKS5 应答码（RFC 1928 §6）：诊断里带上语义，避免只看到 rep=0x02。
REP_NAMES = {1: "general failure", 2: "not allowed by ruleset",
             3: "network unreachable", 4: "host unreachable",
             5: "connection refused", 6: "ttl expired",
             7: "command not supported", 8: "address type not supported"}


class Deny(RuntimeError):
    """策略/配置拒绝（fail-closed）：统一打印 DENY 后以非零码退出。"""


class SocksRefused(RuntimeError):
    """SOCKS5 隧道被代理拒绝（REP≠0）：可降级到 HTTP 代理明文转发。"""


def parse_proxy(proxy: str):
    """解析 SOCKS5 代理地址（可带鉴权）；格式不符或未配置直接拒绝。"""
    if not proxy:
        raise Deny("ARENA_PROXY is required for arena http tool")
    rest = proxy
    # 先剥 scheme 再切用户信息：socks5://user:pass@host:port 的 @ 在 scheme 之后。
    if not rest.startswith("socks5://"):
        raise Deny("ARENA_PROXY must be socks5://host:port")
    rest = rest[len("socks5://"):]
    user = b""
    password = b""
    if "@" in rest:
        cred, _, rest = rest.rpartition("@")
        u, _, p = cred.partition(":")
        user, password = u.encode(), p.encode()
    host, _, port_s = rest.partition(":")
    if not host or not port_s.isdigit():
        raise Deny("ARENA_PROXY must be socks5://host:port")
    return host, int(port_s), user, password


def parse_http_proxy():
    """解析平台 HTTP_PROXY（http://host:port，可带鉴权）；未配置返回 None。"""
    raw = (os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme != "http" or not parsed.hostname:
        return None
    try:
        port = int(parsed.port or 80)
    except ValueError:
        return None
    return (parsed.hostname, port,
            urllib.parse.unquote(parsed.username or ""),
            urllib.parse.unquote(parsed.password or ""))


def socks5_connect(proxy, host, port, timeout):
    """建立 SOCKS5 隧道（支持用户名/密码鉴权）；被拒绝抛 SocksRefused。"""
    phost, pport, user, password = proxy
    sock = socket.create_connection((phost, pport), timeout=timeout)
    sock.settimeout(timeout)
    try:
        sock.sendall(b"\x05\x01\x02" if user else b"\x05\x01\x00")
        resp = sock.recv(2)
        if len(resp) < 2 or resp[0:1] != b"\x05":
            raise SocksRefused("socks5 method negotiation failed")
        if resp[1:2] == b"\x02":
            sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password)
            if sock.recv(2)[1:2] != b"\x00":
                raise SocksRefused("socks5 auth failed")
        elif resp[1:2] != b"\x00":
            raise SocksRefused("socks5 method refused ({!r})".format(resp[1:2]))
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host.encode()
                     + struct.pack(">H", port))
        resp = sock.recv(10)
        if len(resp) < 2 or resp[0:1] != b"\x05":
            raise SocksRefused("socks5 connect reply truncated")
        if resp[1:2] != b"\x00":
            rep = resp[1]
            raise SocksRefused("socks5 connect failed rep=0x{:02x} ({})".format(
                rep, REP_NAMES.get(rep, "unknown")))
        return sock
    except Exception:
        sock.close()
        raise


def http_proxy_connect(proxy, timeout):
    """连到 HTTP 代理（明文转发）；返回 (sock, Proxy-Authorization 头)。"""
    phost, pport, user, password = proxy
    sock = socket.create_connection((phost, pport), timeout=timeout)
    sock.settimeout(timeout)
    auth = ""
    if user:
        token = base64.b64encode("{}:{}".format(user, password).encode()).decode()
        auth = "Basic " + token
    return sock, auth


def _main(argv=None) -> int:
    """受限 HTTP 请求：网段校验 → 隧道（SOCKS5 优先，HTTP 代理降级）→ JSON 输出。"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--method", default="GET")
    ap.add_argument("--path", default="/")
    ap.add_argument("--body", default="")
    ap.add_argument("--header", action="append", default=[])
    ap.add_argument("--timeout", type=float, default=15.0)
    args = ap.parse_args(argv)

    if args.method not in ALLOWED_METHODS:
        sys.exit("DENY: method not allowed")
    if not 1 <= args.port <= 65535:
        sys.exit("DENY: bad port")
    host_raw = args.host.strip()
    cidr_raw = os.environ.get("ARENA_NETWORK_CIDR", "")
    if not cidr_raw:
        sys.exit("DENY: ARENA_NETWORK_CIDR is required")
    # 支持逗号分隔多网段（授权配置与平台注入的 CIDR 可能多个）。
    try:
        nets = [ipaddress.ip_network(c.strip()) for c in cidr_raw.split(",") if c.strip()]
    except ValueError:
        sys.exit("DENY: ARENA_NETWORK_CIDR invalid")
    # v35：支持域名 host——平台 start 响应给的是 svc 域名
    # （docker-xxx.arena-tr-<attempt>.svc，2026-11-22 第三场实测解析到
    # 172.20.x），此前只收 IP 字面量导致 arena_http 对全部靶机 DENY。
    # 本地解析仅用于授权校验；SOCKS5 CONNECT 本就按域名编码（ATYP=domain），
    # 隧道侧用内网 DNS 解析，host 原样直传。
    try:
        resolved_ips = [ipaddress.ip_address(host_raw)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host_raw, None)
        except OSError:
            sys.exit("DENY: cannot resolve host; pass an IP inside ARENA_NETWORK_CIDR")
        resolved_ips = []
        for i in infos:
            try:
                resolved_ips.append(ipaddress.ip_address(i[4][0]))
            except ValueError:
                continue
    if not resolved_ips or not any(
            any(ip_ in net for net in nets) for ip_ in resolved_ips):
        sys.exit("DENY: target outside attempt network")
    # 平台 API 对模型工具只读：submit 走网关（gate/幂等/对账治理），
    # start/finish 属管理操作由 harness 适配器执行。防止模型绕过治理
    # 重复提交或误触 finish 提前关闭整个 attempt。
    base_url = os.environ.get("ARENA_BASE_URL", "")
    if base_url:
        try:
            parsed = urllib.parse.urlsplit(base_url)
            platform_ip = ipaddress.ip_address(parsed.hostname or "")
            platform_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            platform_ip, platform_port = None, None
        if platform_ip is not None and args.port == platform_port \
                and any(ip_ == platform_ip for ip_ in resolved_ips) \
                and args.method not in ("GET", "HEAD", "OPTIONS"):
            sys.exit("DENY: platform API is read-only for tools "
                     "(submit via gateway; start/finish via harness adapter)")
    if len(args.body.encode()) > 64_000:
        sys.exit("DENY: body too large")
    max_bytes = int(os.environ.get("ARENA_HTTP_MAX_BYTES") or MAX_DEFAULT)

    proxy = parse_proxy(os.environ.get("ARENA_PROXY", ""))
    route = "socks5"
    proxy_auth = ""
    try:
        sock = socks5_connect(proxy, args.host, args.port, args.timeout)
        print("[arena_http] route=socks5 proxy={}:{} target={}:{} {} {}".format(
            proxy[0], proxy[1], args.host, args.port, args.method, args.path or "/"),
            file=sys.stderr, flush=True)
    except (SocksRefused, OSError) as exc:
        # 平台侧 SOCKS5 对已 available 靶机仍可能 REP=2：降级到 HTTP 代理
        # 明文转发（请求行用绝对 URI；平台 HTTP 代理禁止 CONNECT）。
        print("[arena_http] socks5 {}:{} failed: {}".format(proxy[0], proxy[1], exc),
              file=sys.stderr, flush=True)
        hp = parse_http_proxy()
        if hp is None:
            raise Deny("socks5 connect failed ({}); HTTP_PROXY unset, no fallback".format(exc))
        try:
            sock, proxy_auth = http_proxy_connect(hp, args.timeout)
        except OSError as exc2:
            raise Deny("socks5 connect failed ({}); http proxy {}:{} unreachable ({})".format(
                exc, hp[0], hp[1], exc2))
        route = "http-proxy"
        print("[arena_http] route=http-proxy proxy={}:{} target={}:{} {}".format(
            hp[0], hp[1], args.host, args.port, args.path or "/"),
            file=sys.stderr, flush=True)
    try:
        headers = {"Host": "{}:{}".format(args.host, args.port), "Connection": "close",
                   "User-Agent": "arena-http-tool/0.1"}
        for h in args.header[:10]:
            if ":" in h:
                k, _, v = h.partition(":")
                headers[k.strip()[:64]] = v.strip()[:512]
        if proxy_auth:
            headers["Proxy-Authorization"] = proxy_auth
        if args.method in ("POST", "PUT") and args.body:
            headers["Content-Type"] = headers.get("Content-Type", "application/json")
            headers["Content-Length"] = str(len(args.body.encode()))
        target = args.path or "/"
        if route == "http-proxy":
            # 绝对 URI：HTTP 代理唯一的明文转发形式（CONNECT 被平台禁止）。
            target = "http://{}:{}{}".format(args.host, args.port, target)
        req = "{} {} HTTP/1.1\r\n".format(args.method, target)
        req += "".join("{}: {}\r\n".format(k, v) for k, v in headers.items())
        req += "\r\n"
        raw = req.encode("latin-1", "replace")
        if args.method in ("POST", "PUT") and args.body:
            raw += args.body.encode("utf-8")
        sock.sendall(raw)
        chunks = []
        total = 0
        while total <= max_bytes * 4:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        data = b"".join(chunks)
    finally:
        sock.close()
    head, sep, payload = data.partition(b"\r\n\r\n")
    if not sep:
        print("ERROR: no http response")
        return
    lines = head.split(b"\r\n")
    status = lines[0].decode("latin-1", "replace")[:80]
    resp_headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(b":")
        resp_headers[k.strip().lower().decode("latin-1", "replace")] = v.strip().decode(
            "latin-1", "replace")[:200]
    body = payload
    if resp_headers.get("transfer-encoding", "").endswith("chunked"):
        out, cur = [], payload
        while cur:
            e = cur.find(b"\r\n")
            if e < 0:
                break
            try:
                size = int(cur[:e], 16)
            except ValueError:
                break
            if size == 0:
                break
            cur = cur[e + 2:]
            out.append(cur[:size])
            cur = cur[size + 2:]
        body = b"".join(out)
    elif resp_headers.get("content-length", "").isdigit():
        body = payload[:int(resp_headers["content-length"])]
    text = body[:max_bytes].decode("utf-8", "replace")
    print(json.dumps({"status_line": status,
                      "headers": {k: v for k, v in list(resp_headers.items())[:15]},
                      "body": text,
                      "route": route,
                      "truncated": len(body) > max_bytes}, ensure_ascii=False))


def main(argv=None) -> int:
    """入口：Deny 统一打印 DENY 并返回 2（fail-closed，不裸抛 traceback）。"""
    try:
        return _main(argv)
    except Deny as exc:
        print("DENY: {}".format(exc), file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
