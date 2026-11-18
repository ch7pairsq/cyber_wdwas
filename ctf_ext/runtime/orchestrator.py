"""最小可运行编排：Mock Harness → Planner 种子 → 领取 → Worker → 结果/证据 → 提交。

设计边界（§4、§8、§14）：
- 主循环不持有唯一状态，一切以黑板（SQLite）为准；
- 每轮先回收过期租约，崩溃 Worker 的 Intent 会被重新派发；
- 结果先过 B.2 契约校验，再幂等写入；事实写入前校验证据（D.1-3）；
- 提交走 SubmitGateway 状态机，UNKNOWN 只查询不重发（附录 E）；
- 单题执行有硬超时，超时只放出该题，不拖死整场；
- 单次异常只记录并继续，连续失败达到上限才停止主循环。
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

from ..domain.models import Challenge
from ..harness.contract import ContractViolation, validate_worker_output
from ..harness.schema import RunSummary
from ..store import lessons as lesson_store
from ..store.blackboard import DuplicateStrategy, IntentDraft, WorkerResult
from .heartbeat import HeartbeatFile
from .lease import LeaseHeartbeat
from .metrics import Metrics, framework_metrics
from .planner import IntentPlanner
from .watchdog import ClaimTimeout, DeadlineGuard, recover_expired_leases


class Orchestrator:
    def __init__(self, harness, board, scheduler, worker,
                 evidence=None, metrics: Optional[Metrics] = None,
                 worker_name: str = "worker-1", lease_seconds: float = 60.0,
                 clock=time.time, hot_config=None, policy_engine=None, db=None,
                 state_store=None, flag_gate=None, gateway=None, reviewer_factory=None,
                 claim_timeout_seconds: float = 90.0, configured_max_workers: int = 1,
                 heartbeat_path: Optional[str] = None, max_consecutive_errors: int = 3,
                 retry_backoff_seconds: float = 0.0, poll_seconds: float = 0.1,
                 planner=None, max_intents_per_challenge: int = 4):
        self._harness = harness
        self._board = board
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
        self._state, self._gate, self._gateway = state_store, flag_gate, gateway
        self._reviewer_factory = reviewer_factory
        self._claim_timeout = max(0.0, float(claim_timeout_seconds))
        self._configured_max_workers = max(1, int(configured_max_workers))
        self._heartbeat = HeartbeatFile(heartbeat_path) if heartbeat_path else None
        self._max_consecutive_errors = max(1, int(max_consecutive_errors))
        self._retry_backoff = max(0.0, float(retry_backoff_seconds))
        self._poll_seconds = max(0.01, float(poll_seconds))
        self._planner = planner or IntentPlanner(
            board, max_intents_per_challenge=max_intents_per_challenge)

    # ------------------------------------------------------------ hot config
    def apply_config(self, cfg: Dict) -> None:
        """把通过校验的配置应用到调度/租约/下载限额（§13 动态配置）。"""
        conc = cfg.get("concurrency") or {}
        self._scheduler.set_limits(conc.get("max_project_workers"),
                                   conc.get("max_running_projects"),
                                   (cfg.get("runtime") or {}).get("retry_backoff_seconds"))
        self._retry_backoff = max(0.0, float((cfg.get("runtime") or {}).get("retry_backoff_seconds")
                                             if (cfg.get("runtime") or {}).get("retry_backoff_seconds") is not None
                                             else self._retry_backoff))
        if (cfg.get("runtime") or {}).get("worker_lease_seconds"):
            self._lease = float(cfg["runtime"]["worker_lease_seconds"])
        budget = cfg.get("budget") or {}
        if budget.get("claim_timeout_seconds"):
            self._claim_timeout = max(0.0, float(budget["claim_timeout_seconds"]))
        runtime = cfg.get("runtime") or {}
        if runtime.get("max_intents_per_challenge") is not None:
            self._planner.set_limit(runtime["max_intents_per_challenge"])
        dl = cfg.get("download") or {}
        if self._policy is not None:
            self._policy.update_limits(dl.get("max_single_bytes", 0), dl.get("max_total_bytes", 0))

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
                "category": task.category, "description": task.description,
                "attachments": list(task.attachments), "difficulty_level": task.difficulty_level,
            }
            try:
                self._board.add_intent(IntentDraft(
                    challenge_id=task.challenge_id, strategy_key="misc.triage.auto",
                    params={"phase": "initial", "category": task.category},
                    points_hint=task.points_hint, est_tokens=2000,
                ))
            except DuplicateStrategy:
                pass  # 重启后复用已存在的探索方向
        if self._gateway is not None:
            self._gateway.recover_legacy()
            try:
                self._collect_submissions(self._gateway.pump())
            except Exception as exc:  # 提交恢复失败不能阻断解题
                self._board.append_event("gateway_recover_failed", "", {"error": type(exc).__name__})
        if self._state is not None:
            for challenge_id in self._state.challenge_ids():
                _, saved = self._state.load(challenge_id)
                self._board.append_event("session_resumed", challenge_id,
                                         {"steps": saved["steps"], "evidence_ids": saved["evidence_ids"]})
                self._metrics.incr("sessions.resumed")
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
        if self._gateway is not None:
            try:
                self._collect_submissions(self._gateway.pump())
            except Exception as exc:  # 单个提交状态异常不能拖垮主循环
                self._board.append_event("gateway_pump_failed", "", {"error": type(exc).__name__})
                self._metrics.incr("submissions.pump_errors")
        claim = self._scheduler.pick_next(self._worker_name, lease_seconds=self._lease)
        if claim is None:
            return None
        self._metrics.incr("intents.claimed")
        with LeaseHeartbeat(self._board, claim, self._worker_name, self._lease):
            return self._execute_claim(claim)

    def _challenge_context(self, claim) -> Dict:
        challenge = dict(self._challenges.get(claim.challenge_id)
                         or self._board.get_challenge(claim.challenge_id) or {})
        try:
            challenge["handoff"] = self._board.handoff(claim.challenge_id)
        except Exception as exc:
            self._board.append_event("handoff_failed", claim.intent_id, {"error": type(exc).__name__})
        return challenge

    def _timeout_result(self, claim, started: float) -> WorkerResult:
        return WorkerResult(
            claim.intent_id, self._worker_name, claim.attempt_no, claim.challenge_id,
            claim.strategy_key, "failed", summary=["单题执行超时，已释放该题供后续重试"],
            outcome="timeout", failure_reason="claim_timeout",
            preconditions=[], actions=["执行单题"], ruled_out=[],
            next_best_action="缩小尝试范围或提高单题预算后重试",
            resource_usage={"wall_seconds": round(max(0.0, self._clock() - started), 3),
                            "model_tokens": 0, "tool_calls": 0},
        )

    def _execute_claim(self, claim):
        """执行一个已领取 Intent；拆成执行/契约/校验/落库/后处理五段。"""
        challenge = self._challenge_context(claim)
        started = self._clock()
        result = self._run_worker(claim, challenge, started)
        self._validate_contract(claim, result)
        self._record_usage(result)
        decision = self._evaluate_candidate(claim, result, started)
        receipt, write_error, submission_keys = self._persist_result(claim, result, decision)
        if receipt is None:
            return self._write_failure_response(claim, result, write_error)

        self._record_side_effects(claim, challenge, result, decision, receipt)
        self._maybe_plan(claim, challenge, result, decision, receipt)

        submission = None
        if submission_keys:
            submission = {"status": "DRAFT", "receipt_id": "",
                          "idempotency_key": submission_keys[0]}
            try:
                self._collect_submissions(self._gateway.pump())
            except Exception as exc:
                self._board.append_event("gateway_pump_failed", "", {"error": type(exc).__name__})

        return {"intent_id": claim.intent_id, "challenge_id": claim.challenge_id,
                "status": result.status, "outcome": result.outcome,
                "candidate_answer": result.candidate_answer, "submission": submission,
                "gate": {"status": decision.status, "level": decision.level} if decision else None}

    def _run_worker(self, claim, challenge, started):
        try:
            if self._claim_timeout:
                with DeadlineGuard(self._claim_timeout):
                    result = self._worker.work(claim, challenge)
            else:
                result = self._worker.work(claim, challenge)
        except ClaimTimeout:
            result = self._timeout_result(claim, started)
            self._metrics.incr("claim.timeouts")
            self._board.append_event("claim_timeout", claim.intent_id,
                                     {"seconds": self._claim_timeout})
        except Exception as exc:
            # 单次 Worker 异常只让该 Intent 可重试；不能把整个题目永久 stopped。
            result = WorkerResult(claim.intent_id, self._worker_name, claim.attempt_no,
                claim.challenge_id, claim.strategy_key, "failed",
                summary=["执行中断，证据与状态已保留"], outcome="error",
                failure_reason=type(exc).__name__, actions=["执行单题"],
                next_best_action="按退避策略重试",
                resource_usage={"wall_seconds": round(max(0.0, self._clock() - started), 3),
                                "model_tokens": 0, "tool_calls": 0})
            self._board.append_event("worker_failed", claim.intent_id, {"error": type(exc).__name__})
        result.owner = self._worker_name
        return result

    def _validate_contract(self, claim, result) -> None:
        check = self._evidence.verify if self._evidence is not None else None
        try:
            validate_worker_output(self._payload(result), evidence_check=check)
        except ContractViolation as exc:
            self._metrics.incr("contract.violations")
            self._board.append_event("failed_contract", claim.intent_id, {"errors": exc.errors})
            result.status, result.outcome = "failed", "error"
            result.candidate_answer = None
            result.failure_reason = "failed_contract: " + "; ".join(exc.errors)
            result.facts = []
            result.evidence_ids = [eid for eid in result.evidence_ids if check is not None and check(eid)]
            # 契约失败是可重试的单次失败；终态由控制器的预算/停滞策略决定，
            # 不能因为一次格式错误就把整题永久 stopped。

    def _record_usage(self, result) -> None:
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

    def _evaluate_candidate(self, claim, result, started):
        decision = None
        if not result.candidate_answer:
            return decision
        if self._gate is None or self._gateway is None:
            self._board.append_event("candidate_rejected", claim.challenge_id,
                                     {"reason": "校验器或提交网关未配置"})
        else:
            if self._reviewer_factory is not None:
                self._gate.reviewer = self._reviewer_factory(claim.challenge_id)
            try:
                gate_budget = max(0.05, self._claim_timeout - (self._clock() - started)) if self._claim_timeout else 0
                if gate_budget:
                    with DeadlineGuard(gate_budget):
                        decision = self._gate.evaluate(claim.challenge_id, result.candidate_answer,
                                                       result.evidence_ids)
                else:
                    decision = self._gate.evaluate(claim.challenge_id, result.candidate_answer,
                                                   result.evidence_ids)
            except ClaimTimeout:
                self._metrics.incr("gates.timeout")
                self._board.append_event("gate_timeout", claim.intent_id, {"seconds": gate_budget})
                result.status, result.outcome = "failed", "timeout"
                result.failure_reason = "gate_timeout"
                result.candidate_answer = None
                decision = None
        if decision is not None and decision.status == "confirmed":
            # Gate 已确认时 Intent 必须终止；否则 Worker 写 partial 会让同一候选
            # 在租约释放后再次被领取，重复消耗预算。
            result.status = "completed"
        return decision

    def _persist_result(self, claim, result, decision):
        key = "{}-{}".format(claim.intent_id, claim.attempt_no)
        submission_keys = []

        def persist_gate(db):
            if decision is not None:
                self._gate.record(db, decision)
                if decision.status == "confirmed":
                    submission_keys.append(self._gateway.enqueue(db, decision))

        try:
            receipt = self._board.write_result(result, key, transaction_hook=persist_gate)
            return receipt, None, submission_keys
        except Exception as exc:
            self._metrics.incr("results.write_failed")
            self._board.append_event("result_write_failed", claim.intent_id,
                                     {"error": type(exc).__name__})
            self._release_write_failure(claim)
            return None, type(exc).__name__, submission_keys

    @staticmethod
    def _write_failure_response(claim, result, write_error):
        return {"intent_id": claim.intent_id, "challenge_id": claim.challenge_id,
                "status": "write_failed", "outcome": result.outcome,
                "candidate_answer": None, "submission": None, "gate": None,
                "write_error": write_error}

    def _record_side_effects(self, claim, challenge, result, decision, receipt) -> None:
        if receipt.first and receipt.attempt_id:
            try:
                with self._board.transaction() as db:
                    lesson_store.record_attempt(db, receipt.attempt_id,
                                                challenge.get("category"), commit=False)
                self._metrics.incr("knowledge.lessons")
            except Exception as exc:
                self._board.append_event("lesson_failed", claim.intent_id,
                                         {"error": type(exc).__name__})

        if decision is not None:
            self._metrics.incr("gates." + decision.level + "." + decision.status)
            self._board.append_event("flag_gate", claim.challenge_id, {
                "decision_id": decision.id, "status": decision.status, "level": decision.level,
                "command_id": decision.command_id, "evidence_ids": decision.evidence_ids,
                "reason": decision.reason, "gates": decision.gates})
            if decision.status == "confirmed" and self._state is not None:
                self._state.patch(claim.challenge_id, lambda s: s.update(status="confirmed"))
                _, current = self._state.load(claim.challenge_id)
                self._metrics.observe("challenge.steps", current["steps"])
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

    def _maybe_plan(self, claim, challenge, result, decision, receipt) -> None:
        if self._planner is None or not receipt.first:
            return
        try:
            planned = self._planner.plan(claim, result, decision, challenge=challenge,
                                         attempt_id=receipt.attempt_id)
            if planned:
                self._metrics.incr("intents.planned")
        except Exception as exc:  # 规划失败不能影响已落库结果
            self._board.append_event("planner_failed", claim.intent_id,
                                     {"error": type(exc).__name__})

    def _release_write_failure(self, claim) -> None:
        """写结果失败时释放 Intent，避免白等整个租约；租约已被别人拿走则安全失败。"""
        try:
            self._board.release(claim.intent_id, self._worker_name, "write_failed",
                                attempt_no=claim.attempt_no)
        except Exception as exc:
            self._board.append_event("release_failed", claim.intent_id,
                                     {"error": type(exc).__name__})

    def _solved(self):
        return self._board.accepted_challenge_ids()

    def _collect_submissions(self, events):
        for event in events:
            status = event.get("status", "unknown")
            self._metrics.incr("submissions." + str(status).lower())
            self._metrics.observe("submissions.latency_seconds", float(event.get("latency_seconds") or 0))

    # ---------------------------------------------------------------- run loop
    def run(self, max_seconds: float = 120.0) -> Dict:
        deadline = self._clock() + max_seconds
        factory = getattr(self._worker, "_control_factory", None)
        if factory is not None:
            factory.deadline = deadline
        self._touch_heartbeat()
        tasks = self.bootstrap()
        try:
            recover_expired_leases(self._board)  # 启动即恢复未完成租约（§17.4）
        except Exception as exc:
            self._board.append_event("watchdog_failed", "", {"error": type(exc).__name__})
        results: List[Dict] = []
        solved = self._solved()
        consecutive_errors = 0
        while self._clock() < deadline and len(solved) < len(tasks):
            self._touch_heartbeat()
            try:
                self.reload_config_if_changed()
                recover_expired_leases(self._board)
                out = self.step()
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                consecutive_errors += 1
                self._board.append_event("step_failed", "", {"error": type(exc).__name__,
                                                             "consecutive": consecutive_errors})
                self._metrics.incr("runtime.step_errors")
                if consecutive_errors >= self._max_consecutive_errors:
                    self._board.append_event("run_aborted", "", {"reason": "consecutive_errors",
                                                                  "count": consecutive_errors})
                    break
                continue
            consecutive_errors = 0
            if out is None:
                if self._scheduler.has_waiting_work() and self._clock() < deadline:
                    time.sleep(min(self._poll_seconds, max(0.0, deadline - self._clock())))
                    continue
                break
            results.append(out)
            if out["submission"] and out["submission"]["status"] == "ACCEPTED":
                solved.add(out["challenge_id"])
        if self._gateway is not None:
            try:
                self._collect_submissions(self._gateway.flush(timeout=min(1, max(0, deadline - self._clock()))))
            except Exception as exc:
                self._board.append_event("gateway_flush_failed", "", {"error": type(exc).__name__})
            self._gateway.close()
        solved = self._solved()
        for result in results:
            if result["submission"]:
                row = self._board.latest_submission(result["challenge_id"])
                if row:
                    result["submission"] = row
        self._metrics.incr("submissions.pending", self._board.pending_submission_count())
        summary = RunSummary(results=results, notes="orch:{}".format(self._worker_name))
        finalized = self._harness.finalize(summary)
        self._board.append_event("run_finished", "", {"results": len(results), "solved": sorted(solved)})
        return {"tasks": len(tasks), "solved": sorted(solved), "results": results,
                "metrics": self._metrics.snapshot(), "finalized": finalized,
                "quality": framework_metrics(self._board.connection, len(tasks)) if self._state else {},
                "execution": {"mode": "serial", "configured_max_workers": self._configured_max_workers,
                              "effective_parallel": 1,
                              "claim_timeout_seconds": self._claim_timeout,
                              "retry_backoff_seconds": self._retry_backoff,
                              "isolation": {"agent": "per_challenge_workspace",
                                            "container": "not_verified"}}}

    def _touch_heartbeat(self) -> None:
        if self._heartbeat is not None:
            self._heartbeat.touch()
