"""LLM 工具循环：注册表 → 策略/预算 → command_log/证据 → FlagGate 不可绕过。"""
import hashlib
import json

from tests.framework_support import FLAG, fixture

from ctf_ext.llm.provider import ProviderResponse
from ctf_ext.runtime.control import SessionControl
from ctf_ext.runtime.llm_worker import LlmWorker
from ctf_ext.store.blackboard import IntentDraft
from ctf_ext.gateway.policy import PolicyConfig, PolicyEngine
from ctf_ext.gateway.tools import ToolRegistry, ToolSpec
from ctf_ext.gateway.catalog import default_specs
from ctf_ext.harness.flag_gate import FlagGate


class FakeProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return self.responses.pop(0)


def _payload(evidence_id):
    return {
        "task_id": "ignored",
        "status": "completed",
        "summary": ["工具输出包含 flag"],
        "facts": [{"statement": "echo 输出与声明一致", "confidence": 0.99,
                   "evidence_ids": [evidence_id]}],
        "attempt": {
            "strategy_key": "misc.triage.auto", "preconditions": ["工具可用"],
            "actions": ["调用 echo_flag"], "outcome": "success", "failure_reason": "",
            "ruled_out": [], "next_best_action": "提交",
        },
        "candidate_answer": FLAG,
        "resource_usage": {"wall_seconds": 0, "model_tokens": 0, "tool_calls": 0},
    }


def _registry(store):
    policy = PolicyEngine(PolicyConfig(command_allowlist=("echo",), sandbox_required=False))
    registry = ToolRegistry(policy, evidence_store=store.evidence, work_root=str(store.root),
                            state_store=store)
    registry.register(ToolSpec(
        name="echo_flag", version="test-1", description="echo fixture",
        risk_level="read", timeout_seconds=5, max_output_bytes=1000,
        evidence_policy="raw", command="echo",
        argv_builder=lambda arguments, root: ["echo", FLAG],
    ))
    return registry


def test_tool_output_is_logged_and_can_ground_flag():
    with fixture() as store:
        store.board.add_intent(IntentDraft(challenge_id="c1", strategy_key="misc.triage.auto", params={}))
        claim = store.board.claim_intent("w1", lease_seconds=60)
        registry = _registry(store)
        evidence_id = hashlib.sha256((FLAG + "\n").encode()).hexdigest()
        provider = FakeProvider([
            ProviderResponse(text="", tool_calls=[{"id": "call-1", "name": "echo_flag", "input": {}}],
                             usage={"total_tokens": 10}, raw={}),
            ProviderResponse(text=json.dumps(_payload(evidence_id), ensure_ascii=False),
                             usage={"total_tokens": 20}, raw={}),
        ])
        worker = LlmWorker(
            provider, state_store=store,
            control_factory=lambda cid: SessionControl(store, cid),
            evidence_check=store.evidence.verify,
            tool_registry=registry,
            tool_cwd=str(store.workspace("c1")),
        )
        result = worker.work(claim, {"challenge_id": "c1"})

        assert result.candidate_answer == FLAG
        # 候选必须先声明到工作区 FLAG，FlagGate 才有可比对声明（与 RuleBasedWorker 同纪律）。
        assert (store.workspace("c1") / "FLAG").read_text(encoding="utf-8") == FLAG
        assert evidence_id in result.evidence_ids
        assert result.resource_usage["tool_calls"] == 1
        assert len(provider.calls) == 2
        assert provider.calls[0]["kwargs"]["tools"][0]["name"] == "echo_flag"
        tool_result = provider.calls[1]["messages"][-1]["content"][0]
        assert tool_result["type"] == "tool_result"
        assert "evidence_id=" + evidence_id in tool_result["content"]

        store.declare("c1", FLAG)
        decision = FlagGate(store).evaluate("c1", FLAG, result.evidence_ids)
        assert decision.status == "confirmed"
        assert decision.level == "L1"
        assert decision.command_id


def test_tool_cannot_escape_work_root_or_change_command():
    with fixture() as store:
        registry = _registry(store)
        outside = registry.execute_arguments("echo_flag", {}, cwd="/tmp")
        assert outside.status == "denied" and outside.error_code == "PATH"

        policy = PolicyEngine(PolicyConfig(command_allowlist=("echo",), sandbox_required=False))
        mismatched = ToolRegistry(policy, evidence_store=store.evidence, work_root=str(store.root))
        mismatched.register(ToolSpec(
            name="echo_flag", version="test-1", command="echo",
            input_schema={"type": "object"},
            argv_builder=lambda arguments, root: ["ls"],
        ))
        blocked = mismatched.execute_arguments("echo_flag", {}, cwd=str(store.workspace("c1")))
        assert blocked.status == "denied" and blocked.error_code == "TOOL_COMMAND_MISMATCH"

        path_registry = ToolRegistry(policy, evidence_store=store.evidence, work_root=str(store.root))
        path_registry.register(next(spec for spec in default_specs() if spec.name == "file_info"))
        traversal = path_registry.execute_arguments("file_info", {"path": "../../etc/passwd"},
                                                    cwd=str(store.workspace("c1")))
        assert traversal.status == "denied" and traversal.error_code == "INVALID_ARGUMENTS"
