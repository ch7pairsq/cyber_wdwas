"""控制器持久化：固定 schema 状态、CAS、命令归档及目标/环境隔离缓存。"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS controller_schema(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO controller_schema VALUES(1);
CREATE TABLE IF NOT EXISTS controller_settings (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
 challenge_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS command_log (
 id TEXT PRIMARY KEY, challenge_id TEXT NOT NULL, evidence_id TEXT NOT NULL,
 argv TEXT NOT NULL, tool_version TEXT NOT NULL, target_revision TEXT NOT NULL,
 environment TEXT NOT NULL, returncode INTEGER, complete INTEGER NOT NULL,
 created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS command_challenge ON command_log(challenge_id);
CREATE TABLE IF NOT EXISTS tool_cache (
 key TEXT PRIMARY KEY, command_id TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS gate_decisions (
 id TEXT PRIMARY KEY, challenge_id TEXT NOT NULL, answer TEXT NOT NULL,
 status TEXT NOT NULL, level TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS submission_jobs (
 idempotency_key TEXT PRIMARY KEY, gate_id TEXT NOT NULL, next_query REAL NOT NULL DEFAULT 0,
 created_at REAL NOT NULL);
"""


def ensure_schema(conn):
    conn.executescript(SCHEMA)


class StateConflict(Exception):
    pass


class StateStore:
    def __init__(self, board, evidence, workspace_root, clock=time.time):
        self.board, self.db, self.evidence = board, board._db, evidence
        self.root = Path(workspace_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(str(self.root), 0o700)
        except OSError:
            pass
        self.clock = clock
        ensure_schema(self.db)

    def pin_policy(self, policy):
        """数据库生命周期内固定路线，重启也不能静默更换。"""
        payload = json.dumps(policy, sort_keys=True, allow_nan=False)
        with self.board.transaction() as db:
            row = db.execute("SELECT payload FROM controller_settings WHERE key='routing'").fetchone()
            if row is not None and row["payload"] != payload:
                raise ValueError("routing policy frozen for this run database")
            db.execute("INSERT OR IGNORE INTO controller_settings VALUES('routing',?)", (payload,))

    def workspace_path(self, challenge_id):
        """只读路径推导（不创建目录、不做符号链接检查）：资源回收等场景用。"""
        if not isinstance(challenge_id, str) or not challenge_id:
            raise ValueError("invalid challenge identity")
        return self.root / hashlib.sha256(challenge_id.encode()).hexdigest()

    def workspace(self, challenge_id):
        path = self.workspace_path(challenge_id)
        if path.is_symlink():
            raise ValueError("workspace symlink forbidden")
        path.mkdir(exist_ok=True, mode=0o700)
        if not path.is_dir() or path.is_symlink():
            raise ValueError("workspace must be a real directory")
        os.chmod(str(path), 0o700)
        return path

    def declare(self, challenge_id, candidate):
        """引擎明确声明唯一候选；原子替换，不沿既有 FLAG 符号链接写入。"""
        import os
        import tempfile
        if not isinstance(candidate, str) or not candidate or len(candidate.encode()) > 4096 or "\n" in candidate or "\r" in candidate:
            raise ValueError("invalid declaration")
        workspace = self.workspace(challenge_id)
        fd, temporary = tempfile.mkstemp(prefix=".flag-", dir=str(workspace))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(candidate.encode())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, str(workspace / "FLAG"))
            try:
                fd_dir = os.open(str(workspace), os.O_RDONLY)
                try:
                    os.fsync(fd_dir)
                finally:
                    os.close(fd_dir)
            except OSError:
                pass
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _default_state(self, challenge_id):
        return {
            'schema_version': 1, 'challenge_id': challenge_id, 'steps': 0,
            'tried_paths': [], 'evidence_ids': [], 'hypotheses': [],
            'budget': {'model_tokens': 0, 'tool_calls': 0, 'started_at': self.clock()},
            'route': {'model': 'deepseek-flash', 'strategy': 'initial', 'stage': 0},
            'call_signatures': {}, 'last_progress_step': 0, 'status': 'ready',
        }

    def load(self, challenge_id):
        with self.board.read_view() as db:
            row = db.execute("SELECT revision,payload FROM sessions WHERE challenge_id=?", (challenge_id,)).fetchone()
        if row:
            state = json.loads(row['payload'])
            self.validate(state)
            return row['revision'], state
        return 0, self._default_state(challenge_id)

    def load_many(self, challenge_ids):
        """批量加载题目状态；缺失题目返回默认状态，避免调度器逐题查询。"""
        ids = list(dict.fromkeys(challenge_ids))
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        with self.board.read_view() as db:
            rows = db.execute(
                "SELECT challenge_id,revision,payload FROM sessions WHERE challenge_id IN ({})".format(placeholders),
                ids,
            ).fetchall()
        found = {}
        for row in rows:
            state = json.loads(row['payload'])
            self.validate(state)
            found[row['challenge_id']] = (row['revision'], state)
        return {cid: found.get(cid) or (0, self._default_state(cid)) for cid in ids}

    def challenge_ids(self):
        with self.board.read_view() as db:
            return [row['challenge_id'] for row in db.execute(
                "SELECT challenge_id FROM sessions ORDER BY challenge_id")]

    @staticmethod
    def validate(state):
        required = {'schema_version', 'challenge_id', 'steps', 'tried_paths', 'evidence_ids',
                    'hypotheses', 'budget', 'route', 'call_signatures', 'last_progress_step', 'status'}
        if set(state) != required or state['schema_version'] != 1:
            raise ValueError('invalid state schema')
        if not isinstance(state['challenge_id'], str) or not state['challenge_id']:
            raise ValueError('invalid challenge identity')
        for name in ('steps', 'last_progress_step'):
            if type(state[name]) is not int or state[name] < 0:
                raise ValueError('invalid step count')
        for name in ('tried_paths', 'evidence_ids', 'hypotheses'):
            if not isinstance(state[name], list):
                raise ValueError('invalid state list')
        if not isinstance(state['route'], dict) or set(state['route']) != {'model', 'strategy', 'stage'} or state['route']['model'] != 'deepseek-flash':
            raise ValueError('model route is fixed')
        if type(state['route']['stage']) is not int or not 0 <= state['route']['stage'] <= 4:
            raise ValueError('invalid routing stage')
        if state['route']['strategy'] not in ('initial', 'hinted', 'alternate', 'deep_water', 'heartbeat'):
            raise ValueError('invalid strategy')
        if state['status'] not in ('ready', 'running', 'confirmed', 'stopped'):
            raise ValueError('invalid session status')
        if not isinstance(state['call_signatures'], dict) or any(not isinstance(k, str) or type(v) is not int or v < 1 for k, v in state['call_signatures'].items()):
            raise ValueError('invalid call signatures')
        if not isinstance(state['budget'], dict) or set(state['budget']) != {'model_tokens', 'tool_calls', 'started_at'}:
            raise ValueError('invalid budget schema')
        if any(type(state['budget'][k]) is not int for k in ('model_tokens', 'tool_calls')):
            raise ValueError('invalid measured usage')
        if state['last_progress_step'] > state['steps'] or any(not isinstance(v, str) for v in state['evidence_ids']):
            raise ValueError('invalid evidence or progress')
        for name in ('model_tokens', 'tool_calls', 'started_at'):
            value = state['budget'][name]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError('invalid budget')
        if len(json.dumps(state, ensure_ascii=False).encode()) > 256_000:
            raise ValueError('state too large; archive before patching')

    def save(self, challenge_id, revision, state):
        self.validate(state)
        if state['challenge_id'] != challenge_id:
            raise ValueError('cross-challenge state')
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True, allow_nan=False)
        with self.board.transaction() as db:
            if revision == 0:
                try:
                    db.execute('INSERT INTO sessions VALUES(?,?,?)', (challenge_id, 1, payload))
                except sqlite3.IntegrityError:
                    raise StateConflict('state already created') from None
            else:
                cur = db.execute('UPDATE sessions SET revision=?,payload=? WHERE challenge_id=? AND revision=?',
                                 (revision + 1, payload, challenge_id, revision))
                if cur.rowcount != 1:
                    raise StateConflict('state changed; reload before patch')
        return revision + 1

    def patch(self, challenge_id, mutate):
        revision, state = self.load(challenge_id)
        updated = copy.deepcopy(state)
        mutate(updated)
        self.save(challenge_id, revision, updated)
        return updated

    def compact(self, challenge_id):
        def trim(state):
            # 原始命令、输出与完整 state 留在内容寻址归档，不以摘要代替证据。
            archive = self.evidence.put_bytes(json.dumps(state, ensure_ascii=False).encode(), kind='state_archive')
            state['evidence_ids'] = list(dict.fromkeys(state['evidence_ids'] + [archive]))
            state['tried_paths'] = state['tried_paths'][-10:]
            state['hypotheses'] = state['hypotheses'][-10:]
        return self.patch(challenge_id, trim)

    def restore_evidence(self, evidence_id):
        with self.board.read_view() as db:
            row = db.execute('SELECT path FROM evidence WHERE sha256=?', (evidence_id,)).fetchone()
        if not row or not row['path'] or not self.evidence.verify(evidence_id):
            raise ValueError('evidence cannot be restored exactly')
        path = Path(row['path']).resolve()
        if self.evidence._root.resolve() not in path.parents:
            raise ValueError('evidence outside archive')
        return path.read_bytes()

    @staticmethod
    def cache_key(challenge_id, target_revision, environment, tool, version, argv, input_hash):
        if not all((challenge_id, target_revision, environment, tool, version, input_hash)):
            raise ValueError('cache identity incomplete')
        data = [challenge_id, target_revision, environment, tool, version, list(argv), input_hash]
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def cached(self, key):
        with self.board.read_view() as db:
            row = db.execute('SELECT command_id FROM tool_cache WHERE key=? AND expires>?', (key, self.clock())).fetchone()
            command = (db.execute('SELECT * FROM command_log WHERE id=?', (row['command_id'],)).fetchone()
                       if row else None)
        if not row:
            return None
        if not command or not command['complete']:
            return None
        try:
            self.restore_evidence(command['evidence_id'])
        except (ValueError, OSError):
            return None
        return dict(command)
