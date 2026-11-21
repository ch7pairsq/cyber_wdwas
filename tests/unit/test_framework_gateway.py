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
            gateway.flush(5)
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
                gateway.flush(5)
                assert len(adapter.calls) == (2 if corrected == FLAG.upper() else 1)
            finally:
                gateway.close()


def test_D02_rejection_has_diagnosis_without_blind_retry():
    with fixture() as store:
        adapter = Adapter(SubmitReceipt('REJECTED', detail='flag mismatch'))
        gateway = SubmitGateway(store, adapter)
        try:
            enqueue(store, gateway, proof(store))
            gateway.flush(5)
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
            gateway.flush(5)
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
                if not release.wait(10):
                    raise TimeoutError()
                return SubmitReceipt('ACCEPTED')
        adapter, gateway = Slow(), None
        gateway = SubmitGateway(store, adapter)
        try:
            enqueue(store, gateway, proof(store))
            started = time.monotonic()
            gateway.pump()
            # 阻塞下界是 10s（adapter 卡在 release）：pump 只要不等到提交完成
            # 即为非阻塞；2s 的上界对负载抖动留足余量，语义与 <.5 时一致。
            assert time.monotonic() - started < 2
            assert entered.wait(5)
            store.board.add_intent(IntentDraft('c1', 'misc.background.test'))
            assert store.board.claim_intent('available-worker') is not None
            assert not gateway.pump()
            release.set()
            gateway.flush(5)
            assert len(adapter.calls) == 1
        finally:
            release.set()
            gateway.flush(5)
            gateway.close()


def test_uncertain_transport_exception_is_query_only_after_restart():
    with fixture() as store:
        adapter = Adapter(TimeoutError('do not log secrets'))
        gateway = SubmitGateway(store, adapter, query_interval=0)
        try:
            enqueue(store, gateway, proof(store))
            gateway.flush(timeout=5)
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
            recovered.flush(5)
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
            recovered.flush(5)
            assert len(adapter.calls) == 1
        finally:
            recovered.close()


def test_rejected_allows_new_answer_but_dedups_same_flag():
    # 2026-11-21 正式赛第 2 题错旗教训：REJECTED 后题目不能永久关闭——
    # 不同答案的新候选必须能重新入队；同一答案重复被拒不重发；
    # 回执未定（DRAFT/SENDING/UNKNOWN）与已 ACCEPTED 仍保持去重。
    with fixture() as store:
        adapter = Adapter(SubmitReceipt('REJECTED', detail='flag mismatch'))
        gateway = SubmitGateway(store, adapter)
        try:
            first = enqueue(store, gateway, proof(store))
            gateway.flush(5)
            assert len(adapter.calls) == 1
            # 同一 flag 再次确认 → 沿用旧键，不重发
            assert enqueue(store, gateway, proof(store)) == first
            # 不同答案的新候选 → 新键入队并重发；回执事件携带 answer 原文
            other = 'flag{retry_91c4e2}'
            second = enqueue(store, gateway, proof(store, output=other, declared=other))
            assert second != first
            completed = gateway.flush(5)
            assert len(adapter.calls) == 2
            assert adapter.calls[1].answer == other
            rejected_events = [e for e in completed if e['status'] == 'REJECTED']
            assert rejected_events and rejected_events[-1]['answer'] == other
            assert store.db.execute('SELECT COUNT(*) FROM submissions').fetchone()[0] == 2
            # 第二个候选也被拒后再提交同答案 → 去重；换新答案 → 放行
            assert enqueue(store, gateway, proof(store, output=other, declared=other)) == second
            third = 'flag{retry_44f0b7}'
            assert enqueue(store, gateway, proof(store, output=third, declared=third)) not in (first, second)
        finally:
            gateway.close()


def test_pending_or_accepted_submission_still_dedups_new_candidates():
    # 回执未定（DRAFT）时不同答案的新候选沿用旧键（test_D03 覆盖）；
    # 这里锁定已 ACCEPTED 后：同答案/新答案都不再入队。
    with fixture() as store:
        adapter = Adapter()
        gateway = SubmitGateway(store, adapter)
        try:
            key = enqueue(store, gateway, proof(store))
            gateway.flush(5)  # → ACCEPTED
            assert len(adapter.calls) == 1
            other = 'flag{pending_5d2e8}'
            assert enqueue(store, gateway, proof(store)) == key
            assert enqueue(store, gateway, proof(store, output=other, declared=other)) == key
            assert len(adapter.calls) == 1
            assert store.db.execute('SELECT COUNT(*) FROM submissions').fetchone()[0] == 1
        finally:
            gateway.close()


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
        gateway.flush(5)
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
