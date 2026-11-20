"""五级 deadline 预算（§14：绝对截止 → 单题 → 单策略 → 工具 → 宽限）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

LEVEL_ABSOLUTE = "absolute"
LEVEL_CHALLENGE = "challenge"
LEVEL_STRATEGY = "strategy"
LEVEL_TOOL = "tool"
LEVEL_GRACE = "grace"


@dataclass
class Budget:
    """五级 deadline 预算；expired() 按优先级返回最先越过的层级。"""
    absolute_deadline: float
    challenge_deadline: float
    strategy_deadline: float
    tool_deadline: float
    tool_timeout: float
    grace: float = 2.0

    def remaining(self, now: float) -> float:
        """单题剩余秒数（负数表示已超时）。"""
        return self.challenge_deadline - now

    def expired(self, now: float) -> Optional[str]:
        """返回最先越过的层级名；未超时返回 None。"""
        if now >= self.absolute_deadline:
            return LEVEL_ABSOLUTE
        if now >= self.challenge_deadline:
            return LEVEL_CHALLENGE
        if now >= self.strategy_deadline:
            return LEVEL_STRATEGY
        if now >= self.tool_deadline:
            return LEVEL_TOOL
        if now >= self.challenge_deadline - self.grace:
            return LEVEL_GRACE
        return None

    @classmethod
    def from_config(cls, now: float, run_deadline_epoch: float, per_challenge_seconds: float,
                    per_strategy_seconds: float, tool_timeout: float, grace: float = 2.0) -> "Budget":
        """由各层配置构造预算：每级取 min 后逐级封顶，保证层级不倒挂。"""
        challenge_end = min(now + per_challenge_seconds, run_deadline_epoch)
        return cls(
            absolute_deadline=run_deadline_epoch,
            challenge_deadline=challenge_end,
            strategy_deadline=min(now + per_strategy_seconds, challenge_end),
            tool_deadline=min(now + tool_timeout, challenge_end),
            tool_timeout=tool_timeout,
            grace=grace,
        )
