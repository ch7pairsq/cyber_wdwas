"""提交状态机：DRAFT→SENDING→ACCEPTED/REJECTED/UNKNOWN（§12.4、附录 E）。

UNKNOWN 只查询不重发；同一幂等键永不二次发送。
"""
from __future__ import annotations

import sqlite3
import time
from typing import Optional

from .adapter import HarnessAdapter
from .schema import Submission, SubmitReceipt

ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"
UNKNOWN = "UNKNOWN"
DRAFT = "DRAFT"
SENDING = "SENDING"


class SubmissionLocked(Exception):
    pass


class Submitter:
    def __init__(self, adapter: HarnessAdapter, policy_engine=None,
                 db: Optional[sqlite3.Connection] = None):
        self._adapter = adapter
        self._policy = policy_engine
        self._db = db
        self._sent = {}  # 进程内“已发送”事实：无 db 时也绝不重复发送

    def _record(self, sub: Submission, status: str, receipt: Optional[SubmitReceipt]) -> None:
        if not self._db:
            return
        self._db.execute(
            "INSERT INTO submissions(idempotency_key, challenge_id, answer, status, receipt_id, detail,"
            " evidence_ids, sent_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(idempotency_key) DO UPDATE SET status=excluded.status,"
            " receipt_id=excluded.receipt_id, detail=excluded.detail, updated_at=excluded.updated_at",
            (
                sub.idempotency_key, sub.challenge_id, sub.answer, status,
                receipt.receipt_id if receipt else "", receipt.detail if receipt else "",
                "\n".join(sub.evidence_ids), time.time(), time.time(),
            ),
        )
        self._db.commit()

    def submit(self, sub: Submission) -> SubmitReceipt:
        if self._policy is not None:
            decision = self._policy.decide_submission()
            if decision.action == "deny":
                raise SubmissionLocked(decision.reason_code)
        existing: Optional[SubmitReceipt] = None
        if sub.idempotency_key in self._sent:
            existing = self._sent[sub.idempotency_key]
        elif self._db:
            row = self._db.execute(
                "SELECT status, receipt_id, detail FROM submissions WHERE idempotency_key=?",
                (sub.idempotency_key,),
            ).fetchone()
            if row:
                existing = SubmitReceipt(status=row["status"], receipt_id=row["receipt_id"], detail=row["detail"])
        if existing is None:
            self._record(sub, SENDING, None)
            existing = self._adapter.submit_answer(sub)
            self._record(sub, existing.status, existing)
        elif existing.status == UNKNOWN:
            # 只查询回执，不重发（附录 E：UNKNOWN 是最危险状态）
            receipt = self._adapter.get_receipt(sub.idempotency_key)
            if receipt is not None:
                self._record(sub, receipt.status, receipt)
                existing = receipt
        self._sent[sub.idempotency_key] = existing
        return existing

    def query_unknown(self, sub: Submission) -> Optional[SubmitReceipt]:
        return self._adapter.get_receipt(sub.idempotency_key)
