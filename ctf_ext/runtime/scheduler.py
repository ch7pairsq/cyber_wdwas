"""调度：优先级 + 单题并发上限 + 题目轮转（§5、§8、附录 D）。"""
from __future__ import annotations

from collections import Counter
import time
from typing import List, Optional

from ..store.blackboard import Blackboard, Claim


class Scheduler:
    """优先级调度器：挑题（背压/开局加成）→ 按题领 Intent，跨题轮转。"""
    def __init__(self, board: Blackboard, max_project_workers: int = 2, max_running_projects: int = 3,
                 state_store=None, step_policy=None, clock=time.time, opening_seconds=1800,
                 retry_backoff_seconds: float = 0.0):
        """max_project_workers 限制单题并发；max_running_projects 限制同时在跑的题数。"""
        self._board = board
        self._max_project_workers = max_project_workers
        self._max_running_projects = max_running_projects
        self._state, self._step_policy = state_store, step_policy
        self._clock, self._opening_seconds = clock, opening_seconds
        self._retry_backoff = max(0.0, float(retry_backoff_seconds))
        self._started = clock()
        self._state_cache = {}

    def set_limits(self, max_project_workers=None, max_running_projects=None,
                   retry_backoff_seconds=None) -> None:
        """运行期热更新并发上限（§13 动态配置），无需重启。"""
        if max_project_workers is not None:
            self._max_project_workers = max(1, int(max_project_workers))
        if max_running_projects is not None:
            self._max_running_projects = max(1, int(max_running_projects))
        if retry_backoff_seconds is not None:
            self._retry_backoff = max(0.0, float(retry_backoff_seconds))

    def free_slots(self) -> int:
        """当前还能再开几道题：max_running_projects 减去已有在跑意图的题数。

        只给终局加时限流用（见 Orchestrator._grant_endgame）：空槽位决定这一轮
        最多追加几个 pass2 意图，避免一次把十几道题的预算抬上去、实际只有
        3 个槽位能跑（11-21 复盘：14 题同时抬升 = 11 题白抬）。只读黑板已有
        聚合查询，不改变调度语义，也不引入新的写路径。
        """
        try:
            summary = self._board.open_intent_summary()
            running = {row["challenge_id"] for row in summary if int(row["running"] or 0)}
        except Exception:
            return max(1, self._max_running_projects)  # 查询失败时退回全槽位，不阻断加时
        return max(0, self._max_running_projects - len(running))

    def _states_for(self, challenge_ids):
        if self._state is None:
            return {}
        loaded = self._state.load_many(challenge_ids)
        return {cid: state for cid, (_, state) in loaded.items()}

    def backfill_slots(self) -> int:
        """空闲槽位扣除正在运行及可领取的普通队列，避免尾盘抢占。"""
        summary = self._board.open_intent_summary()
        solved = set(self._board.accepted_challenge_ids())
        confirmed = set(self._board.confirmed_challenge_ids()) if self._state is not None else set()
        states = self._states_for([row["challenge_id"] for row in summary])
        running = {row["challenge_id"] for row in summary if int(row["running"] or 0)}
        queued = set()
        for row in summary:
            cid = row["challenge_id"]
            if cid in running or cid in solved or cid in confirmed:
                continue
            state = states.get(cid) or {}
            if state.get("status") in ("confirmed", "stopped"):
                continue
            if self._step_policy is not None and self._step_policy.route(int(state.get("steps") or 0))["active_slots"] <= 0:
                continue
            if not int(row.get("ready", 0) or 0):
                continue
            queued.add(cid)
        return max(0, self._max_running_projects - len(running) - len(queued))

    def eligible_challenges(self) -> List[str]:
        """按并发题目数封顶 + 单题并发上限挑题（§5.4 背压）。

        一次聚合查询取开放 Intent，再批量取状态与题目元数据，
        使每轮调度 SQL 数与题目数无关（避免 N+1）。
        """
        summary = self._board.open_intent_summary()
        summary_by_id = {row["challenge_id"]: row for row in summary}
        counter = Counter({row["challenge_id"]: int(row["running"] or 0)
                           for row in summary if int(row["running"] or 0)})
        running = set(counter)
        candidates = [row["challenge_id"] for row in summary if int(row["ready"] or 0) > 0]
        eligible = [c for c in candidates if counter.get(c, 0) < self._max_project_workers]
        if len(running) >= self._max_running_projects:
            eligible = [c for c in eligible if c in running]

        states = self._states_for(eligible)
        challenges = self._board.get_challenges(eligible)
        confirmed = self._board.confirmed_challenge_ids() if self._state is not None else set()
        accepted = self._board.accepted_challenge_ids()

        def rank(cid):
            """排序键：意图优先级 × 五阶段权重（开局难度加成）→ 公平轮转 → 题号。"""
            row = summary_by_id.get(cid) or {}
            factor = 1
            state = states.get(cid) or {}
            steps = int(state.get("steps") or 0)
            if self._state is not None and self._step_policy is not None:
                factor = self._step_policy.route(steps)["priority_factor"]
            # 难度仅用于开局优先级，绝不作为步数升降级的依据。
            challenge = challenges.get(cid) or {}
            if self._clock() - self._started < self._opening_seconds and steps < 30:
                factor *= 2 if challenge.get("difficulty_level", 0) in (1, 2) else 1
            return -(row.get("max_priority") or 0) * factor, counter.get(cid, 0), cid

        if self._state is not None:
            kept = []
            for cid in eligible:
                state = states.get(cid) or {}
                if state.get("status") in ("confirmed", "stopped"):
                    continue
                if self._step_policy is not None and self._step_policy.route(
                        int(state.get("steps") or 0))["active_slots"] <= 0:
                    continue
                if cid in confirmed:
                    continue
                kept.append(cid)
            eligible = kept
        eligible = [cid for cid in eligible if cid not in accepted]
        self._state_cache = states
        return sorted(eligible, key=rank)

    def pick_next(self, worker: str, lease_seconds: float = 180.0) -> Optional[Claim]:
        """按优先级顺序逐题领 Intent；stage≥3 的题并发上限强制降为 1。"""
        eligible = self.eligible_challenges()
        if not eligible:
            return None
        for cid in eligible:
            cap = self._max_project_workers
            state = self._state_cache.get(cid) or {}
            if self._state is not None and self._step_policy is not None \
                    and self._step_policy.route(int(state.get("steps") or 0))["stage"] >= 3:
                cap = 1
            claim = self._board.claim_intent(worker, lease_seconds=lease_seconds, challenge_ids=[cid],
                max_project_workers=cap, max_running_projects=self._max_running_projects,
                retry_backoff_seconds=self._retry_backoff)
            if claim is not None:
                return claim
        return None

    def has_waiting_work(self) -> bool:
        """是否仍有未确认/未停止的开放 Intent（含退避中的重试）。"""
        candidates = self._board.challenge_ids_with_work()
        if not candidates:
            return False
        accepted = self._board.accepted_challenge_ids()
        candidates = [cid for cid in candidates if cid not in accepted]
        if not candidates:
            return False
        if self._state is None:
            return True
        states = self._states_for(candidates)
        for cid in candidates:
            state = states.get(cid) or {}
            if state.get("status") in ("confirmed", "stopped"):
                continue
            if self._step_policy is not None and self._step_policy.route(
                    int(state.get("steps") or 0))["active_slots"] <= 0:
                continue
            return True
        return False
