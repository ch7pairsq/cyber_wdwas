"""Cyber Arena benchmark REST 适配器。

把 Arena 平台的 REST 接口（help / challenges / start / files / submit / finish）
对接到 HarnessAdapter 契约（§12、附录 B.1）。设计约束：

- 凭据 ARENA_SOLVER_TOKEN 只从环境变量读取；任何日志、异常、回执中不得出现 token。
- 端点路径可由配置覆盖；未配置的端点 fail-closed，不臆造。
- 409 duplicate 提交视为已计数（ACCEPTED，幂等回执）；409 challenge_not_started
  自动 start 后重试一次。
- 任何接口失败向上抛，绝不静默当作成功。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from .adapter import HarnessAdapter
from .schema import (
    ChallengeInput,
    HarnessContext,
    RunSummary,
    Submission,
    SubmitReceipt,
    parse_challenge,
)

DEFAULT_HELP_PATH = "/openapi/v1/challenges/help"
DEFAULT_LIST_PATH = "/openapi/v1/challenges"
DEFAULT_START_PATH = "/openapi/v1/challenges/start"
DEFAULT_FILES_PATH = "/openapi/v1/challenges/files"
DEFAULT_SUBMIT_PATH = "/openapi/v1/challenges/submit"
DEFAULT_FINISH_PATH = "/openapi/v1/challenges/finish"
# v33：平台 help 明确 "Release an environment you no longer need"（wait=0 立即
# 返回）。编排层据此在 start 新题前主动让位，把环境回收权从平台随机抢占
# （不区分 attempt 在途，实测漏洞利用4 环境被停时其 attempt 仍在跑）变成
# 按调度状态精准释放。
DEFAULT_CLOSE_PATH = "/openapi/v1/challenges/close"
DEFAULT_DOWNLOAD_TEMPLATE = ("/openapi/v1/challenges/files"
                             "?unique_code={unique_code}&path={path}")
ARENA_REF_SCHEME = "arena://"


class ArenaProtocolError(RuntimeError):
    """Arena 接口失败：属于协议错误，向上抛，不降级。

    status 为 HTTP 状态码（传输层失败时为 None）；startup 阶段据此区分
    "平台未就绪"（5xx，可重试）与凭据/协议错误（4xx，立即失败）。
    """

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def _redact(text: str) -> str:
    """去掉可能混入错误文本的 token；防御性处理，正常路径 token 不进任何字符串。"""
    token = os.environ.get("ARENA_SOLVER_TOKEN", "")
    # 真实 token 足够长；过短的字符串只可能是测试值，替换会误伤 "timed out" 之类的词。
    if len(token) >= 8 and token in text:
        text = text.replace(token, "<redacted>")
    return text


TRANSPORT_ENV_NAMES = ("ARENA_BASE_URL", "ARENA_SOLVER_TOKEN", "ARENA_PROXY",
                       "ARENA_NETWORK_CIDR", "ALL_PROXY", "all_proxy",
                       "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
                       "NO_PROXY", "no_proxy",
                       "ARENA_GATEWAY_URL", "ARENA_GATEWAY_TOKEN_FILE",
                       "ARENA_RUNTIME_API_URL", "ARENA_RUNTIME_TOKEN_FILE",
                       "ARENA_ATTEMPT_ID", "ARENA_PREWARM_RUNTIME",
                       "ARENA_PROVIDER_COUNT", "BENCHMARK_BASE_URL",
                       "BENCHMARK_TOKEN", "CYBER_ARENA_HELP_MARKDOWN")
MODEL_ENV_NAMES = ("OPENAI_BASE_URL", "OPENAI_API_BASE", "MODEL_BASE_URL",
                   "MODEL_PROXY_URL", "BAIZHI_BASE_URL", "BAIZHI_API_KEY")


def _env_safe_value(name: str, value: str) -> str:
    """脱敏展示环境变量：凭据只报长度，URL 只报 scheme://host:port。"""
    if not value:
        return "<unset>"
    if any(k in name.upper() for k in
           ("TOKEN", "KEY", "SECRET", "PASSWORD", "PASSWD", "AUTH", "COOKIE")):
        return "<set len={}>".format(len(value))
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme and parsed.hostname:
        port = ":{}".format(parsed.port) if parsed.port else ""
        return "{}://{}{}".format(parsed.scheme, parsed.hostname, port)
    return value if len(value) <= 64 else value[:61] + "..."


def describe_transport(base_url: str, fallbacks=(), base_source: str = "",
                       token_source: str = "") -> str:
    """启动横幅：Arena 地址（含解析来源）、回退候选、关键环境变量（全部脱敏）。

    平台 harness 的注入契约不完全可知（见 HARNESS-CHECKLIST §12.5），
    启动即打印这些信息，出问题时无需再重建镜像就能判断网络路径。
    """
    lines = ["[arena] base={} (from {}) fallbacks=[{}]".format(
        _env_safe_value("ARENA_BASE_URL", base_url), base_source or "unknown",
        ", ".join(_env_safe_value("PROXY", v) for v in fallbacks) or "none")]
    lines.append("[arena] token from {}".format(token_source or "unknown"))
    lines.append("[env] " + " ".join(
        "{}={}".format(n, _env_safe_value(n, os.environ.get(n, "")))
        for n in TRANSPORT_ENV_NAMES))
    model = ["{}={}".format(n, _env_safe_value(n, os.environ.get(n, "")))
             for n in MODEL_ENV_NAMES if os.environ.get(n)]
    if model:
        lines.append("[env-model] " + " ".join(model))
    lines.append("[env-all] " + " ".join(sorted(os.environ)))
    return "\n".join(lines)


def _read_token_file(path: str):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


_K8S_PORT_SUFFIXES = (("_SERVICE_PORT_CHALLENGES", "challenges"),
                      ("_PORT_8090_TCP_PORT", "tcp-8090"),
                      ("_SERVICE_PORT_HTTP", "http"),
                      ("_SERVICE_PORT_HTTP_PROXY", "http-proxy"),
                      ("_SERVICE_PORT_SOCKS", "socks"))


def _k8s_service_endpoints():
    """收集 k8s 为同 namespace service 注入的变量：前缀 -> {host, 端口名: 端口}。"""
    services = {}
    for name, value in os.environ.items():
        if not value:
            continue
        if name.endswith("_SERVICE_HOST"):
            services.setdefault(name[:-len("_SERVICE_HOST")], {})["host"] = value
            continue
        for suffix, label in _K8S_PORT_SUFFIXES:
            if name.endswith(suffix) and value.isdigit():
                services.setdefault(name[:-len(suffix)], {})[label] = value
                break
    return {prefix: info for prefix, info in services.items()
            if "host" in info and len(info) > 1}


def resolve_arena_base_url(config_url: str = ""):
    """按平台真实契约解析 Arena 地址，返回 (base_url, source)。

    优先级：ARENA_BASE_URL -> ARENA_GATEWAY_URL -> k8s service 注入变量
    （model-proxy 服务的 challenges 端口）-> 配置值。平台实测（2026-11-20
    harness）不注入 ARENA_BASE_URL；service 命名端口 CHALLENGES 对应题目 API。
    """
    for name in ("ARENA_BASE_URL", "BENCHMARK_BASE_URL", "ARENA_GATEWAY_URL"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value.rstrip("/"), name
    for _prefix, info in sorted(_k8s_service_endpoints().items()):
        for label in ("challenges", "tcp-8090", "http"):
            port = info.get(label)
            if port:
                return ("http://{}:{}".format(info["host"], port),
                        "k8s service {}:{} ({})".format(info["host"], port, label))
    if config_url:
        return config_url.rstrip("/"), "config"
    return "", "none"


def resolve_arena_token():
    """解析 Arena token：env 优先，其次平台挂载的 token 文件。返回 (token, source)。"""
    value = (os.environ.get("ARENA_SOLVER_TOKEN") or "").strip()
    if value:
        return value, "env ARENA_SOLVER_TOKEN"
    for name in ("ARENA_GATEWAY_TOKEN_FILE", "ARENA_RUNTIME_TOKEN_FILE"):
        path = (os.environ.get(name) or "").strip()
        if path:
            value = _read_token_file(path)
            if value:
                return value, name
    return "", "none"


def setup_platform_network_env():
    """把平台隧道导出为本进程与工具子进程的环境变量（幂等，不覆盖已有值）。

    平台契约（2026-11-20 harness 实测 + 官方提示词）：赛题靶机只经隧道
    可达（WireGuard 或 SOCKS5），model-proxy 服务的 SOCKS / HTTP_PROXY
    命名端口即隧道入口，k8s 已注入 *_SERVICE_HOST 与端口变量。
    设置 ARENA_PROXY（arena_http 网关工具与子进程用）与
    HTTP(S)_PROXY（模型 urllib 与子进程用；NO_PROXY 由平台预置）。
    返回动作清单；调用方打印。Arena 客户端自身不受影响（直连优先，
    回退链读取这些变量）。
    """
    actions = []
    for _prefix, info in sorted(_k8s_service_endpoints().items()):
        socks_port, http_port = info.get("socks"), info.get("http-proxy")
        if socks_port and not os.environ.get("ARENA_PROXY"):
            value = "socks5://{}:{}".format(info["host"], socks_port)
            os.environ["ARENA_PROXY"] = value
            actions.append("ARENA_PROXY=" + value)
        if http_port and not os.environ.get("HTTPS_PROXY"):
            value = "http://{}:{}".format(info["host"], http_port)
            os.environ["HTTPS_PROXY"] = value
            os.environ.setdefault("https_proxy", value)
            if not os.environ.get("HTTP_PROXY"):
                os.environ["HTTP_PROXY"] = value
                os.environ.setdefault("http_proxy", value)
            actions.append("HTTP(S)_PROXY=" + value)
    return actions

class ArenaClient:
    """只做 HTTP 收发与错误归一化；持有 token，不打印它。"""

    def __init__(self, base_url: str, token: str, timeout: float = 30.0,
                 proxy: str = "", token_source: str = ""):
        """base_url/token 必填；proxy 可选 SOCKS5（[user:pass@]host:port）。"""
        if not base_url:
            raise ArenaProtocolError("arena base_url is required")
        if not token:
            raise ArenaProtocolError("arena token is required (env ARENA_SOLVER_TOKEN)")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._proxy = self._parse_proxy(proxy)
        self._tokens = self._collect_tokens(token, token_source)
        # 路由状态：显式 ARENA_PROXY（参数或环境）固定走 SOCKS5；否则直连优先，
        # 失败时按环境代理回退，成功后记住可用路由（见 _send）。
        self._route = None
        self._resolved = False

    @staticmethod
    def _parse_proxy(proxy):
        if not proxy:
            return None
        rest = proxy
        # 先剥 scheme 再切用户信息：socks5://user:pass@host:port 的 @ 在 scheme 之后。
        if not rest.startswith("socks5://"):
            raise ArenaProtocolError("arena proxy must be socks5://[user:pass@]host:port")
        rest = rest[len("socks5://"):]
        user = b""
        password = b""
        if "@" in rest:
            cred, _, rest = rest.rpartition("@")
            u, _, pw = cred.partition(":")
            user, password = u.encode(), pw.encode()
        host, _, port_s = rest.partition(":")
        if not host or not port_s.isdigit():
            raise ArenaProtocolError("arena proxy must be socks5://[user:pass@]host:port")
        return (host, int(port_s), user, password)

    @staticmethod
    def _route_from_env_value(value: str):
        """把环境里的代理值转成路由类别；不支持的 scheme 一律忽略。"""
        scheme = urllib.parse.urlsplit(value).scheme.lower()
        if scheme == "socks5":
            return "socks"
        if scheme in ("http", "https"):
            return "http"
        return None

    def fallback_values(self):
        """直连失败时可回退的代理：常见环境变量 + k8s model-proxy 服务端口。"""
        out = []
        for name in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy",
                     "HTTP_PROXY", "http_proxy"):
            value = (os.environ.get(name) or "").strip()
            if value and value not in out and self._route_from_env_value(value):
                out.append(value)
        for _prefix, info in sorted(_k8s_service_endpoints().items()):
            if info.get("socks"):
                candidate = "socks5://{}:{}".format(info["host"], info["socks"])
            elif info.get("http-proxy"):
                candidate = "http://{}:{}".format(info["host"], info["http-proxy"])
            else:
                continue
            if candidate not in out:
                out.append(candidate)
        return out

    def _fallback_routes(self):
        routes = []
        for value in self.fallback_values():
            kind = self._route_from_env_value(value)
            if kind == "socks":
                try:
                    route = ("socks", self._parse_proxy(value))
                except ArenaProtocolError:
                    continue
            else:
                route = ("http", value)
            if route not in routes:
                routes.append(route)
        return routes

    @staticmethod
    def _route_name(route):
        if not route:
            return "direct"
        if route[0] == "socks":
            return "socks5://{}:{}".format(route[1][0], route[1][1])
        return _env_safe_value("PROXY", str(route[1]))

    def _collect_tokens(self, primary, source_label):
        """token 候选：主 token 优先，其后是平台挂载的 token 文件。"""
        out = []
        if primary:
            if not source_label:
                env_token = (os.environ.get("ARENA_SOLVER_TOKEN") or "").strip()
                source_label = ("env ARENA_SOLVER_TOKEN" if primary == env_token
                                else "constructor")
            out.append((source_label, primary))
        for name in ("ARENA_GATEWAY_TOKEN_FILE", "ARENA_RUNTIME_TOKEN_FILE"):
            path = (os.environ.get(name) or "").strip()
            if path:
                value = _read_token_file(path)
                if value and all(value != seen for _, seen in out):
                    out.append((name, value))
        return out

    def token_source(self):
        """当前生效 token 的来源标签（诊断用，不返回值）。"""
        return self._tokens[0][0] if self._tokens else ""

    def _route_plan(self):
        if self._proxy:
            return [("socks", self._proxy)]
        if self._resolved:
            return [self._route]
        return [None] + self._fallback_routes()

    def _pin_route(self, route):
        if not self._proxy and not self._resolved:
            self._route = route
            self._resolved = True
            print("[arena] using route {}".format(self._route_name(route)),
                  file=sys.stderr, flush=True)

    def _socks5_connect(self, host, port, proxy=None):
        import socket as _socket
        import struct as _struct
        phost, pport, user, password = proxy or self._proxy
        sock = _socket.create_connection((phost, pport), timeout=self._timeout)
        sock.settimeout(self._timeout)
        try:
            sock.sendall(b"\x05\x01\x02" if user else b"\x05\x01\x00")
            resp = sock.recv(2)
            if not resp or resp[0:1] != b"\x05" or (
                    resp[1:2] not in (b"\x00", b"\x02")):
                raise ArenaProtocolError("socks5 proxy refused method")
            if resp[1:2] == b"\x02":
                sock.sendall(b"\x01" + bytes([len(user)]) + user
                             + bytes([len(password)]) + password)
                if sock.recv(2)[1:2] != b"\x00":
                    raise ArenaProtocolError("socks5 proxy auth failed")
            sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host)])
                         + host.encode() + _struct.pack(">H", port))
            resp = sock.recv(10)
            if len(resp) < 2 or resp[1:2] != b"\x00":
                raise ArenaProtocolError("socks5 connect failed: code={}"
                                         .format(resp[1:2].hex() if resp else "?"))
            return sock
        except Exception:
            sock.close()
            raise

    def _http_over_socks(self, method, host, port, path, headers, body, proxy=None):
        sock = self._socks5_connect(host, port, proxy)
        try:
            lines = ["{} {} HTTP/1.1".format(method, path),
                     "Host: {}:{}".format(host, port), "Connection: close"]
            lines += ["{}: {}".format(k, v) for k, v in headers.items()]
            if body is not None:
                lines.append("Content-Length: {}".format(len(body)))
            raw = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
            if body is not None:
                raw += body
            sock.sendall(raw)
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            data = b"".join(chunks)
        finally:
            sock.close()
        head, sep, payload = data.partition(b"\r\n\r\n")
        if not sep:
            raise ArenaProtocolError("arena proxy HTTP: no response separator")
        lines = head.split(b"\r\n")
        status_line = lines[0].decode("latin-1")
        parts = status_line.split(" ")
        if len(parts) < 2 or not parts[1].isdigit():
            raise ArenaProtocolError("arena proxy HTTP: bad status line")
        code = int(parts[1])
        lower = {}
        for line in lines[1:]:
            k, _, v = line.partition(b":")
            lower[k.strip().lower().decode("latin-1")] = v.strip().lower().decode("latin-1")
        if lower.get("transfer-encoding", "").endswith("chunked"):
            payload = _dechunk(payload)
        elif lower.get("content-length", "").isdigit():
            payload = payload[:int(lower["content-length"])]
        return code, payload


    def _do_request(self, route, method, path, query, data, headers, timeout=None):
        """按指定路由发一次请求；连接层失败向上抛，HTTP 错误码原样返回。"""
        timeout = float(timeout or self._timeout)
        parsed = urllib.parse.urlsplit(self.base_url)
        req_path = path + ("?" + urllib.parse.urlencode(query) if query else "")
        if route and route[0] == "socks":
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            return self._http_over_socks(method, parsed.hostname, port,
                                         req_path, headers, data, proxy=route[1])
        if route and route[0] == "http":
            proxy = route[1]
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        else:
            # 显式空 ProxyHandler：direct 就是真直连，不被环境里的坏代理拖累。
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(parsed.scheme + "://" + parsed.netloc + req_path,
                                     data=data, headers=headers, method=method)
        try:
            with opener.open(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def _send(self, method, path, query, data, headers):
        """发送请求：路由候选 x token 候选依次尝试，成功即记住组合。

        路由：显式 ARENA_PROXY 只走 SOCKS5；否则直连优先，环境代理与 k8s
        model-proxy 服务端口（socks/http-proxy）回退。HTTP 401/403 视为
        token 不匹配，换下一个候选重试；失败请求不产生服务端副作用。
        """
        failures = []
        last_http = None
        started_at = time.monotonic()
        for index, route in enumerate(self._route_plan()):
            timeout = self._timeout if index == 0 else min(self._timeout, 10.0)
            for label, token in self._tokens:
                hdrs = dict(headers)
                hdrs["Authorization"] = "Bearer " + token
                try:
                    code, body = self._do_request(route, method, path, query,
                                                  data, hdrs, timeout=timeout)
                except (urllib.error.URLError, OSError, TimeoutError,
                        ArenaProtocolError) as exc:
                    reason = _redact(str(exc))
                    failures.append("{}: {}".format(self._route_name(route), reason))
                    print("[arena] route {} failed ({} {}): {}".format(
                        self._route_name(route), method, path, reason),
                        file=sys.stderr, flush=True)
                    break  # 该路由传输层不通，换下一条路由
                if code in (401, 403):
                    failures.append("{}: HTTP {}".format(self._route_name(route), code))
                    last_http = (code, body)
                    print("[arena] token {} rejected (HTTP {}) on {}".format(
                        label, code, self._route_name(route)),
                        file=sys.stderr, flush=True)
                    continue  # 同一路由换下一个 token
                if token != self._token:
                    print("[arena] using token from {}".format(label),
                          file=sys.stderr, flush=True)
                    self._token = token
                self._pin_route(route)
                print("[arena] {} {} -> {} ({}B, {:.2f}s, {})".format(
                    method, path, code, len(body), time.monotonic() - started_at,
                    self._route_name(route)), file=sys.stderr, flush=True)
                return code, body
        if last_http is not None:
            return last_http
        raise ArenaProtocolError("arena {} {} failed on all routes [{}]".format(
            method, path, "; ".join(failures) or "none"))

    def request_raw(self, method: str, path: str, query: Dict[str, str] = None) -> bytes:
        """GET 原始字节（附件下载用），不做 JSON/文本解码。"""
        code, body = self._send(method, path, query, None, {})
        if code >= 400:
            raise ArenaProtocolError("arena {} {} -> HTTP {}".format(method, path, code),
                                     status=code)
        return body

    def request(self, method: str, path: str, query: Dict[str, str] = None,
                payload: Dict[str, Any] = None) -> Any:
        """统一请求入口：鉴权头由 _send 按 token 候选注入；路由见 _send。"""
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        code, body = self._send(method, path, query, data, headers)
        if code >= 400:
            detail = _redact(body.decode("utf-8", "replace")[:500])
            raise ArenaProtocolError(
                "arena {} {} -> HTTP {}: {}".format(method, path, code, detail),
                status=code)
        return _decode_body(method, path, body)


def _dechunk(data):
    out = []
    while data:
        line_end = data.find(b"\r\n")
        if line_end < 0:
            break
        try:
            size = int(data[:line_end], 16)
        except ValueError:
            break
        if size == 0:
            break
        data = data[line_end + 2:]
        out.append(data[:size])
        data = data[size + 2:]
    return b"".join(out)

def _decode_body(method: str, path: str, body: bytes) -> Any:
    """优先 JSON；非 JSON（如 help 的 markdown 文档）原样返回文本，不臆造结构。"""
    if not body:
        return {}
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return body.decode("utf-8", "replace")


def _safe_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _receipt_truthy(correct) -> bool:
    """平台回执 correct 字段的真值判定：布尔直读，字符串/数字宽松转写。"""
    if isinstance(correct, bool):
        return correct
    return str(correct).strip().lower() in ("true", "1", "yes")


class ArenaHarnessAdapter(HarnessAdapter):
    """协议版本 v1-arena；端点默认值对应 Arena /openapi/v1 约定，可由配置覆盖。"""

    def __init__(self, base_url: str, token: str,
                 proxy: str = "",
                 base_source: str = "",
                 token_source: str = "",
                 help_path: str = DEFAULT_HELP_PATH,
                 list_path: str = DEFAULT_LIST_PATH,
                 start_path: str = DEFAULT_START_PATH,
                 files_path: str = DEFAULT_FILES_PATH,
                 submit_path: str = DEFAULT_SUBMIT_PATH,
                 finish_path: str = DEFAULT_FINISH_PATH,
                 close_path: str = DEFAULT_CLOSE_PATH,
                 download_template: str = DEFAULT_DOWNLOAD_TEMPLATE,
                 auto_start: bool = True,
                 auto_finish: bool = False,
                 max_envs: int = 3,
                 client_timeout: float = 30.0,
                 env_policy: str = "prewarm",
                 startup_retry_attempts: int = 20,
                 startup_retry_seconds: float = 30.0,
                 ready_wait_seconds: float = 100.0,
                 ready_poll_seconds: float = 8.0):
        """端点路径均可覆盖；env_policy 决定 prewarm / on_demand 两种环境策略。

        startup_retry_*：startup 阶段对平台 5xx（未就绪/过载）的退避重试；
        默认 20 次 × 30s ≈ 10 分钟，supervisor 3 次重启合计约 30 分钟容错，
        避免平台短暂不可用就烧光尝试窗口。
        ready_wait/poll_*：start 返回 pending（容器拉起中）时就绪轮询的
        总预算与间隔。平台拉起容器实测 40-80s（2026-11-22 别队复盘同款
        事故：拿到 pending/空地址即记"已启动"不再轮询 → 模型拿空地址撞墙
        → 误判"靶机未启动"）。
        """
        self._client = ArenaClient(base_url, token, timeout=client_timeout,
                                   proxy=proxy, token_source=token_source)
        self._startup_retry_attempts = max(1, int(startup_retry_attempts))
        self._startup_retry_seconds = max(0.0, float(startup_retry_seconds))
        self._ready_wait = max(0.0, float(ready_wait_seconds))
        self._ready_poll = max(0.5, float(ready_poll_seconds))
        self._base_source = base_source
        self._paths = {"help": help_path, "list": list_path, "start": start_path,
                       "files": files_path, "submit": submit_path, "finish": finish_path,
                       "close": close_path}
        self._download_template = download_template
        self._auto_start = auto_start
        self._auto_finish = auto_finish
        self._max_envs = max_envs
        # prewarm: read_tasks 时预启动前 max_envs 个环境（旧行为）；
        # on_demand: 编排层在每个意图执行前调 ensure_environment 按需拉起。
        self._env_policy = env_policy if env_policy in ("prewarm", "on_demand") else "prewarm"
        self._ctx: Optional[HarnessContext] = None
        self._tasks: List[ChallengeInput] = []
        self._raw: List[Dict[str, Any]] = []
        self._receipts: Dict[str, SubmitReceipt] = {}
        self._started: set = set()
        self._start_info: Dict[str, Any] = {}
        # v33：环境启动时刻（编排层让位时按“最旧优先”回收无在途工作的环境）。
        self._started_at: Dict[str, float] = {}
        # 并发 worker 各自触发 ensure_environment：started/start_info 的
        # 记账用短锁保护；同题并发发起用 in-flight 事件去重（不持锁做 HTTP，
        # 不同题的 start 仍然并行）。
        self._env_lock = threading.RLock()
        self._inflight: Dict[str, threading.Event] = {}
        self._timeout = float(client_timeout)
        self.help_text: Any = {}

    # ------------------------------------------------------------------ startup
    def startup(self) -> HarnessContext:
        """拉取 help（权威规则来源）并建立 v1-arena 上下文。"""
        # 平台注入契约不完全可知：启动即打印脱敏诊断，失败时无需重建镜像定位。
        print(describe_transport(self._client.base_url, self._client.fallback_values(),
                                 base_source=self._base_source,
                                 token_source=self._client.token_source()),
              file=sys.stderr, flush=True)
        print("[arena] adapter: {} auto_start={} auto_finish={} env_policy={} "
              "max_envs={} client_timeout={}s explicit_proxy={}".format(
                  " ".join("{}={}".format(k, v) for k, v in self._paths.items()),
                  self._auto_start, self._auto_finish, self._env_policy,
                  self._max_envs, self._timeout, bool(self._client._proxy)),
              file=sys.stderr, flush=True)
        # help 是权威工作流与规则来源；内容只入 metadata，不做协议推断。
        # 平台后端可能短暂未就绪（HTTP 5xx，如 resource_unavailable）：
        # 启动阶段对 5xx 退避重试，避免 3 秒烧光 supervisor 重启次数浪费
        # 整个尝试窗口；连接层失败与 4xx（凭据/协议错误）仍立即失败，
        # 保持 fail-closed 与断网冒烟的快速退出语义。
        for attempt in range(1, self._startup_retry_attempts + 1):
            try:
                self.help_text = self._client.request("GET", self._paths["help"])
                break
            except ArenaProtocolError as exc:
                if not (exc.status is not None and 500 <= exc.status < 600):
                    raise  # 传输层失败或 4xx：立即向上抛
                if attempt >= self._startup_retry_attempts:
                    raise  # 重试预算耗尽：保持原语义由 supervisor 接管
                print("[arena] platform not ready (HTTP {}), retry {}/{} after {}s".format(
                    exc.status, attempt, self._startup_retry_attempts,
                    self._startup_retry_seconds), file=sys.stderr, flush=True)
                time.sleep(self._startup_retry_seconds)
        try:
            help_dump = json.dumps(self.help_text, ensure_ascii=False)
        except (TypeError, ValueError):
            help_dump = str(self.help_text)
        print("[arena] help: {} chars, head={}".format(
            len(help_dump), help_dump[:160].replace("\n", " ")),
            file=sys.stderr, flush=True)
        self._ctx = HarnessContext(protocol="v1-arena", competition_id="cyber-arena")
        return self._ctx

    # --------------------------------------------------------------- read_tasks
    def read_tasks(self) -> List[ChallengeInput]:
        """拉取题目清单并标准化；prewarm 模式下同时预启动前 max_envs 个环境。"""
        if not self._ctx:
            raise RuntimeError("startup() must be called before read_tasks()")
        resp = self._client.request("GET", self._paths["list"])
        items = resp if isinstance(resp, list) else (resp.get("challenges")
                                                    or resp.get("items") or [])
        if not isinstance(items, list):
            raise ArenaProtocolError("challenges listing is not a list")
        raw_tasks: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            code = str(item.get("unique_code") or item.get("uniqueCode")
                       or item.get("id") or "").strip()
            if not code:
                continue  # 没有 unique_code 无法 start/submit，跳过并保留在 raw 供排查
            raw = {
                "challenge_id": code,
                "name": str(item.get("name") or item.get("title") or code),
                "category": str(item.get("category") or item.get("type") or "unknown"),
                "description": str(item.get("description") or item.get("statement") or ""),
                "attachments": (["{}{}/{}".format(ARENA_REF_SCHEME, code, p)
                                 for p in (item.get("files") or item.get("handouts") or [])
                                 if isinstance(p, str)]),
                "points_hint": _safe_float(item.get("points") or item.get("points_hint")),
                "difficulty_level": _safe_int(item.get("difficulty") or item.get("difficulty_level")),
                "metadata": {"arena": {k: v for k, v in item.items()
                                       if k not in ("description", "statement")},
                             "help": self.help_text,
                             "start_info": self._start_info.get(code, {})},
            }
            raw_tasks.append(raw)
        self._raw = raw_tasks
        self._tasks = [parse_challenge(raw, protocol="v1-arena") for raw in raw_tasks]
        print("[arena] tasks={}: {}".format(
            len(self._tasks),
            ", ".join("{}({})".format(t.challenge_id, t.name) for t in self._tasks)
            or "EMPTY"), file=sys.stderr, flush=True)
        if not self._tasks and items:
            sample = items[0] if isinstance(items[0], dict) else None
            print("[arena] listing had {} items but none parsed; sample keys={}".format(
                len(items), sorted(sample) if sample else type(items[0]).__name__),
                file=sys.stderr, flush=True)
        if self._auto_start and self._env_policy == "prewarm":
            # 平台限制最多 3 个并发环境（409 environment_limit）；超限即停止，
            # 未启动的挑战在提交触发 challenge_not_started 时按需重试。
            # env_policy=on_demand 时跳过预热：改由编排层在每个意图前按需拉起。
            max_envs = int(self._max_envs)
            for task in self._tasks:
                if len(self._started) >= max_envs:
                    break
                try:
                    self._ensure_started(task.challenge_id)
                except ArenaProtocolError as exc:
                    if "environment_limit" in str(exc) or "409" in str(exc):
                        break  # 槽位耗尽：保留已启动的，不作为致命错误
                    raise
        # 回填 start 响应与 files 权威路径（attachments 以 files 列表为准）。
        for task in self._tasks:
            task.metadata["start_info"] = self._start_info.get(task.challenge_id, {})
            try:
                listing = self._client.request("GET", self._paths["files"],
                                               query={"unique_code": task.challenge_id})
            except ArenaProtocolError as exc:
                # files 列表失败不阻塞读题；附件可后续按 ref 重试（日志可见原因）。
                print("[arena] files listing failed for {}: {}".format(
                    task.challenge_id, exc), file=sys.stderr, flush=True)
                continue
            entries = listing.get("entries") or [] if isinstance(listing, dict) else []
            refs = []
            for entry in entries:
                if isinstance(entry, dict) and entry.get("path"):
                    refs.append(ARENA_REF_SCHEME + task.challenge_id + "/" + str(entry["path"]))
            if refs:
                task.metadata["arena_files"] = entries
                task.attachments = tuple(refs)
        # 描述是模型唯一可读面：目标地址、网段、附件位置必须显式给出，
        # 否则模型只能停在"环境前置缺失"。
        for task in self._tasks:
            info = task.metadata.get("start_info") or {}
            note = self._env_note(info, bool(task.metadata.get("arena_files")))
            if note:
                task.description = (task.description or "") + "\n\n" + note
        # 全量打印题目详情与启动参数：平台日志即完整排查材料（脱敏：无凭据）。
        for idx, task in enumerate(self._tasks):
            entries = [e.get("path") for e in (task.metadata.get("arena_files") or [])
                       if isinstance(e, dict)]
            print("[arena] task[{}] {} name={} cat={} pts={} diff={} files={}".format(
                idx, task.challenge_id, task.name, task.category,
                task.points_hint, task.difficulty_level, entries),
                file=sys.stderr, flush=True)
            desc = (task.description or "").replace("\n", " ")
            print("[arena] task[{}] desc={}".format(
                idx, desc[:400] + ("..." if len(desc) > 400 else "")),
                file=sys.stderr, flush=True)
            info = task.metadata.get("start_info")
            if info:
                try:
                    info_dump = json.dumps(info, ensure_ascii=False)
                except (TypeError, ValueError):
                    info_dump = str(info)
                print("[arena] task[{}] start_info={}".format(
                    idx, info_dump[:400]), file=sys.stderr, flush=True)
        return list(self._tasks)

    def _env_note(self, info: Dict[str, Any], has_files: bool) -> str:
        """运行环境说明（附加到题目描述）；info 为空且无附件时返回空串。

        2026-11-21 正式赛教训（static_file_server 40 轮只用 python_exec、
        判定"靶机未启动"）：模型不知道靶机访问约定。2026-11-22 第二场
        修正认知：python requests/httpx 默认走平台预置的 HTTP_PROXY 隧道
        实际可达，真正不可靠的是手写 socket/禁用代理直连；arena_http 走
        SOCKS5 隧道且有 DENY 诊断，仍为首选。措辞三分类，不再一刀切
        "python 网络请求不可靠"（那会让模型放弃可用路径）。
        """
        # container_addr 可能是字符串或列表（多端口）；统一成逗号分隔字符串。
        # 2026-11-21/22 正式赛实测：平台该字段可为空数组 []（弱密码题与
        # static_file_server 都如此），此时走无地址分支靠推导。
        raw_addr = info.get("container_addr")
        if isinstance(raw_addr, (list, tuple)):
            raw_addr = ",".join(str(a) for a in raw_addr if a)
        addr = str(raw_addr or "").strip()
        tunnel_fact = ("python requests/httpx 默认走平台 HTTP_PROXY 隧道，同样可达；"
                       "手写 socket 或显式禁用代理的直连不可达，不要用")
        parts = []
        if addr:
            parts.append("题目服务地址: http://" + addr
                         + "（优先用 arena_http 工具访问，host 直接填地址里的"
                           "主机名即可（svc 域名或 IP 都支持），有 DENY 诊断"
                           "最可靠；" + tunnel_fact + "）")
        else:
            parts.append("平台未返回靶机地址（container_addr 为空）：若题目需要远程服务，"
                         "从题目描述（如 ${challenge_host}:${challenge_port} 占位符）/"
                         "附件/arena_help 推导目标 host:port；"
                         "优先用 arena_http 工具发起请求；" + tunnel_fact + "；"
                         "工具被 DENY 时把原因原样记录进结果 summary")
        cidr = os.environ.get("ARENA_NETWORK_CIDR", "")
        if cidr:
            parts.append("允许访问的目标网段: " + cidr + "（网段之外一律拒绝）")
        # 2026-11-21 第三场实测：start 响应返回 ≠ 容器就绪（weak-password
        # status='available' 有租期；static_file_server status='pending'
        # 且 expires_at=None）。把状态/租期写进模型可见面，防"开局请求
        # 撞上未就绪 → 探测失败 → 误判靶机未启动"重演。
        status = str(info.get("container_status") or "").strip()
        if status and status not in ("available", "running"):
            parts.append("容器状态: " + status + "（平台拉起中，尚未就绪）——"
                         "立即请求可能失败：若连接失败/拒绝，等待 30-60s 后"
                         "重试同一地址，不要判定靶机未启动")
        expires = str(info.get("expires_at") or "").strip()
        if expires:
            parts.append("容器租期至 " + expires
                         + "（到期平台可能回收容器；长任务请尽早取得所需数据）")
        if has_files:
            parts.append("题目附件已物化到工作区 handouts/ 目录，可直接用 file_info/strings/archive_extract 处理")
        return "[Arena 运行环境] " + "；".join(parts)

    def _refresh_description(self, unique_code: str, info: Dict[str, Any]) -> None:
        """把最新 start_info 对应的环境说明刷新进题目描述（幂等替换旧 note）。

        2026-11-21 正式赛教训：on_demand 模式下 read_tasks 先用空 start_info
        写了"平台未返回靶机地址"note，ensure_environment 的幂等分支 early
        return 后，即使 start 实际返回了 container_addr 模型也永远看不到——
        这里改为旧 note 剥离后重写，保证最新地址始终可达。
        """
        for task in self._tasks:
            if task.challenge_id != unique_code:
                continue
            task.metadata["start_info"] = info
            desc = task.description or ""
            marker = "\n\n[Arena 运行环境]"
            if marker in desc:
                desc = desc.split(marker)[0]
            note = self._env_note(info, bool(task.metadata.get("arena_files")))
            if note:
                desc = desc + "\n\n" + note
            task.description = desc
            break

    def ensure_environment(self, unique_code: str):
        """按需拉起题目环境（env_policy=on_demand 时由编排层在每个意图前调用）。

        返回 start_info（新建成功时）或 None（已启动/并发发起中）；409
        duplicate/already 视为环境仍在运行；environment_limit 向上抛，由
        编排层记事件，意图快速失败后等槽位回收再重试。已启动/重复发起
        的题同样刷新描述（幂等），保证最新 start_info 可达模型。
        """
        with self._env_lock:
            already = unique_code in self._started
        if not already:
            try:
                self._ensure_started(unique_code)
            except ArenaProtocolError as exc:
                text = str(exc)
                if "duplicate" in text.lower() or "already" in text.lower():
                    with self._env_lock:
                        self._started.add(unique_code)  # 已有环境在跑，幂等收编
                        # duplicate = 环境在跑但平台未回环境信息：填无字段标记
                        # （_env_ready 对旧形状视为就绪），不进入就绪轮询白等。
                        self._start_info.setdefault(unique_code, {"started": True})
                        self._started_at.setdefault(unique_code, time.time())
                else:
                    raise
        with self._env_lock:
            if unique_code not in self._started:
                # 并发发起者失败落地：本次按"未启动"返回，意图会快速失败重试。
                return None
            info = self._start_info.get(unique_code) or {}
        # v35：start 返回 pending/空地址 ≠ 就绪（平台拉起实测 40-80s）。轮询
        # 重新 start 幂等探测直到就绪——此前把 pending 记成已启动后永不回头，
        # 环境就绪后的真实地址模型永远看不到（缓存旧空地址 → 撞墙误判）。
        if not self._env_ready(info):
            info = self._await_environment_ready(unique_code, info)
        self._refresh_description(unique_code, info)
        return info

    @staticmethod
    def _env_ready(info: Dict[str, Any]) -> bool:
        """start 响应判定环境是否已就绪（兼容旧响应形状：无状态字段但有地址）。

        2026-11-22 实测平台响应 keys 恒含 container_addr（可为 []）与
        container_status（available/pending）；轮询只对这种新形状生效。
        完全没有环境字段的旧形状（如 {"started": true}）无从判定，视为
        就绪不阻塞——提示词兜底。
        """
        if not isinstance(info, dict) or not info:
            return False
        has_env_fields = ("container_status" in info or "container_addr" in info)
        if not has_env_fields:
            return True
        status = str(info.get("container_status") or "").strip().lower()
        raw_addr = info.get("container_addr")
        if isinstance(raw_addr, (list, tuple)):
            addr = ",".join(str(a) for a in raw_addr if a).strip()
        else:
            addr = str(raw_addr or "").strip()
        if status in ("available", "running"):
            return True
        if not status and addr:
            return True
        return False

    def _await_environment_ready(self, unique_code: str, info: Any) -> Any:
        """就绪轮询：重复 POST start 幂等探测，直到拿到就绪响应或预算耗尽。

        只对不可用状态（pending/空地址）的题轮询——此时 worker 尚未开始打靶，
        平台重置环境无副作用；已就绪的题绝不重复 start（防平台重建容器断靶）。
        409 duplicate = 环境仍在跑（地址拿不到就靠提示词兜底）；409
        environment_limit = 槽位被夺，交回编排层让位逻辑。预算耗尽返回最后
        一次 info（模型可见面已有"拉起中，等 30-60s 重试"的提示）。
        """
        deadline = time.monotonic() + self._ready_wait
        attempt = 0
        while time.monotonic() < deadline:
            time.sleep(self._ready_poll)
            attempt += 1
            try:
                resp = self._client.request("POST", self._paths["start"],
                                            query={"unique_code": unique_code})
            except ArenaProtocolError as exc:
                text = str(exc).lower()
                if "duplicate" in text or "already" in text:
                    continue  # 环境在跑但平台不给地址：等下一轮
                raise
            if isinstance(resp, dict) and resp:
                with self._env_lock:
                    self._start_info[unique_code] = resp
                    self._started.add(unique_code)
                    self._started_at.setdefault(unique_code, time.time())
                info = resp
                addr = resp.get("container_addr")
                print("[arena] start-poll {} attempt={} addr={!r} status={!r}".format(
                    unique_code, attempt, addr, resp.get("container_status")),
                    file=sys.stderr, flush=True)
                if self._env_ready(info):
                    print("[arena] env {} ready after {} polls".format(
                        unique_code, attempt), file=sys.stderr, flush=True)
                    break
        return info

    def max_envs(self) -> int:
        """平台并发环境上限（v33 让位判定用；配置可覆盖，默认 3）。"""
        return int(self._max_envs)

    def started_envs(self) -> Dict[str, float]:
        """已启动环境的 unique_code -> 启动时刻快照（让位策略的记账源）。"""
        with self._env_lock:
            return dict(self._started_at)

    def stop_environment(self, unique_code: str, wait: int = 0) -> bool:
        """主动释放平台环境槽位（v33）：编排层对无在途工作的题调用 close。

        close 端点无返回体约定；wait=0 立即返回避免占用 worker 墙钟。
        409 duplicate / 404 / not started / already closed 一律视为已释放
        （幂等）：记账摘除后返回 True。其余错误向上抛，由编排层记事件。
        """
        with self._env_lock:
            if unique_code not in self._started:
                return False
        try:
            self._client.request("POST", self._paths["close"],
                                 query={"unique_code": unique_code, "wait": int(wait)})
        except ArenaProtocolError as exc:
            text = str(exc).lower()
            status = getattr(exc, "status", None)
            settled = ("not_started" in text or "not started" in text
                       or "already" in text or "duplicate" in text or "closed" in text
                       or status == 404)
            if not settled:
                raise
            print("[arena] close {} -> treated as released: {}".format(
                unique_code, exc), file=sys.stderr, flush=True)
        with self._env_lock:
            self._started.discard(unique_code)
            self._started_at.pop(unique_code, None)
            self._start_info.pop(unique_code, None)
        print("[arena] close {} -> slot released (wait={})".format(
            unique_code, wait), file=sys.stderr, flush=True)
        return True

    def task_description(self, challenge_id: str):
        """返回该题最新描述（含运行环境附注）；未知题返回 None。"""
        for task in self._tasks:
            if task.challenge_id == challenge_id:
                return task.description
        return None

    def _ensure_started(self, unique_code: str) -> None:
        with self._env_lock:
            if unique_code in self._started:
                return
            inflight = self._inflight.get(unique_code)
            if inflight is not None:
                # 同题并发发起：等发起方落地即可，不重发 start（幂等去重）。
                inflight.wait(timeout=self._timeout + 5.0)
                return
            self._inflight[unique_code] = threading.Event()
        try:
            # start 接口无返回体约定；失败即抛，绝不当作已启动。
            resp = self._client.request("POST", self._paths["start"],
                                        query={"unique_code": unique_code})
        finally:
            with self._env_lock:
                ev = self._inflight.pop(unique_code, None)
                if ev is not None:
                    ev.set()
        with self._env_lock:
            self._started.add(unique_code)
            self._started_at[unique_code] = time.time()
            # start 响应可能携带环境/服务地址等材料信息，原样保留供 worker 查阅。
            self._start_info[unique_code] = resp
            if resp:
                try:
                    resp_dump = json.dumps(resp, ensure_ascii=False)
                except (TypeError, ValueError):
                    resp_dump = str(resp)
                # 诊断关键项单独打全量（2026-11-21 正式赛：desc 占满截断窗口，
                # container_addr 是否存在无法从 stdout 判断）。
                keys = sorted(resp.keys()) if isinstance(resp, dict) else type(resp).__name__
                addr = resp.get("container_addr") if isinstance(resp, dict) else None
                status = resp.get("container_status") if isinstance(resp, dict) else None
                expires = resp.get("expires_at") if isinstance(resp, dict) else None
                # container_status/expires_at：平台拉起状态与租期（2026-11-22
                # 实测 keys 里存在；container_addr 可为空数组，状态字段是
                # 判断"平台是否真拉起了容器"的直接证据）。
                print("[arena] start {} keys={} container_addr={!r} "
                      "container_status={!r} expires_at={!r}".format(
                          unique_code, keys, addr, status, expires),
                      file=sys.stderr, flush=True)
                print("[arena] start {} -> {}".format(unique_code, resp_dump[:300]),
                      file=sys.stderr, flush=True)

    # ------------------------------------------------------------- attachments
    def read_attachment(self, ref: str, *, max_bytes: int):
        """ref 形如 arena://<unique_code>/<path>；路径必须是 files 列表返回的路径。"""
        if not ref.startswith(ARENA_REF_SCHEME):
            raise FileNotFoundError(ref)
        rest = ref[len(ARENA_REF_SCHEME):]
        code, _, path = rest.partition("/")
        if not code or not path:
            raise FileNotFoundError(ref)
        # files 列表返回权威路径；这里先核对再下载，路径不一致即失败（不猜测）。
        listing = self._client.request("GET", self._paths["files"],
                                       query={"unique_code": code})
        entries = listing if isinstance(listing, list) else (listing.get("files")
                                                             or listing.get("paths") or [])
        names = set()
        for entry in entries or []:
            if isinstance(entry, str):
                names.add(entry)
            elif isinstance(entry, dict):
                names.add(str(entry.get("path") or entry.get("name") or ""))
        if names and path not in names:
            raise FileNotFoundError("path {!r} not in files listing for {}".format(path, code))
        url_path, _, url_query = self._download_template.partition("?")
        query = {"unique_code": code, "path": path}
        if url_query:
            for pair in url_query.split("&"):
                k, _, v = pair.partition("=")
                if v.startswith("{") and v.endswith("}"):
                    query[k] = {"{unique_code}": code, "{path}": path}.get(v, query.get(k, v))
        raw = self._client.request_raw("GET", url_path, query=query)
        if len(raw) > max_bytes:
            raise ValueError("attachment {} exceeds {} bytes".format(ref, max_bytes))
        return raw

    # ----------------------------------------------------------------- submit
    def submit_answer(self, submission: Submission) -> SubmitReceipt:
        """幂等提交：同 key 返回首次回执；底层对 409 特殊状态做对账。

        每次提交（含幂等重放）都打 [submit] 行：flag 原文 + 平台回执状态与
        detail。flag 是题目答案而非凭据，可以落日志；2026-11-21 正式赛
        3 道错题只留下 `submit -> 200 (145B)` 传输日志，回执无处可查——
        本行是错题定位的唯一观测点。
        """
        key = submission.idempotency_key
        cached = key in self._receipts  # 幂等：不重复判定（附录 E）
        receipt = self._receipts[key] if cached else self._submit_once(submission)
        if not cached:
            self._receipts[key] = receipt
        print("[submit] ch={} flag={} verdict={} receipt_id={} detail={}{}".format(
            submission.challenge_id, submission.answer.strip(), receipt.status,
            receipt.receipt_id or "-", (receipt.detail or "")[:200],
            " (idempotent replay)" if cached else ""), file=sys.stderr, flush=True)
        return receipt

    def _submit_once(self, submission: Submission) -> SubmitReceipt:
        code = submission.challenge_id
        key = submission.idempotency_key
        for attempt in (0, 1):
            try:
                resp = self._client.request(
                    "POST", self._paths["submit"],
                    query={"unique_code": code},
                    payload={"unique_code": code, "flag": submission.answer.strip()})
                break
            except ArenaProtocolError as exc:
                text = str(exc)
                if "409" in text and "challenge_not_started" in text:
                    if attempt == 0:
                        self._ensure_started(code)
                        continue
                if "409" in text and ("duplicate" in text.lower() or "already" in text.lower()):
                    # 已计数：视为接受的幂等回执，不重发（附录 E）。
                    return SubmitReceipt(status="ACCEPTED", receipt_id="dup-" + key[:8],
                                         detail="already counted")
                if "409" in text and "invalid_state" in text.lower():
                    # invalid_state ≠ 答案错误：题目可能已被其他路径（如早前提交）
                    # 完成。先只读查询平台题目列表对账；能证实已完成 → 幂等接受；
                    # 证实不了 → 落 UNKNOWN（不伪造 REJECTED 终态），等待人工对账。
                    if self._probe_challenge_state(code):
                        return SubmitReceipt(status="ACCEPTED", receipt_id="state-" + key[:8],
                                             detail="platform reports challenge already completed")
                    return SubmitReceipt(status="UNKNOWN",
                                         detail="invalid_state without completion confirmation; reconcile manually")
                return SubmitReceipt(status="REJECTED", detail=text[:300])
        status = ""
        platform_detail = ""
        if isinstance(resp, dict):
            correct = resp.get("correct")
            if correct is not None:
                # arena 平台真实回执（2026-11-21 实测 143B/145B）：{'correct': bool,
                # awarded, cumulative_score, correct_flag_count, total_flag_count,
                # matched_flag_index, is_completed}——布尔字段，不携带 status/result
                # 字符串协议（v11 正式赛第 1 题平台已判对并计 1000 分，仍因本处
                # 只认字符串而落 UNKNOWN 的教训）。accepted/rejected 的字符串
                # 匹配保留：兼容旧协议与布尔转写的 "true"/"false"。
                status = "true" if _receipt_truthy(correct) else "false"
            else:
                status = str(resp.get("status") or resp.get("result") or "").lower()
            platform_detail = " ".join(
                "{}={}".format(k, resp[k]) for k in
                ("awarded", "cumulative_score", "is_completed",
                 "correct_flag_count", "total_flag_count", "matched_flag_index")
                if k in resp)
        if status in ("accepted", "correct", "success", "ok", "true"):
            return SubmitReceipt(status="ACCEPTED", receipt_id=str(resp.get("receipt") or key[:8]),
                                 detail=platform_detail or "accepted")
        if status in ("rejected", "incorrect", "wrong", "false"):
            detail = platform_detail or str(resp.get("detail") or "")[:300]
            return SubmitReceipt(status="REJECTED", detail=detail[:300])
        return SubmitReceipt(status="UNKNOWN", detail=str(resp)[:300])

    def get_receipt(self, idempotency_key: str) -> Optional[SubmitReceipt]:
        """按幂等键查询本地已记录回执。"""
        return self._receipts.get(idempotency_key)

    def _probe_challenge_state(self, code: str) -> bool:
        """409 invalid_state 时的只读对账：查题目列表判断该题是否已完成。

        平台没有独立的回执查询端点；题目列表若携带完成态字段即可判定。
        字段名各版本不一（solved/completed/correct/...），逐个试探；
        查不到完成态时保守返回 False，由调用方落 UNKNOWN，不伪造终态。
        """
        try:
            resp = self._client.request("GET", self._paths["list"])
        except Exception:
            return False
        items = resp if isinstance(resp, list) else (resp.get("challenges")
                                                    or resp.get("items") or [])
        if not isinstance(items, list):
            return False
        truthy = ("true", "yes", "1", "solved", "completed", "correct", "accepted")
        for item in items:
            if not isinstance(item, dict):
                continue
            item_code = str(item.get("unique_code") or item.get("uniqueCode")
                            or item.get("id") or "").strip()
            if item_code != code:
                continue
            for field in ("solved", "completed", "correct", "accepted",
                          "is_completed", "is_solved", "flag_correct"):
                value = item.get(field)
                if value is True or value == 1:
                    return True
                if isinstance(value, str) and value.strip().lower() in truthy:
                    return True
            if str(item.get("status") or "").strip().lower() in truthy:
                return True
        return False

    # ---------------------------------------------------------------- finalize
    def finalize(self, summary: RunSummary) -> dict:
        """比赛规则的 finish 是显式决策（全部题已决出或无法推进）。

        auto_finish=False（默认）时绝不向平台发送 finish——本地运行结束、
        调试中断都不等于比赛收尾；否则一次普通的 --seconds 运行会把整个
        attempt 关掉。只有配置显式开启后才发送，失败向上抛。
        """
        out = {"protocol": "v1-arena", "competition_id": "cyber-arena",
               "results": summary.results, "notes": summary.notes,
               "finalized_at": time.time(), "arena_finish_sent": False}
        if not self._auto_finish:
            return out
        resp = self._client.request("POST", self._paths["finish"],
                                    payload={"results": summary.results,
                                             "notes": summary.notes})
        out["arena_finish_sent"] = True
        if isinstance(resp, dict):
            out["arena_response"] = resp
        return out
