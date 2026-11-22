"""ProviderError 熔断与空响应诊断回归（2026-11-21）。

正式赛实测：empty model response 在同一 intent 上循环 claim 20+ 次，
每次 round 0 正常、round 1-2 撞空，烧穿 endgame 预算才被预算墙拦住。
本文件锁定：连续 Provider/配置类失败第 3 次即熔断为 blocked；
空响应错误信息附带 finish_reason/usage 诊断（不含响应内容）。
"""
import pytest

from ctf_ext.llm.openai_compat import ALLOWED_MODELS, _parse_response
from ctf_ext.llm.provider import ProviderError
from ctf_ext.runtime.llm_worker import _last_message_head
from ctf_ext.runtime.orchestrator import Orchestrator
from ctf_ext.store.blackboard import WorkerResult


def _failed_result(reason="ProviderError", outcome="error", status="failed"):
    return WorkerResult(
        intent_id="intent-x", owner="worker-1", attempt_no=0,
        challenge_id="ch-1", strategy_key="misc.triage.auto",
        status=status, outcome=outcome, failure_reason=reason)


def _breaker(attempt_no, reason="ProviderError", outcome="error", status="failed"):
    """Orchestrator 未初始化状态下只依赖类常量，可直接调用判断方法。"""
    orch = Orchestrator.__new__(Orchestrator)
    claim = type("ClaimStub", (), {"attempt_no": attempt_no})()
    return orch._should_break_provider_errors(
        claim, _failed_result(reason=reason, outcome=outcome, status=status))


def test_third_provider_error_blocks():
    assert _breaker(attempt_no=3) is True


def test_first_two_provider_errors_stay_retryable():
    assert _breaker(attempt_no=1) is False
    assert _breaker(attempt_no=2) is False


def test_breaker_ignores_transient_and_non_provider_failures():
    # timeout/rate_limited 等瞬态失败保持原重试语义，不熔断。
    assert _breaker(attempt_no=3, reason="timeout", outcome="timeout") is False
    assert _breaker(attempt_no=3, reason="rate_limited") is False
    # non_signal/其他 reason 不属于 Provider 类失败。
    assert _breaker(attempt_no=3, reason="failed_contract") is False
    assert _breaker(attempt_no=3, outcome="no_signal") is False


def _contract_breaker(attempt_no, reason="failed_contract: candidate_answer must look like flag{...}",
                      outcome="error", status="failed"):
    """契约失败熔断：同一 intent 第 2 次领取仍 failed_contract 即 blocked。"""
    orch = Orchestrator.__new__(Orchestrator)
    claim = type("ClaimStub", (), {"attempt_no": attempt_no})()
    return orch._should_break_contract_failures(
        claim, _failed_result(reason=reason, outcome=outcome, status=status))


def test_second_contract_failure_blocks():
    """2026-11-22 模拟赛日志：followup.2 因契约失败被整轮重跑 3 次
    （40+17+13 轮全废）；compact 修复已给过一次改写机会，第 2 次领取
    再失败即 blocked，blocked 后仍可走 endgame 冷却再授权。"""
    assert _contract_breaker(attempt_no=2) is True


def test_first_contract_failure_stays_retryable():
    assert _contract_breaker(attempt_no=1) is False


def test_contract_breaker_ignores_other_failures():
    assert _contract_breaker(attempt_no=3, reason="ProviderError") is False
    assert _contract_breaker(attempt_no=3, reason="budget_exceeded") is False
    assert _contract_breaker(attempt_no=3, outcome="no_signal", status="completed") is False


def test_parse_response_empty_includes_finish_reason_and_usage():
    data = {"model": ALLOWED_MODELS[0],
            "choices": [{"message": {"role": "assistant", "content": ""},
                         "finish_reason": "length"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4096}}
    with pytest.raises(ProviderError) as excinfo:
        _parse_response(data, declared=set())
    message = str(excinfo.value)
    assert "finish_reason=length" in message
    assert "completion_tokens=4096" in message
    # 诊断信息不包含响应正文（空响应本无正文，但锁定该约定）。
    assert "content" not in message.replace("completion_tokens", "")


def test_last_message_head_summarizes_last_message():
    messages = [{"role": "user", "content": "题目"}]
    messages.append({"role": "tool", "content": [
        {"type": "tool_result", "text": "A" * 500}]})
    head = _last_message_head(messages, limit=200)
    assert head.startswith("tool ")
    assert len(head) <= 210  # role + 截断后的摘要
    assert head.count("A") <= 200


def test_last_message_head_never_raises():
    assert _last_message_head([]) == "?"
    assert _last_message_head([{"role": "user"}]) == "user None"
