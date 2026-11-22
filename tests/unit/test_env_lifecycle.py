"""v33 环境生命周期：槽位让位（close）+ 环境不可用快速失败。

2026-11-22 第二场实测：平台不主动回收环境，409 environment_limit 后
worker 模型自行调平台 start/close API 互相踩踏（close 掉别的 worker
正在用的环境）。编排层必须统一管理槽位，且环境不可用的意图不得入 worker。
"""
import tempfile
import time

from ctf_ext.__main__ import build_runtime
from ctf_ext.domain.models import Challenge
from ctf_ext.harness.arena import ArenaProtocolError
from ctf_ext.store.blackboard import IntentDraft


class _EnvHarness:
    """最小 arena 环境管理桩：显式记账，验证编排层让位与快速失败。"""

    def __init__(self, max_envs=3):
        self.started = {}  # cid -> started_at
        self.stopped = []
        self.limit = max_envs
        self.start_fail = set()  # start 时抛 409 environment_limit 的题

    def max_envs(self):
        return self.limit

    def started_envs(self):
        return dict(self.started)

    def stop_environment(self, cid, wait=0):
        if cid in self.started:
            self.started.pop(cid)
            self.stopped.append(cid)
            return True
        return False

    def ensure_environment(self, cid):
        if cid in self.start_fail:
            raise ArenaProtocolError("409 environment_limit", 409)
        self.started.setdefault(cid, time.time())
        return {"container_addr": ["t:1"]}

    def task_description(self, cid):
        return "desc-" + cid


def test_env_slots_evicted_before_new_start():
    # 槽位满（3/3）且 a1 最旧、无在途工作 → start 新题前让位 a1。
    with tempfile.TemporaryDirectory() as root:
        orch = build_runtime(root + "/run.db", root + "/evidence")
        h = _EnvHarness()
        for i, cid in enumerate(("a1", "a2", "a3")):
            h.started[cid] = 1000.0 + i
        orch._harness = h
        info = orch._ensure_environment("new1")
        assert h.stopped == ["a1"]
        assert "new1" in h.started and "a1" not in h.started
        assert info.get("container_addr")
        orch._gateway.close()
        orch._board.close()


def test_eviction_prefers_accepted_and_protects_running():
    # 保护 RUNNING intent 与 SENDING 提交；已 ACCEPTED 的题最优先让位
    # （平台对解题环境自动释放，close 幂等无害）。
    with tempfile.TemporaryDirectory() as root:
        orch = build_runtime(root + "/run.db", root + "/evidence")
        h = _EnvHarness()
        h.started = {"acc": 900.0, "busy": 950.0, "old": 1000.0}
        orch._harness = h
        orch._board.add_challenge(Challenge(id="busy", name="busy", category="misc"))
        orch._board.add_intent(IntentDraft("busy", "misc.env.running"))
        orch._board.claim_intent("w1", lease_seconds=60)  # busy → RUNNING
        orch._board._db.execute(
            "INSERT INTO submissions(idempotency_key,challenge_id,answer,status,"
            "evidence_ids,updated_at) VALUES('k-acc','acc','flag{x}','ACCEPTED','e1',0)")
        orch._board._db.commit()
        orch._ensure_environment("new1")
        assert h.stopped == ["acc"]
        assert "busy" in h.started  # 在跑的意图环境不被让位
        orch._gateway.close()
        orch._board.close()


def test_sending_submission_blocks_eviction():
    with tempfile.TemporaryDirectory() as root:
        orch = build_runtime(root + "/run.db", root + "/evidence")
        h = _EnvHarness()
        h.started = {"send": 900.0, "old": 1000.0, "fill": 1050.0}
        orch._harness = h
        orch._board._db.execute(
            "INSERT INTO submissions(idempotency_key,challenge_id,answer,status,"
            "evidence_ids,updated_at) VALUES('k-send','send','flag{y}','SENDING','e2',0)")
        orch._board._db.commit()
        orch._ensure_environment("new1")
        assert h.stopped == ["old"]  # SENDING 保护：让位的是 old
        assert "send" in h.started
        orch._gateway.close()
        orch._board.close()


def test_environment_unavailable_fails_intent_fast_without_worker():
    # 3 个槽全被 RUNNING 意图占据：无让位候选 → start 409 → 意图快速失败，
    # 绝不入 worker（防止模型拿到 environment_limit 后自行调平台管理 API）。
    with tempfile.TemporaryDirectory() as root:
        orch = build_runtime(root + "/run.db", root + "/evidence")
        orch.bootstrap()
        h = _EnvHarness()
        h.start_fail.add("ch-mock-1")
        for cid in ("e1", "e2", "e3"):
            orch._board.add_challenge(Challenge(id=cid, name=cid, category="misc"))
            h.started[cid] = time.time()
            orch._board.add_intent(IntentDraft(cid, "misc.env.running"))
        orch._board._db.execute(
            "UPDATE intents_ext SET status='RUNNING', lease_until=? "
            "WHERE challenge_id IN ('e1','e2','e3')", (time.time() + 60,))
        orch._board._db.commit()
        orch._harness = h
        # mock 装配的全局并发槽位小（3 个 RUNNING 占位题会挡住领取），放宽。
        orch._scheduler.set_limits(None, 10, None)

        class _ProbeWorker:
            def work(self, claim, challenge):
                raise AssertionError("worker must not run without environment")

        orch._worker = _ProbeWorker
        out = orch.step()
        assert out["status"] == "failed" and out["outcome"] == "error"
        row = orch._board._db.execute(
            "SELECT failure_reason FROM attempts WHERE challenge_id='ch-mock-1'"
            " ORDER BY id DESC LIMIT 1").fetchone()
        assert row["failure_reason"] == "environment_unavailable"
        row = orch._board._db.execute(
            "SELECT status FROM intents_ext WHERE challenge_id='ch-mock-1'").fetchone()
        assert row["status"] == "FAILED_RETRYABLE"  # 回退重试而非 blocked
        orch._gateway.close()
        orch._board.close()
