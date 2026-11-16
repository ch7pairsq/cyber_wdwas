"""调度公平性与看门狗孤儿清理（§5、§8、附录 D）。"""
import time

from _support import add_challenge, new_board

from ctf_ext.domain.models import OUTCOME_SUCCESS
from ctf_ext.runtime.scheduler import Scheduler
from ctf_ext.runtime.watchdog import recover_expired_leases
from ctf_ext.store.blackboard import IntentDraft, WorkerResult


def test_scheduler_respects_per_challenge_cap():
    board, conn = new_board()
    add_challenge(board, "c1")
    add_challenge(board, "c2")
    for c in ("c1", "c2"):
        for i in range(3):
            board.add_intent(IntentDraft(challenge_id=c, strategy_key="s{}".format(i), params={"i": i}))
    sched = Scheduler(board, max_project_workers=1, max_running_projects=3)
    first = sched.pick_next("w1", lease_seconds=600)
    assert first is not None
    second = sched.pick_next("w2", lease_seconds=600)
    assert second is not None
    assert second.challenge_id != first.challenge_id  # c1 已占满 1 个名额 → 从 c2 领取
    # 释放 c1 后，c2 仍占用名额 → 下一次只能从 c1 领取
    board.release(first.intent_id, "w1", OUTCOME_SUCCESS)
    third = sched.pick_next("w3", lease_seconds=600)
    assert third is not None and third.challenge_id == first.challenge_id


def test_rank_prefers_expected_points_per_token():
    board, conn = new_board()
    add_challenge(board, "easy", points=950, level=1)
    add_challenge(board, "hard", points=950, level=5)
    board.add_intent(IntentDraft(challenge_id="easy", strategy_key="s", params={}, est_tokens=10000, points_hint=950))
    board.add_intent(IntentDraft(challenge_id="hard", strategy_key="s", params={}, est_tokens=90000, points_hint=950))
    # 相同得分下，Token 成本低的题优先（§23.2-4）
    rows = board.list_open()
    assert rows[0]["challenge_id"] == "easy"


def test_watchdog_orphan_cleanup():
    board, conn = new_board()
    add_challenge(board, "c1")
    board.add_intent(IntentDraft(challenge_id="c1", strategy_key="s", params={}))
    claim = board.claim_intent("w1", lease_seconds=100000)  # 租约很长
    conn.execute("UPDATE intents_ext SET updated_at=? WHERE id=?", (time.time() - 1000, claim.intent_id))
    conn.commit()
    stats = recover_expired_leases(board, hard_limit_seconds=600)
    assert stats["orphaned"] == 1 and stats["requeued"] == 0
    row = conn.execute("SELECT status, note FROM intents_ext WHERE id=?", (claim.intent_id,)).fetchone()
    assert row["status"] == "FAILED_RETRYABLE" and row["note"] == "stale_orphan"
