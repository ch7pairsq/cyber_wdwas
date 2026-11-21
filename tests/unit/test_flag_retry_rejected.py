"""平台 REJECTED 后重开题目续解（2026-11-21 正式赛第 2 题错旗教训）。

错旗通过 Gate 三门却被平台判 correct=false：会话不能永久钉死在 confirmed
（调度器 eligible_challenges 排除 confirmed、SessionControl 对
confirmed/stopped 抛 'session closed'）。锁定行为：
- REJECTED 回执 → 会话回 ready + 新步数窗口 + misc.flagretry.N 意图携带
  被拒 flag 原文（lessons 层会脱敏 flag，原文必须走 params）；
- ACCEPTED/UNKNOWN 回执不触发；
- 已被平台 ACCEPTED 的题不重开（回执竞态保护）；
- 每题 Intent 规划上限对重试同样生效。
"""
import json

from tests.framework_support import FLAG, fixture
from ctf_ext.runtime.control import SessionControl
from ctf_ext.runtime.metrics import Metrics
from ctf_ext.runtime.orchestrator import Orchestrator
from ctf_ext.runtime.planner import IntentPlanner


def _orch(store):
    """未初始化编排器桩：只装配 _collect_submissions/_plan_flag_retry 依赖。"""
    orch = Orchestrator.__new__(Orchestrator)
    orch._board = store.board
    orch._state = store
    orch._planner = IntentPlanner(store.board, max_intents_per_challenge=4)
    orch._metrics = Metrics()
    orch._flag_retry_seq = {}
    return orch


def _rejected_event(cid="c1", answer=FLAG):
    return {"challenge_id": cid, "status": "REJECTED", "receipt_id": "r-1",
            "idempotency_key": "sub-1", "answer": answer, "latency_seconds": 1.0}


def _intents(store):
    rows = store.db.execute(
        "SELECT strategy_key, params FROM intents_ext ORDER BY rowid").fetchall()
    return [(r["strategy_key"], json.loads(r["params"])) for r in rows]


def test_rejected_reopens_confirmed_session_and_plans_retry():
    with fixture() as store:
        SessionControl(store, "c1")  # 初始化会话状态
        store.patch("c1", lambda s: s.update(status="confirmed", steps=32))
        orch = _orch(store)
        orch._collect_submissions([_rejected_event()])
        _, st = store.load("c1")
        assert st["status"] == "ready"  # confirmed 复位，调度器重新可见
        assert st["steps"] == 0  # 新步数窗口（reset_intent_window）
        intents = _intents(store)
        assert len(intents) == 1
        assert intents[0][0] == "misc.flagretry.1"
        assert intents[0][1]["reason"] == "platform_rejected"
        assert intents[0][1]["rejected_flag"] == FLAG  # 原文进 params，不脱敏
        assert "correct=false" in intents[0][1]["instruction"]


def test_repeated_rejections_accumulate_unique_retry_intents():
    with fixture() as store:
        SessionControl(store, "c1")
        orch = _orch(store)
        orch._collect_submissions([_rejected_event()])
        second = _rejected_event(answer="flag{second_8a1c3d}")
        orch._collect_submissions([second])
        keys = [k for k, _ in _intents(store)]
        assert keys == ["misc.flagretry.1", "misc.flagretry.2"]


def test_accepted_or_unknown_receipts_do_not_reopen():
    with fixture() as store:
        SessionControl(store, "c1")
        store.patch("c1", lambda s: s.update(status="confirmed"))
        orch = _orch(store)
        orch._collect_submissions([dict(_rejected_event(), status="ACCEPTED")])
        orch._collect_submissions([dict(_rejected_event(), status="UNKNOWN")])
        _, st = store.load("c1")
        assert st["status"] == "confirmed"
        assert _intents(store) == []


def test_already_accepted_challenge_never_reopens():
    with fixture() as store:
        SessionControl(store, "c1")
        store.db.execute(
            "INSERT INTO submissions(idempotency_key,challenge_id,answer,status,"
            "evidence_ids,updated_at) VALUES('sub-acc','c1',?,'ACCEPTED','[]',1)",
            (FLAG,))
        orch = _orch(store)
        orch._collect_submissions([_rejected_event()])
        assert _intents(store) == []


def test_retry_respects_planner_cap():
    with fixture() as store:
        SessionControl(store, "c1")
        orch = _orch(store)
        orch._planner = IntentPlanner(store.board, max_intents_per_challenge=1)
        orch._collect_submissions([_rejected_event()])
        orch._collect_submissions([_rejected_event(answer="flag{second_8a1c3d}")])
        assert len(_intents(store)) == 1  # 上限 1：第二次只重开会话不再加意图


def test_missing_state_or_challenge_is_tolerated():
    with fixture() as store:
        orch = _orch(store)
        # 未知题号（无 challenge 行）：FK 拒绝创建会话/意图 → 只记
        # flag_retry_failed 事件，不向 pump 主流程抛出
        orch._collect_submissions([_rejected_event(cid="ghost")])
        assert _intents(store) == []
        # 空 challenge_id 直接忽略
        orch._collect_submissions([dict(_rejected_event(), challenge_id="")])
        assert _intents(store) == []
