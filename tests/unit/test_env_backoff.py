"""v36 环境失败按题退避 + 无地址题释放槽位（第五场 409 风暴根因）。

第五场 attempt 01a20649：某题 start 恒 409，失败意图回 READY 后立即被重领
（backoff=0），日志里出现 2-5 次/秒的 claim → start 409 → failed 循环；
离线题的 container_addr 为空却照样占着并发槽位，其它题 start 撞 409。
"""
from __future__ import annotations

import tempfile
import time

from ctf_ext.__main__ import build_runtime
from ctf_ext.harness.arena import ArenaProtocolError
from ctf_ext.runtime.scheduler import Scheduler
from ctf_ext.store.blackboard import IntentDraft
from tests.unit._support import add_challenge, new_board


class _Harness:
    """最小环境管理桩：可选 start 失败 / 无地址响应，并记录调用。"""

    def __init__(self, fail=(), empty=()):
        self.fail = set(fail)
        self.empty = set(empty)
        self.calls = []
        self.started = {}
        self.stopped = []

    def max_envs(self):
        return 3

    def started_envs(self):
        return dict(self.started)

    def stop_environment(self, cid, wait=0):
        if cid in self.started:
            self.started.pop(cid)
            self.stopped.append(cid)
            return True
        return False

    def ensure_environment(self, cid):
        self.calls.append(cid)
        if cid in self.fail:
            raise ArenaProtocolError("409 environment_limit", 409)
        self.started.setdefault(cid, time.time())
        if cid in self.empty:
            return {"container_addr": [], "container_status": "available"}
        return {"container_addr": ["10.1.2.3:3000"], "container_status": "available"}

    def task_description(self, cid):
        return "desc-" + cid


def test_env_failure_defers_challenge_with_exponential_backoff():
    with tempfile.TemporaryDirectory() as root:
        orch = build_runtime(root + "/run.db", root + "/evidence")
        orch.bootstrap()
        harness = _Harness(fail={"ch-mock-1"})
        orch._harness = harness
        # mock 装配全局并发槽位小：放宽，专注验证退避本身。
        orch._scheduler.set_limits(None, 10, None)

        assert orch._ensure_environment("ch-mock-1") is None
        assert orch._env_failures["ch-mock-1"] == 1
        first_until = orch._scheduler._deferred["ch-mock-1"]
        assert 8.0 <= first_until - time.time() <= 11.0  # 首次 10s
        # 退避窗口内该题不再出现在可领取列表（其余题不受影响）。
        assert "ch-mock-1" not in orch._scheduler.eligible_challenges()

        for _ in range(5):
            orch._ensure_environment("ch-mock-1")
        assert orch._env_failures["ch-mock-1"] == 6
        capped = orch._scheduler._deferred["ch-mock-1"] - time.time()
        assert 250.0 <= capped <= 300.0  # 封顶 300s，不再无限放大

        orch._gateway.close()
        orch._board.close()


def test_env_without_address_releases_slot_and_is_not_restarted():
    with tempfile.TemporaryDirectory() as root:
        orch = build_runtime(root + "/run.db", root + "/evidence")
        orch.bootstrap()
        harness = _Harness(empty={"ch-mock-1"})
        orch._harness = harness

        assert orch._ensure_environment("ch-mock-1") == {}
        assert harness.stopped == ["ch-mock-1"]  # 无地址题主动归还并发槽
        calls = len(harness.calls)
        assert orch._ensure_environment("ch-mock-1") == {}
        assert len(harness.calls) == calls  # 记住后不再重复 start

        orch._gateway.close()
        orch._board.close()


def test_looks_offline_requires_explicit_phrasing():
    with tempfile.TemporaryDirectory() as root:
        orch = build_runtime(root + "/run.db", root + "/evidence")
        # 描述里没有 URL 不等于离线：普通 web 题也常不写地址（弱信号会误判，
        # 直接违反"环境不可用不得入 worker"的既有契约）。
        orch._challenges["c1"] = {"description": "analyze the attachment and print the flag"}
        assert orch._looks_offline("c1") is False
        orch._challenges["c1"] = {
            "description": "No network service is involved. Analyze the binary."}
        assert orch._looks_offline("c1") is True
        orch._challenges["c1"] = {
            "description": "web\n\n[Arena 运行环境] 靶机地址见 start_info"}
        assert orch._looks_offline("c1") is False
        orch._gateway.close()
        orch._board.close()


def test_scheduler_defer_hides_challenge_and_keeps_has_waiting_work():
    board, _ = new_board()
    add_challenge(board, cid="c1")
    board.add_intent(IntentDraft("c1", "misc.triage.auto"))
    scheduler = Scheduler(board, max_project_workers=1, max_running_projects=3)

    assert scheduler.eligible_challenges() == ["c1"]
    scheduler.defer_challenge("c1", 60)
    assert scheduler.eligible_challenges() == []
    assert scheduler.has_waiting_work() is True  # 退避中的题仍算待办，主循环不能退出
    scheduler._deferred["c1"] = time.time() - 1  # 到期自动回到队列
    assert scheduler.eligible_challenges() == ["c1"]
    board.close()


def test_scheduler_defer_is_hard_capped():
    board, _ = new_board()
    add_challenge(board, cid="c1")
    board.add_intent(IntentDraft("c1", "misc.triage.auto"))
    scheduler = Scheduler(board, max_project_workers=1, max_running_projects=3)
    scheduler.defer_challenge("c1", 10 ** 9)
    assert scheduler._deferred["c1"] - time.time() <= 3600.0
    board.close()

