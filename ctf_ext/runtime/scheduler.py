"""调度：优先级 + 单题并发上限 + 题目轮转（§5、§8、附录 D）。"""
from __future__ import annotations

from collections import Counter
from typing import Dict, List, Optional, Sequence

from ..store.blackboard import READY, RUNNING, Blackboard, Claim


class Scheduler:
    def __init__(self, board: Blackboard, max_project_workers: int = 2, max_running_projects: int = 3):
        self._board = board
        self._max_project_workers = max_project_workers
        self._max_running_projects = max_running_projects

    def _running_per_challenge(self) -> Counter:
        counter: Counter = Counter()
        for row in self._board.list_open():
            if row["status"] == RUNNING:
                counter[row["challenge_id"]] += 1
        return counter

    def eligible_challenges(self) -> List[str]:
        """按并发题目数封顶 + 单题并发上限挑题（§5.4 背压）。

        - 单题：RUNNING 数达到 max_project_workers 后不再投喂；
        - 全局：已在跑的题目数达到 max_running_projects 后，不再开启新题目。
        """
        counter = self._running_per_challenge()
        running = set(counter)
        challenges = [r["challenge_id"] for r in self._board._db.execute(
            "SELECT DISTINCT challenge_id FROM intents_ext WHERE status IN ('READY','FAILED_RETRYABLE')"
        )]
        eligible = [c for c in challenges if counter.get(c, 0) < self._max_project_workers]
        if len(running) >= self._max_running_projects:
            eligible = [c for c in eligible if c in running]
        eligible.sort(key=lambda c: counter.get(c, 0))  # 少的优先，避免单题垄断
        return eligible[: self._max_running_projects]

    def pick_next(self, worker: str, lease_seconds: float = 180.0) -> Optional[Claim]:
        eligible = self.eligible_challenges()
        if not eligible:
            return None
        return self._board.claim_intent(worker, lease_seconds=lease_seconds, challenge_ids=eligible)
