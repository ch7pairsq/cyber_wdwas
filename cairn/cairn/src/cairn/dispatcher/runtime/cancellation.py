from __future__ import annotations

import threading

from cairn.dispatcher.runtime.process import ExecProcess


class TaskCancellation:
    """任务取消控制柄：线程安全记录原因并强杀挂接的进程（幂等）。"""
    def __init__(self) -> None:
        """初始化锁与取消状态（无挂接进程）。"""
        self._lock = threading.Lock()
        self._process: ExecProcess | None = None
        self._reason: str | None = None

    def attach_process(self, process: ExecProcess | None) -> None:
        """挂接进程；若此前已请求取消则立即对其生效。"""
        with self._lock:
            self._process = process
            reason = self._reason
        if process is not None and reason is not None:
            process.cancel(reason)

    def cancel(self, reason: str) -> bool:
        """请求取消：首次返回 True（重复取消返回 False）。"""
        with self._lock:
            already_cancelled = self._reason is not None
            if not already_cancelled:
                self._reason = reason
            process = self._process
        if process is not None:
            process.cancel(reason)
        return not already_cancelled

    @property
    def is_cancelled(self) -> bool:
        """是否已请求取消。"""
        return self.reason is not None

    @property
    def reason(self) -> str | None:
        """取消原因（未取消为 None）。"""
        with self._lock:
            return self._reason
