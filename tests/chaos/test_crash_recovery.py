"""故障注入（附录 F-8、E）：Worker 崩溃后租约回收、过期写入拒绝、SENDING 只查询。"""
import sqlite3
import time

from _support import add_challenge, new_board

from ctf_ext.domain.models import OUTCOME_SUCCESS
from ctf_ext.harness.mock import MockHarnessAdapter
from ctf_ext.harness.schema import Submission, SubmitReceipt
from ctf_ext.harness.submit import Submitter
from ctf_ext.runtime.watchdog import recover_expired_leases
from ctf_ext.store.blackboard import IntentDraft, StaleWrite, WorkerResult


def test_crashed_worker_intent_is_retried_and_stale_write_rejected():
    board, conn = new_board()
    add_challenge(board, "c1")
    board.add_intent(IntentDraft(challenge_id="c1", strategy_key="misc.triage.auto", params={}))
    crashed = board.claim_intent("dead-worker", lease_seconds=30)
    recover_expired_leases(board, now=time.time() + 60)  # 租约到期
    revived = board.claim_intent("worker-2", lease_seconds=60)
    assert revived is not None and revived.intent_id == crashed.intent_id
    assert revived.attempt_no == crashed.attempt_no + 1

    stale = WorkerResult(intent_id=crashed.intent_id, owner="dead-worker",
                         attempt_no=crashed.attempt_no, challenge_id="c1",
                         strategy_key="misc.triage.auto", status="completed",
                         outcome=OUTCOME_SUCCESS)
    try:
        board.write_result(stale, "k-stale")
        raise AssertionError("expected StaleWrite")
    except StaleWrite:
        pass

    fresh = WorkerResult(intent_id=revived.intent_id, owner="worker-2",
                         attempt_no=revived.attempt_no, challenge_id="c1",
                         strategy_key="misc.triage.auto", status="completed",
                         outcome=OUTCOME_SUCCESS)
    assert board.write_result(fresh, "k-fresh").first is True


class _ReceiptOnlyAdapter(MockHarnessAdapter):
    def __init__(self):
        super().__init__([{"challenge_id": "c1", "name": "n", "flag": "flag{x}"}])
        self.submit_calls = 0

    def submit_answer(self, submission):
        self.submit_calls += 1
        return SubmitReceipt(status="REJECTED", detail="should never be called")

    def get_receipt(self, idempotency_key):
        return SubmitReceipt(status="ACCEPTED", receipt_id="rcpt-recovered")


def test_sending_state_only_queries_after_crash():
    board, conn = new_board()
    conn.execute(
        "INSERT INTO submissions(idempotency_key, challenge_id, answer, status, receipt_id, detail,"
        " evidence_ids, sent_at, updated_at) VALUES ('k','c1','flag{x}','SENDING','','', '', 0, 0)"
    )
    conn.commit()
    adapter = _ReceiptOnlyAdapter()
    submitter = Submitter(adapter, db=conn)
    receipt = submitter.submit(Submission(challenge_id="c1", answer="flag{x}", idempotency_key="k"))
    assert receipt.status == "ACCEPTED" and receipt.receipt_id == "rcpt-recovered"
    assert adapter.submit_calls == 0  # 已发送事实优先，绝不重发
    row = conn.execute("SELECT status FROM submissions WHERE idempotency_key='k'").fetchone()
    assert row["status"] == "ACCEPTED"
