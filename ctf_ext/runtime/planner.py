"""最小 Planner：把一次失败尝试转成下一轮可执行 Intent（§7、§23.2）。

只做确定性、可审计的规划：
- 触发条件：no_signal、Gate rejected 或 Gate pending；
- 输入：Worker 给出的 next_best_action / ruled_out + 已脱敏教训；
- 去重：strategy_key 带序号并交由黑板 attempt_key 校验；
- 上限：每题累计 Intent 数受 max_intents_per_challenge 约束；
- blocked 属于终态，不扩展。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..domain.models import OUTCOME_NO_SIGNAL
from ..store.blackboard import DuplicateStrategy, IntentDraft
from ..store.lessons import sanitize_lesson_text

REASON_GATE_REJECTED = "gate_rejected"
REASON_GATE_PENDING = "gate_pending"
REASON_NO_SIGNAL = "no_signal"


class IntentPlanner:
    def __init__(self, board, max_intents_per_challenge: int = 4):
        self._board = board
        self.max_intents_per_challenge = max(1, int(max_intents_per_challenge))

    def set_limit(self, value) -> None:
        self.max_intents_per_challenge = max(1, int(value))

    @staticmethod
    def _reason(result, decision) -> str:
        status = getattr(decision, "status", None)
        if status == "confirmed":
            return ""
        if status == "rejected":
            return REASON_GATE_REJECTED
        if status == "pending":
            return REASON_GATE_PENDING
        if getattr(result, "outcome", "") == OUTCOME_NO_SIGNAL:
            return REASON_NO_SIGNAL
        return ""

    def plan(self, claim, result, decision=None, challenge: Optional[Dict[str, Any]] = None,
             attempt_id: Optional[str] = None) -> Optional[str]:
        reason = self._reason(result, decision)
        if not reason:
            return None
        # blocked 表示缺附件/预算等外部条件，重复规划只会空转。
        if getattr(result, "status", "") == "blocked":
            return None
        challenge_id = claim.challenge_id
        count = self._board.count_intents(challenge_id)
        if count >= self.max_intents_per_challenge:
            self._board.append_event("planner_cap_reached", claim.intent_id, {
                "challenge_id": challenge_id,
                "intents": count,
                "max_intents_per_challenge": self.max_intents_per_challenge,
            })
            return None

        next_action = sanitize_lesson_text(getattr(result, "next_best_action", "") or "", 240)
        if not next_action:
            next_action = "根据已排除方向换用不同证据与策略重试"
        ruled_out = [sanitize_lesson_text(item, 120)
                     for item in (getattr(result, "ruled_out", None) or [])]
        ruled_out = [item for item in ruled_out if item][:5]
        lessons = []
        try:
            lessons = [item["text"][:240] for item in
                       self._board.lesson_context(challenge_id, limit=3) if item.get("text")]
        except Exception:
            lessons = []  # 教训检索失败不能阻断规划

        meta = challenge if challenge is not None else (self._board.get_challenge(challenge_id) or {})
        params: Dict[str, Any] = {
            "phase": "followup",
            "planner": "minimal",
            "reason": reason,
            "source_intent_id": claim.intent_id,
            "source_attempt_id": attempt_id or "",
            "next_best_action": next_action,
            "ruled_out": ruled_out,
            "lessons": lessons,
        }
        if decision is not None:
            params["gate_status"] = sanitize_lesson_text(str(decision.status), 40)
            params["gate_level"] = sanitize_lesson_text(str(decision.level), 40)

        strategy_key = "{}.followup.{}".format(claim.strategy_key, count + 1)
        draft = IntentDraft(
            challenge_id=challenge_id,
            strategy_key=strategy_key,
            params=params,
            points_hint=float(meta.get("points_hint") or 0),
            est_tokens=2000,
            budget_seconds=float(getattr(claim, "budget_seconds", 0) or 0),
        )
        try:
            intent_id = self._board.add_intent(draft)
        except DuplicateStrategy:
            return None
        self._board.append_event("intent_planned", intent_id, {
            "challenge_id": challenge_id,
            "reason": reason,
            "source_intent_id": claim.intent_id,
            "source_attempt_id": attempt_id or "",
            "strategy_key": strategy_key,
            "remaining": max(0, self.max_intents_per_challenge - count - 1),
        })
        return intent_id
