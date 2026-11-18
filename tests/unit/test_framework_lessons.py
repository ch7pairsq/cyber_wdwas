"""教训蒸馏、脱敏、幂等与同领域跨题共享。"""
from tests.framework_support import FLAG, fixture
from ctf_ext.domain.models import Challenge
from ctf_ext.store import lessons
from ctf_ext.store.blackboard import IntentDraft, WorkerResult


def _challenge(store, cid, category):
    store.board.add_challenge(Challenge(id=cid, name=cid, category=category))


def _record(store, cid, failure="", ruled=None, next_action="", outcome="error", domain="crypto"):
    store.board.add_intent(IntentDraft(cid, "crypto.path." + cid))
    claim = store.board.claim_intent("w-" + cid)
    result = WorkerResult(claim.intent_id, "w-" + cid, claim.attempt_no, cid, claim.strategy_key,
        "failed", outcome=outcome, failure_reason=failure,
        preconditions=["附件可读", "命令有界"],
        ruled_out=list(ruled or []), next_best_action=next_action,
        resource_usage={"wall_seconds": 0, "model_tokens": 0, "tool_calls": 0})
    receipt = store.board.write_result(result, "key-" + cid)
    with store.board._write() as db:
        return lessons.record_attempt(db, receipt.attempt_id, domain, commit=False)


def test_distill_keeps_conditions_reason_and_next_action_but_redacts_flag():
    with fixture() as store:
        _challenge(store, "c2", "crypto")
        lesson_id = _record(store, "c2", failure="匹配到 " + FLAG + " 但被拒绝",
                            ruled=["不是 RSA 小指数"], next_action="尝试共模攻击")
        row = store.db.execute("SELECT text,tags FROM lessons WHERE id=?", (lesson_id,)).fetchone()
        assert "成立条件" in row["text"] and "失败原因" in row["text"] and "下一步" in row["text"]
        assert FLAG not in row["text"] and "flag{redacted}" in row["text"]
        assert "domain:crypto" in row["tags"] and "outcome:error" in row["tags"]


def test_sanitizer_redacts_flag_adjacent_to_chinese_text():
    assert FLAG not in lessons.sanitize_lesson_text("看到" + FLAG + "后失败")
    assert "flag{redacted}" in lessons.sanitize_lesson_text("看到" + FLAG + "后失败")


def test_lesson_is_idempotent_per_source_attempt():
    with fixture() as store:
        _challenge(store, "c2", "crypto")
        lesson_id = _record(store, "c2", failure="一次失败")
        attempt = store.db.execute("SELECT id FROM attempts WHERE challenge_id='c2'").fetchone()[0]
        with store.board._write() as db:
            again = lessons.record_attempt(db, attempt, "crypto", commit=False)
        assert again == lesson_id
        assert store.db.execute("SELECT COUNT(*) FROM lessons").fetchone()[0] == 1


def test_same_domain_lessons_are_shared_but_other_domain_is_not():
    with fixture() as store:
        _challenge(store, "c2", "crypto")
        _challenge(store, "c3", "forensics")
        _record(store, "c2", failure="RSA 指数太小", next_action="尝试小指数", domain="crypto")
        _record(store, "c3", failure="ZIP 口令未知", next_action="尝试字典", domain="forensics")
        shared = store.board.lesson_context("c2", limit=10)
        texts = " ".join(item["text"] for item in shared)
        assert "RSA 指数太小" in texts and "ZIP 口令" not in texts
        assert all("challenge_id" not in item for item in shared)
        other = store.board.lesson_context("c3", limit=10)
        assert "ZIP 口令" in " ".join(item["text"] for item in other)


def test_search_is_domain_scoped_and_snapshot_is_serializable():
    with fixture() as store:
        _challenge(store, "c2", "crypto")
        _challenge(store, "c3", "forensics")
        _record(store, "c2", failure="RSA 需要更多素数", domain="crypto")
        _record(store, "c3", failure="ZIP 需要爆破", domain="forensics")
        crypto = lessons.search(store.db, domain="crypto", limit=10)
        assert len(crypto) == 1 and "RSA" in crypto[0]["text"]
        snapshot = store.board.snapshot("c2")
        assert isinstance(snapshot["lessons"], list)
        assert snapshot["lessons"][0]["text"]


def test_handoff_redacts_flag_from_recent_failure():
    with fixture() as store:
        _challenge(store, "c2", "crypto")
        _record(store, "c2", failure="看到 " + FLAG + " 但校验失败")
        handoff = store.board.handoff("c2")
        assert FLAG not in str(handoff)
        assert "flag{redacted}" in str(handoff)


def test_distill_without_signal_returns_none():
    row = {"id": "a", "challenge_id": "c", "strategy_key": "misc.x", "outcome": "error",
           "preconditions": "[]", "failure_reason": "", "ruled_out": "[]", "next_best_action": ""}
    assert lessons.distill(row, "misc") is None
