"""模型接入抽象与限流（§11：Provider 接口预留，业务代码零改动可换模型）。"""
from __future__ import annotations

import os
import tempfile
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class RateLimited(Exception):
    """模型端返回 429/限流：触发全局退避，不视为致命错误。"""
    pass


class ProviderTimeout(Exception):
    """模型调用超时。"""
    pass


class ProviderConfigError(Exception):
    """模型配置错误（缺 key/base_url 等）：属阻塞项，不重试。"""
    pass


class ProviderError(Exception):
    """其他模型端错误（5xx/网络/协议）。"""
    pass


@dataclass
class ProviderResponse:
    """模型响应统一形状：正文/用量/原始负载/工具调用/停止原因。"""
    text: str
    usage: Dict[str, int] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    stop_reason: str = ""


# 传输层连接类失败（SSL/连接拒绝等快速失败）的退避重试延迟；超时不在传输层重试。
TRANSPORT_RETRY_DELAYS = (5.0, 15.0, 30.0)

# 平台出口为 TLS-MITM 型时（如比赛环境）注入的 CA 文件变量，
# 与 BAIZHI_API_KEY 同一信任级（均由平台官方注入容器环境）。
CA_ENV_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS")
_ca_bundle_cache = None


def platform_ca_bundle():
    """返回模型传输的 verify 参数：True 或合并 CA bundle 文件路径。

    trust_env=False + verify=True 只认 certifi，平台注入的 CA 会全部失效
    （2026-11-20 平台实跑 SSLError 根因：MITM 拦截链不被信任，direct/proxy
    双路由全挂）。本函数把 certifi 公共 CA 与上述环境变量指向的 PEM 文件
    合并为一次性 bundle（0600）交给 verify=：信任源只是平台官方注入的 CA
    文件，证书链校验完整保留，不是禁用验证。无注入文件或内容非 PEM 时
    返回 True（本地/无 MITM 环境行为不变）。结果按环境变量快照缓存，
    同一快照复用已生成文件（平台注入后内容在进程生命周期内不变）。
    """
    global _ca_bundle_cache
    snapshot = tuple((name, os.environ.get(name) or "") for name in CA_ENV_VARS)
    if _ca_bundle_cache is not None and _ca_bundle_cache[0] == snapshot:
        return _ca_bundle_cache[1]
    sources = []
    for _, path in snapshot:
        path = path.strip()
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        if b"-----BEGIN CERTIFICATE" in data:
            sources.append(data)
    if not sources:
        _ca_bundle_cache = (snapshot, True)
        return True
    import certifi
    fd, merged = tempfile.mkstemp(prefix="ctf-ext-ca-", suffix=".pem")
    try:
        with os.fdopen(fd, "wb") as fh:
            previous_tail = None
            for chunk in [certifi.contents().encode("ascii")] + sources:
                if previous_tail is not None and not previous_tail.endswith(b"\n"):
                    fh.write(b"\n")
                fh.write(chunk)
                previous_tail = chunk
        os.chmod(merged, 0o600)
    except OSError:
        # 写 bundle 失败则退回默认校验源（certifi），与修复前行为一致。
        try:
            os.unlink(merged)
        except OSError:
            pass
        _ca_bundle_cache = (snapshot, True)
        return True
    _ca_bundle_cache = (snapshot, merged)
    return merged


class ModelProvider(ABC):
    """模型通道抽象：所有业务代码只依赖 complete()，可零改动换模型。"""
    @abstractmethod
    def complete(self, messages: List[Dict[str, Any]], *, system: Optional[str] = None,
                 json_mode: bool = False, timeout: float = 120.0,
                 max_tokens: int = 4096,
                 tools: Optional[List[Dict[str, Any]]] = None) -> ProviderResponse:
        """同步补全：返回统一 ProviderResponse；限流/超时/配置错分别抛对应异常。"""
        ...


class RateLimiter:
    """全局模型信号量 + 自适应退避（§5.2：429 时先降模型并发）。"""

    def __init__(self, max_inflight: int = 3):
        """max_inflight 为全局并发上限；退避初始 1s，指数增长封顶 60s。"""
        self._sem = threading.BoundedSemaphore(max_inflight)
        self._lock = threading.Lock()
        self._backoff = 1.0

    def acquire(self, wait_backoff: bool = True) -> bool:
        """占用一个并发槽；等待时长为当前退避值，拿不到返回 False。"""
        timeout = self._backoff if wait_backoff else 0
        return self._sem.acquire(timeout=timeout)

    def release(self) -> None:
        """归还并发槽（重复释放安全）。"""
        try:
            self._sem.release()
        except ValueError:
            pass

    def on_rate_limited(self) -> None:
        """429 后倍增退避（封顶 60s）。"""
        with self._lock:
            self._backoff = min(self._backoff * 2, 60.0)

    def on_success(self) -> None:
        """成功后减半退避（下限 1s）。"""
        with self._lock:
            self._backoff = max(1.0, self._backoff / 2)

    @property
    def backoff(self) -> float:
        """当前退避等待秒数（监控/日志用）。"""
        return self._backoff
