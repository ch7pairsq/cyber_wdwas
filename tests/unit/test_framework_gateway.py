"""D-01..05：持久化提交、非阻塞、幂等及唯一格式修正。"""
import json
import threading
import time

from tests.framework_support import Adapter, FLAG, enqueue, expect, fixture, proof
from ctf_ext.harness.gateway import SubmitGateway
from ctf_ext.harness.schema import SubmitReceipt
from ctf_ext.harness.flag_gate import GateDecision
from ctf_ext.store.blackboard import IntentDraft, WorkerResult


def test_D01_explicit_platform_format_correction_retries_once():
    with fixture() as store:
        class FormatAdapter(Adapter):
            def submit_answer(self, sub):
                self.calls.append(sub)
                if sub.answer == FLAG:
                    return SubmitReceipt('REJECTED', detail='格式错误', corrected_answer=FLAG.upper())
                return SubmitReceipt('ACCEPTED', 'formatted')
        adapter = FormatAdapter()
        gateway = SubmitGateway(store, adapter)
        try:
            enqueue(store, gateway, proof(store))
            gateway.flush()
            assert len(adapter.calls) == 2
            assert adapter.calls[1].answer == FLAG.upper()
            assert adapter.calls[1].idempotency_key.endswith('-format1')
            assert store.db.execute("SELECT COUNT(*) FROM submissions WHERE status='ACCEPTED'").fetchone()[0] == 1
        finally:
            gateway.close()


def test_D01_invalid_correction_or_second_rejection_never_loops():
    for corrected in ('flag{different_6a9e8}', FLAG.upper()):
        with fixture() as store:
            adapter = Adapter(SubmitReceipt('REJECTED', detail='错误', corrected_answer=corrected))
            gateway = SubmitGateway(store, adapter)
            try:
                enqueue(store, gateway, proof(store))
                gateway.flush()
                assert len(adapter.calls) == (2 if corrected == FLAG.upper() else 1)
            finally:
                gateway.close()


def test_D02_rejection_has_diagnosis_without_blind_retry():
    with fixture() as store:
        adapter = Adapter(SubmitReceipt('REJECTED', detail='flag mismatch'))
        gateway = SubmitGateway(store, adapter)
        try:
            enqueue(store, gateway, proof(store))
            gateway.flush()
            payload = json.loads(store.db.execute("SELECT payload FROM events WHERE event_type='submission_diagnosis'").fetchone()[0])
            assert payload['suggestion'] and payload['detail'] == 'flag mismatch'
            assert len(adapter.calls) == 1
        finally:
            gateway.close()


def test_D03_duplicate_and_D05_second_candidate_not_enqueued():
    with fixture() as store:
        adapter, decision = Adapter(), proof(store)
        gateway = SubmitGateway(store, adapter)
        try:
            key = enqueue(store, gateway, decision)
            assert enqueue(store, gateway, decision) == key
            gateway.flush()
            other = 'flag{another_72a39}'
            assert enqueue(store, gateway, proof(store, output=other, declared=other)) == key
            gateway.flush()
            assert len(adapter.calls) == 1
            assert store.db.execute('SELECT COUNT(*) FROM submissions').fetchone()[0] == 1
        finally:
            gateway.close()


def test_D04_slow_submission_does_not_block_main_scheduler():
    with fixture() as store:
        entered, release = threading.Event(), threading.Event()
        class Slow(Adapter):
            def submit_answer(self, sub):
                self.calls.append(sub)
                entered.set()
                if not release.wait(2):
                    raise TimeoutError()
                return SubmitReceipt('ACCEPTED')
        adapter, gateway = Slow(), None
        gateway = SubmitGateway(store, adapter)
        try:
            enqueue(store, gateway, proof(store))
            started = time.monotonic()
            gateway.pump()
            assert time.monotonic() - started < .5
            assert entered.wait(1)
            store.board.add_intent(IntentDraft('c1', 'misc.background.test'))
            assert store.board.claim_intent('available-worker') is not None
            assert not gateway.pump()
            release.set()
            gateway.flush()
            assert len(adapter.calls) == 1
        finally:
            release.set()
            gateway.flush()
            gateway.close()


def test_uncertain_transport_exception_is_query_only_after_restart():
    with fixture() as store:
        adapter = Adapter(TimeoutError('do not log secrets'))
        gateway = SubmitGateway(store, adapter, query_interval=0)
        try:
            enqueue(store, gateway, proof(store))
            gateway.flush(timeout=.5)
            assert len(adapter.calls) == 1 and adapter.queries
            assert store.db.execute('SELECT status FROM submissions').fetchone()[0] == 'ACCEPTED'
        finally:
            gateway.close()


def test_D03_sending_recovery_never_reposts():
    with fixture() as store:
        adapter = Adapter()
        first = SubmitGateway(store, adapter)
        key = enqueue(store, first, proof(store))
        store.db.execute("UPDATE submissions SET status='SENDING' WHERE idempotency_key=?", (key,))
        first.close()
        recovered = SubmitGateway(store, adapter)
        try:
            recovered.flush()
            assert not adapter.calls and adapter.queries == [key]
            assert store.db.execute('SELECT status FROM submissions').fetchone()[0] == 'ACCEPTED'
        finally:
            recovered.close()


def test_D03_committed_draft_survives_gateway_restart():
    with fixture() as store:
        adapter = Adapter()
        first = SubmitGateway(store, adapter)
        enqueue(store, first, proof(store))
        first.close()
        recovered = SubmitGateway(store, adapter)
        try:
            recovered.flush()
            assert len(adapter.calls) == 1
        finally:
            recovered.close()


def test_A03_unconfirmed_candidate_cannot_enter_outbox():
    with fixture() as store:
        gateway = SubmitGateway(store, Adapter())
        expect(ValueError, lambda: enqueue(store, gateway, GateDecision('c1', FLAG, 'pending', 'L2', '不可确认')))
        assert store.db.execute('SELECT COUNT(*) FROM submissions').fetchone()[0] == 0
        assert store.db.execute('SELECT COUNT(*) FROM gate_decisions').fetchone()[0] == 0


def test_A03_proof_change_between_enqueue_and_send_blocks_submission():
    with fixture() as store:
        adapter = Adapter()
        gateway = SubmitGateway(store, adapter)
        enqueue(store, gateway, proof(store))
        store.declare('c1', 'flag{replacement_29ad}')
        gateway.flush()
        assert not adapter.calls
        assert store.db.execute('SELECT status FROM submissions').fetchone()[0] == 'BLOCKED'
        gateway.close()


def test_result_and_outbox_commit_atomically_on_failure():
    with fixture() as store:
        store.board.add_intent(IntentDraft('c1', 'misc.atomic.test'))
        claim = store.board.claim_intent('w')
        result = WorkerResult(claim.intent_id, 'w', claim.attempt_no, 'c1', claim.strategy_key, 'completed')
        decision = proof(store)
        gateway = SubmitGateway(store, Adapter())
        def abort(db):
            from ctf_ext.harness.flag_gate import FlagGate
            FlagGate(store).record(db, decision)
            gateway.enqueue(db, decision)
            raise RuntimeError('模拟事务内崩溃')
        expect(RuntimeError, lambda: store.board.write_result(result, 'atomic-key', transaction_hook=abort))
        assert store.db.execute('SELECT COUNT(*) FROM results_ext').fetchone()[0] == 0
        assert store.db.execute('SELECT COUNT(*) FROM submissions').fetchone()[0] == 0
        assert store.db.execute('SELECT COUNT(*) FROM gate_decisions').fetchone()[0] == 0
        assert store.db.execute('SELECT status FROM intents_ext').fetchone()[0] == 'RUNNING'
        gateway.close()
