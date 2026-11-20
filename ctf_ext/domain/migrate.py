"""扩展表 migration：不改内嵌调度框架原生 8 张表，追加独立 schema（§6、§17、附录 F-3）。

扩展表：challenges / intents_ext / attempts / evidence / lessons / submissions
/ results_ext / schema_migrations。所有表幂等创建，WAL 开启。
"""
from __future__ import annotations

import sqlite3
import time

MIGRATIONS = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS challenges (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'unknown',
            description TEXT NOT NULL DEFAULT '',
            attachments TEXT NOT NULL DEFAULT '[]',
            points_hint REAL NOT NULL DEFAULT 0,
            difficulty_level INTEGER NOT NULL DEFAULT 0,
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS intents_ext (
            id TEXT PRIMARY KEY,
            challenge_id TEXT NOT NULL REFERENCES challenges(id),
            strategy_key TEXT NOT NULL,
            attempt_key TEXT NOT NULL,
            params TEXT NOT NULL DEFAULT '{}',
            priority REAL NOT NULL DEFAULT 0,
            est_tokens INTEGER NOT NULL DEFAULT 0,
            points_hint REAL NOT NULL DEFAULT 0,
            budget_seconds REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'READY',
            owner TEXT,
            lease_until REAL,
            attempt_no INTEGER NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_intents_challenge_status ON intents_ext(challenge_id, status);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_intents_open_attempt_key
            ON intents_ext(attempt_key) WHERE status IN ('READY','RUNNING','FAILED_RETRYABLE');
        CREATE TABLE IF NOT EXISTS attempts (
            id TEXT PRIMARY KEY,
            intent_id TEXT,
            challenge_id TEXT NOT NULL REFERENCES challenges(id),
            strategy_key TEXT NOT NULL,
            attempt_key TEXT NOT NULL,
            outcome TEXT NOT NULL,
            failure_reason TEXT NOT NULL DEFAULT '',
            preconditions TEXT NOT NULL DEFAULT '[]',
            actions TEXT NOT NULL DEFAULT '[]',
            ruled_out TEXT NOT NULL DEFAULT '[]',
            next_best_action TEXT NOT NULL DEFAULT '',
            evidence_ids TEXT NOT NULL DEFAULT '[]',
            idempotency_key TEXT UNIQUE,
            candidate_answer TEXT,
            resource_usage TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_attempts_challenge ON attempts(challenge_id, strategy_key);
        CREATE TABLE IF NOT EXISTS evidence (
            sha256 TEXT PRIMARY KEY,
            kind TEXT NOT NULL DEFAULT 'raw',
            path TEXT NOT NULL,
            bytes INTEGER NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lessons (
            id TEXT PRIMARY KEY,
            challenge_id TEXT NOT NULL,
            text TEXT NOT NULL,
            tags TEXT NOT NULL DEFAULT '[]',
            source_attempt_id TEXT,
            superseded INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS submissions (
            idempotency_key TEXT PRIMARY KEY,
            challenge_id TEXT NOT NULL,
            answer TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'DRAFT',
            receipt_id TEXT,
            detail TEXT NOT NULL DEFAULT '',
            evidence_ids TEXT NOT NULL DEFAULT '[]',
            sent_at REAL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS results_ext (
            idempotency_key TEXT PRIMARY KEY,
            intent_id TEXT NOT NULL,
            attempt_id TEXT,
            payload TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        """,
    ),
    (
        2,
        """
        CREATE TABLE IF NOT EXISTS facts (
            id TEXT PRIMARY KEY,
            challenge_id TEXT NOT NULL,
            statement TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0,
            evidence_ids TEXT NOT NULL DEFAULT '[]',
            source_attempt_id TEXT,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_facts_challenge ON facts(challenge_id, created_at);
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            entity_id TEXT,
            payload TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type, created_at);
        CREATE TABLE IF NOT EXISTS config_snapshots (
            version INTEGER PRIMARY KEY AUTOINCREMENT,
            config_hash TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            payload TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL
        );
        """,
    ),
    (
        3,
        """
        CREATE TABLE IF NOT EXISTS solutions (
            id TEXT PRIMARY KEY,
            challenge_id TEXT NOT NULL,
            challenge_name TEXT NOT NULL DEFAULT '',
            domain TEXT NOT NULL DEFAULT 'unknown',
            title TEXT NOT NULL DEFAULT '',
            approach TEXT NOT NULL DEFAULT '',
            key_commands TEXT NOT NULL DEFAULT '[]',
            keywords TEXT NOT NULL DEFAULT '[]',
            source_submission TEXT UNIQUE,
            reuse_count INTEGER NOT NULL DEFAULT 0,
            last_used_at REAL,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_solutions_domain ON solutions(domain, created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_solutions_challenge ON solutions(challenge_id);
        """,
    ),
]


def migrate(conn: sqlite3.Connection) -> None:
    """按版本号幂等应用迁移；每条迁移在一个事务内完成。"""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    for version, sql in MIGRATIONS:
        if version in applied:
            continue
        # executescript 自带隐式提交（BEGIN IMMEDIATE 会被其吞掉），改为：
        # 先跑建表脚本（CREATE IF NOT EXISTS 幂等），成功后才登记版本号
        conn.executescript(sql)
        conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", (version, time.time()))
        conn.commit()
