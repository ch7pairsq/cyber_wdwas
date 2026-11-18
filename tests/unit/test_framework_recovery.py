"""心跳、外部 supervisor、工作区隔离与证据事务边界。"""
import hashlib
import os
import tempfile
import time
from pathlib import Path

from tests.framework_support import FLAG, fixture
from ctf_ext.llm.compress import ContextItem, build_context, estimate_tokens
from ctf_ext.runtime.heartbeat import HeartbeatFile
from scripts.supervise import should_restart


def test_heartbeat_file_detects_missing_and_stale_process():
    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "heartbeat")
        beat = HeartbeatFile(path)
        assert beat.touch() is True
        assert HeartbeatFile.age(path) is not None
        assert HeartbeatFile.is_stale(path, 60) is False
        os.utime(path, (time.time() - 120, time.time() - 120))
        assert HeartbeatFile.is_stale(path, 60) is True
        assert HeartbeatFile.is_stale(os.path.join(root, "missing"), 60) is True
        assert HeartbeatFile.is_stale(path, 60, running=False) is False


def test_supervisor_restart_policy_is_bounded():
    assert should_restart(1, False, 0, 3) is True
    assert should_restart(1, False, 3, 3) is False
    assert should_restart(None, True, 0, 3) is True
    assert should_restart(None, True, 3, 3) is False
    assert should_restart(0, False, 0, 3) is False


def test_workspace_is_per_challenge_mode_0700_and_rejects_symlink():
    with fixture() as store:
        path = store.workspace("c1")
        assert path != store.workspace("c2")
        assert (path.stat().st_mode & 0o777) == 0o700
        name = hashlib.sha256("symlink-target".encode()).hexdigest()
        target = Path(tempfile.mkdtemp())
        os.symlink(str(target), str(store.root / name))
        try:
            store.workspace("symlink-target")
            raise AssertionError("symlink workspace accepted")
        except ValueError:
            pass


def test_evidence_registration_does_not_commit_board_transaction_early():
    with fixture() as store:
        with store.board._write() as db:
            eid = store.evidence.put_bytes(b"inside-transaction")
            assert db.in_transaction
        assert not store.db.in_transaction
        assert store.evidence.verify(eid) is True


def test_evidence_verify_rejects_symlinked_content_path():
    with fixture() as store:
        eid = store.evidence.put_bytes(b"real evidence")
        row = store.db.execute("SELECT path FROM evidence WHERE sha256=?", (eid,)).fetchone()
        target = Path(row["path"])
        link = target.with_name(target.name + "-link")
        os.symlink(str(target), str(link))
        store.db.execute("UPDATE evidence SET path=? WHERE sha256=?", (str(link), eid))
        assert store.evidence.verify(eid) is False


def test_context_budget_never_exceeds_limit_and_keeps_pointer_ids():
    items = [ContextItem("a", "x" * 100, 3), ContextItem("b", "y" * 100, 2),
             ContextItem("c", "z" * 100, 1)]
    for budget in (1, 2, 5, 10, 20, 100):
        pack = build_context(items, budget)
        assert pack.est_tokens <= budget
        assert estimate_tokens(pack.text) <= budget
        assert pack.pointers == ["[summary:a]", "[summary:b]", "[summary:c]"] or budget >= 100
