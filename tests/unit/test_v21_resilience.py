"""v21 稳定性修复（2026-11-21 第 19 题正式赛审计后续）：四条灾难路径关闭锁定。

- 协作式 claim 超时：SIGALRM 只在主线程生效（watchdog.DeadlineGuard.supported），
  parallel 子线程里静默降级后卡死线程永远等不到 30 分钟硬杀——worker 循环每轮
  开头自检墙钟补上这层边界（19 题赛 35 分钟空转的机制链之一）；
- worker 线程止损隔离：并行模式单线程连续 step 异常只退出自己，不杀全场；
- python_exec 并发 1→3：parallel=3 争抢时未抢到的立即拿 TOOL_BUSY，模型每轮
  重试烧一整个工具轮次（b49c4e1fd53f partial：提取脚本从未成功执行）；
- blocked 题复活：planner 对 blocked 不扩展、intents 又未打满，endgame 与
  planner 互相以为对方会接手 → 永久搁置死锁——纳入 endgame 授权一次重试。
"""
import threading
import time
from types import SimpleNamespace

import pytest

from tests.framework_support import fixture
from ctf_ext.gateway.catalog import default_specs
from ctf_ext.llm.provider import ProviderResponse
from ctf_ext.runtime.llm_worker import LlmWorker
from ctf_ext.runtime.metrics import Metrics
from ctf_ext.runtime.orchestrator import Orchestrator
from ctf_ext.runtime.planner import IntentPlanner
from ctf_ext.runtime.control import SessionControl
from ctf_ext.store.blackboard import IntentDraft
from tests.unit.test_llm_worker import _payload, _resp, FakeProvider, _setup


def test_collaborative_claim_deadline_fires_between_rounds():
    """SIGALRM 失效的子线程场景：循环轮间断点自检墙钟，超线返回可重试超时。

    第一轮 contract 违规触发修复 continue，第二轮开头 elapsed 超线——
    断言恰一次模型调用（未把第二次 complete 跑完就被打断）。
    """
    board, ev, claim, sha = _setup()
    bad = _payload(sha)
    bad["attempt"] = dict(bad["attempt"], strategy_key="")  # 契约违规 → 修复重试
    provider = FakeProvider([_resp(bad, 10), _resp(_payload(sha), 10)])
    tick = [0.0]

    def clock():
        tick[0] += 0.06
        return tick[0]

    worker = LlmWorker(provider, evidence_check=ev.verify, clock=clock,
                       claim_timeout_seconds=0.1)
    result = worker.work(claim, {"challenge_id": "c1"})
    assert result.status == "failed" and result.outcome == "timeout"
    assert result.failure_reason == "claim_deadline"
    assert len(provider.calls) == 1  # 第二轮开头打断，第二次 complete 未发生


def test_collaborative_deadline_disabled_by_default():
    """claim_timeout_seconds=0（默认）时循环行为与旧版一致，不受影响。"""
    board, ev, claim, sha = _setup()
    provider = FakeProvider([_resp(_payload(sha))])
    result = LlmWorker(provider, evidence_check=ev.verify).work(claim, {})
    assert result.status == "completed" and result.outcome == "success"


def _stoploss_orch(parallel):
    """止损测试桩：step 连续抛异常，事件与停止标志可观测。"""
    orch = Orchestrator.__new__(Orchestrator)
    events = []
    orch._board = SimpleNamespace(append_event=lambda *a: events.append(a))
    orch._metrics = SimpleNamespace(incr=lambda *a: None)
    orch._worker_name = "w"
    orch._parallel_agents = parallel
    orch._max_consecutive_errors = 3
    orch._clock = time.monotonic
    orch._heartbeat = None
    orch._stop = threading.Event()
    orch._solutions_store = None

    def _boom():
        raise RuntimeError("arena flash")

    orch.step = _boom
    return orch, events


def test_worker_stoploss_isolated_in_parallel():
    """并行模式：单线程 3 连异常只退出自己，不 _stop.set()（19 题赛 A2 缺陷）。"""
    orch, events = _stoploss_orch(parallel=2)
    t = threading.Thread(target=orch._worker_loop,
                         args=(time.monotonic() + 5, [object()], [], set(), 0))
    t.start()
    t.join(timeout=5)
    assert not t.is_alive()  # 该线程确实止损退出
    assert not orch._stop.is_set()  # 其他线程不被殃及
    aborted = [e for e in events if e[0] == "run_aborted"]
    assert aborted and aborted[0][2].get("isolated") is True


def test_worker_stoploss_serial_still_stops_all():
    """串行模式保持既有语义：连续异常仍然全局停止（行为不回归）。"""
    orch, events = _stoploss_orch(parallel=1)
    t = threading.Thread(target=orch._worker_loop,
                         args=(time.monotonic() + 5, [object()], [], set(), 0))
    t.start()
    t.join(timeout=5)
    assert not t.is_alive()
    assert orch._stop.is_set()  # 串行全场停止语义保留
    aborted = [e for e in events if e[0] == "run_aborted"]
    assert aborted and aborted[0][2].get("isolated") is False


def test_python_exec_concurrency_raised_to_parallel_width():
    """python_exec concurrency_limit 1→3：parallel=3 时每 worker 各占一槽。"""
    spec = next(s for s in default_specs() if s.name == "python_exec")
    assert spec.concurrency_limit == 3
    others = [s for s in default_specs() if s.name != "python_exec"]
    assert all(s.concurrency_limit == 1 for s in others)  # 其余工具默认不变


def test_endgame_revives_blocked_challenge():
    """blocked 题（终态 FAILED intent）纳入 endgame 授权：打破搁置死锁。

    planner 对 blocked 不扩展 + intents 未打满 → 旧判定 continue 跳过；
    v21 后存在 FAILED intent 的 ready 会话题也被授权一次 pass2 重试。
    """
    with fixture() as store:
        SessionControl(store, "c1")  # ready 会话（非 stopped）
        store.board.add_intent(IntentDraft("c1", "misc.blocked.test"))
        # blocked result 落库后 intent 终态是 FAILED（blackboard._write 映射）
        store.board._db.execute(
            "UPDATE intents_ext SET status='FAILED' WHERE challenge_id='c1'")
        store.board._db.commit()
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


def test_endgame_still_skips_healthy_untouched_challenge():
    """无 FAILED intent 且规划未打满的题：planner 正常接手，endgame 不插手。"""
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
        factory = lambda cid: None  # noqa: E731
        orch._worker = SimpleNamespace(_control_factory=factory)
        assert orch._grant_endgame([SimpleNamespace(challenge_id="c1")], set(),
                                   time.time() + 600) is False


# ---- v22：传输瞬态重试 + 无工具收尾轮纠正（2026-11-22 实测日志暴露）----


def _tool_call_response():
    from ctf_ext.llm.provider import ProviderResponse
    return ProviderResponse(text="", tool_calls=[
        {"name": "python_exec", "input": {"code": "print(1)"}, "id": "t1"}])


_TOOL_STUB = SimpleNamespace(
    tool_definitions=lambda: [{"name": "python_exec"}],
    execute_arguments=lambda *a, **k: SimpleNamespace(status="ok"))


def test_transport_failure_retried_once_in_claim():
    """传输类瞬态（网络抖动）不再直接报废 attempt：in-claim 重试一次。"""
    from ctf_ext.llm.provider import ProviderError
    board, ev, claim, sha = _setup()
    provider = FakeProvider([
        ProviderError("official deepseek api transport failed"),
        _resp(_payload(sha)),
    ])
    result = LlmWorker(provider, evidence_check=ev.verify).work(claim, {})
    assert result.status == "completed" and result.outcome == "success"
    assert len(provider.calls) == 2


def test_transport_failure_retried_only_once():
    """第二次传输失败仍走失败路径（防 in-claim 循环绕过熔断计数）。"""
    from ctf_ext.llm.provider import ProviderError
    board, ev, claim, sha = _setup()
    provider = FakeProvider([
        ProviderError("official deepseek api transport failed"),
        ProviderError("official deepseek api transport failed"),
    ])
    result = LlmWorker(provider, evidence_check=ev.verify).work(claim, {})
    assert result.status == "failed" and result.failure_reason == "ProviderError"
    assert len(provider.calls) == 2


def test_final_summary_tool_calls_corrected_once():
    """轮次用尽后的无工具收尾轮模型仍调工具：注入纠正重试一轮而非报废。

    max_tool_rounds=0 让第一次 tool_calls 直接进强制收尾路径；第二次仍
    tool_calls 触发 v22 纠正；第三次输出 JSON 走正常契约。
    """
    board, ev, claim, sha = _setup()
    provider = FakeProvider([
        _tool_call_response(), _tool_call_response(), _resp(_payload(sha))])
    result = LlmWorker(provider, evidence_check=ev.verify,
                       max_tool_rounds=0, tool_registry=_TOOL_STUB).work(claim, {})
    assert result.status == "completed" and result.outcome == "success"
    assert len(provider.calls) == 3
    last_user = [m for m in provider.calls[2]["messages"] if m.get("role") == "user"][-1]
    assert "没有可用工具" in last_user["content"]  # 纠正提示已注入


def test_final_summary_tool_calls_second_time_fails():
    """纠正后仍坚持工具调用：保留 tool_round_limit 失败码。"""
    board, ev, claim, sha = _setup()
    provider = FakeProvider([
        _tool_call_response(), _tool_call_response(), _tool_call_response()])
    result = LlmWorker(provider, evidence_check=ev.verify,
                       max_tool_rounds=0, tool_registry=_TOOL_STUB).work(claim, {})
    assert result.status == "failed" and result.failure_reason == "tool_round_limit"
    assert len(provider.calls) == 3
