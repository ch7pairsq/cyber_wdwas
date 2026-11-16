"""附录 D 不变量验证：互斥领取、幂等写入、去重、租约回收、过期写入拒绝。"""
import threading
import time

from _support import add_challenge, new_board

from ctf_ext.domain.models import OUTCOME_SUCCESS
from ctf_ext.runtime.watchdog import recover_expired_leases
from ctf_ext.store.blackboard import (
    DuplicateStrategy, IntentDraft, StaleWrite, WorkerResult,
)


def test_claim_is_exclusive():
    board, conn = new_board()
    add_challenge(board, "c1")
    for i in range(5):
        board.add_intent(IntentDraft(challenge_id="c1", strategy_key="s{}".format(i), params={"i": i}))
    claims = []
    lock = threading.Lock()

    def work(name):
        for _ in range(10):
            c = board.claim_intent(name, lease_seconds=60)
            if c:
                with lock:
                    claims.append(c.intent_id)

    threads = [threading.Thread(target=work, args=("w{}".format(n),)) for n in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 无人释放租约 → 5 个 Intent 每个只能被领取一次，绝无重复
    assert len(claims) == 5, claims
    assert len(set(claims)) == 5


def test_write_result_idempotent():
    board, conn = new_board()
    add_challenge(board, "c1")
    board.add_intent(IntentDraft(challenge_id="c1", strategy_key="s", params={}))
    claim = board.claim_intent("w1", lease_seconds=60)
    result = WorkerResult(intent_id=claim.intent_id, owner="w1", attempt_no=claim.attempt_no,
                          challenge_id="c1", strategy_key="s", status="completed",
                          outcome=OUTCOME_SUCCESS, summary=["flag found"])
    r1 = board.write_result(result, "key-1")
    r2 = board.write_result(result, "key-1")
    assert r1.first is True and r2.first is False
    assert r1.attempt_id == r2.attempt_id
    assert conn.execute("SELECT COUNT(*) c FROM attempts").fetchone()["c"] == 1


def test_duplicate_strategy_rejected():
    board, conn = new_board()
    add_challenge(board, "c1")
    board.add_intent(IntentDraft(challenge_id="c1", strategy_key="s", params={"a": 1}))
    try:
        board.add_intent(IntentDraft(challenge_id="c1", strategy_key="s", params={"a": 1}))
        raise AssertionError("expected DuplicateStrategy for open intent")
    except DuplicateStrategy as exc:
        assert exc.blocked_by.startswith("intent:")
    # 关闭后同样被 attempts 层去重拦截
    claim = board.claim_intent("w1", lease_seconds=60)
    board.write_result(WorkerResult(intent_id=claim.intent_id, owner="w1", attempt_no=claim.attempt_no,
                                    challenge_id="c1", strategy_key="s", status="completed",
                                    outcome=OUTCOME_SUCCESS), "key-dup")
    try:
        board.add_intent(IntentDraft(challenge_id="c1", strategy_key="s", params={"a": 1}))
        raise AssertionError("expected DuplicateStrategy for finished attempt")
    except DuplicateStrategy as exc:
        assert exc.blocked_by.startswith("attempt:")


def test_lease_recovery_by_watchdog():
    board, conn = new_board()
    add_challenge(board, "c1")
    board.add_intent(IntentDraft(challenge_id="c1", strategy_key="s", params={}))
    assert board.claim_intent("w1", lease_seconds=0.05) is not None
    time.sleep(0.09)
    stats = recover_expired_leases(board)
    assert stats["requeued"] == 1
    claim = board.claim_intent("w2", lease_seconds=60)
    assert claim is not None  # 崩溃 Worker 的任务被接续


def test_stale_write_rejected():
    board, conn = new_board()
    add_challenge(board, "c1")
    board.add_intent(IntentDraft(challenge_id="c1", strategy_key="s", params={}))
    claim = board.claim_intent("w1", lease_seconds=60)
    result = WorkerResult(intent_id=claim.intent_id, owner="w1", attempt_no=claim.attempt_no,
                          challenge_id="c1", strategy_key="s", status="completed",
                          outcome=OUTCOME_SUCCESS)
    try:
        board.write_result(
            WorkerResult(intent_id=claim.intent_id, owner="intruder", attempt_no=claim.attempt_no,
                         challenge_id="c1", strategy_key="s", status="completed",
                         outcome=OUTCOME_SUCCESS), "k-stale")
        raise AssertionError("expected StaleWrite")
    except StaleWrite:
        pass
    # 租约被回收后（状态不再是 RUNNING），原 Worker 的写入同样拒绝
    recover_expired_leases(board, now=time.time() + 120)
    try:
        board.write_result(result, "k-stale2")
        raise AssertionError("expected StaleWrite after recovery")
    except StaleWrite:
        pass
