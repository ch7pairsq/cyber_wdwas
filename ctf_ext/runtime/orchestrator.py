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

import shutil
import sys
import threading
import time
from typing import Dict, List, Optional

from ..domain.models import Challenge
from ..harness.contract import ContractViolation, validate_worker_output
from ..harness.schema import RunSummary
from ..store import lessons as lesson_store
from ..store import solutions as solution_store
from ..store.blackboard import DuplicateStrategy, IntentDraft, WorkerResult
from .heartbeat import HeartbeatFile
from .lease import LeaseHeartbeat
from .metrics import Metrics, framework_metrics
from .planner import IntentPlanner
from .watchdog import ClaimTimeout, DeadlineGuard, recover_expired_leases


class Orchestrator:
    """编排主循环：领取→执行→门控→落库→提交的全流程状态机与资源守护。"""
    # ProviderError 熔断阈值：同一 intent 第 N 次领取（attempt_no 每次 claim 自增）
    # 仍以 Provider/配置类错误失败时，置为 blocked 不再重试。正式赛实测
    # empty model response 在同一 intent 上循环 20+ 次烧穿 endgame 预算，
    # 唯一挡板是单 intent 预算墙（远水救不了近火）。
    provider_error_max_attempts = 3
    # deadline 后收尾宽限（秒）：真实平台提交回执往返可达 client_timeout=40s，
    # 只给 1s 会把在途回执关在门外——该题永远停在 SENDING，分数丢失且
    # 正式赛一次性窗口无重赛机会。排空即返回，happy path 无额外墙钟。
    final_drain_seconds = 30.0
    # 资源守护节拍（秒）：终态工作区回收 + 磁盘水位检查的低频闸。
    housekeeping_interval_seconds = 60.0
    # 临时盘剩余低于该值记 disk_pressure 事件并强制回收一轮（平台 10GiB）。
    disk_pressure_free_bytes = 1.5 * 1024 * 1024 * 1024

    def __init__(self, harness, board, scheduler, worker,
                 evidence=None, metrics: Optional[Metrics] = None,
                 worker_name: str = "worker-1", lease_seconds: float = 60.0,
                 clock=time.time, hot_config=None, policy_engine=None, db=None,
                 state_store=None, flag_gate=None, gateway=None, reviewer_factory=None,
                 claim_timeout_seconds: float = 90.0, configured_max_workers: int = 1,
                 heartbeat_path: Optional[str] = None, max_consecutive_errors: int = 3,
                 retry_backoff_seconds: float = 0.0, poll_seconds: float = 0.1,
                 planner=None, max_intents_per_challenge: int = 4,
                 parallel_agents: int = 1, endgame_token_bonus: float = 0.0,
                 endgame_min_seconds: float = 300.0, best_effort_submit: bool = False):
        """编排器装配：持有黑板/调度器/Worker/Harness 及门控与提交网关。

        parallel_agents 决定 step() 循环线程数（题内互斥由调度器保证）；
        reviewer_factory 为提交前三门评审提供模型审阅者（llm 模式才有）。
        """
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
        # 并发执行（跨题并行、题内串行）：题内互斥由调度器 max_project_workers
        # 保证，这里只决定 step() 循环线程数。存储层所有读写已由 Blackboard
        # 的 RLock + BEGIN IMMEDIATE 串行化。
        self._parallel_agents = max(1, int(parallel_agents))
        self._stop = threading.Event()
        self._pump_lock = threading.Lock()
        # 终局加时：全部可解题做完仍有剩余时间时，给预算耗尽的题一次追加授权。
        # 授权记录（factory.endgame）是内存态：重启丢失可重授——累计记账仍诚实，
        # 全局预算仍兜底；每题每轮运行只授权一次（_endgame_granted）。
        self._endgame_bonus = max(0.0, float(endgame_token_bonus))
        self._endgame_min_seconds = max(0.0, float(endgame_min_seconds))
        self._endgame: Dict[str, float] = {}
        self._endgame_granted: set = set()
        # 平台 REJECTED 后的重试序号：strategy_key 唯一化（misc.flagretry.N），
        # 内存态即可——重启后新回执从 1 重新编号，attempt_key 去重兜底。
        self._flag_retry_seq: Dict[str, int] = {}
        # best-effort 提交（official-arena.yaml submission.best_effort_on_reject）：
        # gate 未确认（rejected/pending）但格式合法的候选仍入队，由平台真值仲裁。
        # 依据：正式平台错提交无计分惩罚（correct=False awarded=0），而 gate
        # 拒绝 ≠ 平台拒绝——v12 第 2 题 gate 四次拒绝零提交零得分。gate 决策
        # 行仍诚实落库（record 照记 rejected/pending），去重由网关幂等键兜底：
        # 同题同答不重发，垃圾候选上界 = 单题意图数。
        self._best_effort_submit = bool(best_effort_submit)
        factory = getattr(worker, "_control_factory", None)
        if callable(factory):
            factory.endgame = self._endgame
        # 资源守护时间闸（主监控线程独占访问，无需加锁）。
        self._last_housekeeping = 0.0
        self._last_snapshot = 0.0

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
        if budget.get("endgame_token_bonus") is not None:
            self._endgame_bonus = max(0.0, float(budget["endgame_token_bonus"]))
        controller = cfg.get("controller") or {}
        if controller.get("parallel_agents"):
            self._parallel_agents = max(1, int(controller["parallel_agents"]))
        runtime = cfg.get("runtime") or {}
        if runtime.get("max_intents_per_challenge") is not None:
            self._planner.set_limit(runtime["max_intents_per_challenge"])
        dl = cfg.get("download") or {}
        if self._policy is not None:
            self._policy.update_limits(dl.get("max_single_bytes", 0), dl.get("max_total_bytes", 0))

    def reload_config_if_changed(self) -> bool:
        """热配置文件变化且通过校验时应用新值；变化写入事件与快照（审计）。"""
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
    def _materialize_attachments(self, task) -> None:
        """把 harness 附件下载到工作区 handouts/，供 file_info/strings 等工具直接使用。

        幂等：同名且非空即跳过；单个附件失败只记事件，不阻塞读题。
        """
        refs = list(getattr(task, "attachments", ()) or ())
        if not refs or self._state is None:
            return
        reader = getattr(self._harness, "read_attachment", None)
        if reader is None:
            return
        try:
            hand = self._state.workspace(task.challenge_id) / "handouts"
        except Exception:
            return
        hand.mkdir(exist_ok=True, mode=0o700)
        import os as _os
        for ref in refs[:8]:
            name = _os.path.basename(str(ref).rsplit("/", 1)[-1]) or "attachment.bin"
            dest = hand / name
            if dest.exists() and dest.stat().st_size > 0:
                continue
            try:
                data = reader(ref, max_bytes=48 * 1024 * 1024)
            except Exception as exc:
                self._board.append_event("attachment_failed", task.challenge_id,
                                         {"ref": str(ref)[:200], "error": type(exc).__name__})
                continue
            if isinstance(data, str):
                data = data.encode("utf-8")
            tmp = dest.with_suffix(dest.suffix + ".part")
            with open(tmp, "wb") as fh:
                fh.write(bytes(data))
            _os.replace(tmp, dest)

    def bootstrap(self) -> List[Dict]:
        """启动 Harness、读题并登记题目与种子 Intent；返回本轮题目清单。"""
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
            self._materialize_attachments(task)
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
        """WorkerResult → B.2 契约校验的标准 dict（validate_worker_output 输入）。"""
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
                # 多 worker 并发时防重复提交：pump 的检查-发送-落账必须串行。
                with self._pump_lock:
                    pumped = self._gateway.pump()
                self._collect_submissions(pumped)
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
        """单题硬超时的标准化失败结果：只释放该题，不拖死整场。"""
        return WorkerResult(
            claim.intent_id, self._worker_name, claim.attempt_no, claim.challenge_id,
            claim.strategy_key, "failed", summary=["单题执行超时，已释放该题供后续重试"],
            outcome="timeout", failure_reason="claim_timeout",
            preconditions=[], actions=["执行单题"], ruled_out=[],
            next_best_action="缩小尝试范围或提高单题预算后重试",
            resource_usage={"wall_seconds": round(max(0.0, self._clock() - started), 3),
                            "model_tokens": 0, "tool_calls": 0},
        )

    def _ensure_environment(self, challenge_id) -> None:
        """按需拉起题目环境（env_policy=on_demand 时真正生效；其他适配器为 no-op）。

        失败只记事件不阻塞调度：意图会带着"环境不可用"快速失败，槽位回收后
        下一个重试意图会再次尝试拉起。
        """
        hook = getattr(self._harness, "ensure_environment", None)
        if hook is None:
            return
        try:
            info = hook(challenge_id)
        except Exception as exc:
            self._board.append_event("environment_failed", challenge_id,
                                     {"error": type(exc).__name__, "detail": str(exc)[:160]})
            return
        # 新启动的环境带出 container_addr：同步进编排层缓存的题目描述（模型唯一可读面）。
        describe = getattr(self._harness, "task_description", None)
        if isinstance(info, dict) and describe is not None:
            try:
                fresh = describe(challenge_id)
                cached = self._challenges.get(challenge_id)
                if fresh and cached is not None and fresh != cached.get("description"):
                    cached["description"] = fresh
            except Exception as exc:
                self._board.append_event("description_refresh_failed", challenge_id,
                                         {"error": type(exc).__name__})

    def _execute_claim(self, claim):
        """执行一个已领取 Intent；拆成执行/契约/校验/落库/后处理五段。"""
        print("[orch] claim ch={} intent={} strategy={}".format(
            claim.challenge_id, claim.intent_id, claim.strategy_key),
            file=sys.stderr, flush=True)
        self._ensure_environment(claim.challenge_id)
        challenge = self._challenge_context(claim)
        started = self._clock()
        result = self._run_worker(claim, challenge, started)
        if self._should_break_provider_errors(claim, result):
            self._board.append_event("provider_error_exhausted", claim.intent_id, {
                "challenge_id": claim.challenge_id,
                "attempts": int(getattr(claim, "attempt_no", 0) or 0),
                "failure_reason": result.failure_reason,
            })
            result.status = "blocked"
            result.failure_reason = "provider_error_exhausted ({} attempts): {}".format(
                getattr(claim, "attempt_no", 0), result.failure_reason)
        print("[orch] result ch={} status={} outcome={} reason={}".format(
            claim.challenge_id, result.status, result.outcome,
            result.failure_reason or "-"), file=sys.stderr, flush=True)
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
        """调 Worker 执行：硬超时用 DeadlineGuard（SIGALRM）可中断；异常转可重试失败。"""
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

    def _should_break_provider_errors(self, claim, result) -> bool:
        """连续 Provider/配置类失败熔断：同一 intent 第 3 次领取仍失败即 blocked。

        只熔断 ProviderError/ProviderConfigError（模型端点性失败，重试同参
        大概率重演）；timeout/rate_limited 等瞬态失败保持原重试语义。
        """
        if result.status != "failed" or result.outcome != "error":
            return False
        if str(result.failure_reason or "") not in ("ProviderError", "ProviderConfigError"):
            return False
        return int(getattr(claim, "attempt_no", 0) or 0) >= self.provider_error_max_attempts

    def _validate_contract(self, claim, result) -> None:
        """B.2 契约校验：不合格输出降级为可重试失败，清空候选答案与事实。"""
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
        """累计资源用量到进程内指标（token/工具调用/耗时）。"""
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
        """候选答案过三门 FlagGate：confirmed 才会进入提交队列；超时按失败处理。"""
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
                print("[gate] ch={} intent={} decision=timeout reason=gate budget exhausted".format(
                    claim.challenge_id, claim.intent_id), file=sys.stderr, flush=True)
        # [gate] 观测行：gate 判定完全在 DB 里，stderr 不可见——2026-11-21
        # 正式赛第 2 题 gate 连拒四次却无任何诊断线索。flag 原文可记日志
        # （凭据才脱敏）。无候选/无门控也要打印，凑齐完整判定链。
        if decision is not None:
            print("[gate] ch={} intent={} decision={} level={} reason={} answer={} best_effort={}".format(
                claim.challenge_id, claim.intent_id, decision.status, decision.level,
                decision.reason or "-", decision.answer or "-", self._best_effort_submit),
                file=sys.stderr, flush=True)
        elif result.candidate_answer:
            print("[gate] ch={} intent={} decision=none reason=gate or gateway not configured answer={}".format(
                claim.challenge_id, claim.intent_id, result.candidate_answer),
                file=sys.stderr, flush=True)
        if decision is not None and decision.status == "confirmed":
            # Gate 已确认时 Intent 必须终止；否则 Worker 写 partial 会让同一候选
            # 在租约释放后再次被领取，重复消耗预算。
            result.status = "completed"
        return decision

    def _persist_result(self, claim, result, decision):
        """结果幂等落库；Gate 决策与提交入队在同一事务内原子提交。"""
        key = "{}-{}".format(claim.intent_id, claim.attempt_no)
        submission_keys = []

        def persist_gate(db):
            """事务钩子：Gate 决策落库；确认态题同步入提交 Outbox（同事务原子）。

            best_effort 开启时，rejected/pending 决策也入队（gateway.enqueue
            校验格式与 gate 行存在性，ValueError 视为不可提交，不影响落库）。"""
            if decision is not None:
                self._gate.record(db, decision)
                if decision.status == "confirmed":
                    submission_keys.append(self._gateway.enqueue(db, decision))
                elif self._best_effort_submit and decision.status in ("rejected", "pending"):
                    try:
                        submission_keys.append(self._gateway.enqueue(db, decision, best_effort=True))
                    except ValueError:
                        self._metrics.incr("best_effort.format_rejected")

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
        """结果写库失败时的标准化响应（write_failed 状态）。"""
        return {"intent_id": claim.intent_id, "challenge_id": claim.challenge_id,
                "status": "write_failed", "outcome": result.outcome,
                "candidate_answer": None, "submission": None, "gate": None,
                "write_error": write_error}

    def _record_side_effects(self, claim, challenge, result, decision, receipt) -> None:
        """结果落库后的副作用：教训蒸馏、Gate 事件、事实写入与指标更新。"""
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
        """按结果规划后续 Intent（no_signal/被拒时扩展）；失败不影响已落库结果。"""
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
        """查询已被受理的题目集合（读视图，主循环用于判停）。"""
        return self._board.accepted_challenge_ids()

    def _collect_submissions(self, events):
        """把提交网关回执转成指标/延迟观测；REJECTED 触发题目重开续解。"""
        for event in events:
            status = event.get("status", "unknown")
            self._metrics.incr("submissions." + str(status).lower())
            self._metrics.observe("submissions.latency_seconds", float(event.get("latency_seconds") or 0))
            if status == "REJECTED":
                try:
                    self._plan_flag_retry(event)
                except Exception as exc:
                    # 重开失败只记事件：回执本身已落库，不能拖垮 pump 主流程。
                    self._board.append_event("flag_retry_failed", str(event.get("challenge_id") or ""),
                                             {"error": type(exc).__name__})

    def _plan_flag_retry(self, event) -> None:
        """平台判 REJECTED 后重开题目并规划换路 followup。

        Gate 三门确认 ≠ 平台真值（2026-11-21 正式赛第 2 题错旗通过三门却被
        平台判错的教训）；错旗提交不能把会话永久钉死在 confirmed——调度器
        排除 confirmed 题目、SessionControl 对 confirmed/stopped 抛
        'session closed'，必须显式复位。重开 = 会话回 ready + 新步数窗口 +
        misc.flagretry.N 意图。被拒 flag 原文放 intent params：lessons 层
        脱敏会抹掉 flag（flag{redacted}），Worker 需要原文才能避开重提。
        """
        challenge_id = str(event.get("challenge_id") or "")
        if not challenge_id or self._state is None:
            return
        if challenge_id in self._board.accepted_challenge_ids():
            return  # 回执竞态：平台终态以 ACCEPTED 为准，不重开
        def _reopen(s):
            if s.get("status") in ("confirmed", "stopped"):
                s["status"] = "ready"
        self._state.patch(challenge_id, _reopen)
        from .control import reset_intent_window
        reset_intent_window(self._state, challenge_id, revive_stopped=True)
        count = self._board.count_intents(challenge_id)
        limit = int(getattr(self._planner, "max_intents_per_challenge", 4) or 4)
        if count >= limit:
            self._board.append_event("planner_cap_reached", "", {
                "challenge_id": challenge_id, "intents": count,
                "max_intents_per_challenge": limit})
            return
        seq = self._flag_retry_seq.get(challenge_id, 0) + 1
        self._flag_retry_seq[challenge_id] = seq
        strategy_key = "misc.flagretry.{}".format(seq)
        rejected_flag = str(event.get("answer") or "")
        draft = IntentDraft(
            challenge_id=challenge_id,
            strategy_key=strategy_key,
            params={
                "phase": "flag_retry",
                "reason": "platform_rejected",
                "rejected_flag": rejected_flag,
                "instruction": "平台判定上次提交的 flag 错误（correct=false，原文见 rejected_flag）。"
                               "不要重复提交同一答案；回到题目环境核对该 flag 的来源"
                               "（文件版本/服务状态可能已变化），换证据路径重新定位真实 flag。",
            },
            est_tokens=2000)
        try:
            intent_id = self._board.add_intent(draft)
        except DuplicateStrategy:
            return  # 已有同名意图在排队；会话重开已生效
        self._board.append_event("flag_retry_planned", intent_id, {
            "challenge_id": challenge_id, "strategy_key": strategy_key,
            "rejected_flag": rejected_flag})

    def _drain_gateway(self) -> None:
        """运行收尾：有界宽限内排空在途提交，然后关网关。

        flush 排空 inflight 即返回；宽限耗尽才放弃（残留行留在 SENDING，
        由 recover_legacy 挂查询）。异常只记事件，不阻断 finalize。
        """
        try:
            self._collect_submissions(self._gateway.flush(timeout=self.final_drain_seconds))
        except Exception as exc:
            self._board.append_event("gateway_flush_failed", "", {"error": type(exc).__name__})
        self._gateway.close()

    # -------------------------------------------------------------- resources
    def _housekeeping(self, force: bool = False) -> None:
        """低频资源守护：终态工作区回收 + 磁盘水位 + 内存快照。

        平台给一次答题的资源是 4 核/6GiB/10GiB 临时盘（官方建议大文件放
        /tmp）；4h × 27 题连续运行时，完成题的 handouts 与工具产物不及时
        回收会侵蚀磁盘，拖慢后续题目的附件物化。只在主监控线程运行，
        时间闸保证对主循环的开销可忽略。
        """
        now = time.monotonic()
        if not force and now - self._last_housekeeping < self.housekeeping_interval_seconds:
            return
        self._last_housekeeping = now
        self._cleanup_finished_workspaces()
        free = None
        try:
            if self._state is not None:
                free = shutil.disk_usage(str(self._state.root)).free
        except Exception:
            free = None
        if free is not None and free < self.disk_pressure_free_bytes:
            # 水位告急：再强制清一轮（_cleanup_finished_workspaces 幂等）。
            self._board.append_event("disk_pressure", "", {"free_mb": int(free / 1e6)})
            self._cleanup_finished_workspaces()
        if now - self._last_snapshot >= 10 * self.housekeeping_interval_seconds:
            self._last_snapshot = now
            snapshot = {"disk_free_mb": None, "rss_mb": None}
            if free is not None:
                snapshot["disk_free_mb"] = int(free / 1e6)
            try:
                import resource as _resource
                raw = _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss
                # linux 单位 KB，darwin 单位字节；仅作内存泄漏观察指标。
                snapshot["rss_mb"] = int(raw / 1024 if sys.platform != "darwin" else raw / (1024 * 1024))
            except Exception:
                pass
            self._board.append_event("resource_snapshot", "", snapshot)

    def _cleanup_finished_workspaces(self) -> None:
        """删除已终结题目的工作区（handouts + 工具产物），幂等。

        三条件同时成立才回收：有提交记录、无 DRAFT/SENDING 在途提交
        （重派发需校验工作区内的声明与证据）、无 READY/RUNNING/
        FAILED_RETRYABLE 意图（后续尝试还可能用到）。工作区路径是
        workspace_root 下的 sha256 名目录，不存在路径穿越。
        """
        if self._state is None:
            return
        try:
            rows = self._board._db.execute(
                "SELECT DISTINCT challenge_id FROM submissions WHERE challenge_id NOT IN"
                " (SELECT challenge_id FROM submissions WHERE status IN ('DRAFT','SENDING'))"
            ).fetchall()
            live = {r[0] for r in self._board._db.execute(
                "SELECT DISTINCT challenge_id FROM intents_ext"
                " WHERE status IN ('READY','RUNNING','FAILED_RETRYABLE')").fetchall()}
        except Exception as exc:
            self._board.append_event("cleanup_error", "", {"error": type(exc).__name__})
            return
        freed_mb = 0.0
        for (cid,) in rows:
            if not isinstance(cid, str) or not cid or cid in live:
                continue
            path = self._state.workspace_path(cid)
            if path.is_symlink() or not path.is_dir():
                continue
            try:
                total = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
                shutil.rmtree(path, ignore_errors=True)
            except Exception as exc:
                self._board.append_event("cleanup_error", cid, {"error": type(exc).__name__})
                continue
            if path.exists():
                continue  # 删除失败（占用等）：不强抢，下轮重试
            freed_mb += total / 1e6
            self._metrics.incr("resources.workspaces_reclaimed")
            self._board.append_event("workspace_reclaimed", cid,
                                     {"freed_mb": round(total / 1e6, 1)})
        if freed_mb:
            self._metrics.observe("resources.workspace_freed_mb", freed_mb)

    # ---------------------------------------------------------------- run loop
    def run(self, max_seconds: float = 120.0) -> Dict:
        """主编排循环：引导 → 回收租约 → 串行或并行执行 → 排空提交 → 汇总报告。

        退出条件：墙钟到 deadline、全部题目受理、或全局预算耗尽；返回
        RunSummary 数据（任务/结果/指标/质量复盘/执行模式）。
        """
        deadline = self._clock() + max_seconds
        factory = getattr(self._worker, "_control_factory", None)
        if factory is not None:
            factory.deadline = deadline
        global_budget = getattr(self._worker, "_global_budget", None)
        self._touch_heartbeat()
        print("[orch] run start worker={} deadline={}s parallel={} "
              "claim_timeout={}s backoff={}s".format(
                  self._worker_name, max_seconds, self._parallel_agents,
                  self._claim_timeout, self._retry_backoff),
              file=sys.stderr, flush=True)
        tasks = self.bootstrap()
        try:
            recover_expired_leases(self._board)  # 启动即恢复未完成租约（§17.4）
        except Exception as exc:
            self._board.append_event("watchdog_failed", "", {"error": type(exc).__name__})
        results: List[Dict] = []
        solved = self._solved()
        mode, effective = "serial", 1
        if self._parallel_agents > 1 and len(tasks) > 1:
            # 跨题并行、题内串行：N 个线程各自领取执行，同题互斥由调度器
            # max_project_workers 保证；存储层由黑板 RLock 串行化。
            mode = "parallel"
            effective = min(self._parallel_agents, len(tasks))
            self._spawn_workers(deadline, tasks, results, solved, effective)
        else:
            self._run_serial(deadline, tasks, results, solved, global_budget)
        if self._gateway is not None:
            self._drain_gateway()
        self._housekeeping(force=True)  # 运行收尾：回收全部终态工作区
        solved = self._solved()
        print("[orch] run loop finished: tasks={} solved={} results={}".format(
            len(tasks), sorted(solved), len(results)), file=sys.stderr, flush=True)
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
                "execution": {"mode": mode, "configured_max_workers": self._configured_max_workers,
                              "effective_parallel": effective,
                              "claim_timeout_seconds": self._claim_timeout,
                              "retry_backoff_seconds": self._retry_backoff,
                              "isolation": {"agent": "per_challenge_workspace",
                                            "container": "not_verified"}}}

    # ---------------------------------------------------------------- run loops
    def _run_serial(self, deadline, tasks, results, solved, global_budget) -> None:
        """串行主循环（parallel_agents=1 或单题时）：行为与历史版本一致。"""
        consecutive_errors = 0
        while self._clock() < deadline and len(solved) < len(tasks):
            if global_budget is not None and getattr(global_budget, "exhausted", False):
                # 运行级预算耗尽：停止领取新意图；已领取意图由租约超时自然收尾。
                self._board.append_event("run_stopped", "",
                                         {"reason": "global_budget_exhausted",
                                          "spent": int(getattr(global_budget, "spent", 0) or 0)})
                break
            self._touch_heartbeat()
            try:
                self.reload_config_if_changed()
                self._housekeeping()
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
                # 终局加时：没有待领工作但时间还有富余时，给预算耗尽的题一次追加授权。
                if self._grant_endgame(tasks, solved, deadline):
                    continue
                break
            results.append(out)
            if out["submission"] and out["submission"]["status"] == "ACCEPTED":
                solved.add(out["challenge_id"])
            self._maybe_record_solution(out)

    def _spawn_workers(self, deadline, tasks, results, solved, count: int) -> None:
        """并行主循环：N 个工作线程跑 step()，主线程负责心跳/热配置/看门狗/加时。"""
        threads = []
        for idx in range(count):
            t = threading.Thread(target=self._worker_loop,
                                 args=(deadline, tasks, results, solved, idx),
                                 name="{}-{}".format(self._worker_name, idx + 1), daemon=True)
            threads.append(t)
        for t in threads:
            t.start()
        try:
            budget_stop_notified = False
            while any(t.is_alive() for t in threads):
                if (self._clock() >= deadline
                        or (getattr(self._worker, "_global_budget", None) is not None
                            and getattr(self._worker._global_budget, "exhausted", False))):
                    if not budget_stop_notified:
                        spent = getattr(getattr(self._worker, "_global_budget", None), "spent", 0)
                        self._board.append_event("run_stopped", "", {
                            "reason": "deadline_or_global_budget",
                            "spent": int(spent or 0)})
                        budget_stop_notified = True
                    self._stop.set()
                elif len(solved) < len(tasks):
                    self._touch_heartbeat()
                    try:
                        # 热配置与租约看门狗只在监控线程执行，避免多线程竞争。
                        self.reload_config_if_changed()
                        recover_expired_leases(self._board)
                        self._housekeeping()
                        self._grant_endgame(tasks, solved, deadline)
                    except Exception as exc:
                        self._board.append_event("monitor_error", "", {"error": type(exc).__name__,
                            "detail": repr(exc)[:300]})
                if not any(t.is_alive() for t in threads):
                    break
                time.sleep(min(self._poll_seconds, max(0.0, deadline - self._clock())))
        finally:
            self._stop.set()
            for t in threads:
                t.join(timeout=5)

    def _worker_loop(self, deadline, tasks, results, solved, idx) -> None:
        """单个工作线程：只领意图与执行；错误止损按线程独立计数。"""
        consecutive_errors = 0
        while (not self._stop.is_set() and self._clock() < deadline
               and len(solved) < len(tasks)):
            self._touch_heartbeat()
            try:
                out = self.step()
            except KeyboardInterrupt:
                self._stop.set()
                break
            except Exception as exc:
                consecutive_errors += 1
                self._board.append_event("step_failed", "", {
                    "worker": "{}-{}".format(self._worker_name, idx + 1),
                    "error": type(exc).__name__, "consecutive": consecutive_errors})
                self._metrics.incr("runtime.step_errors")
                if consecutive_errors >= self._max_consecutive_errors:
                    self._board.append_event("run_aborted", "", {
                        "reason": "consecutive_errors", "count": consecutive_errors})
                    self._stop.set()
                continue
            consecutive_errors = 0
            if out is None:
                time.sleep(min(self._poll_seconds, max(0.0, deadline - self._clock())))
                continue
            results.append(out)
            if out["submission"] and out["submission"]["status"] == "ACCEPTED":
                solved.add(out["challenge_id"])
            self._maybe_record_solution(out)

    def _maybe_record_solution(self, out) -> None:
        """平台确认解出后蒸馏整题解题思路入 solutions 表；失败只记事件不阻断。

        submission 可能是 sqlite.Row：统一转 dict；思路蒸馏与复用检索共用
        store/solutions.py（FLAG 脱敏复用 lessons sanitizer）。
        """
        submission = out.get("submission") if isinstance(out, dict) else None
        if not submission or submission["status"] != "ACCEPTED":
            return
        try:
            payload = dict(submission)
            with self._board.transaction() as db:
                solution_store.record_solution(db, str(out["challenge_id"]), payload, commit=False)
            self._board.append_event("solution_recorded", str(out["challenge_id"]), {})
        except Exception as exc:
            try:
                self._board.append_event("solution_record_failed", str(out.get("challenge_id") or ""),
                                         {"error": type(exc).__name__})
            except Exception:
                pass  # 记忆功能 fail-open：不能影响主循环

    def _grant_endgame(self, tasks, solved, deadline) -> bool:
        """终局加时：可解题做完且时间富余时，给预算耗尽的题授权一次追加额度。

        只对会话已 stopped（预算/步数/停滞耗尽）且未授权过的题生效；授权 =
        factory.endgame 提高上限 + 新增 misc.endgame.pass2 意图。返回是否发生
        了新授权（调用方据此继续循环）。
        """
        if self._endgame_bonus <= 0 or self._state is None or self._clock() >= deadline:
            return False
        if deadline - self._clock() < self._endgame_min_seconds:
            return False  # 剩余时间不够一轮有意义的尝试
        if self._scheduler.has_waiting_work():
            return False  # 还有正常工作没做完，加时留给收尾阶段
        factory = getattr(self._worker, "_control_factory", None)
        if not callable(factory):
            return False
        grants = getattr(factory, "endgame", None)
        if grants is None:
            grants = self._endgame
            factory.endgame = grants
        granted = False
        # tasks 元素是 ChallengeInput 对象（不可排序、不是 id），统一取 challenge_id。
        ids = sorted(getattr(t, "challenge_id", str(t)) for t in tasks)
        for cid in ids:
            if cid in solved or cid in self._endgame_granted:
                continue
            try:
                _, st = self._state.load(cid)
            except Exception:
                continue
            if st.get("status") != "stopped":
                # v12 教训（2026-11-21 正式赛第 2 题）：gate 拒绝的题会话是
                # ready（非 stopped），planner 上限打满后无新意图也不满足
                # 加时条件 → run 提前退出、整题零提交。放行"规划上限已打
                # 满"的未解题（剩余时间富余时），授权一次终局加时重试。
                if self._board.count_intents(cid) < self._planner.max_intents_per_challenge:
                    continue  # 未耗尽预算且还有规划余量的题让 planner 正常接手
            try:
                self._board.add_intent(IntentDraft(
                    challenge_id=cid, strategy_key="misc.endgame.pass2",
                    params={"phase": "endgame"}, est_tokens=2000))
            except DuplicateStrategy:
                self._endgame_granted.add(cid)  # 已有终局意图在排队，不再重复授权
                continue
            grants[cid] = self._endgame_bonus
            self._endgame_granted.add(cid)
            # 授权必须复位会话：否则调度器把 stopped 的题永久排除，
            # pass2 意图永远 READY 无人认领（endgame 失效的根因）。
            try:
                from .control import reset_intent_window
                reset_intent_window(self._state, cid, revive_stopped=True)
            except Exception:
                pass
            self._board.append_event("endgame_grant", cid, {
                "token_bonus": self._endgame_bonus,
                "remaining_seconds": int(deadline - self._clock())})
            self._metrics.incr("endgame.grants")
            granted = True
        return granted

    def _touch_heartbeat(self) -> None:
        """刷新进程心跳文件（供外部 supervisor 判活；未配置时为 no-op）。"""
        if self._heartbeat is not None:
            self._heartbeat.touch()
