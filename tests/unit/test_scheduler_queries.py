"""公开查询 API 与调度器批量查询：不再跨模块访问黑板私有连接。"""
import tempfile

from tests.unit._support import add_challenge, new_board
from ctf_ext.runtime.scheduler import Scheduler
from ctf_ext.store.blackboard import IntentDraft
from ctf_ext.store.session import StateStore


def test_scheduler_uses_constant_number_of_queries():
    board, conn = new_board()
    for index in range(12):
        cid = "c%02d" % index
        add_challenge(board, cid=cid)
        board.add_intent(IntentDraft(cid, "misc.triage.auto", {"index": index}))
    statements = []
    conn.set_trace_callback(statements.append)
    scheduler = Scheduler(board, max_project_workers=1, max_running_projects=3)
    eligible = scheduler.eligible_challenges()
    conn.set_trace_callback(None)
    assert len(eligible) == 12
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) <= 5, selects
    board.close()


def test_named_queries_cover_scheduler_and_runtime_needs():
    board, _ = new_board()
    add_challenge(board, cid="c1", points=100)
    add_challenge(board, cid="c2", points=200)
    board.add_intent(IntentDraft("c1", "misc.triage.auto", {"phase": "initial"}, points_hint=100))
    board.add_intent(IntentDraft("c1", "misc.triage.alt", {"phase": "initial"}, points_hint=100))
    board.add_intent(IntentDraft("c2", "web.triage.auto", {"phase": "initial"}, points_hint=200))
    summary = {row["challenge_id"]: row for row in board.open_intent_summary()}
    assert summary["c1"]["ready"] == 2
    assert summary["c1"]["max_priority"] > 0
    assert board.count_intents("c1") == 2
    assert board.challenge_ids_with_work() == ["c1", "c2"]
    assert set(board.get_challenges(["c1", "c2"])) == {"c1", "c2"}

    with board.transaction() as db:
        db.execute("INSERT INTO submissions(idempotency_key,challenge_id,answer,status,receipt_id,"
                   "detail,evidence_ids,sent_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   ("k1", "c1", "flag{x}", "DRAFT", "", "", "", 1.0, 1.0))
    assert board.pending_submission_count() == 1
    assert board.latest_submission("c1")["status"] == "DRAFT"
    assert board.accepted_challenge_ids() == set()
    with board.transaction() as db:
        db.execute("UPDATE submissions SET status='ACCEPTED' WHERE idempotency_key='k1'")
    assert board.accepted_challenge_ids() == {"c1"}
    assert board.pending_submission_count() == 0
    board.close()


def test_state_store_batch_load_matches_single_load():
    board, _ = new_board()
    add_challenge(board, cid="c1")
    add_challenge(board, cid="c2")
    with tempfile.TemporaryDirectory() as root:
        state = StateStore(board, None, root)
        loaded = state.load_many(["c1", "c2"])
        assert loaded["c1"][0] == 0 and loaded["c2"][1]["status"] == "ready"
        revision, current = state.load("c1")
        current["steps"] = 2
        state.save("c1", revision, current)
        assert state.load_many(["c1"])["c1"][1]["steps"] == 2
        assert state.challenge_ids() == ["c1"]
    board.close()
