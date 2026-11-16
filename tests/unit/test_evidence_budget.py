"""证据库、五级预算与上下文压缩验证。"""
from _support import new_board  # noqa: F401

from ctf_ext.llm.compress import ContextItem, build_context, estimate_tokens
from ctf_ext.runtime.budget import Budget
from ctf_ext.store.evidence import EvidenceStore, EvidenceTooLarge


def _evidence(tmpdir=None):
    board, conn = new_board(tmpdir)
    return EvidenceStore(root=None or __import__("tempfile").mkdtemp(), db=conn)


def test_evidence_roundtrip_and_verify():
    store = _evidence()
    sha = store.put_bytes(b"payload-001", kind="tool:demo")
    assert len(sha) == 64
    assert store.verify(sha) is True
    assert store.verify("0" * 64) is False


def test_evidence_size_cap():
    store = _evidence()
    try:
        store.put_bytes(b"x" * (store._max_bytes + 1))
        raise AssertionError("expected EvidenceTooLarge")
    except EvidenceTooLarge:
        pass


def test_budget_level_ordering():
    now = 1000.0
    b = Budget.from_config(now, run_deadline_epoch=2000, per_challenge_seconds=500,
                           per_strategy_seconds=200, tool_timeout=100, grace=2.0)
    assert b.expired(1000) is None
    assert b.expired(1150) == "tool"
    assert b.expired(1250) == "strategy"
    assert b.expired(1600) == "challenge"
    assert b.expired(2100) == "absolute"
    # strategy 接近 challenge 截止、工具超时很长 → 宽限期触发
    b2 = Budget.from_config(1000.0, 2000, per_challenge_seconds=500,
                            per_strategy_seconds=499, tool_timeout=3600, grace=2.0)
    assert b2.expired(1498.5) == "grace"
    assert b2.expired(1499.5) == "strategy"


def test_compress_keeps_pointers():
    items = [
        ContextItem(key="ev_high", text="高优先证据" * 40, priority=9.0, kind="evidence"),
        ContextItem(key="ev_mid", text="中优先证据" * 40, priority=5.0, kind="evidence"),
        ContextItem(key="ev_low", text="低优先证据" * 40, priority=1.0, kind="evidence"),
    ]
    pack = build_context(items, max_tokens=estimate_tokens("高优先证据" * 40) + 5)
    assert "ev_high" in pack.included
    assert any("ev_mid" in ptr for ptr in pack.pointers)
    assert any("ev_low" in ptr for ptr in pack.pointers)
    assert pack.est_tokens <= estimate_tokens("高优先证据" * 40) + 60
