"""下载控制器：预检 + 流式计数双重限额（§15.1）。

被拒绝时不发起任何网络连接；重定向默认阻断（逐跳需重新校验）。
"""
from __future__ import annotations

import urllib.request
from typing import Callable, Optional

from .policy import PolicyDenied, PolicyEngine


class QuotaExceeded(Exception):
    def __init__(self, used: int, limit: int):
        super().__init__("download exceeded: used {} > limit {}".format(used, limit))
        self.used = used
        self.limit = limit


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise PolicyDenied("REDIRECT_BLOCKED", "redirect blocked: " + str(newurl))


class DownloadController:
    def __init__(self, policy: PolicyEngine, opener: Optional[Callable] = None,
                 chunk_size: int = 65536):
        self._policy = policy
        self._chunk = chunk_size
        self.total_used = 0
        # 已用字节接入策略，总配额在预检与复核两处生效（§15.1 配额共享）
        self._policy.bind_total_used(lambda: self.total_used)
        if opener is None:
            self._opener = urllib.request.build_opener(_NoRedirect)
        else:
            self._opener = opener  # 测试可注入 fake opener

    def fetch_bytes(self, url: str, timeout: int = 30) -> bytes:
        # 先按 URL 做策略判定：拒绝时绝不连接
        self._policy.enforce(self._policy.decide_download(url))
        req = urllib.request.Request(url, headers={"User-Agent": "ctf-agent/0.1"})
        resp = self._opener.open(req, timeout=timeout)
        try:
            content_length = resp.headers.get("Content-Length")
            cl = int(content_length) if content_length else None
            # 响应头预检（Content-Length 已知时二次校验）
            self._policy.enforce(self._policy.decide_download(url, cl))
            limit = self._policy._cfg.max_single_bytes
            buf = bytearray()
            while True:
                chunk = resp.read(self._chunk)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > limit:
                    raise QuotaExceeded(len(buf), limit)
            self.total_used += len(buf)
            self._policy.enforce(self._policy.decide_total(url, self.total_used))  # 总配额复核
            return bytes(buf)
        finally:
            try:
                resp.close()
            except Exception:
                pass
