"""隔离失败关闭、租约续期及多连接并发门禁。"""
import concurrent.futures
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

from tests.framework_support import Adapter, FLAG, enqueue, expect, fixture, proof
from ctf_ext.gateway.execution import tool_environment
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
        env = {'BAIZHI_API_KEY': 'not-a-real-key', 'ARENA_SOLVER_TOKEN': 'solver-secret',
               'ARENA_PROXY': 'socks5://127.0.0.1:1080', 'ARENA_NETWORK_CIDR': '10.201.0.0/16'}
        with patch.dict(os.environ, env):
            probe = ('import os;print(os.environ.get("BAIZHI_API_KEY", "absent"),'
                     'os.environ.get("ARENA_SOLVER_TOKEN", "absent"),'
                     'os.environ.get("ARENA_PROXY", "absent"),'
                     'os.environ.get("ARENA_NETWORK_CIDR", "absent"))')
            out = capture([sys.executable, '-I', '-c', probe],
                          cwd=str(store.workspace('c1')), timeout=2)
        absent, solver, proxy, cidr = out['output'].decode().split()
        assert absent == 'absent' and solver == 'absent'  # 凭据绝不进入工具进程
        assert proxy == 'socks5://127.0.0.1:1080' and cidr == '10.201.0.0/16'  # 工具所需变量放行


def test_H_arena_http_denies_platform_writes_but_allows_reads():
    script = str(Path(__file__).resolve().parents[2] / 'ctf_ext' / 'gateway' / 'arena_http.py')
    env = {**os.environ, 'ARENA_PROXY': 'socks5://127.0.0.1:1080',
           'ARENA_NETWORK_CIDR': '10.201.0.0/16',
           'ARENA_BASE_URL': 'http://10.201.0.77:8090'}
    deny = subprocess.run([sys.executable, script, '--host', '10.201.0.77', '--port', '8090',
                           '--method', 'POST', '--path', '/openapi/v1/challenges/finish'],
                          capture_output=True, text=True, timeout=10, env=env)
    # finish/submit 等管理写操作直接拒绝：提交必须走网关治理，防误关 attempt。
    assert 'platform API is read-only' in (deny.stdout + deny.stderr)
    read = subprocess.run([sys.executable, script, '--host', '10.201.0.77', '--port', '8090',
                           '--method', 'GET', '--path', '/openapi/v1/challenges', '--timeout', '1'],
                          capture_output=True, text=True, timeout=10, env=env)
    # 平台 GET 仍放行（模型需要读题目/服务地址）。
    assert 'platform API is read-only' not in (read.stdout + read.stderr)
    other = subprocess.run([sys.executable, script, '--host', '10.201.0.50', '--port', '5000',
                            '--method', 'POST', '--path', '/load-workflow', '--timeout', '1'],
                           capture_output=True, text=True, timeout=10, env=env)
    # 非平台靶机不受影响：策略放行，进入 socks 连接阶段（本地无代理而失败）。
    assert 'platform API is read-only' not in (other.stdout + other.stderr)


def test_H2_arena_http_accepts_svc_hostname_resolved_into_cidr():
    """v35（第三场实测）：平台 start 给的是 svc 域名（解析到 172.20.x），
    此前只收 IP 字面量 → arena_http 全 DENY。域名解析后在授权网段即放行，
    host 原样直传 SOCKS5（ATYP=domain，隧道侧解析）。"""
    script = str(Path(__file__).resolve().parents[2] / 'ctf_ext' / 'gateway' / 'arena_http.py')
    base_env = {**os.environ, 'ARENA_PROXY': 'socks5://127.0.0.1:1080',
                'ARENA_BASE_URL': ''}
    # 域名解析（localhost→127.0.0.1，模拟 svc 域名→容器 IP）落在授权网段
    # → 通过网段校验，卡在 socks5 连接（本地无代理），而不是网段 DENY。
    env = {**base_env, 'ARENA_NETWORK_CIDR': '127.0.0.0/8,172.16.0.0/12'}
    ok = subprocess.run([sys.executable, script, '--host', 'localhost',
                         '--port', '1337', '--timeout', '1'],
                        capture_output=True, text=True, timeout=10, env=env)
    assert 'outside attempt network' not in (ok.stdout + ok.stderr)
    assert 'must be an IP' not in (ok.stdout + ok.stderr)
    assert 'socks5' in (ok.stdout + ok.stderr)
    # 解析结果不在授权网段 → 网段 DENY（模拟上一场 10.201/16 撞上 172.20 靶机）。
    env_bad = {**base_env, 'ARENA_NETWORK_CIDR': '10.201.0.0/16'}
    deny = subprocess.run([sys.executable, script, '--host', 'localhost',
                           '--port', '8080', '--timeout', '1'],
                          capture_output=True, text=True, timeout=10, env=env_bad)
    assert 'target outside attempt network' in (deny.stdout + deny.stderr)
    # 假域名解析失败 → 明确 DENY，不再误报"must be an IP"。
    fail = subprocess.run([sys.executable, script, '--host', 'no-such-host.invalid',
                           '--port', '80', '--timeout', '1'],
                          capture_output=True, text=True, timeout=15, env=env)
    assert 'cannot resolve host' in (fail.stdout + fail.stderr)


def test_E03_heartbeat_renews_live_lease_and_allows_final_write():
    with fixture() as store:
        store.board.add_intent(IntentDraft('c1', 'misc.heartbeat.test'))
        # 租约取 1s：0.3s 的旧值在全量套件高负载下会被调度抖动翻转
        # （续期线程拿不到时间片 → 判定误报）；放宽时长不改变语义。
        claim = store.board.claim_intent('w', lease_seconds=1)
        with LeaseHeartbeat(store.board, claim, 'w', 1) as heartbeat:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if store.db.execute('SELECT lease_until FROM intents_ext').fetchone()[0] > claim.lease_until + .5:
                    break
                threading.Event().wait(.02)
            current = store.db.execute('SELECT lease_until FROM intents_ext').fetchone()[0]
            assert current > claim.lease_until + .5 and not heartbeat.error
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


def test_H_tool_env_passes_tunnel_proxy_without_credentials():
    env = {'HTTP_PROXY': 'http://user:pw@10.55.0.9:18082',
           'HTTPS_PROXY': 'http://10.55.0.9:18082',
           'NO_PROXY': 'localhost,model-proxy-x',
           'ARENA_PROXY': 'socks5://u:p@10.55.0.9:18081',
           'BAIZHI_API_KEY': 'sk-secret', 'ARENA_SOLVER_TOKEN': 'solver-secret'}
    with patch.dict(os.environ, env):
        tool_env = tool_environment()
    assert tool_env['HTTP_PROXY'] == 'http://10.55.0.9:18082'  # 代理口令被剥离
    assert tool_env['ARENA_PROXY'] == 'socks5://10.55.0.9:18081'
    assert tool_env['HTTPS_PROXY'] == 'http://10.55.0.9:18082'
    assert tool_env['NO_PROXY'] == 'localhost,model-proxy-x'
    assert 'BAIZHI_API_KEY' not in tool_env and 'ARENA_SOLVER_TOKEN' not in tool_env


def test_H_tool_env_strips_svc_wildcard_from_no_proxy():
    # 第三场实测（2026-11-22）：平台 NO_PROXY 含 .svc/.cluster.local 通配，
    # requests 对靶机 svc 域名绕过代理直连（不可达）。工具子进程 env 内
    # 剥除这两个通配项，其余条目（含 localhost 类）原样保留。
    env = {'NO_PROXY': 'localhost,127.0.0.1,::1,.svc,.cluster.local,model-proxy-x',
           'no_proxy': 'localhost,.svc'}
    with patch.dict(os.environ, env):
        tool_env = tool_environment()
    assert tool_env['NO_PROXY'] == 'localhost,127.0.0.1,::1,model-proxy-x'
    assert tool_env['no_proxy'] == 'localhost'
