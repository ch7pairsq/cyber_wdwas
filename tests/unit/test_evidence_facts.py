"""证据不变量与事实层（D.1-3、§7.3）：证据必须已登记且哈希匹配。"""
import os
import tempfile

from _support import add_challenge, new_board

from ctf_ext.domain.migrate import migrate
from ctf_ext.domain.models import OUTCOME_SUCCESS
from ctf_ext.store.blackboard import (
    EvidenceNotRegistered, IntentDraft, WorkerResult, Blackboard,
)
from ctf_ext.store.evidence import EvidenceStore


def test_put_file_keeps_source_and_copies():
    board, conn = new_board()
    ev = EvidenceStore(tempfile.mkdtemp(), db=conn)
    src = os.path.join(tempfile.mkdtemp(), "attachment.bin")
    with open(src, "wb") as fh:
        fh.write(b"attachment-bytes")
    sha = ev.put_file(src)
    assert os.path.exists(src)  # 源附件必须保留（§15.2）
    assert ev.verify(sha) is True
    assert conn.execute("SELECT bytes FROM evidence WHERE sha256=?", (sha,)).fetchone()["bytes"] > 0


def test_hash_only_registration_verifies_without_content():
    board, conn = new_board()
    ev = EvidenceStore(tempfile.mkdtemp(), db=conn)
    sha = ev.put_hash("a" * 64, kind="hash:tool")
    assert ev.verify(sha) is True
    row = conn.execute("SELECT path, bytes FROM evidence WHERE sha256=?", (sha,)).fetchone()
    assert row["path"] == "" and row["bytes"] == 0


def test_migration_is_idempotent_and_adds_new_tables():
    board, conn = new_board()
    migrate(conn)  # 第二次执行不得报错，也不得重复登记版本
    versions = conn.execute("SELECT COUNT(*) c FROM schema_migrations").fetchone()["c"]
    assert versions == 2
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"facts", "events", "config_snapshots"} <= names
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_write_facts_requires_registered_evidence():
    board, conn = new_board()
    ev = EvidenceStore(tempfile.mkdtemp(), db=conn)
    board = Blackboard(conn, evidence_check=ev.verify)
    try:
        board.write_facts("c1", [{"statement": "x", "confidence": 0.9, "evidence_ids": ["0" * 64]}])
        raise AssertionError("expected EvidenceNotRegistered")
    except EvidenceNotRegistered:
        pass
    sha = ev.put_bytes(b"trusted")
    ids = board.write_facts("c1", [{"statement": "附件含 flag", "confidence": 0.9,
                                    "evidence_ids": [sha]}])
    assert len(ids) == 1
    assert conn.execute("SELECT COUNT(*) c FROM facts").fetchone()["c"] == 1


def test_write_result_rejects_unregistered_evidence():
    board, conn = new_board()
    add_challenge(board, "c1")
    ev = EvidenceStore(tempfile.mkdtemp(), db=conn)
    strict = Blackboard(conn, evidence_check=ev.verify)
    strict.add_intent(IntentDraft(challenge_id="c1", strategy_key="misc.triage.auto", params={}))
    claim = strict.claim_intent("w1", lease_seconds=60)
    bad = WorkerResult(intent_id=claim.intent_id, owner="w1", attempt_no=claim.attempt_no,
                       challenge_id="c1", strategy_key="misc.triage.auto", status="completed",
                       outcome=OUTCOME_SUCCESS, evidence_ids=["f" * 64])
    try:
        strict.write_result(bad, "k-bad")
        raise AssertionError("expected EvidenceNotRegistered")
    except EvidenceNotRegistered:
        pass
