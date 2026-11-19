"""A/B/K/L：主入口离线模拟，不冒充比赛全真验收。"""
import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from ctf_ext.__main__ import build_runtime
from ctf_ext.config import HotConfig, validate_config
from ctf_ext.store.blackboard import IntentDraft, WorkerResult
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
