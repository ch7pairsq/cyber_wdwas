"""单题硬超时、失败退避与主循环异常隔离。"""
import time
import tempfile

from tests.framework_support import fixture
from ctf_ext.__main__ import build_runtime
from ctf_ext.domain.models import Challenge
from ctf_ext.store.blackboard import IntentDraft
from ctf_ext.runtime.watchdog import ClaimTimeout, DeadlineGuard
from ctf_ext.store.blackboard import StaleWrite, WorkerResult


class SlowWorker:
    def work(self, claim, challenge):
        time.sleep(2)


def test_deadline_guard_interrupts_main_thread_and_restores_handler():
    started = time.monotonic()
    try:
        with DeadlineGuard(0.05):
            time.sleep(2)
        raise AssertionError("timeout did not fire")
    except ClaimTimeout:
        pass
    assert time.monotonic() - started < 1


def test_claim_timeout_releases_intent_for_retry_without_stopping_challenge():
    with tempfile.TemporaryDirectory() as root:
        orch = build_runtime(root + "/run.db", root + "/evidence")
        orch.bootstrap()
        orch._worker = SlowWorker()
        orch._claim_timeout = 0.05
        started = time.monotonic()
        out = orch.step()
        assert time.monotonic() - started < 1
        assert out["status"] == "failed" and out["outcome"] == "timeout"
        row = orch._board._db.execute("SELECT status FROM intents_ext").fetchone()
        assert row["status"] == "FAILED_RETRYABLE"
        attempt = orch._board._db.execute("SELECT outcome,failure_reason FROM attempts").fetchone()
        assert attempt["outcome"] == "timeout" and attempt["failure_reason"] == "claim_timeout"
        assert orch._board._db.execute("SELECT COUNT(*) FROM events WHERE event_type='claim_timeout'").fetchone()[0] == 1
        assert orch._state.load("ch-mock-1")[1]["status"] != "stopped"
        orch._gateway.close()
        orch._board.close()


def test_worker_exception_is_retryable_and_does_not_stop_session():
    with tempfile.TemporaryDirectory() as root:
        orch = build_runtime(root + "/run.db", root + "/evidence")
        orch.bootstrap()
        class Broken:
            def work(self, claim, challenge):
                raise RuntimeError("transport down")
        orch._worker = Broken()
        out = orch.step()
        assert out["status"] == "failed" and out["outcome"] == "error"
        assert orch._board._db.execute("SELECT status FROM intents_ext").fetchone()["status"] == "FAILED_RETRYABLE"
        assert orch._state.load("ch-mock-1")[1]["status"] != "stopped"
        orch._gateway.close()
        orch._board.close()


def test_result_write_failure_does_not_crash_step():
    with tempfile.TemporaryDirectory() as root:
        orch = build_runtime(root + "/run.db", root + "/evidence")
        orch.bootstrap()
        def boom(*args, **kwargs):
            raise StaleWrite("lease lost")
        orch._board.write_result = boom
        out = orch.step()
        assert out["status"] == "write_failed" and out["write_error"] == "StaleWrite"
        assert orch._board._db.execute("SELECT COUNT(*) FROM events WHERE event_type='result_write_failed'").fetchone()[0] == 1
        assert orch._board._db.execute("SELECT status FROM intents_ext").fetchone()["status"] == "FAILED_RETRYABLE"
        orch._gateway.close()
        orch._board.close()


def test_contract_violation_does_not_stop_challenge_permanently():
    with tempfile.TemporaryDirectory() as root:
        orch = build_runtime(root + "/run.db", root + "/evidence")
        orch.bootstrap()
        class BadContract:
            def work(self, claim, challenge):
                return WorkerResult(claim.intent_id, "bad", claim.attempt_no, claim.challenge_id,
                    "", "completed", summary=["bad"], outcome="success")
        orch._worker = BadContract()
        out = orch.step()
        assert out["status"] == "failed" and out["candidate_answer"] is None
        assert orch._board._db.execute("SELECT status FROM intents_ext").fetchone()["status"] == "FAILED_RETRYABLE"
        assert orch._state.load("ch-mock-1")[1]["status"] != "stopped"
        orch._gateway.close()
        orch._board.close()


def test_gateway_pump_error_is_recorded_without_crashing_step():
    with tempfile.TemporaryDirectory() as root:
        orch = build_runtime(root + "/run.db", root + "/evidence")
        orch.bootstrap()
        def boom():
            raise RuntimeError("gateway stuck")
        orch._gateway.pump = boom
        orch._scheduler.pick_next = lambda *args, **kwargs: None
        assert orch.step() is None
        assert orch._board._db.execute("SELECT COUNT(*) FROM events WHERE event_type='gateway_pump_failed'").fetchone()[0] >= 1
        orch._gateway.close()
        orch._board.close()


def test_release_requires_current_attempt_to_protect_other_process():
    with fixture() as store:
        store.board.add_challenge(Challenge(id="c2", name="c2", category="crypto"))
        store.board.add_intent(IntentDraft("c2", "crypto.rsa"))
        claim = store.board.claim_intent("llm")
        store.board.release(claim.intent_id, "llm", "error", attempt_no=claim.attempt_no + 1)
        assert store.board._db.execute("SELECT status FROM intents_ext").fetchone()["status"] == "RUNNING"
        store.board.release(claim.intent_id, "llm", "error", attempt_no=claim.attempt_no)
        assert store.board._db.execute("SELECT status FROM intents_ext").fetchone()["status"] == "FAILED_RETRYABLE"


def test_consecutive_step_errors_abort_run_instead_of_hanging():
    with tempfile.TemporaryDirectory() as root:
        orch = build_runtime(root + "/run.db", root + "/evidence")
        def boom():
            raise RuntimeError("scheduler failure")
        orch.step = boom
        out = orch.run(2)
        assert out["solved"] == []
        assert orch._board._db.execute("SELECT COUNT(*) FROM events WHERE event_type='run_aborted'").fetchone()[0] == 1
        orch._gateway.close()
        orch._board.close()
