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


def test_planner_caps_no_signal_chain_depth():
    """no_signal 链深达上限不再扩展（2026-11-22 模拟赛日志：pass2 连挂 9 层
    followup 全 no_signal 空转）；链深 2 仍正常扩展。"""
    board, _ = new_board()
    add_challenge(board)
    board.add_intent(IntentDraft(
        "c1", "misc.triage.auto.followup.1.followup.2.followup.3", {"phase": "followup"}))
    claim = board.claim_intent("worker")
    planner = IntentPlanner(board, max_intents_per_challenge=50)
    assert planner.plan(claim, _no_signal(claim), attempt_id="a1") is None
    types = [row["event_type"] for row in board.connection.execute(
        "SELECT event_type FROM events ORDER BY id")]
    assert "planner_no_signal_chain_capped" in types

    board.add_intent(IntentDraft(
        "c1", "web.recon.auto.followup.1.followup.2", {"phase": "followup"}))
    claim2 = board.claim_intent("worker")
    assert planner.plan(claim2, _no_signal(claim2), attempt_id="a2")
    board.close()


def test_planner_rejected_not_limited_by_chain_depth():
    """Gate rejected（平台反馈）不受 no_signal 链深限制：错旗续解循环依赖它。"""
    board, _ = new_board()
    add_challenge(board)
    board.add_intent(IntentDraft(
        "c1", "misc.triage.auto.followup.1.followup.2.followup.3", {"phase": "followup"}))
    claim = board.claim_intent("worker")
    planner = IntentPlanner(board, max_intents_per_challenge=50)
    decision = type("Decision", (), {"status": "rejected", "level": "L4"})()
    next_intent = planner.plan(claim, _no_signal(claim), decision, attempt_id="a1")
    assert next_intent
    rows = [row for row in board.list_open() if row["id"] == next_intent]
    # 序号取题内 intent 计数（现有语义）；关键断言：rejected 在链深 3 上仍扩展。
    assert rows and rows[0]["strategy_key"].startswith(
        "misc.triage.auto.followup.1.followup.2.followup.3.followup.")
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
