"""纯代码看门狗：租约回收、孤儿清理与单题硬超时（§14、附录 F-8）。

租约回收不依赖进程内存：Worker 崩溃后 Intent 在 lease_seconds 内被回收；
超过 hard_limit 仍未更新的标记为孤儿。DeadlineGuard 只用于主线程的
单题执行边界，超时后由上层写入可重试结果，不直接修改数据库。
"""
from __future__ import annotations

import signal
import threading
import time
from typing import Dict


class ClaimTimeout(BaseException):
    """单题硬超时信号。

    继承 BaseException，避免被 Worker/Provider 的通用 except Exception
    吞掉；只有编排层负责把它转换为可重试结果。
    """


class DeadlineGuard:
    """主线程 SIGALRM 超时；非主线程或平台不支持时自动降级为不拦截。"""

    def __init__(self, seconds: float):
        """seconds<=0 表示已到期，进入即抛 ClaimTimeout。"""
        self.seconds = max(0.0, float(seconds))
        self.active = False
        self._old_handler = None
        self._old_timer = None
        self._started = 0.0

    @staticmethod
    def supported() -> bool:
        """仅主线程且平台支持 SIGALRM/setitimer 时可用。"""
        return (threading.current_thread() is threading.main_thread()
                and hasattr(signal, "SIGALRM") and hasattr(signal, "setitimer"))

    def __enter__(self):
        """安装 SIGALRM 定时器；不支持的平台降级为不设闹钟（软超时兑底）。"""
        if self.seconds <= 0:
            raise ClaimTimeout("deadline already expired")
        if not self.supported():
            return self
        self._old_handler = signal.getsignal(signal.SIGALRM)
        self._old_timer = signal.getitimer(signal.ITIMER_REAL)
        self._started = time.monotonic()
        signal.signal(signal.SIGALRM, self._raise)
        signal.setitimer(signal.ITIMER_REAL, self.seconds)
        self.active = True
        return self

    @staticmethod
    def _raise(signum, frame):
        raise ClaimTimeout("claim deadline exceeded")

    def __exit__(self, *args):
        """恢复旧定时器与旧信号处理器（剩余时间重新折算）。"""
        if not self.active:
            return False
        signal.setitimer(signal.ITIMER_REAL, 0)
        if self._old_handler is not None:
            signal.signal(signal.SIGALRM, self._old_handler)
        if self._old_timer and self._old_timer[0] > 0:
            remaining = max(0.0, self._old_timer[0] - (time.monotonic() - self._started))
            signal.setitimer(signal.ITIMER_REAL, remaining, self._old_timer[1])
        self.active = False
        return False


def recover_expired_leases(board, now: float = None, hard_limit_seconds: float = 600.0) -> Dict[str, int]:
    """租约看门狗：过期租约回 READY 重派；超硬限制仍无更新的标记为孤儿。

    返回 {"requeued": 回收数, "orphaned": 孤儿标记数}。
    """
    now = now if now is not None else time.time()
    requeued = orphaned = 0
    with board.transaction() as db:
        rows = db.execute("SELECT id, owner, lease_until, updated_at FROM intents_ext WHERE status='RUNNING'").fetchall()
        for r in rows:
            if r["lease_until"] is not None and r["lease_until"] < now:
                db.execute(
                    # 回收必须同时清空 lease_until，否则新 Worker 仍会被旧租约挡住
                    "UPDATE intents_ext SET status='READY', owner=NULL, lease_until=NULL,"
                    " note='lease_expired:'||COALESCE(owner,''), updated_at=? WHERE id=?",
                    (now, r["id"]),
                )
                requeued += 1
            elif now - r["updated_at"] > hard_limit_seconds:
                db.execute(
                    "UPDATE intents_ext SET status='FAILED_RETRYABLE', owner=NULL, lease_until=NULL,"
                    " note='stale_orphan', updated_at=? WHERE id=?",
                    (now, r["id"]),
                )
                orphaned += 1
    return {"requeued": requeued, "orphaned": orphaned}
