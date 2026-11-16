"""最小可运行编排：Mock Harness → Planner 种子 → 领取 → Worker → 结果/证据 → 提交。

设计边界（§4、§8、§14）：
- 主循环不持有唯一状态，一切以黑板（SQLite）为准；
- 每轮先回收过期租约，崩溃 Worker 的 Intent 会被重新派发；
- 结果先过 B.2 契约校验，再幂等写入；事实写入前校验证据（D.1-3）；
- 提交走 Submitter 状态机，UNKNOWN 只查询不重发（附录 E）。
"""
from __future__ import annotations

import hashlib
import time
from typing import Dict, List, Optional

from ..domain.models import Challenge
from ..harness.contract import ContractViolation, validate_worker_output
from ..harness.schema import RunSummary, Submission
from ..store.blackboard import DuplicateStrategy, IntentDraft, WriteReceipt
from .metrics import Metrics
from .watchdog import recover_expired_leases


class Orchestrator:
    def __init__(self, harness, board, submitter, scheduler, worker,
                 evidence=None, metrics: Optional[Metrics] = None,
                 worker_name: str = "worker-1", lease_seconds: float = 60.0,
                 clock=time.time, hot_config=None, policy_engine=None, db=None):
        self._harness = harness
        self._board = board
        self._submitter = submitter
        self._scheduler = scheduler
        self._worker = worker
        self._evidence = evidence
        self._metrics = metrics or Metrics()
        self._worker_name = worker_name
        self._lease = lease_seconds
        self._clock = clock
        self._challenges: Dict[str, Dict] = {}
        self._hot = hot_config
        self._policy = policy_engine
        self._db = db
        self._config_version = hot_config.version if hot_config else 0

    # ------------------------------------------------------------ hot config
    def apply_config(self, cfg: Dict) -> None:
        """把通过校验的配置应用到调度/租约/下载限额（§13 动态配置）。"""
        conc = cfg.get("concurrency") or {}
        self._scheduler.set_limits(conc.get("max_project_workers"),
                                   conc.get("max_running_projects"))
        if (cfg.get("runtime") or {}).get("worker_lease_seconds"):
            self._lease = float(cfg["runtime"]["worker_lease_seconds"])
        dl = cfg.get("download") or {}
        if self._policy is not None:
            self._policy.update_limits(dl.get("max_single_bytes"), dl.get("max_total_bytes"))

    def reload_config_if_changed(self) -> bool:
        if self._hot is None or not self._hot.reload_if_changed():
            return False
        self.apply_config(self._hot.config)
        self._config_version = self._hot.version
        self._board.append_event("config_reloaded", "", {
            "version": self._hot.version, "hash": self._hot.hash,
        })
        self._metrics.incr("config.reloads")
        if self._db is not None:
            self._hot.snapshot(self._db, source="hot_reload")
        return True

    # ------------------------------------------------------------------ setup
    def bootstrap(self) -> List[Dict]:
        self._harness.startup()
        tasks = self._harness.read_tasks()
        for task in tasks:
            self._board.add_challenge(Challenge(
                id=task.challenge_id, name=task.name, category=task.category,
                description=task.description, attachments=task.attachments,
                points_hint=task.points_hint, difficulty_level=task.difficulty_level,
                metadata=task.metadata,
            ))
            self._challenges[task.challenge_id] = {
                "challenge_id": task.challenge_id, "name": task.name,
                "category": task.category, "attachments": list(task.attachments),
            }
            try:
                self._board.add_intent(IntentDraft(
                    challenge_id=task.challenge_id, strategy_key="misc.triage.auto",
                    params={"phase": "initial", "category": task.category},
                    points_hint=task.points_hint, est_tokens=2000,
                ))
            except DuplicateStrategy:
                pass  # 重启后复用已存在的探索方向
        self._board.append_event("run_started", "", {"tasks": len(tasks)})
        return tasks

    # -------------------------------------------------------------- one claim
    def _payload(self, result) -> Dict:
        return {
            "task_id": result.intent_id,
            "status": result.status,
            "summary": result.summary or ["无结论"],
            "facts": result.facts,
            "evidence_ids": result.evidence_ids,
            "attempt": {
                "strategy_key": result.strategy_key,
                "preconditions": result.preconditions,
                "actions": result.actions,
                "outcome": result.outcome,
                "failure_reason": result.failure_reason,
                "ruled_out": result.ruled_out,
                "next_best_action": result.next_best_action,
            },
            "candidate_answer": result.candidate_answer,
            "resource_usage": result.resource_usage,
        }

    def step(self) -> Optional[Dict]:
        """领取并执行一个 Intent；无可用任务返回 None。"""
        claim = self._scheduler.pick_next(self._worker_name, lease_seconds=self._lease)
        if claim is None:
            return None
        self._metrics.incr("intents.claimed")
        challenge = self._challenges.get(claim.challenge_id) or self._board.get_challenge(claim.challenge_id) or {}
        result = self._worker.work(claim, challenge)
        result.owner = self._worker_name

        check = self._evidence.verify if self._evidence is not None else None
        try:
            validate_worker_output(self._payload(result), evidence_check=check)
        except ContractViolation as exc:
            self._metrics.incr("contract.violations")
            self._board.append_event("failed_contract", claim.intent_id, {"errors": exc.errors})
            result.status, result.outcome = "failed", "error"
            result.candidate_answer = None
            result.failure_reason = "failed_contract: " + "; ".join(exc.errors)

        usage = result.resource_usage or {}
        tokens = int(usage.get("model_tokens") or 0)
        if tokens:
            self._metrics.incr("model.tokens", tokens)
        tool_calls = int(usage.get("tool_calls") or 0)
        if tool_calls:
            self._metrics.incr("tools.calls", tool_calls)
        wall = float(usage.get("wall_seconds") or 0)
        if wall:
            self._metrics.observe("attempt.wall_seconds", wall)

        key = "{}-{}".format(claim.intent_id, claim.attempt_no)
        receipt: WriteReceipt = self._board.write_result(result, key)
        self._metrics.incr("intents.completed" if receipt.first else "intents.duplicate_write")
        self._board.append_event("result_written", claim.intent_id, {
            "status": result.status, "outcome": result.outcome, "first": receipt.first,
        })
        if result.facts:
            try:
                self._board.write_facts(claim.challenge_id, result.facts,
                                        source_attempt_id=receipt.attempt_id)
                self._metrics.incr("knowledge.facts", len(result.facts))
            except Exception as exc:  # 证据不成立时不污染事实层
                self._board.append_event("fact_rejected", claim.intent_id,
                                         {"error": type(exc).__name__})

        submission = None
        if result.candidate_answer:
            idem = "sub-" + hashlib.sha256(
                (claim.challenge_id + "|" + result.candidate_answer).encode("utf-8")
            ).hexdigest()[:16]
            receipt_sub = self._submitter.submit(Submission(
                challenge_id=claim.challenge_id, answer=result.candidate_answer,
                idempotency_key=idem, evidence_ids=list(result.evidence_ids),
            ))
            submission = {"status": receipt_sub.status, "receipt_id": receipt_sub.receipt_id,
                          "idempotency_key": idem}
            self._metrics.incr("submissions." + receipt_sub.status.lower())
            self._board.append_event("submission", claim.challenge_id, submission)

        return {"intent_id": claim.intent_id, "challenge_id": claim.challenge_id,
                "status": result.status, "outcome": result.outcome,
                "candidate_answer": result.candidate_answer, "submission": submission}

    # ---------------------------------------------------------------- run loop
    def run(self, max_seconds: float = 120.0) -> Dict:
        deadline = self._clock() + max_seconds
        tasks = self.bootstrap()
        recover_expired_leases(self._board)  # 启动即恢复未完成租约（§17.4）
        results: List[Dict] = []
        solved = set()
        while self._clock() < deadline and len(solved) < len(tasks):
            self.reload_config_if_changed()
            recover_expired_leases(self._board)
            out = self.step()
            if out is None:
                break
            results.append(out)
            if out["submission"] and out["submission"]["status"] == "ACCEPTED":
                solved.add(out["challenge_id"])
        summary = RunSummary(results=results, notes="orch:{}".format(self._worker_name))
        finalized = self._harness.finalize(summary)
        self._board.append_event("run_finished", "", {"results": len(results), "solved": sorted(solved)})
        return {"tasks": len(tasks), "solved": sorted(solved), "results": results,
                "metrics": self._metrics.snapshot(), "finalized": finalized}
