"""端到端（附录 F-4）：Mock Harness → 领取 → Worker → 证据 → 结果 → 提交。"""
import json
import os
import sqlite3
import tempfile

from ctf_ext.__main__ import build_runtime


def _paths():
    root = tempfile.mkdtemp(prefix="orch-it-")
    return os.path.join(root, "practice.db"), os.path.join(root, "evidence")


def test_mock_end_to_end_accepts_flag():
    db, ev = _paths()
    orch = build_runtime(db, ev)
    out = orch.run(max_seconds=30)
    assert out["solved"] == ["ch-mock-1"], json.dumps(out, ensure_ascii=False)[:500]
    assert out["results"][0]["submission"]["status"] == "ACCEPTED"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    assert conn.execute("SELECT status FROM submissions").fetchone()["status"] == "ACCEPTED"
    assert conn.execute("SELECT COUNT(*) c FROM facts").fetchone()["c"] >= 1
    types = {r["event_type"] for r in conn.execute("SELECT event_type FROM events")}
    assert {"run_started", "result_written", "submission", "run_finished"} <= types
    assert conn.execute("SELECT COUNT(*) c FROM intents_ext WHERE status='COMPLETED'").fetchone()["c"] == 1


def test_restart_does_not_resubmit_or_replay():
    db, ev = _paths()
    build_runtime(db, ev).run(max_seconds=30)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    before = conn.execute("SELECT COUNT(*) c FROM submissions").fetchone()["c"]
    second = build_runtime(db, ev).run(max_seconds=30)
    assert second["results"] == []          # 幂等：已完成的探索方向不会再派发
    assert conn.execute("SELECT COUNT(*) c FROM submissions").fetchone()["c"] == before
