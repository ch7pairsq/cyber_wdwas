"""Arena 题目目标受限 HTTP 客户端（只读为主的探测/利用请求）。

安全约束（全部必须满足，否则拒绝执行）：
1. 必须设置 ARENA_PROXY（SOCKS5），请求只经隧道发出；
2. 目标 host 必须落在 ARENA_NETWORK_CIDR（平台 attempt 网段）内；
3. method 限定 GET/POST/PUT/HEAD/OPTIONS；
4. 响应只输出文本（≤ ARENA_HTTP_MAX_BYTES，默认 60000），不落盘、不执行。

用法：
  python3 arena_http.py --host 10.201.0.201 --port 3000 --method GET --path /
  python3 arena_http.py --host 10.201.0.201 --port 3000 --method POST --path /api/login --body '{"u":"x"}'
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import struct
import sys
import urllib.parse

MAX_DEFAULT = 60000
ALLOWED_METHODS = ("GET", "POST", "PUT", "HEAD", "OPTIONS")


def parse_proxy(proxy: str):
    """解析 SOCKS5 代理地址（可带鉴权）；格式不符或未配置直接拒绝。"""
    if not proxy:
        sys.exit("DENY: ARENA_PROXY is required for arena http tool")
    rest = proxy
    # 先剥 scheme 再切用户信息：socks5://user:pass@host:port 的 @ 在 scheme 之后。
    if not rest.startswith("socks5://"):
        sys.exit("DENY: ARENA_PROXY must be socks5://host:port")
    rest = rest[len("socks5://"):]
    user = b""
    password = b""
    if "@" in rest:
        cred, _, rest = rest.rpartition("@")
        u, _, p = cred.partition(":")
        user, password = u.encode(), p.encode()
    host, _, port_s = rest.partition(":")
    if not host or not port_s.isdigit():
        sys.exit("DENY: ARENA_PROXY must be socks5://host:port")
    return host, int(port_s), user, password


def socks5_connect(proxy, host, port, timeout):
    """建立 SOCKS5 隧道（支持用户名/密码鉴权）；握手失败即拒绝。"""
    phost, pport, user, password = proxy
    sock = socket.create_connection((phost, pport), timeout=timeout)
    sock.settimeout(timeout)
    try:
        sock.sendall(b"\x05\x01\x02" if user else b"\x05\x01\x00")
        resp = sock.recv(2)
        if not resp or resp[0:1] != b"\x05" or resp[1:2] not in (b"\x00", b"\x02"):
            sys.exit("DENY: socks5 method refused")
        if resp[1:2] == b"\x02":
            sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password)
            if sock.recv(2)[1:2] != b"\x00":
                sys.exit("DENY: socks5 auth failed")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host.encode()
                     + struct.pack(">H", port))
        resp = sock.recv(10)
        if len(resp) < 2 or resp[1:2] != b"\x00":
            sys.exit("DENY: socks5 connect failed")
        return sock
    except Exception:
        sock.close()
        raise


def main():
    """受限 HTTP 请求：网段校验 → SOCKS5 隧道 → 响应文本限量输出（JSON）。"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--method", default="GET")
    ap.add_argument("--path", default="/")
    ap.add_argument("--body", default="")
    ap.add_argument("--header", action="append", default=[])
    ap.add_argument("--timeout", type=float, default=15.0)
    args = ap.parse_args()

    if args.method not in ALLOWED_METHODS:
        sys.exit("DENY: method not allowed")
    if not 1 <= args.port <= 65535:
        sys.exit("DENY: bad port")
    try:
        ip = ipaddress.ip_address(args.host)
    except ValueError:
        sys.exit("DENY: host must be an IP inside ARENA_NETWORK_CIDR")
    cidr_raw = os.environ.get("ARENA_NETWORK_CIDR", "")
    if not cidr_raw:
        sys.exit("DENY: ARENA_NETWORK_CIDR is required")
    try:
        net = ipaddress.ip_network(cidr_raw)
    except ValueError:
        sys.exit("DENY: ARENA_NETWORK_CIDR invalid")
    if ip not in net:
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
        if platform_ip is not None and ip == platform_ip and args.port == platform_port \
                and args.method not in ("GET", "HEAD", "OPTIONS"):
            sys.exit("DENY: platform API is read-only for tools "
                     "(submit via gateway; start/finish via harness adapter)")
    if len(args.body.encode()) > 64_000:
        sys.exit("DENY: body too large")
    max_bytes = int(os.environ.get("ARENA_HTTP_MAX_BYTES") or MAX_DEFAULT)

    proxy = parse_proxy(os.environ.get("ARENA_PROXY", ""))
    sock = socks5_connect(proxy, args.host, args.port, args.timeout)
    try:
        headers = {"Host": "{}:{}".format(args.host, args.port), "Connection": "close",
                   "User-Agent": "arena-http-tool/0.1"}
        for h in args.header[:10]:
            if ":" in h:
                k, _, v = h.partition(":")
                headers[k.strip()[:64]] = v.strip()[:512]
        if args.method in ("POST", "PUT") and args.body:
            headers["Content-Type"] = headers.get("Content-Type", "application/json")
            headers["Content-Length"] = str(len(args.body.encode()))
        req = "{} {} HTTP/1.1\r\n".format(args.method, args.path or "/")
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
                      "truncated": len(body) > max_bytes}, ensure_ascii=False))

if __name__ == "__main__":
    main()
