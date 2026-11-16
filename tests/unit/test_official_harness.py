"""官方 Harness：SDK 缺失时 Fail Closed，注入 SDK 后只转发（§12.5）。"""
from ctf_ext.harness.official import OfficialHarnessAdapter, OfficialProtocolUnavailable


class FullSDK:
    def startup(self):
        return "started"

    def read_tasks(self):
        return ["task-1"]

    def read_attachment(self, ref, *, max_bytes=None):
        return ("bytes:" + ref + ":" + str(max_bytes)).encode()

    def submit_answer(self, submission):
        return "accepted"

    def get_receipt(self, key):
        return "receipt:" + key

    def finalize(self, summary):
        return {"ok": True}


def test_no_sdk_fails_closed_with_checklist():
    adapter = OfficialHarnessAdapter()
    report = adapter.capability_report()
    assert report["ready"] is False and len(report["missing"]) == 6
    try:
        adapter.read_tasks()
        raise AssertionError("expected OfficialProtocolUnavailable")
    except OfficialProtocolUnavailable as exc:
        assert "read_tasks" in str(exc)


def test_partial_sdk_reports_exact_gap():
    class Partial:
        def read_tasks(self):
            return []

    adapter = OfficialHarnessAdapter(Partial())
    missing = adapter.capability_report()["missing"]
    assert "submit_answer" in missing and "read_tasks" not in missing
    try:
        adapter.submit_answer(object())
        raise AssertionError("expected OfficialProtocolUnavailable")
    except OfficialProtocolUnavailable as exc:
        assert "submit_answer" in str(exc)


def test_complete_sdk_is_delegated_verbatim():
    adapter = OfficialHarnessAdapter(FullSDK())
    assert adapter.capability_report()["ready"] is True
    assert adapter.startup() == "started"
    assert adapter.read_tasks() == ["task-1"]
    assert adapter.read_attachment("a.bin", max_bytes=10) == b"bytes:a.bin:10"
    assert adapter.submit_answer("sub") == "accepted"
    assert adapter.get_receipt("k") == "receipt:k"
    assert adapter.finalize(None) == {"ok": True}
