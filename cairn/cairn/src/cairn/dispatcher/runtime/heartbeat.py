from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from cairn.dispatcher.protocol.client import ApiResult, CairnClient
from cairn.dispatcher.runtime.process import ExecProcess


LOG = logging.getLogger(__name__)
HEARTBEAT_FAILURE_GRACE_MULTIPLIER = 2


@dataclass(slots=True)
class HeartbeatFailure:
    """心跳终态失败记录：状态码与响应文本。"""
    status_code: int | None
    text: str


class HeartbeatLease:
    """后台心跳租约线程：周期续期，连续失败或 403/409 时杀 worker 进程止损。"""
    def __init__(
        self,
        heartbeat: Callable[[], ApiResult],
        scope: str,
        worker_name: str,
        interval: int,
    ):
        """heartbeat 为续期回调；interval 为发送周期（秒）。"""
        self._heartbeat = heartbeat
        self._scope = scope
        self._worker_name = worker_name
        self._interval = interval
        self._process: ExecProcess | None = None
        self._failure: HeartbeatFailure | None = None
        self._last_success_at = time.monotonic()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)

    @classmethod
    def for_intent(
        cls,
        client: CairnClient,
        project_id: str,
        intent_id: str,
        worker_name: str,
        interval: int,
    ) -> "HeartbeatLease":
        """构造 Intent 租约心跳。"""
        return cls(
            heartbeat=lambda: client.heartbeat(project_id, intent_id, worker_name),
            scope=f"project={project_id} intent={intent_id}",
            worker_name=worker_name,
            interval=interval,
        )

    @classmethod
    def for_reason(
        cls,
        client: CairnClient,
        project_id: str,
        worker_name: str,
        interval: int,
    ) -> "HeartbeatLease":
        """构造项目级 reason 锁心跳。"""
        return cls(
            heartbeat=lambda: client.reason_heartbeat(project_id, worker_name),
            scope=f"project={project_id} reason",
            worker_name=worker_name,
            interval=interval,
        )

    def start(self) -> None:
        """启动心跳线程。"""
        self._thread.start()

    def stop(self) -> None:
        """停止心跳线程（任务结束时调用）。"""
        self._stop.set()
        self._thread.join(timeout=1)

    def attach_process(self, process: ExecProcess | None) -> None:
        """挂接/解绑当前进程；若已判失败立即补杀。"""
        with self._lock:
            self._process = process

    @property
    def failure(self) -> HeartbeatFailure | None:
        """心跳失败详情（未失败为 None）。"""
        return self._failure

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            result = self._heartbeat()
            if result.ok:
                self._last_success_at = time.monotonic()
                continue
            if result.status_code in (403, 409):
                self._fail(result.status_code, result.text)
                return
            elapsed = time.monotonic() - self._last_success_at
            grace_seconds = max(float(self._interval), float(self._interval * HEARTBEAT_FAILURE_GRACE_MULTIPLIER))
            LOG.warning(
                "heartbeat transient failure scope=%s worker=%s status=%s elapsed=%.1fs grace=%.1fs",
                self._scope,
                self._worker_name,
                result.status_code,
                elapsed,
                grace_seconds,
            )
            if elapsed < grace_seconds:
                continue
            self._fail(result.status_code or None, result.text)
            return

    def _fail(self, status_code: int | None, text: str) -> None:
        self._failure = HeartbeatFailure(status_code, text)
        LOG.warning(
            "heartbeat failed scope=%s worker=%s status=%s",
            self._scope,
            self._worker_name,
            status_code,
        )
        with self._lock:
            process = self._process
        if process is not None:
            process.kill()
