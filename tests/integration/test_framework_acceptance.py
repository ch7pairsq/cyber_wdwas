"""A/B/K/L：主入口离线模拟，不冒充比赛全真验收。"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from ctf_ext.__main__ import build_runtime
from ctf_ext.config import HotConfig, validate_config
from ctf_ext.domain.models import Challenge
from ctf_ext.store.blackboard import IntentDraft, WorkerResult
from ctf_ext.harness.gateway import SubmitGateway
from ctf_ext.harness.schema import SubmitReceipt
from tests.framework_support import FLAG, expect, fixture


def config():
    return {'concurrency': {'max_workers': 1, 'max_project_workers': 1},
            'storage': {'db_path': 'unused.db'},
            'harness': {'protocol': 'mock', 'competition_locked': False},
            'download': {'max_single_bytes': 100, 'max_total_bytes': 1000}}


def test_A01_A02_K04_full_entrypoint_has_traceable_gates_and_metrics():
    with tempfile.TemporaryDirectory() as root:
        orch = build_runtime(root + '/run.db', root + '/evidence')
        result = orch.run(10)
        assert result['solved'] == ['ch-mock-1']
        assert result['results'][0]['gate'] == {'status': 'confirmed', 'level': 'L1'}
        row = orch._board._db.execute('SELECT payload FROM gate_decisions').fetchone()
        assert json.loads(row[0])['command_id']
        quality = result['quality']
        assert quality['submission_success_rate'] == 1
        assert quality['average_steps'] > 0
        assert quality['submission_average_latency_seconds'] >= 0
        assert set(quality['gate_veto_rates']) == {'grounding', 'negation', 'interrogation'}
        assert quality['first_kill_rate'] is None
        orch._board.close()


def test_A03_main_entrypoint_rejects_injected_ungrounded_candidate():
    with tempfile.TemporaryDirectory() as root:
        orch = build_runtime(root + '/run.db', root + '/evidence')
        class HallucinatingWorker:
            def work(self, claim, challenge):
                eid = orch._evidence.put_bytes(b'not a flag')
                return WorkerResult(claim.intent_id, '', claim.attempt_no, claim.challenge_id,
                    claim.strategy_key, 'completed', summary=['候选缺乏证据'], outcome='success',
                    evidence_ids=[eid], candidate_answer=FLAG,
                    resource_usage={'wall_seconds': 0, 'model_tokens': 0, 'tool_calls': 0})
        orch._worker = HallucinatingWorker()
        result = orch.run(5)
        assert result['solved'] == []
        assert result['results'][0]['gate'] == {'status': 'rejected', 'level': 'L4'}
        assert orch._board._db.execute('SELECT COUNT(*) FROM submissions').fetchone()[0] == 0
        orch._board.close()


def test_K01_offline_twenty_cases_including_five_adversarial_inputs():
    with tempfile.TemporaryDirectory() as root:
        orch = build_runtime(root + '/run.db', root + '/evidence')
        tasks = []
        for index in range(20):
            answer = 'flag{exercise_%02d_71be9}' % index
            output = answer
            if index == 15:
                output = 'session-id: ' + answer
            elif index == 16:
                output = '诱饵: ' + answer
            elif index == 17:
                output = answer[:-2]
            elif index == 18:
                output = 'prefix' + answer
            elif index == 19:
                output = 'flag{aaaaaaa}'
            cid = 'fixture-%02d' % index
            ref = 'mock://' + cid + '/input'
            if index % 2 == 0:
                output = base64.b64encode(output.encode()).decode()
            tasks.append({'challenge_id': cid, 'name': '离线混合用例 ' + str(index),
                'category': 'misc', 'difficulty_level': 1 + index % 5, 'points_hint': 100,
                'description': '固定离线命令测试，不代表真实解题难度', 'attachments': [ref],
                'metadata': {'files': {ref: output}}, 'flag': answer})
        orch._harness._raw = tasks
        # run(n) 的语义是 n 秒墙钟而非 n 步：20 个意图串行本身就要 ~19s，
        # 20s 预算在负载下必然截断（曾致 10/15 的偶发失败）；本测试验证
        # 端到端接线而非耗时，放宽到 120s。
        result = orch.run(120)
        assert result['tasks'] == 20 and len(result['solved']) == 15
        assert all(int(cid[-2:]) < 15 for cid in result['solved'])
        assert orch._board._db.execute('SELECT COUNT(*) FROM submissions').fetchone()[0] == 15
        assert orch._board._db.execute("SELECT COUNT(*) FROM submissions WHERE status='REJECTED'").fetchone()[0] == 0
        assert orch._board._db.execute('SELECT COUNT(*) FROM command_log').fetchone()[0] == 30
        assert result['quality']['accepted_first_30m_conversion'] == .75
        orch._board.close()


def test_final_drain_recovers_inflight_submission_at_deadline():
    # 一次性窗口（正式赛 4h）：主循环 deadline 时 SENDING 的提交必须在
    # 收尾宽限内收回回执，不能停在 SENDING 丢分。旧实现收尾 flush 只有
    # min(1, 剩余) 秒（deadline 后为 0），回执往返一慢就永远丢失。
    with tempfile.TemporaryDirectory() as root:
        orch = build_runtime(root + '/run.db', root + '/evidence')
        entered, release = threading.Event(), threading.Event()

        class SlowAdapter:
            def __init__(self):
                self.calls, self.queries = [], []

            def submit_answer(self, sub):
                self.calls.append(sub)
                entered.set()
                if not release.wait(10):
                    raise TimeoutError()
                return SubmitReceipt('ACCEPTED', 'drained')

            def get_receipt(self, key):
                self.queries.append(key)
                return None

        slow = SlowAdapter()
        orch._gateway = SubmitGateway(orch._state, slow)
        # run(2) 的主循环最早 ~1.5s 就因无待领工作退出；放行时间取 4s，
        # 远超旧实现的 ≤1s 收尾预算（旧代码必然停在 SENDING），
        # 新实现靠 final_drain_seconds=30 的宽限收回回执。
        timer = threading.Timer(4.0, release.set)
        timer.daemon = True
        timer.start()
        try:
            result = orch.run(2)
        finally:
            release.set()
            timer.cancel()
        assert entered.is_set()  # 提交确实进入过在途
        assert slow.calls and slow.calls[0].challenge_id == 'ch-mock-1'
        status = orch._board._db.execute('SELECT status FROM submissions').fetchone()[0]
        assert status == 'ACCEPTED'
        assert result['solved'] == ['ch-mock-1']  # 收尾宽限计入 solved
        orch._board.close()


def test_resource_housekeeping_reclaims_finished_workspaces():
    # 4h × 27 题连续运行，平台临时盘只有 10GiB：终态题的工作区
    # （handouts + 工具产物）必须及时回收，且不能碰在跑题。
    with tempfile.TemporaryDirectory() as root:
        orch = build_runtime(root + '/run.db', root + '/evidence')
        # workspace_path 只读推导：不创建目录
        ghost = orch._state.workspace_path('ghost-cid')
        assert not ghost.exists()
        # c1：REJECTED 终态、无在途意图 → 可回收
        # c2：ACCEPTED 但意图仍 RUNNING → 保留
        # c3：SENDING 在途提交 → 保留（重派发要校验工作区证据）
        orch._board.add_challenge(Challenge(id='c1', name='c1', category='misc'))
        orch._board.add_challenge(Challenge(id='c2', name='c2', category='misc'))
        orch._board.add_challenge(Challenge(id='c3', name='c3', category='misc'))
        orch._board.add_intent(IntentDraft('c2', 'misc.housekeeping.running'))
        orch._board.add_intent(IntentDraft('c3', 'misc.housekeeping.sending'))
        orch._board.claim_intent('w', lease_seconds=60)  # c2 → RUNNING
        orch._board.claim_intent('w2', lease_seconds=60)  # c3 → RUNNING
        orch._board._db.executemany(
            'INSERT INTO submissions(idempotency_key,challenge_id,answer,status,evidence_ids,updated_at)'
            ' VALUES(?,?,?,?,?,0)',
            [('k1', 'c1', 'flag{x1}', 'REJECTED', 'e1'),
             ('k2', 'c2', 'flag{x2}', 'ACCEPTED', 'e2'),
             ('k3', 'c3', 'flag{x3}', 'SENDING', 'e3')])
        orch._board._db.commit()
        for cid in ('c1', 'c2', 'c3'):
            blob = orch._state.workspace(cid) / 'handouts'
            blob.mkdir()
            (blob / 'big.bin').write_bytes(b'x' * 4096)
        orch._cleanup_finished_workspaces()
        assert not orch._state.workspace_path('c1').exists()  # 终态 → 回收
        assert orch._state.workspace_path('c2').exists()  # 意图在跑 → 保留
        assert orch._state.workspace_path('c3').exists()  # 在途提交 → 保留
        assert orch._board._db.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='workspace_reclaimed'").fetchone()[0] == 1
        # c2 意图终结后，下一轮回收
        orch._board._db.execute("UPDATE intents_ext SET status='COMPLETED' WHERE challenge_id='c2'")
        orch._board._db.commit()
        orch._cleanup_finished_workspaces()
        assert not orch._state.workspace_path('c2').exists()
        assert orch._state.workspace_path('c3').exists()  # SENDING 仍保留
        # 水位守护：强制执行一遍不抛错，事件通道可用
        orch._housekeeping(force=True)
        assert orch._board._db.execute(
            "SELECT COUNT(*) FROM events WHERE event_type IN ('disk_pressure','resource_snapshot',"
            "'workspace_reclaimed')").fetchone()[0] >= 1
        orch._board.close()


def test_B06_confirmed_outbox_is_dispatched_before_new_work():
    with tempfile.TemporaryDirectory() as root:
        orch = build_runtime(root + '/run.db', root + '/evidence')
        orch.bootstrap()
        original = orch._gateway.pump
        orch._gateway.pump = lambda: []
        first = orch.step()
        assert first['submission']['status'] == 'DRAFT'
        orch._gateway.pump = original
        seen = []
        def pick(*args, **kwargs):
            seen.append(orch._board._db.execute('SELECT status FROM submissions').fetchone()[0])
            return None
        orch._scheduler.pick_next = pick
        orch.step()
        assert seen[0] in ('SENDING', 'ACCEPTED')
        orch._gateway.flush()
        orch._gateway.close()
        orch._board.close()


def test_L01_L02_fallback_reuses_state_and_preserves_full_gate_chain():
    with tempfile.TemporaryDirectory() as root:
        first = build_runtime(root + '/run.db', root + '/evidence')
        first._state.patch('ch-mock-1', lambda s: s.update(hypotheses=['保留的已知线索']))
        first._gateway.close()
        first._board.close()
        fallback = build_runtime(root + '/run.db', root + '/evidence', mode='fallback')
        result = fallback.run(10)
        assert result['solved'] == ['ch-mock-1']
        assert fallback._state.load('ch-mock-1')[1]['hypotheses'] == ['保留的已知线索']
        assert result['results'][0]['gate']['status'] == 'confirmed'
        assert fallback._scheduler._max_project_workers == 1
        fallback._board.close()


def test_I02_L03_route_is_pinned_across_restart():
    with tempfile.TemporaryDirectory() as root:
        orch = build_runtime(root + '/run.db', root + '/evidence')
        orch._gateway.close()
        orch._board.close()
        cfg = config()
        cfg['controller'] = {'thresholds': [10, 20, 30, 40]}
        path = Path(root) / 'config.json'
        path.write_text(json.dumps(cfg))
        expect(ValueError, lambda: build_runtime(root + '/run.db', root + '/evidence', config_path=str(path)))


def test_invalid_hot_config_and_missing_quota_fail_closed():
    with tempfile.TemporaryDirectory() as root:
        path = Path(root) / 'config.json'
        cfg = config()
        path.write_text(json.dumps(cfg))
        orch = build_runtime(root + '/run.db', root + '/evidence', config_path=str(path))
        for invalid in ([1, 2], {'concurrency': []}, {**cfg, 'concurrency': {'max_workers': 'invalid'}}):
            path.write_text(json.dumps(invalid))
            assert orch.reload_config_if_changed() is False
            assert orch._hot.last_error
        cfg.pop('download')
        path.write_text(json.dumps(cfg))
        assert orch.reload_config_if_changed() is True
        assert orch._policy._cfg.max_single_bytes == 0 and orch._policy._cfg.max_total_bytes == 0
        cfg['model'] = {'max_tokens': 9000}
        path.write_text(json.dumps(cfg))
        assert orch.reload_config_if_changed() is False
        assert 'fixed' in orch._hot.last_error
        orch._gateway.close()
        orch._board.close()


def test_I02_J01_parallel_is_explicitly_disabled_until_acceptance():
    cfg = config()
    cfg['controller'] = {'parallel_agents': 3}
    # 无显式验收标志：仍拒绝（闸门保持默认关闭）。
    assert validate_config(cfg)
    # 2026-11-19 用户验收通过：parallel_acceptance=true 显式放行，三题并行、题内 intent 串行。
    cfg['controller']['parallel_acceptance'] = True
    assert not validate_config(cfg)
    # 验收标志不豁免非法值。
    cfg['controller']['parallel_agents'] = 0
    assert validate_config(cfg)
