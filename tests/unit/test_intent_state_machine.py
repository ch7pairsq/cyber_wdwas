"""Intent 状态机回归：释放/写结果必须清租约，blocked 不得无限重领。"""
from _support import add_challenge, new_board

from ctf_ext.domain.models import OUTCOME_ERROR, OUTCOME_SUCCESS
from ctf_ext.store.blackboard import IntentDraft, WorkerResult


def _result(claim, status, outcome=OUTCOME_ERROR, owner="w1"):
    return WorkerResult(
        intent_id=claim.intent_id,
        owner=owner,
        attempt_no=claim.attempt_no,
        challenge_id=claim.challenge_id,
        strategy_key=claim.strategy_key,
        status=status,
        outcome=outcome,
    )


def _intent_row(conn, intent_id):
    return conn.execute(
        "SELECT status, owner, lease_until FROM intents_ext WHERE id=?", (intent_id,)
    ).fetchone()


def test_release_clears_lease_and_allows_immediate_reclaim():
    board, conn = new_board()
    add_challenge(board, "c1")
    board.add_intent(IntentDraft(challenge_id="c1", strategy_key="s", params={}))
    first = board.claim_intent("w1", lease_seconds=60)
    board.release(first.intent_id, "w1", "error", attempt_no=first.attempt_no)

    row = _intent_row(conn, first.intent_id)
    assert row["status"] == "FAILED_RETRYABLE"
    assert row["owner"] is None and row["lease_until"] is None

    second = board.claim_intent("w2", lease_seconds=60)
    assert second is not None and second.intent_id == first.intent_id


def test_retryable_result_clears_lease_and_allows_immediate_reclaim():
    board, conn = new_board()
    add_challenge(board, "c1")
    board.add_intent(IntentDraft(challenge_id="c1", strategy_key="s", params={}))
    claim = board.claim_intent("w1", lease_seconds=60)
    board.write_result(_result(claim, "partial"), "key-partial")

    row = _intent_row(conn, claim.intent_id)
    assert row["status"] == "FAILED_RETRYABLE"
    assert row["owner"] is None and row["lease_until"] is None
    assert board.claim_intent("w2", lease_seconds=60) is not None


def test_completed_with_error_outcome_is_retryable():
    board, conn = new_board()
    add_challenge(board, "c1")
    board.add_intent(IntentDraft(challenge_id="c1", strategy_key="s", params={}))
    claim = board.claim_intent("w1", lease_seconds=60)
    board.write_result(_result(claim, "completed", outcome=OUTCOME_ERROR), "key-error")

    row = _intent_row(conn, claim.intent_id)
    assert row["status"] == "FAILED_RETRYABLE"
    assert row["lease_until"] is None


def test_blocked_result_is_terminal_and_not_reclaimable():
    board, conn = new_board()
    add_challenge(board, "c1")
    board.add_intent(IntentDraft(challenge_id="c1", strategy_key="s", params={}))
    claim = board.claim_intent("w1", lease_seconds=60)
    result = _result(claim, "blocked")
    result.failure_reason = "attachment missing"
    board.write_result(result, "key-blocked")

    row = _intent_row(conn, claim.intent_id)
    assert row["status"] == "FAILED"
    assert row["owner"] is None and row["lease_until"] is None
    assert board.claim_intent("w2", lease_seconds=60) is None
    assert board.list_open() == []


def test_successful_result_clears_lease():
    board, conn = new_board()
    add_challenge(board, "c1")
    board.add_intent(IntentDraft(challenge_id="c1", strategy_key="s", params={}))
    claim = board.claim_intent("w1", lease_seconds=60)
    board.write_result(_result(claim, "completed", outcome=OUTCOME_SUCCESS), "key-success")

    row = _intent_row(conn, claim.intent_id)
    assert row["status"] == "COMPLETED"
    assert row["owner"] is None and row["lease_until"] is None
