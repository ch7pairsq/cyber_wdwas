"""最小 Planner：后续 Intent 的生成、去重与上限。"""
import tempfile

from tests.unit._support import add_challenge, new_board
from ctf_ext.__main__ import build_runtime
from ctf_ext.domain.models import OUTCOME_NO_SIGNAL
from ctf_ext.runtime.planner import IntentPlanner
from ctf_ext.store.blackboard import IntentDraft, WorkerResult


def _claim(board, cid="c1"):
    board.add_intent(IntentDraft(cid, "misc.triage.auto", {"phase": "initial"}))
    return board.claim_intent("worker")


def _no_signal(claim):
    return WorkerResult(
        claim.intent_id, "worker", claim.attempt_no, claim.challenge_id,
        claim.strategy_key, "completed", summary=["未发现信号"],
        outcome=OUTCOME_NO_SIGNAL, preconditions=["附件可读"],
        actions=["读取附件"], ruled_out=["原始输出无 flag"],
        next_best_action="尝试其他编码", resource_usage={},
    )


def test_planner_creates_serialized_followup_and_respects_cap():
    board, _ = new_board()
    add_challenge(board)
    claim = _claim(board)
    planner = IntentPlanner(board, max_intents_per_challenge=3)
    result = _no_signal(claim)

    first = planner.plan(claim, result, challenge={"points_hint": 100}, attempt_id="attempt-1")
    assert first
    rows = [row for row in board.list_open() if row["id"] == first]
    assert rows and rows[0]["strategy_key"].endswith(".followup.2")
    assert '"next_best_action":"尝试其他编码"' in rows[0]["params"]
    assert board.count_intents("c1") == 2

    second = planner.plan(claim, result, challenge={"points_hint": 100}, attempt_id="attempt-2")
    assert second and board.count_intents("c1") == 3

    assert planner.plan(claim, result, challenge={"points_hint": 100}, attempt_id="attempt-3") is None
    assert board.count_intents("c1") == 3
    types = [row["event_type"] for row in board.connection.execute(
        "SELECT event_type FROM events ORDER BY id")]
    assert "intent_planned" in types and "planner_cap_reached" in types
    board.close()


def test_planner_skips_blocked_and_confirmed_results():
    board, _ = new_board()
    add_challenge(board)
    claim = _claim(board)
    planner = IntentPlanner(board, max_intents_per_challenge=4)
    blocked = _no_signal(claim)
    blocked.status = "blocked"
    assert planner.plan(claim, blocked, attempt_id="attempt-blocked") is None
    confirmed = type("Decision", (), {"status": "confirmed", "level": "L1"})()
    assert planner.plan(claim, _no_signal(claim), confirmed, attempt_id="attempt-ok") is None
    assert board.count_intents("c1") == 1
    board.close()


def test_orchestrator_wires_planner_after_no_signal():
    with tempfile.TemporaryDirectory() as root:
        orch = build_runtime(root + "/run.db", root + "/evidence")
        orch.bootstrap()
        orch._planner.set_limit(2)
        class NoSignal:
            def work(self, claim, challenge):
                return _no_signal(claim)
        orch._worker = NoSignal()
        first = orch.step()
        assert first["status"] == "completed"
        cid = first["challenge_id"]
        assert orch._board.count_intents(cid) == 2
        assert any(row["event_type"] == "intent_planned" for row in orch._board.connection.execute(
            "SELECT event_type FROM events"))
        orch.step()
        assert orch._board.count_intents(cid) <= 2
        orch._gateway.close()
        orch._board.close()
