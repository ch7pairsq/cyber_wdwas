"""模型接入抽象与限流（§11：Provider 接口预留，业务代码零改动可换模型）。"""
from __future__ import annotations

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
