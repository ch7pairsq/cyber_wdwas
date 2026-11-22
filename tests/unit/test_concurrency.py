"""并发执行与终局加时：跨题并行、题内串行（max_project_workers=1）。

对应平台约束：并发环境上限 3（max_running_projects=3），推荐形态是
跨题并行、每题同时只跑 1 个意图。本文件覆盖：
- 存储层共享连接的读写互斥（snapshot 与 BEGIN IMMEDIATE 共用 RLock）；
- 调度器同题互斥与在跑题数封顶；
- 适配器同题并发 ensure_environment 去重；
- 编排器并行 run 与终局加时授权（一次、提额、不清账）。
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tests.framework_support import fixture

TOKEN = "st_conc_token_must_not_leak"


# ------------------------------------------------------------------ 存储层
def test_concurrent_patch_and_load_is_consistent():
    """4 线程各自 patch 同一 store：写串行、跨线程读不与写事务交叠。"""
    from ctf_ext.runtime.control import SessionControl
    with fixture() as store:
        for i in range(4):
            SessionControl(store, "c%d" % i)
        errors = []

        def hammer(i):
            try:
                for _ in range(50):
                    store.patch("c%d" % i, lambda s: s.update(steps=s["steps"] + 1))
                    store.load("c%d" % ((i + 1) % 4))  # 读另一线程正在写的题
            except Exception as exc:  # pragma: no cover - 诊断用
                errors.append(exc)

        threads = [threading.Thread(target=hammer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        for i in range(4):
            assert store.load("c%d" % i)[1]["steps"] == 50  # 无丟更新


def _add_challenge(board, cid):
    from ctf_ext.domain.models import Challenge
    board.add_challenge(Challenge(id=cid, name="测试题", category="misc"))


def test_claim_intent_same_challenge_exclusive():
    """题内串行：max_project_workers=1 时同题第二线程领不到新意图。"""
    from ctf_ext.store.blackboard import IntentDraft
    with fixture() as store:
        _add_challenge(store.board, "c2")  # fixture 只注册 c1；其余题补注册
        for k in range(3):  # params 不同避免 DuplicateStrategy
            store.board.add_intent(IntentDraft(
                challenge_id="c1", strategy_key="misc.triage.auto", params={"i": k}))
        results = []
        lock = threading.Lock()

        def claim(tag):
            claim_ = store.board.claim_intent(
                tag, lease_seconds=60, max_project_workers=1, max_running_projects=5)
            with lock:
                results.append(claim_)

        threads = [threading.Thread(target=claim, args=("w1",)),
                   threading.Thread(target=claim, args=("w2",))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        claimed = [r for r in results if r is not None]
        assert len(claimed) == 1  # 同题同时只允许 1 个 RUNNING，第二个被拒


def test_claim_intent_running_projects_cap():
    """在跑题数封顶：2 个题在跑时第三个题不可领取（对齐环境上限 3）。"""
    from ctf_ext.store.blackboard import IntentDraft
    with fixture() as store:
        _add_challenge(store.board, "c2")
        _add_challenge(store.board, "c3")
        for cid in ("c1", "c2", "c3"):
            store.board.add_intent(IntentDraft(
                challenge_id=cid, strategy_key="misc.triage.auto", params={}))
        assert store.board.claim_intent("w1", lease_seconds=60,
                                        max_project_workers=1, max_running_projects=2)
        assert store.board.claim_intent("w2", lease_seconds=60,
                                        max_project_workers=1, max_running_projects=2)
        assert store.board.claim_intent("w3", lease_seconds=60,
                                        max_project_workers=1, max_running_projects=2) is None


# ------------------------------------------------------------------ 适配器
def test_adapter_concurrent_ensure_environment_dedups_start():
    """同题并发 ensure_environment 只发一次 start（in-flight 去重）。"""
    from ctf_ext.harness.arena import ArenaHarnessAdapter
    seen = {"starts": 0}
    lock = threading.Lock()

    class _Stub(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.headers.get("Authorization") != "Bearer " + TOKEN:
                return self._json({"error": "unauthorized"}, 401)
            if self.path.startswith("/openapi/v1/challenges/help"):
                return self._json({"workflow": "x"})
            if self.path.startswith("/openapi/v1/challenges"):
                return self._json({"challenges": [
                    {"unique_code": "CH-1", "name": "n", "category": "misc"}]})
            return self._json({"error": "not found"}, 404)

        def do_POST(self):
            if self.headers.get("Authorization") != "Bearer " + TOKEN:
                return self._json({"error": "unauthorized"}, 401)
            if self.path.startswith("/openapi/v1/challenges/start"):
                with lock:
                    seen["starts"] += 1
                time.sleep(0.05)  # 放大并发窗口
                return self._json({"started": True})
            return self._json({"error": "not found"}, 404)

    server = HTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ad = ArenaHarnessAdapter("http://127.0.0.1:{}".format(server.server_address[1]),
                                 token=TOKEN, client_timeout=5.0, env_policy="on_demand")
        ad.startup()
        ad.read_tasks()
        errors = []

        def go():
            try:
                ad.ensure_environment("CH-1")
            except Exception as exc:  # pragma: no cover - 诊断用
                errors.append(exc)

        threads = [threading.Thread(target=go) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert seen["starts"] == 1
    finally:
        server.shutdown()


# ------------------------------------------------------------------ 编排器
@pytest.fixture()
def two_challenge_orch(monkeypatch):
    """两个 mock 题的完整运行时（mock 模式默认只有 1 题）。"""
    import ctf_ext.harness.mock as mock_mod
    from ctf_ext.__main__ import build_runtime

    original = mock_mod.default_practice_challenges

    def two():
        base = original()
        extra = dict(base[0])
        extra["challenge_id"] = "ch-mock-2"
        extra["name"] = "second starter"
        return base + [extra]

    # __main__ 是 from-import 绑定，必须补丁它命名空间里的名字才生效。
    monkeypatch.setattr("ctf_ext.__main__.default_practice_challenges", two)
    with tempfile.TemporaryDirectory() as root:
        orch = build_runtime(root + "/run.db", root + "/evidence")
        yield orch
        orch._gateway.close()
        orch._board.close()


def test_parallel_run_reports_effective_parallel(two_challenge_orch):
    """parallel_agents=2：报告记录 parallel/effective，跨题并行正常收尾。"""
    orch = two_challenge_orch
    orch._parallel_agents = 2
    out = orch.run(max_seconds=8)
    assert out["execution"]["mode"] == "parallel"
    assert out["execution"]["effective_parallel"] == 2
    assert out["tasks"] == 2
    # 并行没有引入状态损坏：每题意图都有终态记录
    rows = orch._board._db.execute(
        "SELECT status, COUNT(*) AS n FROM intents_ext GROUP BY status").fetchall()
    assert {r["status"] for r in rows} <= {"RUNNING", "READY", "FAILED",
                                           "FAILED_RETRYABLE", "CONFIRMED", "COMPLETED",
                                           "SENT", "UNKNOWN"}


def test_endgame_grant_boosts_caps_once_and_runs_pass2(two_challenge_orch):
    """终局加时：授权一次、上限提升、不清账、pass2 意图可领取执行。"""
    from ctf_ext.runtime.control import SessionControl
    orch = two_challenge_orch
    orch.bootstrap()
    factory = orch._worker._control_factory
    factory.deadline = orch._clock() + 1000
    orch._endgame_bonus = 0.5
    orch._endgame_min_seconds = 10
    cid = "ch-mock-1"
    SessionControl(orch._state, cid)  # 建立会话状态
    base_control = factory(cid)  # 授权前的基准上限（此时会话活着）
    orch._state.patch(cid, lambda s: s.update(status="stopped"))  # 模拟预算耗尽
    # 清掉在途工作，模拟"可解题已做完"的收尾状态
    with orch._board.transaction() as db:
        db.execute("UPDATE intents_ext SET status='FAILED' "
                   "WHERE status IN ('READY','FAILED_RETRYABLE')")
    tasks = {cid: {"challenge_id": cid}}
    assert orch._grant_endgame(tasks, set(), orch._clock() + 1000) is True
    assert factory.endgame.get(cid) == 0.5
    boosted = factory(cid)
    assert boosted.max_tokens == int(base_control.max_tokens * 1.5)
    assert boosted.max_tools == int(base_control.max_tools * 1.5)
    assert boosted.seconds > base_control.seconds  # 会话有效期延到运行截止
    assert orch._board._db.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='endgame_grant'"
    ).fetchone()[0] == 1
    # 同题只授权一次
    assert orch._grant_endgame(tasks, set(), orch._clock() + 1000) is False
    # pass2 意图在队列中且可被领取执行（窗口重置后 stopped 已复活）
    out = orch.step()
    assert out is not None and out["challenge_id"] == cid
    row = orch._board._db.execute(
        "SELECT status FROM intents_ext WHERE strategy_key='misc.endgame.pass2'").fetchone()
    assert row is not None
    assert row["status"] in ("COMPLETED", "CONFIRMED")
    assert orch._state.load(cid)[1]["status"] != "stopped"


def test_endgame_grants_capped_by_free_slots_and_handed_to_best_score(two_challenge_orch):
    """终局授权按空闲槽位封顶：只有 1 个空槽时只授 1 题，且是分值更高的那题。

    11-21 复盘：20 分钟剩余时一次抬升 14 题，实际并发只有 3，11 题白抬。
    """
    from ctf_ext.runtime.control import SessionControl
    orch = two_challenge_orch
    orch.bootstrap()
    factory = orch._worker._control_factory
    factory.deadline = orch._clock() + 1000
    orch._endgame_bonus = 0.5
    orch._endgame_min_seconds = 10
    orch._scheduler.set_limits(max_project_workers=1, max_running_projects=1)
    cids = ["ch-mock-1", "ch-mock-2"]
    for cid in cids:
        SessionControl(orch._state, cid)  # 建立会话状态
        orch._state.patch(cid, lambda s: s.update(status="stopped"))  # 模拟预算耗尽
        orch._challenges.setdefault(cid, {})["points_hint"] = 100.0
    orch._challenges["ch-mock-2"]["points_hint"] = 500.0  # 高分题应优先拿到槽位
    with orch._board.transaction() as db:
        db.execute("UPDATE intents_ext SET status='FAILED' "
                   "WHERE status IN ('READY','FAILED_RETRYABLE')")
    tasks = {cid: {"challenge_id": cid} for cid in cids}

    assert orch._endgame_grant_limit() == 1  # 空槽位 = 1
    assert orch._grant_endgame(tasks, set(), orch._clock() + 1000) is True
    granted = [r[0] for r in orch._board._db.execute(
        "SELECT entity_id FROM events WHERE event_type='endgame_grant'").fetchall()]
    assert granted == ["ch-mock-2"]
    assert factory.endgame == {"ch-mock-2": 0.5}  # 只有被授权的题抬了上限
    # 已有一道 pass2 在排队 → 本轮不再追加，授权节奏与真实吞吐对齐
    assert orch._grant_endgame(tasks, set(), orch._clock() + 1000) is False
    # 显式配置可覆盖自动槽位（0 = 自动）
    orch._endgame_max_grants = 2
    assert orch._endgame_grant_limit() == 2
