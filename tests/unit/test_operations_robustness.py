"""运行期加固回归：证据权限与看板指标逐行容错。"""
import json
import os
import stat
import tempfile

from _support import new_board

from ctf_ext.runtime.metrics import framework_metrics
from ctf_ext.store.evidence import EvidenceStore
from ctf_ext.store.session import StateStore


def _mode(path):
    return stat.S_IMODE(os.stat(str(path)).st_mode)


def test_evidence_directories_and_files_are_private():
    board, conn = new_board()
    root = os.path.join(tempfile.mkdtemp(prefix="ev-private-"), "store")
    evidence = EvidenceStore(root, db=conn)
    sha = evidence.put_bytes(b"flag{secret}", kind="raw")

    shard = os.path.join(root, sha[:2])
    path = os.path.join(shard, sha)
    assert _mode(root) == 0o700
    assert _mode(shard) == 0o700
    assert _mode(path) == 0o600


def test_framework_metrics_skips_corrupt_rows_instead_of_failing():
    board, conn = new_board()
    StateStore(board, EvidenceStore(tempfile.mkdtemp(prefix="ev-metrics-"), db=conn),
               tempfile.mkdtemp(prefix="ws-metrics-"))

    def gate(payload):
        conn.execute(
            "INSERT INTO gate_decisions(id,challenge_id,answer,status,level,payload,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            ("g-" + str(len(payload)), "c1", "flag{x}", "confirmed", "L1", payload, 0.0),
        )

    gate("not-json")
    gate(json.dumps({"gates": {"grounding": False}}))
    gate(json.dumps({"status": "missing-gates"}))
    conn.execute("INSERT INTO sessions(challenge_id,revision,payload) VALUES(?,?,?)",
                 ("c1", 1, "not-json"))
    conn.execute("INSERT INTO sessions(challenge_id,revision,payload) VALUES(?,?,?)",
                 ("c2", 1, json.dumps({"steps": 2, "challenge_id": "c2", "status": "running"})))

    metrics = framework_metrics(conn, task_count=2)
    assert metrics["unreadable_records"] == 3
    assert metrics["average_steps"] == 2
    assert metrics["gate_veto_rates"]["grounding"] == 1.0
