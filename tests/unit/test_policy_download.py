"""Fail Closed 与白名单验证（§15、§16、附录 B.4）。"""
import io

from _support import new_board  # noqa: F401  确保导入路径

from ctf_ext.gateway.download import DownloadController, QuotaExceeded
from ctf_ext.gateway.policy import PolicyConfig, PolicyDenied, PolicyEngine
from ctf_ext.gateway.sandbox import run_command
from ctf_ext.harness.mock import MockHarnessAdapter
from ctf_ext.harness.schema import Submission
from ctf_ext.harness.submit import Submitter, SubmissionLocked


class _FakeResp:
    def __init__(self, data: bytes, content_length: str = ""):
        self._data = data
        self.headers = {"Content-Length": content_length}

    def read(self, n):
        out, self._data = self._data[:n], self._data[n:]
        return out

    def close(self):
        pass


class _FakeOpener:
    def __init__(self, data: bytes, content_length: str = ""):
        self.data = data
        self.cl = content_length
        self.opened = False

    def open(self, req, timeout=None):
        self.opened = True
        return _FakeResp(self.data, self.cl)


def test_download_fail_closed_when_limits_unset():
    policy = PolicyEngine(PolicyConfig(authorized_scope=("example.com",)))
    opener = _FakeOpener(b"x")
    ctrl = DownloadController(policy, opener=opener)
    try:
        ctrl.fetch_bytes("https://example.com/x")
        raise AssertionError("expected PolicyDenied")
    except PolicyDenied as exc:
        assert exc.reason_code == "QUOTA_UNSET"
    assert opener.opened is False  # 拒绝时绝不发起连接


def test_download_scope_and_scheme_deny():
    policy = PolicyEngine(PolicyConfig(authorized_scope=("example.com",),
                                       max_single_bytes=100, max_total_bytes=100))
    ctrl = DownloadController(policy, opener=_FakeOpener(b"x"))
    for url, code in (("http://example.com/x", "QUOTA_SCHEME"),
                      ("https://evil.com/x", "SCOPE")):
        try:
            ctrl.fetch_bytes(url)
            raise AssertionError("expected deny for " + url)
        except PolicyDenied as exc:
            assert exc.reason_code == code


def test_download_single_and_total_quota():
    policy = PolicyEngine(PolicyConfig(authorized_scope=("example.com",),
                                       max_single_bytes=10, max_total_bytes=15))
    ctrl = DownloadController(policy, opener=_FakeOpener(b"123456", "6"))
    assert ctrl.fetch_bytes("https://example.com/a") == b"123456"
    assert ctrl.fetch_bytes("https://example.com/b") == b"123456"
    assert ctrl.total_used == 12
    # 第三个 6B 会突破总配额 15 → 拒绝
    ctrl2 = DownloadController(policy, opener=_FakeOpener(b"123456", "6"))
    ctrl2.total_used = 12
    try:
        ctrl2.fetch_bytes("https://example.com/c")
        raise AssertionError("expected QuotaExceeded")
    except (QuotaExceeded, PolicyDenied) as exc:
        assert isinstance(exc, PolicyDenied) and exc.reason_code == "QUOTA_TOTAL"


def test_download_content_length_precheck():
    policy = PolicyEngine(PolicyConfig(authorized_scope=("example.com",),
                                       max_single_bytes=10, max_total_bytes=100))
    opener = _FakeOpener(b"x" * 12, "12")
    ctrl = DownloadController(policy, opener=opener)
    try:
        ctrl.fetch_bytes("https://example.com/big")
        raise AssertionError("expected QUOTA_SIZE")
    except PolicyDenied as exc:
        assert exc.reason_code == "QUOTA_SIZE"
    assert opener.opened is True  # 已连接但被预检拦截，未读入超限数据


def test_command_allowlist():
    policy = PolicyEngine(PolicyConfig(command_allowlist=("echo",)))
    out = run_command(["echo", "hi"], policy, timeout=10)
    assert out["status"] == "ok" and "hi" in out["output"]
    strict = PolicyEngine(PolicyConfig(command_allowlist=("ls",)))
    try:
        run_command(["echo", "hi"], strict, timeout=10)
        raise AssertionError("expected COMMAND_NOT_ALLOWED")
    except PolicyDenied as exc:
        assert exc.reason_code == "COMMAND_NOT_ALLOWED"


def test_submission_locked_in_competition():
    policy = PolicyEngine(PolicyConfig(competition_locked=True))
    adapter = MockHarnessAdapter([{"challenge_id": "c1", "name": "n", "flag": "flag{x}"}])
    submitter = Submitter(adapter, policy_engine=policy)
    try:
        submitter.submit(Submission(challenge_id="c1", answer="flag{x}", idempotency_key="k"))
        raise AssertionError("expected SubmissionLocked")
    except SubmissionLocked as exc:
        assert str(exc) == "SUBMIT_LOCK"
