"""双模型换挡（借鉴 Tsecbench 冠军方案）：Flash 高频探索，Pro 关键决策。

覆盖：stage>=pro_from_stage 才路由 Pro、每题次数上限、Pro 故障 fail-open
回退主模型、未配置 Pro 时行为与单模型完全一致。
"""
from __future__ import annotations

import hashlib
import json

import pytest

from tests.framework_support import FLAG, fixture
from tests.unit.test_llm_tool_loop import FakeProvider, _payload
from ctf_ext.runtime.control import SessionControl
from ctf_ext.runtime.llm_worker import LlmWorker
import ctf_ext.llm.provider as provider_mod

ProviderResponse = provider_mod.ProviderResponse
ProviderError = provider_mod.ProviderError


def _evidence_id(store) -> str:
    return store.evidence.put_bytes((FLAG + "\n").encode(), kind="tool_output")


def _worker(store, control, fast, pro=None, pro_max_calls=8, pro_from_stage=2):
    return LlmWorker(
        fast, state_store=store, control_factory=lambda cid: control,
        evidence_check=store.evidence.verify,
        pro_provider=pro, pro_max_calls=pro_max_calls, pro_from_stage=pro_from_stage)


def _drive_to_stage(store, control, stage_steps):
    """把步数推过指定 stage 阈值（变参防重复签名，stall 调大防误停）。"""
    for i in range(1, stage_steps + 1):
        control.reserve("tool", "v1", [str(i)], str(i))


def test_stage_below_threshold_uses_fast_only():
    with fixture() as store:
        control = SessionControl(store, "c1", max_steps=200, max_tokens=1_000_000,
                                 stall_steps=1000, repeat_limit=1000)
        eid = _evidence_id(store)
        fast = FakeProvider([ProviderResponse(
            text=json.dumps(_payload(eid), ensure_ascii=False),
            usage={"total_tokens": 10}, raw={})])
        pro = FakeProvider([])
        w = _worker(store, control, fast, pro)
        result = w.work(type("C", (), {"intent_id": "i1", "challenge_id": "c1",
                                       "attempt_no": 1, "strategy_key": "misc.triage.auto",
                                       "params": {}, "budget_seconds": 60})(),
                        {"challenge_id": "c1"})
        assert result.candidate_answer == FLAG
        assert len(fast.calls) == 1 and pro.calls == []  # stage 0 只用主模型


def test_stage_ge_threshold_routes_to_pro():
    with fixture() as store:
        control = SessionControl(store, "c1", max_steps=200, max_tokens=1_000_000,
                                 stall_steps=1000, repeat_limit=1000)
        _drive_to_stage(store, control, 55)  # stage >= 2（alternate）
        eid = _evidence_id(store)
        fast = FakeProvider([])
        pro = FakeProvider([ProviderResponse(
            text=json.dumps(_payload(eid), ensure_ascii=False),
            usage={"total_tokens": 30}, raw={})])
        w = _worker(store, control, fast, pro)
        result = w.work(type("C", (), {"intent_id": "i1", "challenge_id": "c1",
                                       "attempt_no": 1, "strategy_key": "misc.triage.auto",
                                       "params": {}, "budget_seconds": 60})(),
                        {"challenge_id": "c1"})
        assert result.candidate_answer == FLAG
        assert len(pro.calls) == 1 and fast.calls == []  # 换路径回合用 Pro
        assert w._pro_calls["c1"] == 1  # Pro 次数按题记账


def test_pro_call_cap_falls_back_to_fast():
    with fixture() as store:
        control = SessionControl(store, "c1", max_steps=200, max_tokens=1_000_000,
                                 stall_steps=1000, repeat_limit=1000)
        _drive_to_stage(store, control, 55)
        w = _worker(store, control, FakeProvider([]), FakeProvider([]), pro_max_calls=1)
        w._pro_calls["c1"] = 1  # 已达上限
        assert w._select_provider(type("C", (), {"challenge_id": "c1"})()) is w._provider


def test_pro_failure_falls_open_to_fast():
    with fixture() as store:
        control = SessionControl(store, "c1", max_steps=200, max_tokens=1_000_000,
                                 stall_steps=1000, repeat_limit=1000)
        _drive_to_stage(store, control, 55)
        eid = _evidence_id(store)
        fast = FakeProvider([ProviderResponse(
            text=json.dumps(_payload(eid), ensure_ascii=False),
            usage={"total_tokens": 10}, raw={})])

        class BrokenPro:
            def complete(self, messages, **kwargs):
                raise ProviderError("pro gateway down")

        w = _worker(store, control, fast, BrokenPro())
        result = w.work(type("C", (), {"intent_id": "i1", "challenge_id": "c1",
                                       "attempt_no": 1, "strategy_key": "misc.triage.auto",
                                       "params": {}, "budget_seconds": 60})(),
                        {"challenge_id": "c1"})
        assert result.candidate_answer == FLAG  # Pro 挂了仍由主模型完成
        assert w._pro_down is True  # 故障标记粘滞，本题后续不再尝试 Pro


def test_no_pro_configured_behaves_as_single_model():
    with fixture() as store:
        control = SessionControl(store, "c1", max_steps=200, max_tokens=1_000_000,
                                 stall_steps=1000, repeat_limit=1000)
        _drive_to_stage(store, control, 55)
        eid = _evidence_id(store)
        fast = FakeProvider([ProviderResponse(
            text=json.dumps(_payload(eid), ensure_ascii=False),
            usage={"total_tokens": 10}, raw={})])
        w = _worker(store, control, fast, None)  # 未配置 Pro
        assert w._select_provider(type("C", (), {"challenge_id": "c1"})()) is w._provider


def test_thinking_by_stage_and_fail_open():
    with fixture() as store:
        control = SessionControl(store, "c1", max_steps=200, max_tokens=1_000_000,
                                 stall_steps=1000, repeat_limit=1000)
        eid = _evidence_id(store)
        fast = FakeProvider([ProviderResponse(
            text=json.dumps(_payload(eid), ensure_ascii=False),
            usage={"total_tokens": 10}, raw={})])
        w = _worker(store, control, fast, None)
        w._thinking_by_stage = (0, 4000, 8000, 0, 0)
        # stage 0：不开思考（侦察/简单任务防思维过载）
        w.work(type("C", (), {"intent_id": "i1", "challenge_id": "c1",
                              "attempt_no": 1, "strategy_key": "misc.triage.auto",
                              "params": {}, "budget_seconds": 60})(),
               {"challenge_id": "c1"})
        assert "thinking_budget" not in fast.calls[0]["kwargs"]
        # stage 2：换路径回合带思考预算
        _drive_to_stage(store, control, 55)
        fast2 = FakeProvider([ProviderResponse(
            text=json.dumps(_payload(eid), ensure_ascii=False),
            usage={"total_tokens": 10}, raw={})])
        w2 = _worker(store, control, fast2, None)
        w2._thinking_by_stage = (0, 4000, 8000, 0, 0)
        w2.work(type("C", (), {"intent_id": "i2", "challenge_id": "c1",
                               "attempt_no": 1, "strategy_key": "misc.triage.auto",
                               "params": {}, "budget_seconds": 60})(),
                {"challenge_id": "c1"})
        assert fast2.calls[0]["kwargs"]["thinking_budget"] == 8000


def test_thinking_rejected_falls_open_without_thinking():
    with fixture() as store:
        control = SessionControl(store, "c1", max_steps=200, max_tokens=1_000_000,
                                 stall_steps=1000, repeat_limit=1000)
        _drive_to_stage(store, control, 55)
        eid = _evidence_id(store)

        calls = []

        class NoThinkingGateway:
            def complete(self, messages, **kwargs):
                calls.append(kwargs)
                if kwargs.get("thinking_budget"):
                    raise ProviderError("gateway rejects thinking param")
                return ProviderResponse(
                    text=json.dumps(_payload(eid), ensure_ascii=False),
                    usage={"total_tokens": 10}, raw={})

        w = _worker(store, control, NoThinkingGateway(), None)
        w._thinking_by_stage = (0, 4000, 8000, 0, 0)
        result = w.work(type("C", (), {"intent_id": "i1", "challenge_id": "c1",
                                       "attempt_no": 1, "strategy_key": "misc.triage.auto",
                                       "params": {}, "budget_seconds": 60})(),
                        {"challenge_id": "c1"})
        assert result.candidate_answer == FLAG  # 思考被拒后仍无思考完成
        assert w._thinking_down is True
        assert calls[0].get("thinking_budget") == 8000
        assert "thinking_budget" not in calls[1]  # 第二次自动不带该参数
