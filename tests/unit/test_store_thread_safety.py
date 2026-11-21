"""并发线程安全回归（2026-11-21 8 题并发模拟教训）。

3 个 worker 并发 + 监控线程共用同一 sqlite 连接时，EvidenceStore 裸写、
FlagGate/Gateway 裸读会触发 sqlite InterfaceError（连接级未定义行为）。
修复：EvidenceStore 持根锁、Blackboard 复用、FlagGate/Gateway 裸读改走
read_view。本测试用 barrier 对齐的多线程并发触发全部 db 触点。
"""
import threading

from tests.framework_support import FLAG, fixture
from ctf_ext.harness.flag_gate import FlagGate
from ctf_ext.harness.gateway import SubmitGateway
from ctf_ext.store.blackboard import IntentDraft, WorkerResult
from tests.framework_support import Adapter


def test_concurrent_db_touchpoints_never_raise_interface_error():
    with fixture() as store:
        from ctf_ext.domain.models import Challenge
        for n in range(1, 5):  # writer 需要 4 道题（intents 外键）
            if n > 1:
                store.board.add_challenge(Challenge(id="c{}".format(n),
                                                    name="并发测试{}".format(n), category="misc"))
        gateway = SubmitGateway(store, Adapter())
        errors = []

        def writer(n):
            """模拟 worker 线程：证据登记 + 意图/结果/会话写。"""
            try:
                for i in range(20):
                    store.evidence.put_bytes(
                        "payload-{}-{}".format(n, i).encode(), kind="raw")
                    cid = "c{}".format(n)
                    store.board.add_intent(IntentDraft(cid, "misc.concurrent.{}".format(i)))
                    claim = store.board.claim_intent("w{}".format(n))
                    if claim is not None:
                        result = WorkerResult(claim.intent_id, "w{}".format(n),
                                              claim.attempt_no, cid,
                                              claim.strategy_key, "completed")
                        store.board.write_result(result, "{}-{}".format(claim.intent_id, claim.attempt_no))
                    store.patch(cid, lambda s: s.update(steps=i))
                    store.declare(cid, FLAG)
            except Exception as exc:  # InterfaceError/编程错误都要失败
                errors.append(repr(exc))

        def reader(_n):
            """模拟监控/pump 线程：gate 评估读 + gateway 派发读。"""
            try:
                gate = FlagGate(store)
                for i in range(20):
                    gate.evaluate("c1", FLAG, [])
                    gateway.pump()
                    gateway._proof_valid({"gate_id": "ghost", "challenge_id": "c1", "answer": FLAG})
            except Exception as exc:
                errors.append(repr(exc))

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(1, 5)]
        threads += [threading.Thread(target=reader, args=(n,)) for n in range(3)]
        barrier = threading.Barrier(len(threads))

        # barrier 注入：所有线程就绪后同时开跑，最大化并发窗口
        for t in threads:
            target = t._target
            args = t._args
            def wrapped(*_a, _target=target, _args=args):
                barrier.wait()
                _target(*_args)
            t._target = wrapped
            t._args = ()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        gateway.close()
        assert not errors, "concurrent db access raised: {}".format(errors[:3])
        assert store.db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] >= 80
