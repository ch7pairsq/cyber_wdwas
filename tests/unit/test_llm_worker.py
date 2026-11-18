"""LLM Worker：契约校验、一次修复重试、Provider 失败分类（§11、§14.1、B.2）。"""
import json
import tempfile

from _support import add_challenge, new_board

from ctf_ext.llm.provider import ProviderResponse, ProviderTimeout, RateLimited
from ctf_ext.runtime.llm_worker import LlmWorker
from ctf_ext.store.blackboard import IntentDraft
from ctf_ext.store.evidence import EvidenceStore

FENCE = chr(96) * 3


class FakeProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _setup():
    board, conn = new_board()
    add_challenge(board, "c1")
    ev = EvidenceStore(tempfile.mkdtemp(prefix="ev-"), db=conn)
    sha = ev.put_bytes(b"flag{demo}", kind="attachment")
    board.add_intent(IntentDraft(challenge_id="c1", strategy_key="misc.triage.auto", params={}))
    claim = board.claim_intent("w1", lease_seconds=60)
    return board, ev, claim, sha


def _payload(sha, **overrides):
    base = {
        "task_id": "ignored",
        "status": "completed",
        "summary": ["附件含 flag"],
        "facts": [{"statement": "附件解码得到 flag", "confidence": 0.9, "evidence_ids": [sha]}],
        "attempt": {
            "strategy_key": "misc.triage.auto", "preconditions": ["附件可读"],
            "actions": ["解码"], "outcome": "success", "failure_reason": "",
            "ruled_out": [], "next_best_action": "提交",
        },
        "candidate_answer": "flag{demo}",
        "resource_usage": {"wall_seconds": 0, "model_tokens": 0, "tool_calls": 0},
    }
    base.update(overrides)
    return base


def _resp(payload, tokens=120):
    return ProviderResponse(text=json.dumps(payload, ensure_ascii=False),
                            usage={"total_tokens": tokens})


def test_valid_response_becomes_worker_result():
    board, ev, claim, sha = _setup()
    provider = FakeProvider([_resp(_payload(sha))])
    result = LlmWorker(provider, evidence_check=ev.verify).work(claim, {"challenge_id": "c1"})
    assert result.status == "completed" and result.outcome == "success"
    assert result.candidate_answer == "flag{demo}"
    assert sha in result.evidence_ids
    assert result.resource_usage["model_tokens"] == 120
    assert len(provider.calls) == 1
    assert provider.calls[0]["kwargs"]["json_mode"] is True


def test_fenced_json_is_parsed():
    board, ev, claim, sha = _setup()
    text = FENCE + "json\n" + json.dumps(_payload(sha), ensure_ascii=False) + "\n" + FENCE
    provider = FakeProvider([ProviderResponse(text=text, usage={"total_tokens": 10})])
    result = LlmWorker(provider, evidence_check=ev.verify).work(claim, {})
    assert result.candidate_answer == "flag{demo}"


def test_contract_failure_retried_once_then_accepted():
    board, ev, claim, sha = _setup()
    bad = _payload(sha)
    bad["attempt"] = dict(bad["attempt"], strategy_key="")
    provider = FakeProvider([_resp(bad, 50), _resp(_payload(sha), 70)])
    result = LlmWorker(provider, evidence_check=ev.verify).work(claim, {})
    assert result.status == "completed" and result.outcome == "success"
    assert len(provider.calls) == 2
    repair = provider.calls[1]["messages"][-1]["content"]
    assert "strategy_key" in repair  # 修复提示必须包含具体错误
    assert result.resource_usage["model_tokens"] == 120


def test_persistent_contract_failure_marks_failed_contract():
    board, ev, claim, sha = _setup()
    bad = _payload(sha)
    bad["attempt"] = dict(bad["attempt"], strategy_key="")
    provider = FakeProvider([_resp(bad, 10), _resp(bad, 10)])
    result = LlmWorker(provider, evidence_check=ev.verify).work(claim, {})
    assert result.status == "failed" and result.outcome == "error"
    assert "failed_contract" in result.failure_reason
    assert len(provider.calls) == 2


def test_unregistered_evidence_rejected():
    board, ev, claim, sha = _setup()
    bad = _payload(sha, facts=[{"statement": "x", "confidence": 0.9, "evidence_ids": ["f" * 64]}])
    provider = FakeProvider([_resp(bad, 10), _resp(bad, 10)])
    result = LlmWorker(provider, evidence_check=ev.verify).work(claim, {})
    assert result.status == "failed"
    assert "unregistered" in result.failure_reason


def test_timeout_and_rate_limit_are_reported_not_retried():
    board, ev, claim, sha = _setup()
    provider = FakeProvider([ProviderTimeout("slow")])
    result = LlmWorker(provider, evidence_check=ev.verify).work(claim, {})
    assert result.status == "failed" and result.outcome == "timeout"
    assert len(provider.calls) == 1

    provider = FakeProvider([RateLimited("429")])
    result = LlmWorker(provider, evidence_check=ev.verify).work(claim, {})
    assert result.status == "failed" and result.failure_reason == "rate_limited"


def test_prompt_file_is_used_as_system_prompt():
    board, ev, claim, sha = _setup()
    path = tempfile.mktemp(suffix=".md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("自定义系统提示")
    provider = FakeProvider([_resp(_payload(sha))])
    worker = LlmWorker(provider, prompt_path=path, evidence_check=ev.verify)
    worker.work(claim, {})
    assert provider.calls[0]["kwargs"]["system"] == "自定义系统提示"
