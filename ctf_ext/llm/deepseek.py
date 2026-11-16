"""DeepSeek Flash 实现（已核验：base_url、模型名；附录 F-5）。

Key 只从环境变量读取，绝不写入日志或 Prompt；requests 为懒加载，
本地骨架验证（无 requests）也能 import 本模块。
"""
from __future__ import annotations

import os
from typing import List, Optional

from .provider import (
    ModelProvider,
    ProviderConfigError,
    ProviderError,
    ProviderResponse,
    ProviderTimeout,
    RateLimited,
    RateLimiter,
)

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"


class DeepSeekProvider(ModelProvider):
    def __init__(self, api_key: Optional[str] = None, base_url: str = DEFAULT_BASE_URL,
                 model: str = DEFAULT_MODEL, timeout: float = 120.0,
                 limiter: Optional[RateLimiter] = None):
        self._api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._limiter = limiter or RateLimiter(max_inflight=3)

    def __repr__(self) -> str:  # 防止 Key 泄漏到日志
        return "DeepSeekProvider(model={}, base_url={}, key=***)".format(self._model, self._base_url)

    def complete(self, messages, *, system=None, json_mode=False, timeout=None,
                 max_tokens=4096) -> ProviderResponse:
        import requests  # 懒加载：骨架自检环境可能未安装

        if not self._api_key:
            raise ProviderConfigError("DEEPSEEK_API_KEY not set")
        payload = {
            "model": self._model,
            "messages": ([{"role": "system", "content": system}] if system else []) + list(messages),
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": "Bearer " + self._api_key}
        if not self._limiter.acquire():
            raise RateLimited("model inflight saturated")
        try:
            resp = requests.post(self._base_url + "/chat/completions", json=payload,
                                 headers=headers, timeout=timeout or self._timeout)
            if resp.status_code == 429:
                self._limiter.on_rate_limited()
                raise RateLimited("429 from provider")
            if resp.status_code >= 400:
                raise ProviderError("provider error {}: {}".format(resp.status_code, resp.text[:200]))
            data = resp.json()
            self._limiter.on_success()
            usage = data.get("usage") or {}
            text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
            prompt_tokens = int(usage.get("prompt_tokens", 0))
            completion_tokens = int(usage.get("completion_tokens", 0))
            return ProviderResponse(text=text, usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            }, raw=data)
        except requests.Timeout as exc:
            raise ProviderTimeout(str(exc)) from exc
        finally:
            self._limiter.release()
