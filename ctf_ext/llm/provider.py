"""模型接入抽象与限流（§11：Provider 接口预留，业务代码零改动可换模型）。"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class RateLimited(Exception):
    pass


class ProviderTimeout(Exception):
    pass


class ProviderConfigError(Exception):
    pass


class ProviderError(Exception):
    pass


@dataclass
class ProviderResponse:
    text: str
    usage: Dict[str, int] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    stop_reason: str = ""


class ModelProvider(ABC):
    @abstractmethod
    def complete(self, messages: List[Dict[str, Any]], *, system: Optional[str] = None,
                 json_mode: bool = False, timeout: float = 120.0,
                 max_tokens: int = 4096,
                 tools: Optional[List[Dict[str, Any]]] = None) -> ProviderResponse:
        ...


class RateLimiter:
    """全局模型信号量 + 自适应退避（§5.2：429 时先降模型并发）。"""

    def __init__(self, max_inflight: int = 3):
        self._sem = threading.BoundedSemaphore(max_inflight)
        self._lock = threading.Lock()
        self._backoff = 1.0

    def acquire(self, wait_backoff: bool = True) -> bool:
        timeout = self._backoff if wait_backoff else 0
        return self._sem.acquire(timeout=timeout)

    def release(self) -> None:
        try:
            self._sem.release()
        except ValueError:
            pass

    def on_rate_limited(self) -> None:
        with self._lock:
            self._backoff = min(self._backoff * 2, 60.0)

    def on_success(self) -> None:
        with self._lock:
            self._backoff = max(1.0, self._backoff / 2)

    @property
    def backoff(self) -> float:
        return self._backoff
