"""竞赛鲁棒性回归：预览不漏尾部线索；全局预算不能重启洗白或并发丢账。"""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch
from ctf_ext.gateway.policy import PolicyConfig, PolicyEngine
from ctf_ext.gateway.tools import ToolRegistry, ToolResult, ToolSpec
from ctf_ext.harness.flag_gate import FlagGate
from ctf_ext.llm.compress import tool_output_preview
from ctf_ext.runtime.control import BudgetExceeded, GlobalBudget, SessionControl
from ctf_ext.runtime.llm_worker import LlmWorker
from ctf_ext.store.blackboard import Blackboard
from ctf_ext.store.session import StateStore
from tests.framework_support import FLAG, expect, fixture


def test_preview_short_output_is_unchanged():
    assert tool_output_preview("原始输出\n" + FLAG, 512) == "原始输出\n" + FLAG


def test_preview_keeps_tail_flag_and_exact_utf8_offset():
    source = "证据\n" * 2000 + FLAG
    preview = tool_output_preview(source, 1024)
    assert len(preview.encode("utf-8")) <= 1024
    assert FLAG in preview and "未验证观察" in preview
    assert '"byte_offset":' + str(len(("证据\n" * 2000).encode("utf-8"))) in preview


def test_preview_preserves_secret_markers_without_copying_private_key_body():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1MSJ9.signature"
    connection = "postgresql://user:pass@db.local/db"
    header = "-----BEGIN RSA PRIVATE KEY-----"
    source = ("noise\n" * 3000 + jwt + "\n" + connection + "\n" + header
              + "\nVERY_PRIVATE_KEY_BODY\n-----END RSA PRIVATE KEY-----")
    preview = tool_output_preview(source, 4096)
    assert jwt in preview and connection in preview and header in preview
    assert "VERY_PRIVATE_KEY_BODY" not in preview
    assert len(preview.encode("utf-8")) <= 4096


def test_preview_many_candidates_stays_bounded_and_does_not_claim_verified():
    source = "noise " * 2000 + "\n".join("flag{candidate_%03d}" % i for i in range(50))
    preview = tool_output_preview(source, 1200)
    assert len(preview.encode("utf-8")) <= 1200
    assert 0 < preview.count('"kind":"flag_candidate"') <= 8
    assert "未验证观察" in preview and "完整内容见 evidence_id" in preview


def test_worker_preview_retains_evidence_anchor_and_candidate():
    worker = LlmWorker(object(), max_tool_result_bytes=1024)
    result = ToolResult(status="ok", output="输出\n" * 3000 + FLAG,
                        evidence_id="evidence-123", command_id="command-456")
    rendered = worker._tool_result_text(result)
    header, body = rendered.split("\n", 1)
    assert "evidence_id=evidence-123" in header and "command_id=command-456" in header
    assert FLAG in body and len(body.encode("utf-8")) <= 1024
    assert result.output.endswith(FLAG)


def test_summary_policy_archives_full_output_and_cache_does_not_lose_tail():
    with fixture() as store:
        source = "noise\n" * 3000 + FLAG
        policy = PolicyEngine(PolicyConfig(command_allowlist=("echo",), sandbox_required=False))
        registry = ToolRegistry(policy, evidence_store=store.evidence, work_root=str(store.root),
                                state_store=store)
        registry.register(ToolSpec(name="echo", command="echo", evidence_policy="summary"))
        control = SessionControl(store, "c1")
        with patch("ctf_ext.gateway.tools.run_command", return_value={
                "output": source, "returncode": 0, "status": "ok", "truncated": False}) as run:
            first = registry.execute("echo", ["echo", "fixture"], control=control)
            second = registry.execute("echo", ["echo", "fixture"], control=control)
        assert run.call_count == 1 and first.status == second.status == "ok"
        assert store.restore_evidence(first.evidence_id).decode("utf-8") == source
        assert second.output == source
        store.declare("c1", FLAG)
        assert FlagGate(store).evaluate("c1", FLAG, [second.evidence_id]).status == "confirmed"
        assert FLAG in LlmWorker(object(), max_tool_result_bytes=1024)._tool_result_text(second)


def test_preview_tiny_budgets_and_multibyte_output_remain_valid():
    source = "字" * 6000 + FLAG
    for limit in (256, 257, 512, 1024):
        preview = tool_output_preview(source, limit)
        assert len(preview.encode("utf-8")) <= limit
        assert "截断" in preview and "\ufffd" not in preview


def test_global_budget_restored_at_exact_limit_blocks_new_call():
    with fixture() as store:
        budget = GlobalBudget(store, max_total_tokens=10)
        budget.charge(10)
        restored = GlobalBudget(store, max_total_tokens=10)
        assert restored.spent == 10 and restored.exhausted and restored.remaining() == 0
        control = SessionControl(store, "c1", global_budget=restored)
        expect(BudgetExceeded, lambda: control.reserve("model", "v1", [], "input", is_tool=False))
        assert store.load("c1")[1]["steps"] == 0


def test_global_budget_caps_session_remaining_tokens():
    with fixture() as store:
        budget = GlobalBudget(store, max_total_tokens=100)
        budget.charge(93)
        control = SessionControl(store, "c1", max_tokens=50000, global_budget=budget)
        assert control.remaining()["tokens"] == 7


def test_global_budget_multiple_instances_and_threads_do_not_lose_usage():
    with fixture() as store:
        budgets = [GlobalBudget(store, max_total_tokens=10000) for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: budgets[i % 4].charge(1), range(80)))
        assert all(budget.spent == 80 for budget in budgets)
        assert GlobalBudget(store, max_total_tokens=10000).remaining() == 9920


def test_global_budget_separate_connections_accumulate_usage():
    with fixture() as store:
        first = GlobalBudget(store, max_total_tokens=100)
        path = store.db.execute("PRAGMA database_list").fetchone()[2]
        db = sqlite3.connect(path, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            second_store = StateStore(Blackboard(db), store.evidence, store.root)
            second = GlobalBudget(second_store, max_total_tokens=100)
            first.charge(13)
            second.charge(17)
            first.charge(1)
            assert first.spent == second.spent == 31
        finally:
            db.close()


def test_global_budget_unlimited_mode_still_records_usage():
    with fixture() as store:
        budget = GlobalBudget(store, max_total_tokens=0)
        budget.charge(25)
        restored = GlobalBudget(store, max_total_tokens=100)
        assert restored.spent == 25 and restored.remaining() == 75
        assert budget.remaining() == float("inf") and not budget.exhausted


def test_global_budget_corrupt_ledger_is_not_treated_as_zero():
    with fixture() as store:
        for payload in ("bad-json", "{}", "[]", '{"model_tokens":true}',
                        '{"model_tokens":-1}', '{"model_tokens":"30"}'):
            with store.board.transaction() as db:
                db.execute("INSERT OR REPLACE INTO controller_settings VALUES(?,?)",
                           (GlobalBudget.KEY, payload))
            expect(ValueError, lambda: GlobalBudget(store, max_total_tokens=100))


def test_global_budget_database_error_is_not_treated_as_zero():
    broken = SimpleNamespace(board=SimpleNamespace(read_view=lambda: (_ for _ in ()).throw(
        sqlite3.OperationalError("unavailable"))))
    expect(sqlite3.OperationalError, lambda: GlobalBudget(broken, max_total_tokens=100))


def test_global_budget_corruption_after_load_does_not_get_overwritten():
    with fixture() as store:
        budget = GlobalBudget(store, max_total_tokens=100)
        with store.board.transaction() as db:
            db.execute("INSERT INTO controller_settings VALUES(?,?)", (GlobalBudget.KEY, "{}"))
        expect(ValueError, lambda: budget.charge(1))
        assert store.db.execute("SELECT payload FROM controller_settings WHERE key=?",
                                (GlobalBudget.KEY,)).fetchone()[0] == "{}"


def test_global_budget_overrun_is_persisted_before_exception():
    with fixture() as store:
        budget = GlobalBudget(store, max_total_tokens=10)
        expect(BudgetExceeded, lambda: budget.charge(11))
        restored = GlobalBudget(store, max_total_tokens=10)
        assert restored.exhausted and restored.spent == 11


def test_global_budget_rejects_invalid_limit_and_usage():
    with fixture() as store:
        for limit in (-1, True, 1.5, "100"):
            expect(ValueError, lambda: GlobalBudget(store, max_total_tokens=limit))
        budget = GlobalBudget(store, max_total_tokens=100)
        for tokens in (-1, True, 1.5, "10"):
            expect(ValueError, lambda: budget.charge(tokens))
        assert budget.spent == 0
