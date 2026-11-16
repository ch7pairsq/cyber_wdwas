"""LLM Worker：prompt → Provider → B.2 契约校验 → 一次修复重试（§11、§14.1）。

设计边界：
- 不自动降级为无证据回答；没有 evidence_id 的答案会被契约拒收；
- Provider 超时/限流/错误不重试（由上层按 budget 与退避决定），只返回可重试失败；
- 契约失败只重写一次，仍失败即 failed_contract，交回黑板记录。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from ..domain.models import OUTCOME_ERROR, OUTCOME_NO_SIGNAL, OUTCOME_SUCCESS, OUTCOME_TIMEOUT
from ..harness.contract import ContractViolation, validate_worker_output
from ..llm.provider import (
    ModelProvider, ProviderConfigError, ProviderError, ProviderTimeout, RateLimited,
)
from ..store.blackboard import Claim, WorkerResult

DEFAULT_SYSTEM = (
    "你是 CTF 解题 Worker。只输出一个 JSON 对象，遵循附录 B.2："
    "task_id/status/summary/facts/attempt/candidate_answer/resource_usage。"
    "没有证据的结论只能写进 summary 并标注假设；candidate_answer 必须带 evidence_ids。"
)
REPAIR_SUFFIX = "上一次输出不合格，原因如下：\n- {errors}\n只输出修正后的 JSON 对象，不要解释。"
FENCE = chr(96) * 3


def _parse_json(text: str) -> Optional[Dict[str, Any]]:
    body = (text or "").strip()
    if body.startswith(FENCE):
        body = body[len(FENCE):].lstrip()
        if body.lower().startswith("json"):
            body = body[4:]
        body = body.strip()
        if body.endswith(FENCE):
            body = body[:-len(FENCE)]
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(body[start:end + 1])
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


class LlmWorker:
    """实现 runtime.worker.Worker 协议的模型 Worker（不 import 抽象类，便于单测）。"""

    name = "llm"

    def __init__(self, provider: ModelProvider, prompt_path: Optional[str] = None,
                 evidence_check=None, timeout: float = 120.0, max_tokens: int = 4096,
                 max_attempts: int = 2, clock=time.time):
        self._provider = provider
        self._check = evidence_check
        self._timeout = timeout
        self._max_tokens = max_tokens
        self._max_attempts = max(1, max_attempts)
        self._clock = clock
        self._system = DEFAULT_SYSTEM
        if prompt_path:
            with open(prompt_path, "r", encoding="utf-8") as fh:
                self._system = fh.read().strip() or DEFAULT_SYSTEM

    # ------------------------------------------------------------------ public
    def work(self, claim: Claim, challenge: Dict[str, Any]) -> WorkerResult:
        started = self._clock()
        messages: List[Dict[str, str]] = [{"role": "user", "content": json.dumps({
            "intent_id": claim.intent_id,
            "challenge": {
                "id": claim.challenge_id,
                "name": challenge.get("name", ""),
                "category": challenge.get("category", ""),
                "description": challenge.get("description", ""),
                "attachments": list(challenge.get("attachments") or ()),
            },
            "strategy_key": claim.strategy_key,
            "params": claim.params,
            "budget_seconds": claim.budget_seconds,
        }, ensure_ascii=False)}]
        errors: List[str] = []
        tokens = 0
        for attempt in range(self._max_attempts):
            try:
                response = self._provider.complete(
                    messages, system=self._system, json_mode=True,
                    timeout=self._timeout, max_tokens=self._max_tokens,
                )
            except ProviderTimeout:
                return self._failure(claim, "blocked", OUTCOME_TIMEOUT, "timeout", started, tokens)
            except RateLimited:
                return self._failure(claim, "blocked", OUTCOME_ERROR, "rate_limited", started, tokens)
            except (ProviderError, ProviderConfigError) as exc:
                return self._failure(claim, "failed", OUTCOME_ERROR, type(exc).__name__, started, tokens)
            tokens += int((response.usage or {}).get("total_tokens", 0))
            payload = _parse_json(response.text)
            if payload is None:
                errors = ["response is not a JSON object"]
            else:
                payload.setdefault("task_id", claim.intent_id)
                try:
                    validate_worker_output(payload, evidence_check=self._check)
                except ContractViolation as exc:
                    errors = exc.errors
                else:
                    return self._result(claim, payload, started, tokens)
            if attempt + 1 < self._max_attempts:
                messages.append({"role": "assistant", "content": response.text})
                messages.append({"role": "user", "content": REPAIR_SUFFIX.format(
                    errors="\n- ".join(errors[:8]))})
        return self._failure(claim, "failed", OUTCOME_ERROR,
                             "failed_contract: " + "; ".join(errors[:5]), started, tokens)

    # ----------------------------------------------------------------- mapping
    def _result(self, claim: Claim, payload: Dict[str, Any], started: float, tokens: int) -> WorkerResult:
        attempt = payload.get("attempt") or {}
        facts = [f for f in (payload.get("facts") or []) if isinstance(f, dict)]
        evidence_ids: List[str] = []
        for fact in facts:
            evidence_ids.extend(fact.get("evidence_ids") or [])
        evidence_ids.extend(e for e in (payload.get("evidence_ids") or []) if isinstance(e, str))
        usage = dict(payload.get("resource_usage") or {})
        usage["wall_seconds"] = round(self._clock() - started, 3)
        usage["model_tokens"] = max(int(usage.get("model_tokens") or 0), tokens)
        answer = payload.get("candidate_answer")
        outcome = OUTCOME_SUCCESS if answer else (OUTCOME_NO_SIGNAL if payload.get("status") == "completed"
                                                  else OUTCOME_ERROR)
        return WorkerResult(
            intent_id=claim.intent_id, owner="", attempt_no=claim.attempt_no,
            challenge_id=claim.challenge_id, strategy_key=attempt.get("strategy_key") or claim.strategy_key,
            status=payload.get("status", "completed"), summary=list(payload.get("summary") or []),
            outcome=outcome, failure_reason=str(attempt.get("failure_reason") or ""),
            preconditions=list(attempt.get("preconditions") or []),
            actions=list(attempt.get("actions") or []),
            ruled_out=list(attempt.get("ruled_out") or []),
            next_best_action=str(attempt.get("next_best_action") or ""),
            evidence_ids=evidence_ids, candidate_answer=answer, facts=facts,
            resource_usage=usage,
        )

    def _failure(self, claim: Claim, status: str, outcome: str, reason: str,
                 started: float, tokens: int) -> WorkerResult:
        return WorkerResult(
            intent_id=claim.intent_id, owner="", attempt_no=claim.attempt_no,
            challenge_id=claim.challenge_id, strategy_key=claim.strategy_key,
            status=status, summary=["LLM 调用未完成：{}".format(reason)],
            outcome=outcome, failure_reason=reason,
            actions=["构造 prompt", "调用模型"], ruled_out=[],
            next_best_action="检查模型可用性后重试" if "contract" in reason else "按退避策略重试",
            resource_usage={"wall_seconds": round(self._clock() - started, 3),
                            "model_tokens": tokens, "tool_calls": 0},
        )
