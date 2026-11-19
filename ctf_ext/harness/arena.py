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
DEFAULT_DOWNLOAD_TEMPLATE = ("/openapi/v1/challenges/files"
                             "?unique_code={unique_code}&path={path}")
ARENA_REF_SCHEME = "arena://"


class ArenaProtocolError(RuntimeError):
    """Arena 接口失败：属于协议错误，向上抛，不降级。"""


def _redact(text: str) -> str:
    """去掉可能混入错误文本的 token；防御性处理，正常路径 token 不进任何字符串。"""
    token = os.environ.get("ARENA_SOLVER_TOKEN", "")
    if token and token in text:
        text = text.replace(token, "<redacted>")
    return text


class ArenaClient:
    """只做 HTTP 收发与错误归一化；持有 token，不打印它。"""

    def __init__(self, base_url: str, token: str, timeout: float = 30.0,
                 proxy: str = ""):
        if not base_url:
            raise ArenaProtocolError("arena base_url is required")
        if not token:
            raise ArenaProtocolError("arena token is required (env ARENA_SOLVER_TOKEN)")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._proxy = self._parse_proxy(proxy)

    @staticmethod
    def _parse_proxy(proxy):
        if not proxy:
            return None
        rest = proxy
        user = b""
        password = b""
        if "@" in rest:
            cred, _, rest = rest.rpartition("@")
            u, _, pw = cred.partition(":")
            user, password = u.encode(), pw.encode()
        if not rest.startswith("socks5://"):
            raise ArenaProtocolError("arena proxy must be socks5://[user:pass@]host:port")
        rest = rest[len("socks5://"):]
        host, _, port_s = rest.partition(":")
        if not host or not port_s.isdigit():
            raise ArenaProtocolError("arena proxy must be socks5://[user:pass@]host:port")
        return (host, int(port_s), user, password)

    def _socks5_connect(self, host, port):
        import socket as _socket
        import struct as _struct
        phost, pport, user, password = self._proxy
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

    def _http_over_socks(self, method, host, port, path, headers, body):
        sock = self._socks5_connect(host, port)
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


    def request_raw(self, method: str, path: str, query: Dict[str, str] = None) -> bytes:
        """GET 原始字节（附件下载用），不做 JSON/文本解码。"""
        if self._proxy:
            parsed = urllib.parse.urlsplit(self.base_url)
            req_path = path + ("?" + urllib.parse.urlencode(query) if query else "")
            headers = {"Authorization": "Bearer " + self._token}
            code, body = self._http_over_socks(method, parsed.hostname,
                                               parsed.port or 80, req_path,
                                               headers, None)
            if code >= 400:
                raise ArenaProtocolError("arena {} {} -> HTTP {}".format(method, path, code))
            return body
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + self._token},
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            raise ArenaProtocolError("arena {} {} -> HTTP {}".format(
                method, path, exc.code)) from exc

    def request(self, method: str, path: str, query: Dict[str, str] = None,
                payload: Dict[str, Any] = None) -> Any:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None
        headers = {"Authorization": "Bearer " + self._token}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self._proxy:
            parsed = urllib.parse.urlsplit(self.base_url)
            req_path = path + ("?" + urllib.parse.urlencode(query) if query else "")
            code, body = self._http_over_socks(method, parsed.hostname,
                                               parsed.port or 80, req_path,
                                               headers, data)
            if code >= 400:
                detail = _redact(body.decode("utf-8", "replace")[:500])
                raise ArenaProtocolError("arena {} {} -> HTTP {}: {}".format(
                    method, path, code, detail))
            return _decode_body(method, path, body)
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            detail = _redact(exc.read().decode("utf-8", "replace")[:500])
            raise ArenaProtocolError(
                "arena {} {} -> HTTP {}: {}".format(method, path, exc.code, detail)) from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ArenaProtocolError(
                "arena {} {} failed: {}".format(method, path, _redact(str(exc)))) from exc
        ctype = ""
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


class ArenaHarnessAdapter(HarnessAdapter):
    """协议版本 v1-arena；端点默认值对应 Arena /openapi/v1 约定，可由配置覆盖。"""

    def __init__(self, base_url: str, token: str,
                 proxy: str = "",
                 help_path: str = DEFAULT_HELP_PATH,
                 list_path: str = DEFAULT_LIST_PATH,
                 start_path: str = DEFAULT_START_PATH,
                 files_path: str = DEFAULT_FILES_PATH,
                 submit_path: str = DEFAULT_SUBMIT_PATH,
                 finish_path: str = DEFAULT_FINISH_PATH,
                 download_template: str = DEFAULT_DOWNLOAD_TEMPLATE,
                 auto_start: bool = True,
                 auto_finish: bool = False,
                 max_envs: int = 3,
                 client_timeout: float = 30.0,
                 env_policy: str = "prewarm"):
        self._client = ArenaClient(base_url, token, timeout=client_timeout,
                                   proxy=proxy)
        self._paths = {"help": help_path, "list": list_path, "start": start_path,
                       "files": files_path, "submit": submit_path, "finish": finish_path}
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
        # 并发 worker 各自触发 ensure_environment：started/start_info 的
        # 记账用短锁保护；同题并发发起用 in-flight 事件去重（不持锁做 HTTP，
        # 不同题的 start 仍然并行）。
        self._env_lock = threading.RLock()
        self._inflight: Dict[str, threading.Event] = {}
        self._timeout = float(client_timeout)
        self.help_text: Any = {}

    # ------------------------------------------------------------------ startup
    def startup(self) -> HarnessContext:
        # help 是权威工作流与规则来源；内容只入 metadata，不做协议推断。
        self.help_text = self._client.request("GET", self._paths["help"])
        self._ctx = HarnessContext(protocol="v1-arena", competition_id="cyber-arena")
        return self._ctx

    # --------------------------------------------------------------- read_tasks
    def read_tasks(self) -> List[ChallengeInput]:
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
            except ArenaProtocolError:
                continue  # files 列表失败不阻塞读题；附件可后续按 ref 重试
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
        return list(self._tasks)

    def _env_note(self, info: Dict[str, Any], has_files: bool) -> str:
        """运行环境说明（附加到题目描述）；info 为空且无附件时返回空串。"""
        # container_addr 可能是字符串或列表（多端口）；统一成逗号分隔字符串。
        raw_addr = info.get("container_addr")
        if isinstance(raw_addr, (list, tuple)):
            raw_addr = ",".join(str(a) for a in raw_addr if a)
        addr = str(raw_addr or "").strip()
        parts = []
        if addr:
            parts.append("题目服务地址: http://" + addr
                         + "（用 arena_http 工具访问，host 填 IP 部分）")
        cidr = os.environ.get("ARENA_NETWORK_CIDR", "")
        if cidr:
            parts.append("允许访问的目标网段: " + cidr + "（网段之外一律拒绝）")
        if has_files:
            parts.append("题目附件已物化到工作区 handouts/ 目录，可直接用 file_info/strings/archive_extract 处理")
        if not parts:
            return ""
        return "[Arena 运行环境] " + "；".join(parts)

    def ensure_environment(self, unique_code: str):
        """按需拉起题目环境（env_policy=on_demand 时由编排层在每个意图前调用）。

        返回 start_info（新建成功时）或 None（已启动/并发发起中）；409
        duplicate/already 视为环境仍在运行；environment_limit 向上抛，由
        编排层记事件，意图快速失败后等槽位回收再重试。
        """
        with self._env_lock:
            if unique_code in self._started:
                return None
        try:
            self._ensure_started(unique_code)
        except ArenaProtocolError as exc:
            text = str(exc)
            if "duplicate" in text.lower() or "already" in text.lower():
                with self._env_lock:
                    self._started.add(unique_code)  # 已有环境在跑，幂等收编
                return None
            raise
        with self._env_lock:
            if unique_code not in self._started:
                # 并发发起者失败落地：本次按"未启动"返回，意图会快速失败重试。
                return None
            info = self._start_info.get(unique_code) or {}
            for task in self._tasks:
                if task.challenge_id == unique_code:
                    task.metadata["start_info"] = info
                    note = self._env_note(info, bool(task.metadata.get("arena_files")))
                    if note and "[Arena 运行环境]" not in (task.description or ""):
                        task.description = (task.description or "") + "\n\n" + note
                    break
            return info

    def task_description(self, challenge_id: str):
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
            # start 响应可能携带环境/服务地址等材料信息，原样保留供 worker 查阅。
            self._start_info[unique_code] = resp

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
        key = submission.idempotency_key
        if key in self._receipts:  # 幂等：不重复判定（附录 E）
            return self._receipts[key]
        receipt = self._submit_once(submission)
        self._receipts[key] = receipt
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
        if isinstance(resp, dict):
            status = str(resp.get("status") or resp.get("result") or "").lower()
        if status in ("accepted", "correct", "success", "ok", "true"):
            return SubmitReceipt(status="ACCEPTED", receipt_id=str(resp.get("receipt") or key[:8]))
        if status in ("rejected", "incorrect", "wrong", "false"):
            return SubmitReceipt(status="REJECTED", detail=str(resp.get("detail") or "")[:300])
        return SubmitReceipt(status="UNKNOWN", detail=str(resp)[:300])

    def get_receipt(self, idempotency_key: str) -> Optional[SubmitReceipt]:
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
