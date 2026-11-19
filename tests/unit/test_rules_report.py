"""规则候选报告：只读、可复现、0 注入。"""
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _db(tmp):
    path = Path(tmp) / "rules.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE attempts (id TEXT PRIMARY KEY, intent_id TEXT, challenge_id TEXT, "
                 "strategy_key TEXT, attempt_key TEXT, outcome TEXT, failure_reason TEXT DEFAULT '', "
                 "preconditions TEXT DEFAULT '[]', actions TEXT DEFAULT '[]', ruled_out TEXT DEFAULT '[]', "
                 "next_best_action TEXT DEFAULT '', evidence_ids TEXT DEFAULT '[]', idempotency_key TEXT, "
                 "candidate_answer TEXT, resource_usage TEXT DEFAULT '{}', created_at REAL)")
    conn.execute("CREATE TABLE lessons (id TEXT PRIMARY KEY, challenge_id TEXT, text TEXT, "
                 "tags TEXT DEFAULT '[]', source_attempt_id TEXT, superseded INTEGER DEFAULT 0, created_at REAL)")
    for i in range(2):
        conn.execute("INSERT INTO attempts(id, challenge_id, strategy_key, attempt_key, outcome, "
                     "actions, next_best_action, evidence_ids, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     ("a%d" % i, "c%d" % i, "crypto.rsa.small_e", "k%d" % i, "success",
                      json.dumps(["run"]), "verify", json.dumps(["e%d" % i]), 1.0 + i))
    conn.execute("INSERT INTO lessons(id, challenge_id, text, tags, source_attempt_id, superseded, created_at) "
                 "VALUES(?,?,?,?,?,?,?)", ("l1", "c1", "flag{secret} lesson", "[]", "a1", 0, 1.0))
    conn.commit()
    conn.close()
    return path


def test_rules_report_is_read_only_and_never_injects():
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        before = db.read_bytes()
        out = subprocess.run([sys.executable, str(ROOT / "scripts" / "rules_report.py"),
                              "--db", str(db), "--min-challenges", "2"],
                             capture_output=True, text=True, cwd=str(ROOT))
        assert out.returncode == 0, out.stderr
        report = json.loads(out.stdout)
        assert report["candidates"][0]["strategy_key"] == "crypto.rsa.small_e"
        assert report["candidates"][0]["status"] == "candidate"
        assert report["injection"].startswith("none")
        assert db.read_bytes() == before


def test_rules_report_missing_db_is_nonzero():
    with tempfile.TemporaryDirectory() as tmp:
        out = subprocess.run([sys.executable, str(ROOT / "scripts" / "rules_report.py"),
                              "--db", str(Path(tmp) / "missing.db")],
                             capture_output=True, text=True, cwd=str(ROOT))
        assert out.returncode == 1
