"""隔离失败关闭、租约续期及多连接并发门禁。"""
import concurrent.futures
import json
import os
import sqlite3
import sys
import threading
import time
from unittest.mock import patch

from tests.framework_support import Adapter, FLAG, enqueue, expect, fixture, proof
from ctf_ext.gateway.sandbox import run_command
from ctf_ext.gateway.execution import capture
from ctf_ext.gateway.policy import PolicyConfig, PolicyDenied, PolicyEngine
from ctf_ext.harness.gateway import SubmitGateway
from ctf_ext.runtime.lease import LeaseHeartbeat
from ctf_ext.runtime.scheduler import Scheduler
from ctf_ext.store.blackboard import Blackboard, IntentDraft, WorkerResult
from ctf_ext.domain.models import Challenge


def test_H_default_tool_execution_requires_real_sandbox():
    policy = PolicyEngine(PolicyConfig(command_allowlist=(sys.executable.rsplit('/', 1)[-1],)))
    with patch('ctf_ext.gateway.execution.capture', side_effect=AssertionError('must not execute')):
        expect(PolicyDenied, lambda: run_command([sys.executable, '-c', 'print(1)'], policy))


def test_H_capture_does_not_inherit_model_credentials():
    with fixture() as store:
        with patch.dict(os.environ, {'BAIZHI_API_KEY': 'not-a-real-key'}):
            out = capture([sys.executable, '-I', '-c', 'import os;print(os.environ.get("BAIZHI_API_KEY", "absent"))'],
                          cwd=str(store.workspace('c1')), timeout=2)
        assert out['output'].strip() == b'absent'


def test_E03_heartbeat_renews_live_lease_and_allows_final_write():
    with fixture() as store:
        store.board.add_intent(IntentDraft('c1', 'misc.heartbeat.test'))
        claim = store.board.claim_intent('w', lease_seconds=.3)
        with LeaseHeartbeat(store.board, claim, 'w', .3) as heartbeat:
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                if store.db.execute('SELECT lease_until FROM intents_ext').fetchone()[0] > claim.lease_until + .2:
                    break
                threading.Event().wait(.02)
            current = store.db.execute('SELECT lease_until FROM intents_ext').fetchone()[0]
            assert current > claim.lease_until + .2 and not heartbeat.error
            result = WorkerResult(claim.intent_id, 'w', claim.attempt_no, 'c1', claim.strategy_key, 'completed')
            assert store.board.write_result(result, 'live').first


def test_K02_parallel_schedulers_atomically_respect_project_cap():
    with fixture() as store:
        for index in range(8):
            cid = 'parallel-' + str(index)
            store.board.add_challenge(Challenge(id=cid, name=cid, category='misc'))
            store.board.add_intent(IntentDraft(cid, 'misc.parallel.test'))
        path = store.db.execute('PRAGMA database_list').fetchone()[2]
        barrier = threading.Barrier(8)
        def claim(index):
            db = sqlite3.connect(path, isolation_level=None, timeout=5)
            db.row_factory = sqlite3.Row
            scheduler = Scheduler(Blackboard(db), max_project_workers=1, max_running_projects=1)
            barrier.wait(timeout=5)
            try:
                return scheduler.pick_next('worker-' + str(index))
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(claim, range(8)))
        assert sum(item is not None for item in claims) == 1
        assert store.db.execute("SELECT COUNT(*) FROM intents_ext WHERE status='RUNNING'").fetchone()[0] == 1


def test_D03_multiple_gateways_never_post_same_draft_twice():
    with fixture() as store:
        adapter = Adapter()
        first, second = SubmitGateway(store, adapter), SubmitGateway(store, adapter)
        enqueue(store, first, proof(store))
        try:
            first.pump()
            second.flush()
            first.flush()
            assert len(adapter.calls) == 1
        finally:
            first.close()
            second.close()


def test_D03_legacy_unknown_record_is_query_only():
    with fixture() as store:
        store.db.execute("INSERT INTO submissions(idempotency_key,challenge_id,answer,status,evidence_ids,updated_at) VALUES('old','c1',?,'UNKNOWN','legacy-evidence-id',0)", (FLAG,))
        adapter, gateway = Adapter(), None
        gateway = SubmitGateway(store, adapter)
        try:
            gateway.recover_legacy()
            gateway.flush()
            assert adapter.queries == ['old'] and not adapter.calls
        finally:
            gateway.close()
