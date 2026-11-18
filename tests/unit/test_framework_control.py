"""B/E/F/G/I：步数策略、持久状态、缓存与预算边界。"""
import copy
import json
import sqlite3
import sys
import threading
from pathlib import Path
from unittest.mock import patch

from tests.framework_support import FLAG, expect, fixture, proof
from ctf_ext.runtime.control import BudgetExceeded, LoopDetected, SessionControl, StepPolicy
from ctf_ext.runtime.scheduler import Scheduler
from ctf_ext.gateway.execution import CommandExecutor, SandboxSpec, capture
from ctf_ext.store.session import StateConflict, StateStore
from ctf_ext.store.blackboard import IntentDraft, StaleWrite, WorkerResult
from ctf_ext.domain.models import Challenge


def test_B01_B02_B03_steps_drive_persisted_routes():
    with fixture() as store:
        control = SessionControl(store, 'c1', stall_steps=200)
        for index in range(1, 71):
            control.reserve('tool', 'v1', [str(index)], str(index))
            _, state = store.load('c1')
            if index in (29, 30, 49, 50, 69, 70):
                assert state['route']['stage'] == sum(index >= n for n in (30, 50, 70, 100))
        events = store.db.execute("SELECT payload FROM events WHERE event_type='route_changed'").fetchall()
        assert [json.loads(row[0])['steps'] for row in events] == [30, 50, 70]
        assert all(json.loads(row[0])['hint'] for row in events)


def test_B04_mislabeled_easy_challenge_is_demoted_by_actual_steps():
    with fixture() as store:
        for cid in ('c1', 'c2'):
            if cid == 'c2':
                store.board.add_challenge(Challenge(id=cid, name=cid, category='misc', difficulty_level=1))
            store.board.add_intent(IntentDraft(cid, 'misc.schedule.test', priority=10))
        store.db.execute('UPDATE challenges SET difficulty_level=1')
        store.patch('c1', lambda s: s.update(steps=70, route=dict(stage=3, strategy='deep_water', model='deepseek-flash')))
        scheduler = Scheduler(store.board, state_store=store, step_policy=StepPolicy())
        assert scheduler.pick_next('w').challenge_id == 'c2'


def test_B05_opening_window_weights_simple_challenges():
    with fixture() as store:
        store.board.add_challenge(Challenge(id='easy', name='简单题', category='misc', difficulty_level=1))
        store.db.execute('UPDATE challenges SET difficulty_level=5 WHERE id="c1"')
        for cid in ('easy', 'c1'):
            store.board.add_intent(IntentDraft(cid, 'misc.schedule.test', priority=1))
        scheduler = Scheduler(store.board, state_store=store, step_policy=StepPolicy())
        assert scheduler.pick_next('w').challenge_id == 'easy'


def test_B03_deep_water_has_one_slot_and_heartbeat_none():
    with fixture() as store:
        for index in range(3):
            store.board.add_intent(IntentDraft('c1', 'misc.path.p' + str(index)))
        store.patch('c1', lambda s: s.update(steps=70))
        scheduler = Scheduler(store.board, max_project_workers=5, state_store=store, step_policy=StepPolicy())
        assert scheduler.pick_next('w1') is not None
        assert scheduler.pick_next('w2') is None
        store.db.execute("UPDATE intents_ext SET status='COMPLETED',owner=NULL,lease_until=NULL WHERE status='RUNNING'")
        store.patch('c1', lambda s: s.update(steps=100))
        assert scheduler.pick_next('w3') is None


def test_E01_E04_compaction_keeps_exact_state_and_command_archives():
    with fixture() as store:
        decision = proof(store)
        store.patch('c1', lambda s: s.update(tried_paths=['path-' + str(i) for i in range(50)]))
        _, before = store.load('c1')
        after = store.compact('c1')
        archived = after['evidence_ids'][-1]
        assert json.loads(store.restore_evidence(archived)) == before
        assert len(after['tried_paths']) == 10
        assert store.restore_evidence(decision.evidence_ids[0]) == FLAG.encode()


def test_E02_E03_fresh_connection_recovers_steps_route_and_evidence():
    with fixture() as store:
        decision = proof(store)
        store.patch('c1', lambda s: s.update(steps=50, route=dict(stage=2, strategy='alternate', model='deepseek-flash')))
        revision, before = store.load('c1')
        db = sqlite3.connect(store.db.execute('PRAGMA database_list').fetchone()[2], isolation_level=None)
        db.row_factory = sqlite3.Row
        from ctf_ext.store.blackboard import Blackboard
        from ctf_ext.store.evidence import EvidenceStore
        recovered = StateStore(Blackboard(db), EvidenceStore(store.evidence._root, db), store.root)
        assert recovered.load('c1') == (revision, before)
        assert recovered.restore_evidence(decision.evidence_ids[0]) == FLAG.encode()
        db.close()


def test_E05_schema_and_stale_revision_are_rejected():
    with fixture() as store:
        revision, state = store.load('c1')
        store.save('c1', revision, state)
        expect(StateConflict, lambda: store.save('c1', revision, state))
        for key, value in [('steps', -1), ('budget', {}), ('route', {}), ('status', 'untrusted'), ('call_signatures', {'x': -1})]:
            bad = copy.deepcopy(state)
            bad[key] = value
            expect(ValueError, lambda: store.validate(bad))


def test_F01_repeated_call_signature_intervenes():
    with fixture() as store:
        control = SessionControl(store, 'c1')
        control.reserve('tool', 'v1', ['x'], 'input')
        control.reserve('tool', 'v1', ['x'], 'input')
        expect(LoopDetected, lambda: control.reserve('tool', 'v1', ['x'], 'input'))
        assert store.load('c1')[1]['status'] == 'stopped'
        assert store.db.execute("SELECT COUNT(*) FROM events WHERE event_type='loop_intervention'").fetchone()[0] == 1


def test_F02_budget_compacts_before_exhaustion_and_stops_calls():
    with fixture() as store:
        control = SessionControl(store, 'c1', max_tokens=100)
        control.charge_tokens(80)
        assert store.load('c1')[1]['evidence_ids']
        expect(BudgetExceeded, lambda: control.charge_tokens(21))
        expect(BudgetExceeded, lambda: control.reserve('model', 'v1', [], 'x', is_tool=False))


def test_F02_deadline_and_tool_budget_are_enforced():
    with fixture() as store:
        control = SessionControl(store, 'c1', max_tools=1)
        control.reserve('tool', 'v1', [], 'first')
        expect(BudgetExceeded, lambda: control.reserve('tool', 'v1', [], 'second'))
    with fixture() as store:
        control = SessionControl(store, 'c1', absolute_deadline=0)
        expect(BudgetExceeded, lambda: control.reserve('tool', 'v1', [], 'first'))


def test_F03_stall_and_confirmed_session_stop_work():
    with fixture() as store:
        control = SessionControl(store, 'c1', stall_steps=2)
        control.reserve('tool', 'v1', ['a'], 'a')
        expect(LoopDetected, lambda: control.reserve('tool', 'v1', ['b'], 'b'))
    with fixture() as store:
        control = SessionControl(store, 'c1')
        store.patch('c1', lambda s: s.update(status='confirmed'))
        expect(BudgetExceeded, lambda: control.reserve('tool', 'v1', [], 'x'))


def test_G01_cache_hit_skips_subprocess():
    with fixture() as store:
        executor = CommandExecutor(store, SessionControl(store, 'c1'), 'environment-1', allow_local_mock=True)
        first = executor.execute_mock_decoder(FLAG.encode(), decode=False)
        with patch('ctf_ext.gateway.execution.capture', side_effect=AssertionError('cache should avoid execution')):
            second = executor.execute_mock_decoder(FLAG.encode(), decode=False)
        assert first[:3] == second[:3] and second[3] is True


def test_G02_G03_cache_identity_contains_target_environment_version_and_input():
    args = ['c1', 'target-v1', 'environment-1', 'decoder', 'v1', ['decode'], 'input-hash']
    original = StateStore.cache_key(*args)
    for index in range(len(args)):
        changed = list(args)
        changed[index] = ['other'] if index == 5 else 'other'
        assert StateStore.cache_key(*changed) != original


def test_G02_expired_or_tampered_cache_is_not_used():
    with fixture() as store:
        executor = CommandExecutor(store, SessionControl(store, 'c1'), 'e1', allow_local_mock=True)
        executor.execute_mock_decoder(FLAG.encode(), decode=False)
        key = store.db.execute('SELECT key FROM tool_cache').fetchone()[0]
        assert store.cached(key)
        store.db.execute('UPDATE tool_cache SET expires=0')
        assert store.cached(key) is None
        store.db.execute('UPDATE tool_cache SET expires=99999999999')
        path = store.db.execute('SELECT path FROM evidence').fetchone()[0]
        Path(path).write_bytes(b'corrupted')
        assert store.cached(key) is None


def test_H_sandbox_spec_is_fail_closed_and_has_all_isolation_flags():
    with fixture() as store:
        spec = SandboxSpec('sha256:' + 'a' * 64, str(store.workspace('c1')), 'ctf-test-c1')
        argv = spec.argv(['python3', '-c', 'print(1)'])
        for flag, value in [('--network', 'none'), ('--user', '10001:10001'), ('--cap-drop', 'ALL'), ('--pull', 'never')]:
            assert argv[argv.index(flag) + 1] == value
        assert '--read-only' in argv and 'no-new-privileges:true' in argv
        expect(ValueError, lambda: SandboxSpec('image:latest', spec.workspace, spec.name).argv(['true']))
        assert store.workspace('c1') != store.workspace('c2')


def test_capture_is_bounded_and_timeout_is_not_complete_evidence():
    with fixture() as store:
        output = capture([sys.executable, '-I', '-c', 'print("x"*100000)'], cwd=str(store.workspace('c1')), timeout=2, limit=64)
        assert len(output['output']) == 64 and output['truncated'] and not output['complete']
        output = capture([sys.executable, '-I', '-c', 'import time;time.sleep(2)'], cwd=str(store.workspace('c1')), timeout=.05)
        assert output['timed_out'] and not output['complete']


def test_expired_lease_cannot_write_or_be_resurrected():
    with fixture() as store:
        store.board.add_intent(IntentDraft('c1', 'misc.lease.test'))
        claim = store.board.claim_intent('w')
        store.db.execute('UPDATE intents_ext SET lease_until=0')
        assert store.board.heartbeat(claim.intent_id, 'w') is False
        result = WorkerResult(claim.intent_id, 'w', claim.attempt_no, 'c1', claim.strategy_key, 'completed')
        expect(StaleWrite, lambda: store.board.write_result(result, 'expired'))
