"""best-effort 提交与观测行（2026-11-21 正式赛第 2 题教训）。

v12 正式赛：static_file_server 四次尝试全部 gate rejected/pending，
_persist_result 只对 confirmed 入队 → 零提交零得分；planner 上限 4 打满
后无新意图，endgame 只救 stopped 会话 → run 提前 idle 退出。锁定行为：
- best_effort_submit 开启（official 配置门控）时 rejected/pending 决策
  也入队，由平台真值仲裁；关闭时保持既有否决语义；
- gate 决策行始终诚实落库（best-effort 不伪造 confirmed）；
- [gate] stderr 观测行覆盖 confirmed/rejected/pending/timeout/无候选；
- endgame 扩展：规划上限打满且无在途工作的未解题也授权一次追加额度。
"""
import time
from pathlib import Path
from types import SimpleNamespace

from tests.framework_support import Adapter, FLAG, fixture
from ctf_ext.harness.flag_gate import FlagGate, GateDecision
from ctf_ext.harness.gateway import SubmitGateway
from ctf_ext.runtime.control import SessionControl
from ctf_ext.runtime.metrics import Metrics
from ctf_ext.runtime.orchestrator import Orchestrator
from ctf_ext.runtime.planner import IntentPlanner
from ctf_ext.store.blackboard import IntentDraft, WorkerResult

OFFICIAL_ARENA = Path(__file__).parents[2] / "config" / "official-arena.yaml"


def _stub(store, best_effort=True):
    """未初始化编排器桩：_persist_result / _evaluate_candidate 所需依赖。"""
    orch = Orchestrator.__new__(Orchestrator)
    orch._board = store.board
    orch._state = store
    orch._gate = FlagGate(store)
    orch._gateway = SubmitGateway(store, Adapter())
    orch._metrics = Metrics()
    orch._best_effort_submit = best_effort
    orch._claim_timeout = 0
    orch._clock = time.time
    orch._reviewer_factory = None
    return orch


def _claim_result(store, candidate=FLAG, strategy="misc.be.test"):
    store.board.add_intent(IntentDraft("c1", strategy))
    claim = store.board.claim_intent("w")
    result = WorkerResult(claim.intent_id, "w", claim.attempt_no, "c1",
                          claim.strategy_key, "completed", outcome="success")
    result.candidate_answer = candidate
    return claim, result


def test_persist_gate_best_effort_enqueues_rejected_candidate():
    with fixture() as store:
        SessionControl(store, "c1")
        orch = _stub(store, best_effort=True)
        claim, result = _claim_result(store)
        rejected = GateDecision("c1", FLAG, "rejected", "L4",
                                "missing declaration or grounding")
        receipt, _, keys = orch._persist_result(claim, result, rejected)
        assert receipt.first and len(keys) == 1
        assert store.db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 1
        assert store.db.execute("SELECT answer FROM submissions").fetchone()[0] == FLAG
        # 决策行诚实保留 rejected（审计不撒谎，best-effort 不伪造 confirmed）
        assert store.db.execute("SELECT status FROM gate_decisions").fetchone()[0] == "rejected"


def test_persist_gate_without_best_effort_keeps_veto():
    with fixture() as store:
        SessionControl(store, "c1")
        orch = _stub(store, best_effort=False)
        claim, result = _claim_result(store)
        rejected = GateDecision("c1", FLAG, "rejected", "L4",
                                "missing declaration or grounding")
        receipt, _, keys = orch._persist_result(claim, result, rejected)
        assert receipt.first and keys == []
        assert store.db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 0
        assert store.db.execute("SELECT COUNT(*) FROM gate_decisions").fetchone()[0] == 1


def test_persist_gate_best_effort_skips_invalid_format_without_failing_write():
    with fixture() as store:
        SessionControl(store, "c1")
        orch = _stub(store, best_effort=True)
        claim, result = _claim_result(store, candidate="flag{bad format}")
        rejected = GateDecision("c1", "flag{bad format}", "rejected", "L4", "decoy")
        receipt, _, keys = orch._persist_result(claim, result, rejected)
        assert receipt.first and keys == []  # 入队被拒不影响结果落库
        assert store.db.execute("SELECT COUNT(*) FROM results_ext").fetchone()[0] == 1
        assert store.db.execute("SELECT COUNT(*) FROM gate_decisions").fetchone()[0] == 1
        assert store.db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 0


def test_gate_line_prints_rejected_and_missing_gate(capsys):
    with fixture() as store:
        SessionControl(store, "c1")
        orch = _stub(store, best_effort=True)
        claim, result = _claim_result(store, candidate="flag{no_decl_31c4e2}")
        decision = orch._evaluate_candidate(claim, result, time.time())
        assert decision is not None and decision.status == "rejected"
        err = capsys.readouterr().err
        assert "[gate]" in err and "decision=rejected" in err and "intent=" in err
        # gate/gateway 未配置：也要打印（凑齐完整判定链）
        orch2 = _stub(store)
        orch2._gate = orch2._gateway = None
        claim2, result2 = _claim_result(store, strategy="misc.be.test.2")
        assert orch2._evaluate_candidate(claim2, result2, time.time()) is None
        err2 = capsys.readouterr().err
        assert "decision=none" in err2 and "gate or gateway not configured" in err2


def test_endgame_grants_capped_ready_challenge():
    """gate 拒绝的题会话是 ready：规划上限打满后也必须授权加时，不能提前退出。"""
    with fixture() as store:
        SessionControl(store, "c1")  # ready 会话（非 stopped）
        for i in range(4):  # planner 上限 4 打满
            store.board.add_intent(IntentDraft("c1", "misc.capped.{}".format(i)))
        orch = Orchestrator.__new__(Orchestrator)
        orch._board = store.board
        orch._state = store
        orch._planner = IntentPlanner(store.board, max_intents_per_challenge=4)
        orch._scheduler = SimpleNamespace(has_waiting_work=lambda: False)
        orch._endgame_bonus = 1.0
        orch._endgame_min_seconds = 0
        orch._endgame_granted = set()
        orch._endgame = {}
        orch._endgame_max_grants = 0
        orch._parallel_agents = 1
        orch._challenges = {}
        orch._metrics = Metrics()
        orch._clock = time.time
        factory = lambda cid: None  # noqa: E731 — 授权目标容器
        orch._worker = SimpleNamespace(_control_factory=factory)
        assert orch._grant_endgame([SimpleNamespace(challenge_id="c1")], set(),
                                   time.time() + 600) is True
        assert factory.endgame.get("c1") == 1.0
        assert store.db.execute(
            "SELECT COUNT(*) FROM intents_ext WHERE strategy_key='misc.endgame.pass2'"
        ).fetchone()[0] == 1


def test_endgame_skips_uncapped_ready_challenge():
    """还有规划余量的题让 planner 正常接手，endgame 不插手。"""
    with fixture() as store:
        SessionControl(store, "c1")
        orch = Orchestrator.__new__(Orchestrator)
        orch._board = store.board
        orch._state = store
        orch._planner = IntentPlanner(store.board, max_intents_per_challenge=4)
        orch._scheduler = SimpleNamespace(has_waiting_work=lambda: False)
        orch._endgame_bonus = 1.0
        orch._endgame_min_seconds = 0
        orch._endgame_granted = set()
        orch._endgame = {}
        orch._endgame_max_grants = 0
        orch._parallel_agents = 1
        orch._challenges = {}
        orch._metrics = Metrics()
        orch._clock = time.time
        orch._worker = SimpleNamespace(_control_factory=lambda cid: None)
        assert orch._grant_endgame([SimpleNamespace(challenge_id="c1")], set(),
                                   time.time() + 600) is False
        assert store.db.execute(
            "SELECT COUNT(*) FROM intents_ext WHERE strategy_key='misc.endgame.pass2'"
        ).fetchone()[0] == 0


def test_official_arena_config_enables_new_knobs():
    """official-arena.yaml 必须可加载、可校验，且新开关按预期生效。"""
    from ctf_ext.config import load_config, validate_config
    cfg = load_config(str(OFFICIAL_ARENA))
    assert validate_config(cfg) == []
    runtime = cfg.get("runtime") or {}
    submission = cfg.get("submission") or {}
    assert int(runtime.get("max_intents_per_challenge") or 0) == 12
    assert submission.get("best_effort_on_reject") is True
    # practice 入口模式不变：best-effort 仅由 submission 键控制，不动 app.mode
    assert (cfg.get("app") or {}).get("mode") == "practice"
