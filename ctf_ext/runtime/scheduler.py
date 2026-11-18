"""调度：优先级 + 单题并发上限 + 题目轮转（§5、§8、附录 D）。"""
from __future__ import annotations

from collections import Counter
import time
from typing import List, Optional

from ..store.blackboard import RUNNING, Blackboard, Claim


class Scheduler:
    def __init__(self, board: Blackboard, max_project_workers: int = 2, max_running_projects: int = 3,
                 state_store=None, step_policy=None, clock=time.time, opening_seconds=1800,
                 retry_backoff_seconds: float = 0.0):
        self._board = board
        self._max_project_workers = max_project_workers
        self._max_running_projects = max_running_projects
        self._state, self._step_policy = state_store, step_policy
        self._clock, self._opening_seconds = clock, opening_seconds
        self._retry_backoff = max(0.0, float(retry_backoff_seconds))
        self._started = clock()

    def set_limits(self, max_project_workers=None, max_running_projects=None,
                   retry_backoff_seconds=None) -> None:
        """运行期热更新并发上限（§13 动态配置），无需重启。"""
        if max_project_workers is not None:
            self._max_project_workers = max(1, int(max_project_workers))
        if max_running_projects is not None:
            self._max_running_projects = max(1, int(max_running_projects))
        if retry_backoff_seconds is not None:
            self._retry_backoff = max(0.0, float(retry_backoff_seconds))

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
        def rank(cid):
            row = self._board._db.execute("SELECT MAX(priority) FROM intents_ext WHERE challenge_id=? AND status IN ('READY','FAILED_RETRYABLE')", (cid,)).fetchone()
            factor = 1
            steps = 0
            if self._state is not None:
                _, state = self._state.load(cid)
                steps = state["steps"]
                route = self._step_policy.route(steps)
                factor = route["priority_factor"]
            # 难度仅用于开局优先级，绝不作为步数升降级的依据。
            challenge = self._board.get_challenge(cid) or {}
            if self._clock() - self._started < self._opening_seconds and steps < 30:
                factor *= 2 if challenge.get("difficulty_level", 0) in (1, 2) else 1
            return -(row[0] or 0) * factor, counter.get(cid, 0), cid
        if self._state is not None:
            eligible = [cid for cid in eligible if self._state.load(cid)[1]["status"] not in ("confirmed", "stopped")
                        and self._step_policy.route(self._state.load(cid)[1]["steps"])["active_slots"] > 0
                        and not self._board._db.execute("SELECT 1 FROM gate_decisions WHERE challenge_id=? AND status='confirmed'", (cid,)).fetchone()]
        eligible = [cid for cid in eligible if not self._board._db.execute("SELECT 1 FROM submissions WHERE challenge_id=? AND status='ACCEPTED'", (cid,)).fetchone()]
        return sorted(eligible, key=rank)

    def pick_next(self, worker: str, lease_seconds: float = 180.0) -> Optional[Claim]:
        eligible = self.eligible_challenges()
        if not eligible:
            return None
        for cid in eligible:
            cap = self._max_project_workers
            if self._state is not None and self._step_policy.route(self._state.load(cid)[1]["steps"])["stage"] >= 3:
                cap = 1
            claim = self._board.claim_intent(worker, lease_seconds=lease_seconds, challenge_ids=[cid],
                max_project_workers=cap, max_running_projects=self._max_running_projects,
                retry_backoff_seconds=self._retry_backoff)
            if claim is not None:
                return claim
        return None

    def has_waiting_work(self) -> bool:
        """是否仍有未确认/未停止的开放 Intent（含退避中的重试）。"""
        rows = self._board._db.execute(
            "SELECT DISTINCT challenge_id FROM intents_ext"
            " WHERE status IN ('READY','RUNNING','FAILED_RETRYABLE')")
        for row in rows:
            cid = row["challenge_id"]
            if self._board._db.execute(
                    "SELECT 1 FROM submissions WHERE challenge_id=? AND status='ACCEPTED'", (cid,)).fetchone():
                continue
            if self._state is not None:
                _, state = self._state.load(cid)
                if state["status"] in ("confirmed", "stopped"):
                    continue
                if self._step_policy is not None and self._step_policy.route(state["steps"])["active_slots"] <= 0:
                    continue
            return True
        return False
