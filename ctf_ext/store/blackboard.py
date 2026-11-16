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
    def __init__(self, attempt_key: str, blocked_by: str):
        super().__init__("duplicate strategy {} blocked by {}".format(attempt_key, blocked_by))
        self.attempt_key = attempt_key
        self.blocked_by = blocked_by


class StaleWrite(Exception):
    pass


class EvidenceNotRegistered(Exception):
    """引用未登记/哈希不匹配的证据（附录 D.1-3：写入 Fact 前证据必须已存在）。"""
    def __init__(self, evidence_ids):
        super().__init__("evidence not registered or hash mismatch: {}".format(list(evidence_ids)))
        self.evidence_ids = list(evidence_ids)


@dataclass
class IntentDraft:
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
    first: bool
    idempotency_key: str
    attempt_id: Optional[str] = None


def compute_priority(draft: IntentDraft) -> float:
    """期望得分/Token 比（§23.2-4：按性价比排序，而不是按题号）。"""
    if draft.priority:
        return draft.priority
    return draft.points_hint / max(draft.est_tokens, 1)


class Blackboard:
    def __init__(self, db: sqlite3.Connection, lease_default: float = 180.0, evidence_check=None):
        self._db = db
        self._lock = threading.RLock()
        self._lease_default = lease_default
        # evidence_check: evidence_id -> bool（通常是 EvidenceStore.verify）；
        # 配置后所有结果引用的证据都必须通过校验（D.1-3）
        self._evidence_check = evidence_check

    @contextmanager
    def _write(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    # ---------------------------------------------------------------- intents
    def add_challenge(self, challenge) -> None:
        with self._write() as db:
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
        with self._write() as db:
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
                     challenge_ids: Optional[Sequence[str]] = None) -> Optional[Claim]:
        """领取一个可执行 Intent；无可用任务返回 None（附录 B.1）。"""
        now = time.time()
        lease = lease_seconds or self._lease_default
        with self._write() as db:
            sql = ("SELECT id FROM intents_ext WHERE status IN ('READY','FAILED_RETRYABLE')"
                   " AND (lease_until IS NULL OR lease_until < ?)")
            args: List[Any] = [now]
            if challenge_ids:
                sql += " AND challenge_id IN ({})".format(",".join("?" * len(challenge_ids)))
                args.extend(challenge_ids)
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
        with self._write() as db:
            cur = db.execute(
                "UPDATE intents_ext SET lease_until=MAX(lease_until, ?), updated_at=?"
                " WHERE id=? AND owner=? AND status='RUNNING'",
                (time.time() + extend, time.time(), intent_id, worker),
            )
            return cur.rowcount == 1

    def release(self, intent_id: str, worker: str, outcome: str) -> None:
        status = {"success": COMPLETED, "no_signal": COMPLETED}.get(outcome, FAILED_RETRYABLE)
        with self._write() as db:
            db.execute(
                "UPDATE intents_ext SET status=?, owner=NULL, note=?, updated_at=?"
                " WHERE id=? AND owner=? AND status='RUNNING'",
                (status, "released:" + outcome, time.time(), intent_id, worker),
            )

    def list_open(self, challenge_id: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM intents_ext WHERE status IN ('READY','RUNNING','FAILED_RETRYABLE')"
        args: tuple = ()
        if challenge_id:
            sql += " AND challenge_id=?"
            args = (challenge_id,)
        sql += " ORDER BY priority DESC"
        return [dict(r) for r in self._db.execute(sql, args)]

    # ----------------------------------------------------------------- result
    def write_result(self, result: WorkerResult, idempotency_key: str) -> WriteReceipt:
        """幂等写入：同 key 只落库一次；错主/过期写入拒绝（D.1-2、D.4）。"""
        payload = json.dumps({
            "status": result.status, "summary": result.summary, "outcome": result.outcome,
            "failure_reason": result.failure_reason, "ruled_out": result.ruled_out,
            "next_best_action": result.next_best_action, "candidate_answer": result.candidate_answer,
            "resource_usage": result.resource_usage,
        }, ensure_ascii=False)
        with self._write() as db:
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
                    or row["status"] != RUNNING):
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
            new_status = {"completed": COMPLETED, "blocked": READY}.get(result.status, FAILED_RETRYABLE)
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
                "UPDATE intents_ext SET status=?, owner=NULL, updated_at=? WHERE id=?",
                (new_status, time.time(), result.intent_id),
            )
            return WriteReceipt(first=True, idempotency_key=idempotency_key, attempt_id=attempt_id)

    def recent_attempts(self, challenge_id: str, strategy_key: Optional[str] = None,
                        limit: int = 20) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM attempts WHERE challenge_id=?"
        args: List[Any] = [challenge_id]
        if strategy_key:
            sql += " AND strategy_key=?"
            args.append(strategy_key)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self._db.execute(sql, args)]

    def snapshot(self, challenge_id: str, max_items: int = 50) -> Dict[str, Any]:
        """交接包快照：开 Intent + 近期尝试 + 有效教训（§9、handoff.md）。"""
        return {
            "challenge_id": challenge_id,
            "open_intents": self.list_open(challenge_id)[:max_items],
            "recent_attempts": self.recent_attempts(challenge_id, limit=max_items),
            "lessons": self._db.execute(
                "SELECT text, tags FROM lessons WHERE challenge_id=? AND superseded=0 ORDER BY created_at DESC LIMIT ?",
                (challenge_id, max_items),
            ).fetchall(),
        }

    def get_challenge(self, challenge_id: str) -> Optional[Dict[str, Any]]:
        row = self._db.execute("SELECT * FROM challenges WHERE id=?", (challenge_id,)).fetchone()
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
        with self._write() as db:
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
        with self._write() as db:
            cur = db.execute(
                "INSERT INTO events(event_type, entity_id, payload, created_at) VALUES (?,?,?,?)",
                (event_type, entity_id, json.dumps(payload or {}, ensure_ascii=False), time.time()),
            )
            return int(cur.lastrowid)

    def close(self) -> None:
        self._db.close()
