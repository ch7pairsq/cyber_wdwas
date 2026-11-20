"""解题思路记忆：蒸馏脱敏、幂等、检索优先级与 worker 注入。"""
import json

from tests.framework_support import FLAG, fixture
from ctf_ext.domain.models import Challenge
from ctf_ext.runtime.llm_worker import LlmWorker
from ctf_ext.store import solutions
from ctf_ext.store.blackboard import IntentDraft, WorkerResult


def _solved(store, cid="s1", category="forensics", name="zip-nested-flag"):
    """构造一道"平台已确认解出"的题：尝试 + 命令日志 + 接受的提交。"""
    store.board.add_challenge(Challenge(id=cid, name=name, category=category))
    store.board.add_intent(IntentDraft(cid, "forensics.layer." + cid))
    claim = store.board.claim_intent("w-" + cid)
    result = WorkerResult(claim.intent_id, "w-" + cid, claim.attempt_no, cid, claim.strategy_key,
                          "completed", outcome="success",
                          preconditions=["附件为多层 zip"],
                          actions=["binwalk 提取得到 " + FLAG, "逐层 unzip", "strings 定位"],
                          ruled_out=["直接 file"],
                          next_best_action="")
    store.board.write_result(result, "key-" + cid)
    with store.board._write() as db:
        db.execute("INSERT INTO command_log VALUES(?,?,?,?,?,?,?,?,?,?)",
                   ("cmd1-" + cid, cid, "ev1", json.dumps(["binwalk", "-e", "run"]),
                    "1", "rev1", "local", 0, 1, 1.0))
        db.execute("INSERT INTO command_log VALUES(?,?,?,?,?,?,?,?,?,?)",
                   ("cmd2-" + cid, cid, "ev2", json.dumps(["/usr/bin/procyon", "x.jar"]),
                    "1", "rev1", "local", 0, 1, 2.0))
        db.execute("INSERT INTO submissions VALUES(?,?,?,?,?,?,?,?,?)",
                   ("key-" + cid, cid, FLAG, "ACCEPTED", None, "",
                    json.dumps(["ev1", "ev2"]), 3.0, 3.0))


def _submission(store, cid="s1"):
    return dict(store.db.execute("SELECT * FROM submissions WHERE challenge_id=?", (cid,)).fetchone())


def test_record_distills_approach_and_redacts_flag():
    with fixture() as store:
        _solved(store)
        with store.board._write() as db:
            solution_id = solutions.record_solution(db, "s1", _submission(store), commit=False)
        row = store.db.execute("SELECT * FROM solutions WHERE id=?", (solution_id,)).fetchone()
        assert row["challenge_id"] == "s1" and row["domain"] == "forensics"
        assert FLAG not in row["approach"] and "flag{redacted}" in row["approach"]
        assert json.loads(row["key_commands"]) == ["binwalk", "procyon"]
        assert "路线" in row["approach"] and "关键命令" in row["approach"]
        assert "已排除" in row["approach"]


def test_record_is_idempotent_per_source_submission():
    with fixture() as store:
        _solved(store)
        with store.board._write() as db:
            first = solutions.record_solution(db, "s1", _submission(store), commit=False)
        with store.board._write() as db:
            again = solutions.record_solution(db, "s1", _submission(store), commit=False)
        assert first == again
        assert store.db.execute("SELECT COUNT(*) FROM solutions").fetchone()[0] == 1


def test_resolve_same_challenge_preserves_reuse_count():
    with fixture() as store:
        _solved(store)
        with store.board._write() as db:
            solutions.record_solution(db, "s1", _submission(store), commit=False)
        with store.board._write() as db:
            solutions.solutions_context(db, "forensics", "zip", challenge_id=None, limit=1)
        reuse = store.db.execute(
            "SELECT reuse_count FROM solutions WHERE challenge_id='s1'").fetchone()[0]
        assert reuse == 1
        sub2 = _submission(store)
        sub2["idempotency_key"] = "key-c1-r2"  # 模拟同题第二次被确认解出
        with store.board._write() as db:
            solutions.record_solution(db, "s1", sub2, commit=False)
        rows = store.db.execute("SELECT * FROM solutions").fetchall()
        assert len(rows) == 1 and rows[0]["reuse_count"] == reuse


def test_context_prefers_same_challenge_then_same_domain_excludes_cross_domain():
    with fixture() as store:
        _solved(store, "s1", "forensics", "zip-nested-flag")
        _solved(store, "c2", "forensics", "png-trailing-data")
        _solved(store, "c3", "crypto", "rsa-small-exponent")
        with store.board._write() as db:
            for cid in ("s1", "c2", "c3"):
                solutions.record_solution(db, cid, _submission(store, cid), commit=False)
        with store.board._write() as db:
            ctx = solutions.solutions_context(db, "forensics", "zip nested",
                                              challenge_id="c9", limit=2)
        assert len(ctx) == 2
        assert all(entry["domain"] == "forensics" for entry in ctx)
        assert ctx[0]["domain"] == "forensics" and "zip" in str(ctx[0]["approach"])
        # crypto 题的思路不得跨域泄露
        assert all(entry["domain"] != "crypto" for entry in ctx)
        with store.board._write() as db:
            exact = solutions.solutions_context(db, "misc", "", challenge_id="c2", limit=1)
        assert len(exact) == 1 and exact[0]["domain"] == "forensics"  # 同题精确命中优先


def test_no_match_returns_empty():
    with fixture() as store:
        _solved(store, "s1", "forensics", "zip-nested-flag")
        with store.board._write() as db:
            assert solutions.solutions_context(db, "crypto", "rsa", limit=2) == []
            assert solutions.solutions_context(db, "", "", limit=2) == []


def test_worker_injects_solution_message_and_fails_open():
    recall_ok = lambda d, q, cid: [{"title": "layer", "domain": "forensics",
                                    "approach": "先 binwalk 再逐层 unzip",
                                    "reuse_count": 1}]

    def boom(*args):
        raise RuntimeError("recall broken")

    worker_ok = LlmWorker(object(), skills_mode="off", solutions_recall=recall_ok)
    messages = []
    titles = worker_ok._append_solutions(messages, {"category": "forensics", "name": "zip"}, "c9")
    assert titles == ["layer"]
    assert "解题思路" in messages[-1]["content"] and "binwalk" in messages[-1]["content"]
    assert "只作参考" in messages[-1]["content"]

    worker_broken = LlmWorker(object(), skills_mode="off", solutions_recall=boom)
    messages_broken = []
    assert worker_broken._append_solutions(messages_broken, {"category": "forensics"}, "c9") == []
    assert messages_broken == []

    worker_off = LlmWorker(object(), skills_mode="off")
    assert worker_off._append_solutions([], {"category": "forensics"}, "c9") == []

    worker_empty = LlmWorker(object(), skills_mode="off",
                             solutions_recall=lambda d, q, cid: [{"title": "x", "approach": "  "}])
    assert worker_empty._append_solutions([], {"category": "forensics"}, "c9") == []
