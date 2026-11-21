"""结论守门测试：远程题的"靶机未启动/不可达"结论必须有 arena_http 证据。

2026-11-21 正式赛教训：static_file_server 40 轮只用 python_exec，网络探测
失败后模型自行得出"靶机未启动"的错误结论并 blocked。两层守门：
1. 主动提示（REMOTE_NUDGE_AT）：远程题前 N 轮 0 次 arena_http → 注入提示；
2. 不可达结论守卫：0 次 arena_http 却声称不可达 → 拒收重写一次，二次放行。
"""
from __future__ import annotations

import json

from tests.framework_support import fixture

from ctf_ext.llm.provider import ProviderResponse
from ctf_ext.runtime.control import SessionControl
from ctf_ext.runtime.llm_worker import (
    LlmWorker, REMOTE_NUDGE, REMOTE_NUDGE_AT, UNREACHABLE_GUARD,
)
from ctf_ext.store.blackboard import IntentDraft
from ctf_ext.gateway.policy import PolicyConfig, PolicyEngine
from ctf_ext.gateway.tools import ToolRegistry, ToolSpec

REMOTE_CHALLENGE = {
    "challenge_id": "c1", "name": "static file server", "category": "web",
    "description": "find the flag",
    "metadata": {"arena": {"start_info": {"started": True},
                           "help": "workflow: start/solve/submit"}},
}
OFFLINE_CHALLENGE = {"challenge_id": "c1", "name": "hash me", "category": "misc",
                     "description": "decode me", "metadata": {}}


class FakeProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
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
        for call in provider.calls:
            assert call["messages"][-1]["content"] == UNREACHABLE_GUARD


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
