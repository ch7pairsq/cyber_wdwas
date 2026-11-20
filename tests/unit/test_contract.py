"""Worker 输出契约（附录 B.2）：不合格输出必须拒收。"""
import tempfile

from _support import new_board

from ctf_ext.harness.contract import ContractViolation, extract_flag, validate_worker_output
from ctf_ext.store.evidence import EvidenceStore


def _payload(**overrides):
    base = {
        "task_id": "intent-1",
        "status": "completed",
        "summary": ["附件解码后得到 flag"],
        "facts": [{"statement": "附件是 base64", "confidence": 0.9,
                   "evidence_ids": ["ev-1"]}],
        "attempt": {"strategy_key": "misc.triage.auto", "preconditions": ["附件可读"],
                    "actions": ["读取", "解码"], "outcome": "success",
                    "failure_reason": "", "ruled_out": ["无其他编码"],
                    "next_best_action": "提交"},
        "candidate_answer": "flag{x}",
        "resource_usage": {"wall_seconds": 0, "model_tokens": 0, "tool_calls": 1},
    }
    base.update(overrides)
    return base


def test_valid_payload_passes():
    validate_worker_output(_payload())
    validate_worker_output(_payload(candidate_answer=None))


def test_strategy_key_required_and_stable():
    bad = _payload()
    bad["attempt"] = dict(bad["attempt"], strategy_key="")
    try:
        validate_worker_output(bad)
        raise AssertionError("expected ContractViolation")
    except ContractViolation as exc:
        assert any("strategy_key" in e for e in exc.errors)


def test_candidate_answer_requires_evidence():
    bad = _payload(facts=[])
    try:
        validate_worker_output(bad, evidence_check=lambda sha: True)
        raise AssertionError("expected ContractViolation")
    except ContractViolation as exc:
        assert any("evidence" in e for e in exc.errors)


def test_fact_evidence_must_be_registered():
    board, conn = new_board()
    ev = EvidenceStore(tempfile.mkdtemp(), db=conn)
    try:
        validate_worker_output(_payload(), evidence_check=ev.verify)
        raise AssertionError("expected ContractViolation for unregistered evidence")
    except ContractViolation as exc:
        assert any("unregistered" in e for e in exc.errors)


def test_flag_extraction():
    assert extract_flag("答案：flag{mock_001}") == "flag{mock_001}"
    assert extract_flag("没有答案") is None


def test_candidate_answer_must_match_platform_flag_format():
    bad = _payload(candidate_answer="the password is hunter2")
    try:
        validate_worker_output(bad)
        raise AssertionError("expected ContractViolation")
    except ContractViolation as exc:
        assert any("flag{" in e for e in exc.errors)
    validate_worker_output(_payload(candidate_answer="flag{ok_12345}"))


def test_strategy_key_allows_hyphen():
    # 连字符合法：worker 产出的 key 含 hyphen（如
    # web.flaglang.signed-cookie-deny-bypass）曾被误拒为整轮 failed_contract。
    validate_worker_output(_payload(attempt=dict(
        _payload()["attempt"], strategy_key="web.flaglang.signed-cookie-deny-bypass")))
    validate_worker_output(_payload(attempt=dict(
        _payload()["attempt"], strategy_key="misc.waf-bypass.header-smuggling.pass2")))
    # 大写/空段仍拒绝。
    bad = _payload()
    bad["attempt"] = dict(bad["attempt"], strategy_key="Misc.Triage.AUTO")
    try:
        validate_worker_output(bad)
        raise AssertionError("expected ContractViolation")
    except ContractViolation as exc:
        assert any("strategy_key" in e for e in exc.errors)
