"""交接包的 6000 字节硬上限：裁剪后仍超限时继续降级。"""
import json

from ctf_ext.runtime.llm_worker import LlmWorker


def test_handoff_is_bounded_after_trimming():
    handoff = {
        "recent_attempts": [{"strategy_key": "x" * 2000, "failure_reason": "y" * 2000}
                            for _ in range(5)],
        "lessons": [{"text": "z" * 2000} for _ in range(6)],
        "open_intents": [{"id": "i" * 2000} for _ in range(5)],
    }
    bounded = LlmWorker._bounded_handoff(handoff)
    assert len(json.dumps(bounded, ensure_ascii=False)) <= 6000
    assert set(bounded) == {"recent_attempts", "lessons", "open_intents"}


def test_handoff_keeps_short_fields_unchanged():
    handoff = {"recent_attempts": [{"strategy_key": "x"}],
               "lessons": [{"text": "L"}], "open_intents": [{"id": "i"}]}
    bounded = LlmWorker._bounded_handoff(handoff)
    assert bounded == handoff
