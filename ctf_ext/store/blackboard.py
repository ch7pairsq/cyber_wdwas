"""共享黑板：Intent 租约领取、幂等写入、策略去重（§5、§7、附录 B.1、D.1–D.4）。

实现的不变量（附录 D.1）：
1. 同一 Intent 任一时刻最多一个有效租约持有者（BEGIN IMMEDIATE 前置写锁）；
2. 同一 idempotency_key 的结果只落库一次，重复提交返回首次回执；
3. 租约过期可被 runtime/watchdog 回收；
4. 过期/错主的写入被拒绝（stale_write）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..domain.models import (
    OUTCOME_NO_SIGNAL,
    OUTCOME_SUCCESS,
    build_attempt_key,
    canonical_params,
    new_id,
)

READY = "READY"
RUNNING = "RUNNING"
COMPLETED = "COMPLETED"
FAILED_RETRYABLE = "FAILED_RETRYABLE"
FAILED = "FAILED"

_SUCCESS_LIKE = (OUTCOME_SUCCESS, OUTCOME_NO_SIGNAL)


class DuplicateStrategy(Exception):
    """同策略去重命中：携带冲突的 attempt_key 与阻塞来源（intent 或 attempt）。"""
    def __init__(self, attempt_key: str, blocked_by: str):
        """blocked_by 标识阻塞来源（intent/attempt），便于调度层决策。"""
        super().__init__("duplicate strategy {} blocked by {}".format(attempt_key, blocked_by))
        self.attempt_key = attempt_key
        self.blocked_by = blocked_by


class StaleWrite(Exception):
    """过期/错主写入：租约已过期、owner 不匹配或 attempt_no 不符时抛出。"""
    pass


class EvidenceNotRegistered(Exception):
    """引用未登记/哈希不匹配的证据（附录 D.1-3：写入 Fact 前证据必须已存在）。"""
    def __init__(self, evidence_ids):
        """携带全部未登记/哈希不符的证据 id 供修复。"""
        super().__init__("evidence not registered or hash mismatch: {}".format(list(evidence_ids)))
        self.evidence_ids = list(evidence_ids)


@dataclass
class IntentDraft:
    """新 Intent 草稿：策略、参数、优先级与预算估算，入库前生成 attempt_key 去重。"""
    challenge_id: str
    strategy_key: str
    params: Dict[str, Any] = field(default_factory=dict)
    priority: float = 0.0
    est_tokens: int = 0
    points_hint: float = 0.0
    budget_seconds: float = 0.0
    tool_version: str = ""


@dataclass
class Claim:
    """一次成功领取的租约凭据：attempt_no 用于释放/写结果时的乐观校验。"""
    intent_id: str
    challenge_id: str
    strategy_key: str
    params: Dict[str, Any]
    priority: float
    attempt_no: int
    lease_until: float
    budget_seconds: float = 0.0


@dataclass
class WorkerResult:
    """Worker 输出的标准封装：契约校验通过后经 write_result 幂等落库。"""
    intent_id: str
    owner: str
    attempt_no: int
    challenge_id: str
    strategy_key: str
    status: str  # completed | partial | blocked | failed
    summary: List[str] = field(default_factory=list)
    outcome: str = OUTCOME_NO_SIGNAL
    failure_reason: str = ""
    preconditions: List[str] = field(default_factory=list)
    actions: List[str] = field(default_factory=list)
    ruled_out: List[str] = field(default_factory=list)
    next_best_action: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    candidate_answer: Optional[str] = None
    facts: List[Dict[str, Any]] = field(default_factory=list)
    resource_usage: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WriteReceipt:
    """写结果回执：first=False 表示幂等命中，返回首次 attempt_id。"""
    first: bool
    idempotency_key: str
    attempt_id: Optional[str] = None


def compute_priority(draft: IntentDraft) -> float:
    """期望得分/Token 比（§23.2-4：按性价比排序，而不是按题号）。"""
    if draft.priority:
        return draft.priority
    return draft.points_hint / max(draft.est_tokens, 1)


class Blackboard:
    """黑板：意图/租约/尝试/事实/提交的共享状态库，全部写操作走事务与乐观校验。"""
    def __init__(self, db: sqlite3.Connection, lease_default: float = 180.0, evidence_check=None,
                 db_lock: threading.RLock = None):
        """db 为共享 SQLite 连接；lease_default 为租约时长；evidence_check 用于证据引用校验。

        db_lock：与 EvidenceStore 等其他同一连接的持有者共享同一把 RLock。
        生产装配传 evidence.db_lock；不传则自建（与 EvidenceStore 默认锁
        不同把——仅适合单线程场景，生产必须共享否则连接并发未定义行为）。
        """
        self._db = db
        self._lock = db_lock if db_lock is not None else threading.RLock()
        self._lease_default = lease_default
        # evidence_check: evidence_id -> bool（通常是 EvidenceStore.verify）；
        # 配置后所有结果引用的证据都必须通过校验（D.1-3）
        self._evidence_check = evidence_check

    @property
    def connection(self) -> sqlite3.Connection:
        """只读连接入口：跨模块统计/审计使用；写操作必须走 transaction()。"""
        return self._db

    @contextmanager
    def transaction(self):
        """公开写事务：BEGIN IMMEDIATE + 锁 + 失败回滚（附录 D.1）。"""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    # 兼容既有调用方与测试；新代码统一使用 transaction()。
    _write = transaction

    @contextmanager
    def read_view(self):
        """共享读视图：与写事务共用同一把 RLock，保证共享连接上读写不交叠。

        SQLite 连接是跨线程共享的（check_same_thread=False），读不与
        BEGIN IMMEDIATE 并发是正确性前提；LLM 调用占墙钟，毫秒级读锁
        竞争可忽略。命名避开既有 snapshot(challenge_id)（handoff 视图）。
        """
        with self._lock:
            yield self._db

    # ---------------------------------------------------------------- intents
    def add_challenge(self, challenge) -> None:
        """登记题目（幂等）：重复导入同一题目不覆盖已有元数据。"""
        with self.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO challenges(id, name, category, description, attachments, points_hint,"
                " difficulty_level, metadata, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    challenge.id, challenge.name, challenge.category, challenge.description,
                    json.dumps(list(challenge.attachments)), challenge.points_hint,
                    challenge.difficulty_level, json.dumps(challenge.metadata), time.time(),
                ),
            )

    def add_intent(self, draft: IntentDraft) -> str:
        """发布探索方向；attempt_key 重复时拒绝（§7 去重、D.3）。"""
        akey = build_attempt_key(draft.challenge_id, draft.strategy_key, draft.params, draft.tool_version)
        with self.transaction() as db:
            row = db.execute(
                "SELECT id, outcome FROM attempts WHERE attempt_key=? AND outcome IN (?,?) LIMIT 1",
                (akey, OUTCOME_SUCCESS, OUTCOME_NO_SIGNAL),
            ).fetchone()
            if row:
                raise DuplicateStrategy(akey, "attempt:" + row["id"])
            row = db.execute(
                "SELECT id FROM intents_ext WHERE attempt_key=? AND status IN"
                " ('READY','RUNNING','FAILED_RETRYABLE') LIMIT 1",
                (akey,),
            ).fetchone()
            if row:
                raise DuplicateStrategy(akey, "intent:" + row["id"])
            intent_id = new_id("intent")
            now = time.time()
            db.execute(
                "INSERT INTO intents_ext(id, challenge_id, strategy_key, attempt_key, params, priority,"
                " est_tokens, points_hint, budget_seconds, status, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?, 'READY', ?, ?)",
                (
                    intent_id, draft.challenge_id, draft.strategy_key, akey,
                    canonical_params(draft.params), compute_priority(draft), draft.est_tokens,
                    draft.points_hint, draft.budget_seconds, now, now,
                ),
            )
            return intent_id

    def claim_intent(self, worker: str, lease_seconds: Optional[float] = None,
                     challenge_ids: Optional[Sequence[str]] = None,
                     max_project_workers=None, max_running_projects=None,
                     retry_backoff_seconds: float = 0.0) -> Optional[Claim]:
        """领取一个可执行 Intent；无可用任务返回 None（附录 B.1）。

        FAILED_RETRYABLE 在退避窗口内不可领取，避免故障时热循环和单题饿死。
        """
        now = time.time()
        lease = lease_seconds or self._lease_default
        with self.transaction() as db:
            sql = ("SELECT id FROM intents_ext WHERE status IN ('READY','FAILED_RETRYABLE')"
                   " AND (lease_until IS NULL OR lease_until < ?)")
            args: List[Any] = [now]
            if retry_backoff_seconds and retry_backoff_seconds > 0:
                sql += " AND (status='READY' OR updated_at <= ?)"
                args.append(now - float(retry_backoff_seconds))
            if challenge_ids:
                sql += " AND challenge_id IN ({})".format(",".join("?" * len(challenge_ids)))
                args.extend(challenge_ids)
            if max_project_workers is not None:
                sql += " AND (SELECT COUNT(*) FROM intents_ext r WHERE r.challenge_id=intents_ext.challenge_id AND r.status='RUNNING' AND r.lease_until>?)<?"
                args.extend([now, max_project_workers])
            if max_running_projects is not None:
                sql += " AND ((SELECT COUNT(DISTINCT r.challenge_id) FROM intents_ext r WHERE r.status='RUNNING' AND r.lease_until>?)<? OR EXISTS(SELECT 1 FROM intents_ext r WHERE r.challenge_id=intents_ext.challenge_id AND r.status='RUNNING' AND r.lease_until>?))"
                args.extend([now, max_running_projects, now])
            sql += " ORDER BY priority DESC, est_tokens ASC, created_at ASC LIMIT 1"
            row = db.execute(sql, args).fetchone()
            if not row:
                return None
            cur = db.execute(
                "UPDATE intents_ext SET owner=?, lease_until=?, attempt_no=attempt_no+1,"
                " status='RUNNING', updated_at=? WHERE id=? AND status IN ('READY','FAILED_RETRYABLE')"
                " AND (lease_until IS NULL OR lease_until < ?)",
                (worker, now + lease, now, row["id"], now),
            )
            if cur.rowcount != 1:  # changes()==1 才算领取成功（D.2）
                return None
            r = db.execute("SELECT * FROM intents_ext WHERE id=?", (row["id"],)).fetchone()
            return Claim(
                intent_id=r["id"], challenge_id=r["challenge_id"], strategy_key=r["strategy_key"],
                params=json.loads(r["params"]), priority=r["priority"], attempt_no=r["attempt_no"],
                lease_until=r["lease_until"], budget_seconds=r["budget_seconds"],
            )

    def heartbeat(self, intent_id: str, worker: str, extend: float = 120.0) -> bool:
        """续约租约：只对在租且未过期的 Intent 生效，失败表示租约已丢失。"""
        with self.transaction() as db:
            cur = db.execute(
                "UPDATE intents_ext SET lease_until=MAX(lease_until, ?), updated_at=?"
                " WHERE id=? AND owner=? AND status='RUNNING' AND lease_until>?",
                (time.time() + extend, time.time(), intent_id, worker, time.time()),
            )
            return cur.rowcount == 1

    def release(self, intent_id: str, worker: str, outcome: str,
                attempt_no: Optional[int] = None) -> None:
        """释放租约：成功类结果置 COMPLETED，否则置 FAILED_RETRYABLE 待重试。"""
        status = {"success": COMPLETED, "no_signal": COMPLETED}.get(outcome, FAILED_RETRYABLE)
        sql = ("UPDATE intents_ext SET status=?, owner=NULL, lease_until=NULL, note=?, updated_at=?"
               " WHERE id=? AND owner=? AND status='RUNNING'")
        args = (status, "released:" + outcome, time.time(), intent_id, worker)
        if attempt_no is not None:
            # 多个进程可能共用 llm/rule-based 名称，必须带 attempt_no 才能
            # 避免释放另一个进程刚领取的同一 Intent。
            sql += " AND attempt_no=?"
            args += (int(attempt_no),)
        with self.transaction() as db:
            db.execute(sql, args)

    def list_open(self, challenge_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """列出开放 Intent（READY/RUNNING/FAILED_RETRYABLE），按优先级倒序。"""
        sql = "SELECT * FROM intents_ext WHERE status IN ('READY','RUNNING','FAILED_RETRYABLE')"
        args: tuple = ()
        if challenge_id:
            sql += " AND challenge_id=?"
            args = (challenge_id,)
        sql += " ORDER BY priority DESC"
        with self.read_view() as db:
            return [dict(r) for r in db.execute(sql, args)]

    def open_intent_summary(self) -> List[Dict[str, Any]]:
        """每题开放 Intent 的聚合视图，供调度器一次查询完成背压判断。"""
        with self.read_view() as db:
            rows = db.execute(
                "SELECT challenge_id,"
                " MAX(CASE WHEN status IN ('READY','FAILED_RETRYABLE') THEN priority END) AS max_priority,"
                " SUM(CASE WHEN status='RUNNING' THEN 1 ELSE 0 END) AS running,"
                " SUM(CASE WHEN status IN ('READY','FAILED_RETRYABLE') THEN 1 ELSE 0 END) AS ready"
                " FROM intents_ext WHERE status IN ('READY','RUNNING','FAILED_RETRYABLE')"
                " GROUP BY challenge_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def challenge_ids_with_work(self) -> List[str]:
        """仍有 READY/RUNNING/退避重试 Intent 的题目（不含已确认终态）。"""
        with self.read_view() as db:
            rows = db.execute(
                "SELECT DISTINCT challenge_id FROM intents_ext"
                " WHERE status IN ('READY','RUNNING','FAILED_RETRYABLE')"
                " ORDER BY challenge_id"
            ).fetchall()
        return [row["challenge_id"] for row in rows]

    def count_intents(self, challenge_id: str) -> int:
        """该题累计 Intent 数（含已完成），用于 Planner 上限。"""
        with self.read_view() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM intents_ext WHERE challenge_id=?", (challenge_id,)
            ).fetchone()
        return int(row["n"] or 0)

    def accepted_challenge_ids(self) -> set:
        """已获平台 ACCEPTED 的题目集合（调度层直接排除重解）。"""
        with self.read_view() as db:
            rows = db.execute(
                "SELECT DISTINCT challenge_id FROM submissions WHERE status='ACCEPTED'"
            ).fetchall()
        return {row["challenge_id"] for row in rows}

    def confirmed_challenge_ids(self) -> set:
        """已过三门确认且平台未判错的题（调度器排除，不空转重解）。

        v12 REJECTED 重开语义：gate 曾 confirmed 但平台已判 REJECTED 的题
        必须重新可领（flagretry 意图在队），否则重开意图永远卡在 READY、
        has_waiting_work 永真、endgame 永不触发（2026-11-21 8 题模拟发现
        ch-sim-5 卡死）。判定口径：该题最新 submission 状态为 REJECTED →
        不进排除集；ACCEPTED/在途/无回执 → gate confirmed 仍排除。
        """
        with self.read_view() as db:
            rows = db.execute(
                "SELECT DISTINCT gd.challenge_id FROM gate_decisions gd "
                "WHERE gd.status='confirmed' AND gd.challenge_id NOT IN ("
                "  SELECT s.challenge_id FROM submissions s WHERE s.status='REJECTED'"
                "    AND s.updated_at=(SELECT MAX(s2.updated_at) FROM submissions s2"
                "                      WHERE s2.challenge_id=s.challenge_id))"
            ).fetchall()
        return {row["challenge_id"] for row in rows}

    def latest_submission(self, challenge_id: str) -> Optional[Dict[str, Any]]:
        """该题最近一条提交记录（状态/回执/幂等键）。"""
        with self.read_view() as db:
            row = db.execute(
                "SELECT status,receipt_id,idempotency_key FROM submissions"
                " WHERE challenge_id=? ORDER BY updated_at DESC LIMIT 1", (challenge_id,)
            ).fetchone()
        return dict(row) if row else None

    def pending_submission_count(self) -> int:
        """尚未收到终态回执的提交数（收尾排空判断用）。"""
        with self.read_view() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM submissions WHERE status IN ('DRAFT','SENDING','UNKNOWN')"
            ).fetchone()
        return int(row["n"] or 0)

    def get_challenges(self, challenge_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        """批量取题目元数据，避免调度器逐题查询。"""
        ids = list(dict.fromkeys(challenge_ids))
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        with self.read_view() as db:
            rows = db.execute(
                "SELECT * FROM challenges WHERE id IN ({})".format(placeholders), ids
            ).fetchall()
        return {row["id"]: dict(row) for row in rows}

    # ----------------------------------------------------------------- result
    def write_result(self, result: WorkerResult, idempotency_key: str, transaction_hook=None) -> WriteReceipt:
        """幂等写入：同 key 只落库一次；错主/过期写入拒绝（D.1-2、D.4）。"""
        payload = json.dumps({
            "status": result.status, "summary": result.summary, "outcome": result.outcome,
            "failure_reason": result.failure_reason, "ruled_out": result.ruled_out,
            "next_best_action": result.next_best_action, "candidate_answer": result.candidate_answer,
            "resource_usage": result.resource_usage,
        }, ensure_ascii=False)
        with self.transaction() as db:
            # 幂等命中提前返回：重复回执不属于过期写入，必须在 owner/状态校验之前
            prev = db.execute(
                "SELECT attempt_id FROM results_ext WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if prev is not None:
                return WriteReceipt(first=False, idempotency_key=idempotency_key,
                                    attempt_id=prev["attempt_id"])
            row = db.execute("SELECT * FROM intents_ext WHERE id=?", (result.intent_id,)).fetchone()
            if not row:
                raise KeyError("intent not found: " + result.intent_id)
            if (row["owner"] != result.owner or row["attempt_no"] != result.attempt_no
                    or row["status"] != RUNNING or row["lease_until"] is None
                    or row["lease_until"] <= time.time()):
                raise StaleWrite("intent {} is owned by {} (attempt {}), writer is {} (attempt {})".format(
                    result.intent_id, row["owner"], row["attempt_no"], result.owner, result.attempt_no))
            if self._evidence_check is not None and result.evidence_ids:
                bad = [e for e in result.evidence_ids if not self._evidence_check(e)]
                if bad:
                    raise EvidenceNotRegistered(bad)
            cur = db.execute(
                "INSERT OR IGNORE INTO results_ext(idempotency_key, intent_id, payload, created_at)"
                " VALUES (?,?,?,?)",
                (idempotency_key, result.intent_id, payload, time.time()),
            )
            if cur.rowcount != 1:  # 并发写入兜底（_write 已串行化，正常不可达）
                prev = db.execute(
                    "SELECT attempt_id FROM results_ext WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
                return WriteReceipt(first=False, idempotency_key=idempotency_key,
                                    attempt_id=prev["attempt_id"] if prev else None)
            attempt_id = new_id("attempt")
            # 终态由 Worker 的 status 与 outcome 共同决定：completed 但 outcome=error
            # 仍应重试；blocked 表示需要外部条件（缺附件、预算耗尽），不能留在可领取状态。
            if result.status == "completed":
                new_status = COMPLETED if result.outcome in _SUCCESS_LIKE else FAILED_RETRYABLE
            elif result.status == "blocked":
                new_status = FAILED
            else:
                new_status = FAILED_RETRYABLE
            db.execute(
                "INSERT INTO attempts(id, intent_id, challenge_id, strategy_key, attempt_key, outcome,"
                " failure_reason, preconditions, actions, ruled_out, next_best_action, evidence_ids,"
                " idempotency_key, candidate_answer, resource_usage, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt_id, result.intent_id, result.challenge_id, result.strategy_key,
                    row["attempt_key"], result.outcome, result.failure_reason,
                    json.dumps(result.preconditions), json.dumps(result.actions),
                    json.dumps(result.ruled_out), result.next_best_action,
                    json.dumps(result.evidence_ids), idempotency_key, result.candidate_answer,
                    json.dumps(result.resource_usage), time.time(),
                ),
            )
            db.execute(
                "UPDATE results_ext SET attempt_id=? WHERE idempotency_key=?", (attempt_id, idempotency_key)
            )
            db.execute(
                "UPDATE intents_ext SET status=?, owner=NULL, lease_until=NULL, updated_at=? WHERE id=?",
                (new_status, time.time(), result.intent_id),
            )
            if transaction_hook is not None:
                transaction_hook(db)  # 校验决策与 Outbox 同结果原子提交，避免完成后断电丢候选
            return WriteReceipt(first=True, idempotency_key=idempotency_key, attempt_id=attempt_id)

    def get_attempt(self, attempt_id: str) -> Optional[Dict[str, Any]]:
        """按 id 取尝试记录。"""
        with self.read_view() as db:
            row = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        return dict(row) if row else None

    def recent_attempts(self, challenge_id: str, strategy_key: Optional[str] = None,
                        limit: int = 20) -> List[Dict[str, Any]]:
        """近期尝试列表（可按策略过滤），供交接包与审计。"""
        sql = "SELECT * FROM attempts WHERE challenge_id=?"
        args: List[Any] = [challenge_id]
        if strategy_key:
            sql += " AND strategy_key=?"
            args.append(strategy_key)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self.read_view() as db:
            return [dict(r) for r in db.execute(sql, args)]

    def lesson_context(self, challenge_id: str, limit: int = 6) -> List[Dict[str, Any]]:
        """本题 + 同领域历史教训；只返回脱敏文本，不返回其他题号。"""
        from . import lessons as lesson_store
        challenge = self.get_challenge(challenge_id) or {}
        with self.read_view() as db:
            return lesson_store.lesson_context(db, challenge_id,
                                           domain=challenge.get("category"), limit=limit)

    def handoff(self, challenge_id: str, max_attempts: int = 5,
                max_lessons: int = 6) -> Dict[str, Any]:
        """给 Worker 的最小交接包：近期尝试、教训与开 Intent，均有限量且脱敏。"""
        from . import lessons as lesson_store
        def safe(value, limit=300):
            """脱敏包装：限长并抹去其他题号等敏感信息。"""
            return lesson_store.sanitize_lesson_text(str(value or ""), limit)
        attempts = []
        for row in self.recent_attempts(challenge_id, limit=max_attempts):
            attempts.append({
                "strategy_key": safe(row.get("strategy_key"), 80),
                "outcome": safe(row.get("outcome"), 40),
                "failure_reason": safe(row.get("failure_reason")),
                "preconditions": [safe(x, 120) for x in json.loads(row.get("preconditions") or "[]")],
                "ruled_out": [safe(x, 120) for x in json.loads(row.get("ruled_out") or "[]")],
                "next_best_action": safe(row.get("next_best_action")),
            })
        return {
            "recent_attempts": attempts,
            "lessons": self.lesson_context(challenge_id, limit=max_lessons),
            "open_intents": [
                {"strategy_key": safe(row.get("strategy_key"), 80), "status": safe(row.get("status"), 40)}
                for row in self.list_open(challenge_id)[:max_attempts]
            ],
        }

    def snapshot(self, challenge_id: str, max_items: int = 50) -> Dict[str, Any]:
        """脱敏后的交接包快照：开 Intent + 近期尝试 + 有效教训（§9、handoff.md）。"""
        handoff = self.handoff(challenge_id, max_attempts=max_items, max_lessons=max_items)
        return {
            "challenge_id": challenge_id,
            "open_intents": handoff["open_intents"][:max_items],
            "recent_attempts": handoff["recent_attempts"][:max_items],
            "lessons": handoff["lessons"][:max_items],
        }

    def get_challenge(self, challenge_id: str) -> Optional[Dict[str, Any]]:
        """按 id 取题目元数据行。"""
        with self.read_view() as db:
            row = db.execute("SELECT * FROM challenges WHERE id=?", (challenge_id,)).fetchone()
        return dict(row) if row else None

    def claim_intent_for(self, challenge_id: str, worker: str,
                         lease_seconds: Optional[float] = None) -> Optional[Claim]:
        """附录 B.1 契约签名：按题目领取。"""
        return self.claim_intent(worker, lease_seconds=lease_seconds, challenge_ids=[challenge_id])

    # ------------------------------------------------------------------- facts
    def write_facts(self, challenge_id: str, facts: List[Dict[str, Any]],
                    source_attempt_id: Optional[str] = None) -> List[str]:
        """写入已验证事实；每条事实必须带已登记证据（D.1-3、B.2）。"""
        ids: List[str] = []
        for fact in facts:
            evidence_ids = list(fact.get("evidence_ids") or [])
            if not evidence_ids:
                raise EvidenceNotRegistered(["<empty>"])
            if self._evidence_check is None:
                raise EvidenceNotRegistered(evidence_ids)  # 未配置校验器 → 无法证明，拒绝
            bad = [e for e in evidence_ids if not self._evidence_check(e)]
            if bad:
                raise EvidenceNotRegistered(bad)
            fid = new_id("fact")
            ids.append(fid)
        with self.transaction() as db:
            now = time.time()
            for fid, fact in zip(ids, facts):
                db.execute(
                    "INSERT INTO facts(id, challenge_id, statement, confidence, evidence_ids,"
                    " source_attempt_id, created_at) VALUES (?,?,?,?,?,?,?)",
                    (fid, challenge_id, str(fact.get("statement", "")),
                     float(fact.get("confidence", 0.0)),
                     json.dumps(list(fact.get("evidence_ids") or [])), source_attempt_id, now),
                )
        return ids

    # ------------------------------------------------------------------ events
    def append_event(self, event_type: str, entity_id: str = "", payload: Optional[Dict[str, Any]] = None) -> int:
        """追加审计事件（§17.2 events 表：只追加，不改写历史）。"""
        with self.transaction() as db:
            cur = db.execute(
                "INSERT INTO events(event_type, entity_id, payload, created_at) VALUES (?,?,?,?)",
                (event_type, entity_id, json.dumps(payload or {}, ensure_ascii=False), time.time()),
            )
            return int(cur.lastrowid)

    def close(self) -> None:
        """关闭共享 SQLite 连接（进程退出前调用）。"""
        self._db.close()
