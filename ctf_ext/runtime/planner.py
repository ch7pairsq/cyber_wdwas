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
# no_signal 链深上限（2026-11-22 模拟赛日志）：misc.endgame.pass2 连挂
# 9 层 followup 全 no_signal，每层 8-16 轮 python_exec 空转零产出，意图
# 上限放大到 50 后会吃满预算槽。链深达上限不再扩展，通道让给其他题；
# 该题剩余机会走 endgame 冷却再授权。rejected/pending 有平台反馈，不受此限。
NO_SIGNAL_CHAIN_LIMIT = 3


class IntentPlanner:
    """确定性规划器：失败/被拒/无信号时生成下一轮 followup Intent。"""
    def __init__(self, board, max_intents_per_challenge: int = 4):
        """max_intents_per_challenge 为单题累计 Intent 上限（超出记事件不再规划）。"""
        self._board = board
        self.max_intents_per_challenge = max(1, int(max_intents_per_challenge))

    def set_limit(self, value) -> None:
        """热更新单题 Intent 上限。"""
        self.max_intents_per_challenge = max(1, int(value))

    @staticmethod
    def _reason(result, decision) -> str:
        """判定是否需要规划：confirmed 不规划，rejected/pending/no_signal 分别返回原因码。"""
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
        """生成 followup Intent（带已排除方向与教训）；blocked/超限/去重命中不扩展。"""
        reason = self._reason(result, decision)
        if not reason:
            return None
        # blocked 表示缺附件/预算等外部条件，重复规划只会空转。
        if getattr(result, "status", "") == "blocked":
            return None
        challenge_id = claim.challenge_id
        if reason == REASON_NO_SIGNAL:
            chain_depth = claim.strategy_key.count(".followup.")
            if chain_depth >= NO_SIGNAL_CHAIN_LIMIT:
                self._board.append_event("planner_no_signal_chain_capped", claim.intent_id, {
                    "challenge_id": challenge_id,
                    "strategy_key": claim.strategy_key,
                    "chain_depth": chain_depth,
                })
                return None
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
