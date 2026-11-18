"""Mock Harness 端到端：输入→判定→幂等→UNKNOWN 只查不发（附录 F-4、E）。"""
from _support import new_board  # noqa: F401

from ctf_ext.harness.mock import MockHarnessAdapter
from ctf_ext.harness.schema import Submission
from tests.framework_submitter import Submitter


class _FlakyAdapter(MockHarnessAdapter):
    """首次提交返回 UNKNOWN，之后可查询回执；记录提交次数验证不重发。"""

    def __init__(self, challenges):
        super().__init__(challenges)
        self.submit_calls = 0
        self._pending = {}

    def submit_answer(self, submission):
        self.submit_calls += 1
        if self.submit_calls == 1:
            from ctf_ext.harness.schema import SubmitReceipt
            self._pending[submission.idempotency_key] = submission
            return SubmitReceipt(status="UNKNOWN", detail="connection lost")
        return super().submit_answer(submission)

    def get_receipt(self, idempotency_key):
        sub = self._pending.get(idempotency_key)
        if sub is None:
            return None
        receipt = super().submit_answer(sub)  # 查询即真实判定（幂等）
        return receipt if receipt.status == "ACCEPTED" else None


def test_mock_end_to_end():
    adapter = MockHarnessAdapter([
        {"challenge_id": "ch1", "name": "ez", "category": "misc", "flag": "flag{mock_001}"},
    ])
    ctx = adapter.startup()
    assert ctx.protocol == "v1-mock"
    tasks = adapter.read_tasks()
    assert len(tasks) == 1 and tasks[0].challenge_id == "ch1"
    submitter = Submitter(adapter)
    ok = submitter.submit(Submission(challenge_id="ch1", answer="flag{mock_001}", idempotency_key="k1"))
    assert ok.status == "ACCEPTED"
    bad = submitter.submit(Submission(challenge_id="ch1", answer="flag{wrong}", idempotency_key="k2"))
    assert bad.status == "REJECTED"
    # 同幂等键重复提交 → 返回首次回执，不产生第二次判定（附录 E）
    again = submitter.submit(Submission(challenge_id="ch1", answer="flag{mock_001}", idempotency_key="k1"))
    assert again.status == "ACCEPTED" and again.receipt_id == ok.receipt_id
    assert len(adapter._receipts) == 2


def test_unknown_never_resent():
    adapter = _FlakyAdapter([{"challenge_id": "ch1", "name": "n", "flag": "flag{m}"}])
    adapter.startup()
    adapter.read_tasks()
    submitter = Submitter(adapter)
    sub = Submission(challenge_id="ch1", answer="flag{m}", idempotency_key="ku")
    first = submitter.submit(sub)
    assert first.status == "UNKNOWN"
    second = submitter.submit(sub)  # 只查询回执
    assert second.status == "ACCEPTED"
    assert adapter.submit_calls == 1  # 绝不自动重发


def test_unsupported_protocol_fails_closed():
    from ctf_ext.harness.schema import UnsupportedProtocol, parse_challenge
    try:
        parse_challenge({"challenge_id": "x", "name": "y"}, protocol="v9-unknown")
        raise AssertionError("expected UnsupportedProtocol")
    except UnsupportedProtocol:
        pass
