"""结论守门测试：远程题的"靶机未启动/不可达"结论必须有 arena_http 证据。

2026-11-21 正式赛教训：static_file_server 40 轮只用 python_exec，网络探测
失败后模型自行得出"靶机未启动"的错误结论并 blocked。两层守门：
1. 主动提示（REMOTE_NUDGE_AT）：远程题前 N 轮 0 次 arena_http → 注入提示；
2. 不可达结论守卫：0 次 arena_http 却声称不可达 → 拒收重写一次，二次放行。
"""
from __future__ import annotations

import copy
import json

from tests.framework_support import fixture

from ctf_ext.llm.provider import ProviderResponse
from ctf_ext.runtime.control import SessionControl
from ctf_ext.runtime.llm_worker import (
    LlmWorker, REMOTE_NUDGE, REMOTE_NUDGE_AT, UNREACHABLE_GUARD,
    ROUND_BUDGET_NUDGE, ROUND_BUDGET_NUDGE_LEFT,
)
from ctf_ext.store.blackboard import IntentDraft
from ctf_ext.gateway.policy import PolicyConfig, PolicyEngine
from ctf_ext.gateway.tools import ToolRegistry, ToolSpec

REMOTE_CHALLENGE = {
    "challenge_id": "c1", "name": "static file server", "category": "web",
    "description": "find the flag",
    "metadata": {"arena": {"start_info": {"started": True,
                           "container_addr": ["svc-host:3000"]},
                           "help": "workflow: start/solve/submit"}},
}
OFFLINE_CHALLENGE = {"challenge_id": "c1", "name": "hash me", "category": "misc",
                     "description": "decode me", "metadata": {}}


class FakeProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, **kwargs):
        # 快照（深拷贝）：worker 对 messages 原地 append，存引用会让所有
        # 历史调用看到同一份持续增长的列表，"注入前不存在"类断言失真。
        self.calls.append({"messages": copy.deepcopy(messages), "kwargs": kwargs})
        return self.responses.pop(0)


def _unreachable_payload():
    return {
        "task_id": "ignored", "status": "completed",
        "summary": ["靶机未启动，无法访问题目服务，本会话无凭据取得 flag"],
        "facts": [],
        "attempt": {
            "strategy_key": "web.triage.auto", "preconditions": [],
            "actions": ["python 网络探测"], "outcome": "error",
            "failure_reason": "connection refused", "ruled_out": [],
            "next_best_action": "无",
        },
        "candidate_answer": None,
        "resource_usage": {"wall_seconds": 0, "model_tokens": 0, "tool_calls": 0},
    }


def _registry(store):
    policy = PolicyEngine(PolicyConfig(command_allowlist=("echo",), sandbox_required=False))
    registry = ToolRegistry(policy, evidence_store=store.evidence, work_root=str(store.root),
                            state_store=store)
    for name in ("strings", "arena_http"):
        registry.register(ToolSpec(
            name=name, version="test-1", description="stub {}".format(name),
            risk_level="read", timeout_seconds=5, max_output_bytes=1000,
            evidence_policy="raw", command="echo",
            argv_builder=lambda arguments, root: ["echo", name],
        ))
    return registry


def _worker(store, provider):
    return LlmWorker(
        provider, state_store=store,
        control_factory=lambda cid: SessionControl(store, cid),
        evidence_check=store.evidence.verify,
        tool_registry=_registry(store),
        tool_cwd=str(store.workspace("c1")),
    )


def _tool_response(i):
    # 每轮 input 不同：同一签名重复 3 次会被 SessionControl 判 LoopDetected。
    return ProviderResponse(text="", tool_calls=[
        {"id": "call-{}".format(i), "name": "strings", "input": {"q": i}}],
        usage={"total_tokens": 10}, raw={})


def test_remote_gate_requires_real_container_addr():
    """第三场实测（2026-11-22）：全量题都带运行环境 note 与共享 help，
    旧判据恒真 → 离线题（FlareCalc，start_info 非空但 container_addr=[]）
    也吃 arena_http nudge，误导模型找不存在的靶机。改为只认真实地址。"""
    offline = {"metadata": {"arena": {"start_info": {
        "container_status": "available", "container_addr": []}}}}
    remote = {"metadata": {"arena": {"start_info": {
        "container_addr": ["docker-x.arena-tr-attempt.svc:1337"]}}}}
    assert not LlmWorker._is_remote_challenge(offline)
    assert not LlmWorker._is_remote_challenge({"metadata": {}})
    assert LlmWorker._is_remote_challenge(remote)


def test_remote_nudge_injected_after_silent_rounds():
    """远程题 5 轮工具调用 0 次 arena_http：第 6 轮请求必须带上主动提示。"""
    with fixture() as store:
        store.board.add_intent(IntentDraft(challenge_id="c1", strategy_key="misc.triage.auto", params={}))
        claim = store.board.claim_intent("w1", lease_seconds=60)
        responses = [_tool_response(i) for i in range(REMOTE_NUDGE_AT)] + [
            ProviderResponse(text=json.dumps({
                "task_id": "t", "status": "completed",
                "summary": ["strings 扫描未发现 flag，需要进一步访问靶机"],
                "facts": [], "attempt": {"strategy_key": "web.triage.auto",
                    "preconditions": [], "actions": ["strings"], "outcome": "error",
                    "failure_reason": "", "ruled_out": [], "next_best_action": "用 arena_http"},
                "candidate_answer": None,
                "resource_usage": {"wall_seconds": 0, "model_tokens": 0, "tool_calls": 0},
            }, ensure_ascii=False), usage={"total_tokens": 10}, raw={}),
        ]
        provider = FakeProvider(responses)
        result = _worker(store, provider).work(claim, REMOTE_CHALLENGE)
        assert result.status == "completed"
        assert len(provider.calls) == REMOTE_NUDGE_AT + 1
        sixth = [str(m.get("content")) for m in provider.calls[5]["messages"]]
        assert any(REMOTE_NUDGE[:30] in text for text in sixth)


def test_round_budget_nudge_fires_once_near_limit():
    """轮次预算中途提示：剩 15 轮时注入一次敦促收束（第三场 8/8 intent
    小步打满 40 轮被强制无工具收尾）。用 max_tool_rounds=20 缩短触发线
    到第 5 轮，规模与现有用例一致；离线题不触发 remote nudge，互不干扰。"""
    with fixture() as store:
        store.board.add_intent(IntentDraft(challenge_id="c1", strategy_key="misc.triage.auto", params={}))
        claim = store.board.claim_intent("w1", lease_seconds=60)
        responses = [_tool_response(i) for i in range(5)] + [
            ProviderResponse(text=json.dumps({
                "task_id": "t", "status": "completed",
                "summary": ["按提示收束：整理既有结论并如实标注未完成部分"],
                "facts": [],
                "attempt": {"strategy_key": "misc.triage.auto", "preconditions": [],
                            "actions": ["strings"], "outcome": "error",
                            "failure_reason": "时间不足，未完成", "ruled_out": [],
                            "next_best_action": "无"},
                "candidate_answer": None,
                "resource_usage": {"wall_seconds": 0, "model_tokens": 0, "tool_calls": 0},
            }), usage={"total_tokens": 10}, raw={}),
        ]
        provider = FakeProvider(responses)
        worker = LlmWorker(provider, state_store=store,
                           control_factory=lambda cid: SessionControl(store, cid),
                           evidence_check=store.evidence.verify,
                           tool_registry=_registry(store),
                           tool_cwd=str(store.workspace("c1")),
                           max_tool_rounds=20)
        result = worker.work(claim, OFFLINE_CHALLENGE)
        assert result.status == "completed"
        # 注入文本经 .format(left=…) 渲染，断言用不含占位符的稳定子串。
        probe = ROUND_BUDGET_NUDGE.format(left=ROUND_BUDGET_NUDGE_LEFT)
        def _has_nudge(call):
            return any(probe[:26] in json.dumps(m.get("content"), ensure_ascii=False)
                       for m in call["messages"])
        assert _has_nudge(provider.calls[5])  # 第 6 次请求带上提示
        # 提示留在消息历史里：此后每次请求都应携带，注入前不存在。
        assert all(_has_nudge(call) for call in provider.calls[5:])
        assert not any(_has_nudge(call) for call in provider.calls[:5])


def test_unreachable_claim_without_arena_http_is_refused_once():
    """0 次 arena_http 却声称靶机不可达：拒收一次；二次仍坚持则放行（限 1 次）。"""
    with fixture() as store:
        store.board.add_intent(IntentDraft(challenge_id="c1", strategy_key="misc.triage.auto", params={}))
        claim = store.board.claim_intent("w1", lease_seconds=60)
        bad = _unreachable_payload()
        provider = FakeProvider([
            ProviderResponse(text=json.dumps(bad, ensure_ascii=False), usage={"total_tokens": 10}, raw={}),
            ProviderResponse(text=json.dumps(bad, ensure_ascii=False), usage={"total_tokens": 10}, raw={}),
        ])
        result = _worker(store, provider).work(claim, REMOTE_CHALLENGE)
        assert result.status == "completed"  # 二次放行，不无限阻塞
        assert len(provider.calls) == 2
        # 拒收重发那次请求的末条必须是守卫文本；首次请求末条是任务上下文。
        assert provider.calls[1]["messages"][-1]["content"] == UNREACHABLE_GUARD
        assert provider.calls[0]["messages"][-1]["content"] != UNREACHABLE_GUARD


def test_unreachable_claim_with_arena_http_evidence_passes():
    """调用过 arena_http 后声称不可达：有证据，不拦。"""
    with fixture() as store:
        store.board.add_intent(IntentDraft(challenge_id="c1", strategy_key="misc.triage.auto", params={}))
        claim = store.board.claim_intent("w1", lease_seconds=60)
        provider = FakeProvider([
            ProviderResponse(text="", tool_calls=[
                {"id": "call-1", "name": "arena_http", "input": {"host": "10.201.0.201", "port": 3000}}],
                usage={"total_tokens": 10}, raw={}),
            ProviderResponse(text=json.dumps(_unreachable_payload(), ensure_ascii=False),
                             usage={"total_tokens": 10}, raw={}),
        ])
        result = _worker(store, provider).work(claim, REMOTE_CHALLENGE)
        assert result.status == "completed"
        assert len(provider.calls) == 2  # 未被拒收重写
        assert provider.calls[1]["messages"][-1]["content"] != UNREACHABLE_GUARD


def test_offline_challenge_never_triggers_guard():
    """离线题声称"解码失败/不可达"：无靶机语境，守门不触发。"""
    with fixture() as store:
        store.board.add_intent(IntentDraft(challenge_id="c1", strategy_key="misc.triage.auto", params={}))
        claim = store.board.claim_intent("w1", lease_seconds=60)
        provider = FakeProvider([
            ProviderResponse(text=json.dumps(_unreachable_payload(), ensure_ascii=False),
                             usage={"total_tokens": 10}, raw={}),
        ])
        result = _worker(store, provider).work(claim, OFFLINE_CHALLENGE)
        assert result.status == "completed"
        assert len(provider.calls) == 1  # 一次通过，未拒收
