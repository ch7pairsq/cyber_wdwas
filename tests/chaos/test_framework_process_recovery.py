"""E-03：真实子进程骤停后的已提交事务恢复，不仅重建 Python 对象。"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from ctf_ext.__main__ import build_runtime
from ctf_ext.harness.schema import SubmitReceipt


def crash_after_checkpoint(root, sending=False):
    code = """
import os, sys
from ctf_ext.__main__ import build_runtime
orch = build_runtime(sys.argv[1] + '/run.db', sys.argv[1] + '/evidence')
orch.bootstrap()
orch._gateway.pump = lambda: []
orch.step()
if sys.argv[2] == 'sending':
    orch._board._db.execute("UPDATE submissions SET status='SENDING'")
    orch._board._db.commit()
os._exit(23)
"""
    done = subprocess.run([sys.executable, '-c', code, root, 'sending' if sending else 'draft'],
                          cwd=str(Path(__file__).resolve().parents[2]), capture_output=True, timeout=10)
    assert done.returncode == 23, done.stderr.decode()


def test_E03_real_process_exit_resumes_draft_without_replaying_solver():
    with tempfile.TemporaryDirectory() as root:
        crash_after_checkpoint(root)
        orch = build_runtime(root + '/run.db', root + '/evidence')
        before = orch._board._db.execute('SELECT COUNT(*) FROM command_log').fetchone()[0]
        result = orch.run(5)
        assert result['results'] == [] and result['solved'] == ['ch-mock-1']
        assert orch._board._db.execute('SELECT COUNT(*) FROM command_log').fetchone()[0] == before
        assert result['quality']['resume_events'] == 1
        orch._board.close()


def test_E03_real_process_sending_recovery_queries_only():
    with tempfile.TemporaryDirectory() as root:
        crash_after_checkpoint(root, sending=True)
        orch = build_runtime(root + '/run.db', root + '/evidence')
        calls = []
        def forbidden(sub):
            raise AssertionError('must not post a possibly sent flag')
        def query(key):
            calls.append(key)
            return SubmitReceipt('ACCEPTED', 'recovered')
        orch._harness.submit_answer = forbidden
        orch._harness.get_receipt = query
        result = orch.run(5)
        assert result['solved'] == ['ch-mock-1'] and len(calls) == 1
        orch._board.close()
