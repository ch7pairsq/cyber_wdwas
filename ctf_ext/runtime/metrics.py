"""运行时指标（§18.1）：只做进程内计数，供看板/日志消费，不参与判定。"""
from __future__ import annotations

import threading
import time

CATEGORIES = ("intents", "model", "tools", "knowledge", "submissions", "resources")


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._counters = {}
        self._started = time.time()

    def incr(self, name: str, value: int = 1) -> int:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + value
            return self._counters[name]

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            self._counters.setdefault(name + ".sum", 0)
            self._counters.setdefault(name + ".count", 0)
            self._counters[name + ".sum"] += value
            self._counters[name + ".count"] += 1

    def snapshot(self) -> dict:
        with self._lock:
            data = dict(self._counters)
        data["uptime_seconds"] = round(time.time() - self._started, 3)
        return data
