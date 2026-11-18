"""运行期加固：有界超时、退避等待、执行报告与跨重启记忆。"""
import os
import tempfile
import time

from ctf_ext.__main__ import build_runtime
from ctf_ext.runtime.heartbeat import HeartbeatFile


class SlowWorker:
    def work(self, claim, challenge):
        time.sleep(2)


def test_timeout_is_bounded_and_does_not_abort_other_work():
    with tempfile.TemporaryDirectory() as root:
        orch = build_runtime(root + "/run.db", root + "/evidence")
        orch.bootstrap()
        orch._worker = SlowWorker()
        orch._claim_timeout = 0.05
        orch._retry_backoff = 0.2
        orch._scheduler._retry_backoff = 0.2
        start = time.monotonic()
        out = orch.run(0.6)
        assert time.monotonic() - start < 3
        assert out["solved"] == []
        assert out["execution"]["mode"] == "serial" and out["execution"]["effective_parallel"] == 1
        assert out["execution"]["retry_backoff_seconds"] == 0.2
        count = orch._board._db.execute("SELECT COUNT(*) FROM events WHERE event_type='claim_timeout'").fetchone()[0]
        assert count >= 1
        assert orch._board._db.execute("SELECT COUNT(*) FROM events WHERE event_type='run_aborted'").fetchone()[0] == 0
        orch._gateway.close()
        orch._board.close()


def test_failed_intent_is_not_reclaimed_inside_backoff_window():
    with tempfile.TemporaryDirectory() as root:
        orch = build_runtime(root + "/run.db", root + "/evidence")
        orch.bootstrap()
        orch._worker = SlowWorker()
        orch._claim_timeout = 0.05
        orch._scheduler._retry_backoff = 60
        assert orch.step()["outcome"] == "timeout"
        before = orch._board._db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
        assert orch.step() is None
        assert orch._board._db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == before
        assert orch._scheduler.has_waiting_work() is True
        orch._gateway.close()
        orch._board.close()


def test_run_report_states_serial_execution_and_does_not_claim_parallelism():
    with tempfile.TemporaryDirectory() as root:
        orch = build_runtime(root + "/run.db", root + "/evidence")
        out = orch.run(5)
        execution = out["execution"]
        assert execution["mode"] == "serial"
        assert execution["effective_parallel"] == 1
        assert execution["configured_max_workers"] >= 1
        assert execution["isolation"]["container"] == "not_verified"
        assert "parallel" in execution["mode"] or execution["effective_parallel"] == 1
        orch._board.close()


def test_heartbeat_file_is_refreshed_during_run():
    with tempfile.TemporaryDirectory() as root:
        beat = os.path.join(root, "heartbeat")
        orch = build_runtime(root + "/run.db", root + "/evidence", heartbeat_path=beat)
        orch.run(5)
        assert os.path.exists(beat)
        assert HeartbeatFile.age(beat) is not None and HeartbeatFile.age(beat) < 5
        orch._board.close()


def test_lessons_persist_across_runtime_restart_without_replay():
    with tempfile.TemporaryDirectory() as root:
        db, ev = root + "/run.db", root + "/evidence"
        first = build_runtime(db, ev)
        one = first.run(5)
        assert one["solved"] == ["ch-mock-1"]
        lessons_before = first._board._db.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
        assert lessons_before >= 1
        first._board.close()
        second = build_runtime(db, ev)
        two = second.run(5)
        assert two["results"] == [] and two["solved"] == ["ch-mock-1"]
        assert second._board._db.execute("SELECT COUNT(*) FROM lessons").fetchone()[0] == lessons_before
        assert second._board.lesson_context("ch-mock-1"), "重启后教训应可检索"
        second._board.close()
