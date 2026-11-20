"""Worker 输出契约校验（附录 B.2）。

不合格输出不得入库：调用方可重写一次，仍失败记为 failed_contract（§14.1）。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

STATUSES = ("completed", "partial", "blocked", "failed")
OUTCOMES = ("success", "no_signal", "error", "timeout")
# 连字符合法：worker 产出的 strategy_key 可能含 hyphen（如
# web.flaglang.signed-cookie-deny-bypass），旧正则误拒导致整轮 failed_contract。
STRATEGY_RE = re.compile(r"^[a-z0-9_-]+(\.[a-z0-9_-]+){2,}$")
MAX_SUMMARY_ITEMS = 5
FLAG_RE = re.compile(r"flag\{[^}\s]{1,200}\}", re.IGNORECASE)


class ContractViolation(Exception):
    """契约校验失败：errors 为全部错误列表；code 固定 failed_contract。"""
    def __init__(self, errors: List[str]):
        """errors 为累积的全部契约错误（一次性上报，便于一次修复）。"""
        super().__init__("; ".join(errors))
        self.errors = list(errors)

    @property
    def code(self) -> str:
        """失败码：编排层按 failed_contract 记账并触发重写。"""
        return "failed_contract"


def _error(errors: List[str], cond: bool, message: str) -> None:
    """条件不成立时追加错误信息（累积全部错误后一次性抛出）。"""
    if not cond:
        errors.append(message)


def validate_worker_output(payload: Dict[str, Any], evidence_check=None) -> Dict[str, Any]:
    """校验并返回 payload；不合格抛 ContractViolation（含全部错误）。"""
    errors: List[str] = []
    _error(errors, isinstance(payload, dict), "payload must be an object")
    if not isinstance(payload, dict):
        raise ContractViolation(errors)
    _error(errors, bool(str(payload.get("task_id") or "").strip()), "task_id is required")
    _error(errors, payload.get("status") in STATUSES,
           "status must be one of {}".format(list(STATUSES)))

    summary = payload.get("summary")
    _error(errors, isinstance(summary, list) and 0 < len(summary) <= MAX_SUMMARY_ITEMS,
           "summary must be a list of 1..{} items".format(MAX_SUMMARY_ITEMS))
    if isinstance(summary, list):
        _error(errors, all(isinstance(s, str) and s.strip() for s in summary),
               "summary items must be non-empty strings")

    attempt = payload.get("attempt")
    _error(errors, isinstance(attempt, dict), "attempt object is required")
    attempt = attempt if isinstance(attempt, dict) else {}
    strategy_key = str(attempt.get("strategy_key") or "")
    _error(errors, bool(STRATEGY_RE.match(strategy_key)),
           "strategy_key must look like domain.technique.variant, got {!r}".format(strategy_key))
    _error(errors, attempt.get("outcome") in OUTCOMES,
           "attempt.outcome must be one of {}".format(list(OUTCOMES)))
    for field_name in ("preconditions", "actions", "ruled_out"):
        _error(errors, isinstance(attempt.get(field_name, []), list),
               "attempt.{} must be a list".format(field_name))

    facts = payload.get("facts") or []
    _error(errors, isinstance(facts, list), "facts must be a list")
    evidence_ids: List[str] = []
    if isinstance(facts, list):
        for idx, fact in enumerate(facts):
            if not isinstance(fact, dict):
                errors.append("facts[{}] must be an object".format(idx))
                continue
            _error(errors, bool(str(fact.get("statement") or "").strip()),
                   "facts[{}].statement is required".format(idx))
            confidence = fact.get("confidence")
            _error(errors, isinstance(confidence, (int, float)) and 0 <= confidence <= 1,
                   "facts[{}].confidence must be within [0,1]".format(idx))
            ids = fact.get("evidence_ids") or []
            _error(errors, isinstance(ids, list) and len(ids) > 0,
                   "facts[{}].evidence_ids must be non-empty".format(idx))
            if isinstance(ids, list):
                evidence_ids.extend(ids)

    usage = payload.get("resource_usage") or {}
    _error(errors, isinstance(usage, dict), "resource_usage must be an object")
    if isinstance(usage, dict):
        for key in ("wall_seconds", "model_tokens", "tool_calls"):
            _error(errors, isinstance(usage.get(key, 0), (int, float)),
                   "resource_usage.{} must be numeric".format(key))

    extra_ids = payload.get("evidence_ids") or []
    _error(errors, isinstance(extra_ids, list), "evidence_ids must be a list")
    if isinstance(extra_ids, list):
        evidence_ids.extend([e for e in extra_ids if isinstance(e, str)])

    answer = payload.get("candidate_answer")
    if answer is not None:
        _error(errors, isinstance(answer, str) and bool(answer.strip()),
               "candidate_answer must be a non-empty string or null")
        # 平台提交与证据匹配都只接受 flag{...}（见 flag_gate.normalized_flag）；
        # 在契约层拦截格式错误，可让它进入修复轮而不是到 Gate 才静默拒绝。
        _error(errors, isinstance(answer, str) and bool(FLAG_RE.fullmatch(answer.strip())),
               "candidate_answer must look like flag{...}")
        _error(errors, bool(evidence_ids),
               "candidate_answer requires at least one evidence_id")

    if evidence_check is not None and evidence_ids:
        missing = [e for e in evidence_ids if not evidence_check(e)]
        _error(errors, not missing, "unregistered evidence_ids: {}".format(missing))

    if errors:
        raise ContractViolation(errors)
    return payload


def extract_flag(text: str) -> Optional[str]:
    """从文本中提取第一个 flag{...}（无则返回 None）。"""
    match = FLAG_RE.search(text or "")
    return match.group(0) if match else None
