"""持久化提交 Outbox：主线程持有 SQLite，守护线程只执行网络调用。

DRAFT 才允许 POST；SENDING/UNKNOWN 重启后只查询。同题去重按状态分级：
回执未定（DRAFT/SENDING/UNKNOWN）或已 ACCEPTED 不接收第二候选；
平台判 REJECTED 后允许不同答案的新候选入队（错旗续解）。
正式 Harness 必须自行提供有界网络超时；停机不等待可能卡死的供应商调用。
"""
from __future__ import annotations

import hashlib
import json
import queue
import threading
import time

from .flag_gate import FlagGate, normalized_flag
from .schema import Submission, SubmitReceipt


class SubmissionLocked(Exception):
    """参赛锁定或策略拒绝：不得把候选送入 Outbox。"""


class SubmitGateway:
    """持久化提交网关：Outbox 模式，主线程写库、守护线程发网络请求。"""
    def __init__(self, store, adapter, policy=None, *, query_interval=5, clock=time.time):
        """store 提供共享 sqlite 连接；adapter 为 Harness 提交通道。"""
        self.store, self.db, self.adapter = store, store.db, adapter
        self.policy, self.clock, self.query_interval = policy, clock, query_interval
        self._requests, self._responses = queue.Queue(), queue.Queue()
        self._thread = None
        self._inflight = None
        self._closed = False

    @staticmethod
    def key(challenge_id, answer):
        """幂等键：sha256(challenge_id|normalized_flag)，同题同答同键。"""
        return 'sub-' + hashlib.sha256((challenge_id + '|' + normalized_flag(answer)).encode()).hexdigest()

    def enqueue(self, db, decision):
        """与结果、校验记录在同一事务调用；不执行任何网络操作。"""
        if decision.status != 'confirmed' or not all(decision.gates.get(k) is True for k in ('grounding', 'negation', 'interrogation')):
            raise ValueError('unconfirmed candidate cannot enter outbox')
        gate = db.execute('SELECT payload,status FROM gate_decisions WHERE id=?', (decision.id,)).fetchone()
        if not gate or gate['status'] != 'confirmed' or json.loads(gate['payload'])['declaration_hash'] != decision.declaration_hash:
            raise ValueError('recorded gate decision required')
        key = self.key(decision.challenge_id, decision.answer)
        rows = db.execute(
            'SELECT idempotency_key,answer,status FROM submissions WHERE challenge_id=?'
            ' ORDER BY updated_at, rowid', (decision.challenge_id,)).fetchall()
        if rows:
            # 同题去重按状态分级（2026-11-21 正式赛第 2 题错旗教训：REJECTED
            # 后题目被判永久关闭，无法用新答案续解）：
            # - 已 ACCEPTED：题目完成，不再入队任何新候选；
            # - DRAFT/SENDING/UNKNOWN：回执未知，等查询对账，不并发重发；
            # - REJECTED/BLOCKED：允许"不同答案"的新候选入队；同一答案重复
            #   被拒 → 沿用旧键去重，避免无意义重发。
            for row in rows:
                if row['status'] == 'ACCEPTED':
                    return row['idempotency_key']
            last = rows[-1]
            if last['status'] in ('DRAFT', 'SENDING', 'UNKNOWN'):
                return last['idempotency_key']
            try:
                same = any(row['status'] in ('REJECTED', 'BLOCKED')
                           and normalized_flag(decision.answer) == normalized_flag(row['answer'])
                           for row in rows)
            except (ValueError, TypeError):
                same = True  # 答案格式异常时保守处理：不重复提交
            if same:
                return last['idempotency_key']
        db.execute('INSERT INTO submissions(idempotency_key,challenge_id,answer,status,evidence_ids,updated_at) VALUES(?,?,?,?,?,?)',
                   (key, decision.challenge_id, decision.answer, 'DRAFT', json.dumps(decision.evidence_ids), self.clock()))
        db.execute('INSERT INTO submission_jobs VALUES(?,?,?,?)', (key, decision.id, 0, self.clock()))
        return key

    def _worker(self):
        while True:
            request = self._requests.get()
            if request is None:
                return
            key, mode, sub = request
            try:
                receipt = (self.adapter.submit_answer(sub) if mode == 'send'
                           else self.adapter.get_receipt(key))
                if receipt is None:
                    receipt = SubmitReceipt('UNKNOWN', detail='回执尚不可查')
                if not isinstance(receipt, SubmitReceipt) or receipt.status not in ('ACCEPTED', 'REJECTED', 'UNKNOWN'):
                    raise ValueError('invalid platform receipt')
            except Exception as exc:
                # 不能根据传输异常判断是否已送达；也不记录可能携带凭据的异常正文。
                receipt = SubmitReceipt('UNKNOWN', detail=type(exc).__name__)
            self._responses.put((key, receipt))

    def _start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._worker, name='submission-transport', daemon=True)
            self._thread.start()

    def _proof_valid(self, row):
        gate = self.db.execute('SELECT * FROM gate_decisions WHERE id=?', (row['gate_id'],)).fetchone()
        if not gate or gate['status'] != 'confirmed' or gate['challenge_id'] != row['challenge_id']:
            return False
        data = json.loads(gate['payload'])
        if not all(data.get('gates', {}).get(k) is True for k in ('grounding', 'negation', 'interrogation')):
            return False
        try:
            declared, digest = FlagGate.declaration(self.store.workspace(row['challenge_id']))
            if digest != data['declaration_hash'] or normalized_flag(declared) != normalized_flag(row['answer']):
                return False
            command = self.db.execute('SELECT * FROM command_log WHERE id=? AND challenge_id=? AND complete=1 AND returncode=0',
                                      (data['command_id'], row['challenge_id'])).fetchone()
            if not command or command['evidence_id'] not in data['evidence_ids']:
                return False
            self.store.restore_evidence(command['evidence_id'])
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def _correct_once(self, db, row, receipt):
        corrected = receipt.corrected_answer
        if not isinstance(corrected, str) or not corrected or row['gate_id'] == 'legacy-query-only' or row['idempotency_key'].endswith('-format1') or corrected == row['answer']:
            return
        # 只接受平台结构化给出的格式修正；禁止把错误文本猜成新答案。
        try:
            if normalized_flag(corrected) != normalized_flag(row['answer']):
                return
        except (ValueError, TypeError):
            return
        key = row['idempotency_key'] + '-format1'
        db.execute('INSERT OR IGNORE INTO submissions(idempotency_key,challenge_id,answer,status,evidence_ids,updated_at) VALUES(?,?,?,?,?,?)',
                   (key, row['challenge_id'], corrected, 'DRAFT', row['evidence_ids'], self.clock()))
        db.execute('INSERT OR IGNORE INTO submission_jobs VALUES(?,?,?,?)', (key, row['gate_id'], 0, self.clock()))

    def pump(self):
        """非阻塞：收取已完成回执，然后最多派发一项已确认提交/回执查询。"""
        completed = []
        while True:
            try:
                key, receipt = self._responses.get_nowait()
            except queue.Empty:
                break
            row = self.db.execute('SELECT s.*,j.gate_id,j.created_at FROM submissions s JOIN submission_jobs j USING(idempotency_key) WHERE idempotency_key=?', (key,)).fetchone()
            with self.store.board.transaction() as db:
                db.execute('UPDATE submissions SET status=?,receipt_id=?,detail=?,updated_at=? WHERE idempotency_key=?',
                           (receipt.status, receipt.receipt_id, receipt.detail, self.clock(), key))
                db.execute('UPDATE submission_jobs SET next_query=? WHERE idempotency_key=?', (self.clock() + self.query_interval, key))
                if receipt.status == 'REJECTED':
                    self._correct_once(db, row, receipt)
            event = {'challenge_id': row['challenge_id'], 'status': receipt.status,
                     'receipt_id': receipt.receipt_id, 'idempotency_key': key,
                     'answer': row['answer'],
                     'latency_seconds': max(0, self.clock() - row['created_at'])}
            self.store.board.append_event('submission', row['challenge_id'], event)
            if receipt.status == 'REJECTED':
                self.store.board.append_event('submission_diagnosis', row['challenge_id'],
                    {'idempotency_key': key, 'detail': receipt.detail,
                     'suggestion': '仅接受平台显式格式修正；否则核对证据和官方协议，不盲目重发'})
            completed.append(event)
            self._inflight = None
        if self._inflight or self._closed:
            return completed
        row = self.db.execute("SELECT s.*,j.gate_id,j.created_at FROM submissions s JOIN submission_jobs j USING(idempotency_key) WHERE s.status IN ('DRAFT','SENDING','UNKNOWN') AND j.next_query<=? ORDER BY CASE s.status WHEN 'DRAFT' THEN 0 ELSE 1 END,j.created_at LIMIT 1", (self.clock(),)).fetchone()
        if not row:
            return completed
        if self.policy is not None and self.policy.decide_submission().action == 'deny':
            raise SubmissionLocked('submission policy denied')
        mode = 'send' if row['status'] == 'DRAFT' else 'query'
        if mode == 'send' and not self._proof_valid(row):
            with self.store.board.transaction() as db:
                db.execute("UPDATE submissions SET status='BLOCKED',detail='proof changed before dispatch' WHERE idempotency_key=?", (row['idempotency_key'],))
            self.store.board.append_event('submission_blocked', row['challenge_id'], {'reason': 'proof changed'})
            return completed
        with self.store.board.transaction() as db:
            if mode == 'send':
                claimed = db.execute("UPDATE submissions SET status='SENDING',sent_at=? WHERE idempotency_key=? AND status='DRAFT'", (self.clock(), row['idempotency_key']))
                if claimed.rowcount != 1:
                    return completed
            db.execute('UPDATE submission_jobs SET next_query=? WHERE idempotency_key=?', (self.clock() + self.query_interval, row['idempotency_key']))
        try:
            evidence_ids = json.loads(row['evidence_ids'])
        except (ValueError, TypeError):
            evidence_ids = row['evidence_ids'].splitlines()
        sub = Submission(row['challenge_id'], row['answer'], row['idempotency_key'], evidence_ids)
        self._inflight = sub.idempotency_key
        self._start()
        self._requests.put((sub.idempotency_key, mode, sub))
        return completed

    def recover_legacy(self):
        """旧库的 SENDING/UNKNOWN 只挂接查询，不把旧候选当成已校验答案。"""
        with self.store.board.transaction() as db:
            db.execute("INSERT OR IGNORE INTO submission_jobs SELECT idempotency_key,'legacy-query-only',0,? FROM submissions WHERE status IN ('SENDING','UNKNOWN')", (self.clock(),))

    def flush(self, timeout=1):
        """限时泵送：循环 pump 直到无在途请求或超时，返回完成回执列表。"""
        deadline = time.monotonic() + max(0, timeout)
        completed = self.pump()
        while self._inflight and time.monotonic() < deadline:
            threading.Event().wait(min(.01, max(0, deadline - time.monotonic())))
            completed.extend(self.pump())
        return completed

    def close(self):
        """停机：置关闭标志并投哨兵结束守护线程（不等待网络调用）。"""
        self._closed = True
        if self._thread:
            self._requests.put(None)
