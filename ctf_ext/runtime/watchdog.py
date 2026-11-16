"""纯代码看门狗：租约回收与孤儿清理（§14、附录 F-8）。

不依赖进程内存：任何 Worker 崩溃后，其租约在 lease_seconds 内被回收；
超过 hard_limit 仍未更新的标记为孤儿并转入 FAILED_RETRYABLE。
"""
from __future__ import annotations

import time
from typing import Dict


def recover_expired_leases(board, now: float = None, hard_limit_seconds: float = 600.0) -> Dict[str, int]:
    now = now if now is not None else time.time()
    requeued = orphaned = 0
    with board._write() as db:
        rows = db.execute("SELECT id, owner, lease_until, updated_at FROM intents_ext WHERE status='RUNNING'").fetchall()
        for r in rows:
            if r["lease_until"] is not None and r["lease_until"] < now:
                db.execute(
                    "UPDATE intents_ext SET status='READY', owner=NULL, note='lease_expired:'||COALESCE(owner,''),"
                    " updated_at=? WHERE id=?",
                    (now, r["id"]),
                )
                requeued += 1
            elif now - r["updated_at"] > hard_limit_seconds:
                db.execute(
                    "UPDATE intents_ext SET status='FAILED_RETRYABLE', owner=NULL, note='stale_orphan',"
                    " updated_at=? WHERE id=?",
                    (now, r["id"]),
                )
                orphaned += 1
    return {"requeued": requeued, "orphaned": orphaned}
