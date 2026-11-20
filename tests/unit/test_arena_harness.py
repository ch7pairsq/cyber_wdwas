"""Arena harness 单元测试：stub HTTP 服务验证映射、409 语义、token 不泄漏。

不访问真实网络；token 用假值，断言它不出现在任何异常文本中。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ctf_ext.harness.arena import ArenaHarnessAdapter, ArenaProtocolError
from ctf_ext.harness.schema import Submission, parse_challenge

TOKEN = "st_test_token_must_not_leak"


class _StubArena(BaseHTTPRequestHandler):
    started: set = set()
    submissions: list = []
    finished: list = []
    # invalid_state 模拟：平台因"题目已完成/attempt 已关闭"拒绝提交（非答案错误）。
    invalid_state: set = {"CH-DONE", "CH-CLOSED"}
    # True = 题目列表携带完成态字段（供 invalid_state 对账探测）。
    extra_challenges: bool = False
    # startup 重试模拟：前 N 次 help 返回 503 resource_unavailable。
    help_failures_remaining: int = 0

    def log_message(self, *args):  # 静默
        pass

    def _auth_ok(self):
        return self.headers.get("Authorization") == "Bearer " + TOKEN

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._auth_ok():
            return self._json({"error": "unauthorized"}, 401)
        if self.path.startswith("/openapi/v1/challenges/help"):
            if _StubArena.help_failures_remaining > 0:
                _StubArena.help_failures_remaining -= 1
                return self._json({"code": "resource_unavailable",
                                   "message": "platform is unavailable", "detail": {}}, 503)
            return self._json({"workflow": "1. start 2. solve 3. submit"})
        if self.path.startswith("/openapi/v1/challenges/files"):
            if "path=" in self.path:
                return self._json({"content": "flag{arena_stub_1}\n"})
            return self._json({"files": ["handouts/note.txt"]})
        if self.path.startswith("/openapi/v1/challenges") or self.path == "/openapi/v1/challenges":
            items = [{
                "unique_code": "CH-1", "name": "stub base64", "category": "misc",
                "description": "decode me", "files": ["handouts/note.txt"], "points": 100,
            }]
            if _StubArena.extra_challenges:
                items = [{
                    "unique_code": "CH-DONE", "name": "already solved", "category": "misc",
                    "completed": True,
                }, {
                    "unique_code": "CH-CLOSED", "name": "attempt closed", "category": "misc",
                    "completed": False,
                }] + items
            return self._json({"challenges": items})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._auth_ok():
            return self._json({"error": "unauthorized"}, 401)
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
        if self.path.startswith("/openapi/v1/challenges/start"):
            code = self.path.split("unique_code=")[-1].split("&")[0]
            if code == "CH-LIMIT":
                return self._json({"error": "environment_limit"}, 409)
            if code in _StubArena.started:
                return self._json({"error": "duplicate"}, 409)
            _StubArena.started.add(code)
            return self._json({"started": True})
        if self.path.startswith("/openapi/v1/challenges/submit"):
            code = self.path.split("unique_code=")[-1].split("&")[0]
            if code in _StubArena.invalid_state:
                return self._json({"code": "invalid_state",
                                   "message": "the attempt or challenge is not in a state "
                                              "that allows this operation"}, 409)
            if code not in _StubArena.started:
                return self._json({"error": "challenge_not_started"}, 409)
            _StubArena.submissions.append(payload)
            if payload.get("flag") == "flag{arena_stub_1}":
                return self._json({"status": "ACCEPTED"})
            return self._json({"status": "REJECTED", "detail": "flag mismatch"})
        if self.path.startswith("/openapi/v1/challenges/finish"):
            _StubArena.finished.append(payload)
            return self._json({"finished": True})
        return self._json({"error": "not found"}, 404)


@pytest.fixture()
def arena_url():
    _StubArena.started = set()
    _StubArena.submissions = []
    _StubArena.finished = []
    _StubArena.extra_challenges = False
    _StubArena.help_failures_remaining = 0
    server = HTTPServer(("127.0.0.1", 0), _StubArena)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:{}".format(server.server_address[1])
    server.shutdown()


def _adapter(url, **kw):
    return ArenaHarnessAdapter(url, token=TOKEN, client_timeout=5.0, **kw)


def test_startup_reads_help_and_context(arena_url):
    ad = _adapter(arena_url)
    ctx = ad.startup()
    assert ctx.protocol == "v1-arena"
    assert ad.help_text["workflow"].startswith("1. start")


def test_startup_retries_5xx_then_succeeds(arena_url):
    """平台 503 resource_unavailable：startup 退避重试后成功（默认烧光重启的教训）。"""
    _StubArena.help_failures_remaining = 2
    ad = _adapter(arena_url, startup_retry_attempts=5, startup_retry_seconds=0.0)
    ad.startup()
    assert ad.help_text["workflow"].startswith("1. start")


def test_startup_persistent_5xx_raises_after_budget(arena_url):
    _StubArena.help_failures_remaining = 99
    ad = _adapter(arena_url, startup_retry_attempts=3, startup_retry_seconds=0.0)
    with pytest.raises(ArenaProtocolError) as ei:
        ad.startup()
    assert ei.value.status == 503 and "503" in str(ei.value)


def test_startup_4xx_fails_immediately_without_retry(arena_url):
    """凭据被拒（401）：立即失败，不消耗重试预算（保持 fail-closed 语义）。"""
    ad = ArenaHarnessAdapter(arena_url, token="st_wrong_token_value",
                             client_timeout=5.0,
                             startup_retry_attempts=5, startup_retry_seconds=0.0)
    with pytest.raises(ArenaProtocolError) as ei:
        ad.startup()
    assert ei.value.status == 401


def test_read_tasks_maps_arena_fields_and_auto_starts(arena_url):
    ad = _adapter(arena_url)
    ad.startup()
    tasks = ad.read_tasks()
    assert len(tasks) == 1
    task = tasks[0]
    assert task.challenge_id == "CH-1"
    assert task.category == "misc"
    assert task.attachments == ("arena://CH-1/handouts/note.txt",)
    assert "CH-1" in _StubArena.started  # 离线题也需要 start
    assert task.metadata["help"]["workflow"].startswith("1. start")


def test_parse_challenge_accepts_v1_arena(arena_url):
    raw = {"challenge_id": "CH-9", "name": "x"}
    assert parse_challenge(raw, protocol="v1-arena").challenge_id == "CH-9"


def test_read_attachment_returns_content(arena_url):
    ad = _adapter(arena_url)
    ad.startup()
    ad.read_tasks()
    data = ad.read_attachment("arena://CH-1/handouts/note.txt", max_bytes=4096)
    assert b"flag{arena_stub_1}" in data


def test_read_attachment_rejects_unlisted_path(arena_url):
    ad = _adapter(arena_url)
    ad.startup()
    ad.read_tasks()
    with pytest.raises(FileNotFoundError):
        ad.read_attachment("arena://CH-1/evil/../etc/passwd", max_bytes=4096)


def test_submit_success_and_idempotency(arena_url):
    ad = _adapter(arena_url)
    ad.startup()
    ad.read_tasks()
    sub = Submission(challenge_id="CH-1", answer="flag{arena_stub_1}",
                     idempotency_key="k-1")
    r1 = ad.submit_answer(sub)
    assert r1.status == "ACCEPTED"
    assert _StubArena.submissions and _StubArena.submissions[0]["flag"] == "flag{arena_stub_1}"
    r2 = ad.submit_answer(sub)  # 幂等：不产生第二次判定
    assert r2 is r1
    assert len(_StubArena.submissions) == 1


def test_submit_not_started_retries_after_start(arena_url):
    ad = _adapter(arena_url, auto_start=False)
    ad.startup()
    ad.read_tasks()
    assert "CH-1" not in _StubArena.started
    sub = Submission(challenge_id="CH-1", answer="flag{arena_stub_1}",
                     idempotency_key="k-2")
    receipt = ad.submit_answer(sub)
    assert receipt.status == "ACCEPTED"
    assert "CH-1" in _StubArena.started  # 409 后自动 start 重试一次


def test_finish_not_sent_by_default(arena_url):
    # 默认绝不自动 finish：本地运行结束不等于比赛收尾（防止误关 attempt）。
    ad = _adapter(arena_url)
    ad.startup()
    from ctf_ext.harness.schema import RunSummary
    out = ad.finalize(RunSummary(results=[{"challenge_id": "CH-1", "status": "completed"}],
                                 notes="done"))
    assert out["protocol"] == "v1-arena"
    assert out["arena_finish_sent"] is False
    assert _StubArena.finished == []


def test_finish_posts_only_when_explicit(arena_url):
    ad = _adapter(arena_url, auto_finish=True)
    ad.startup()
    from ctf_ext.harness.schema import RunSummary
    out = ad.finalize(RunSummary(results=[{"challenge_id": "CH-1", "status": "completed"}],
                                 notes="done"))
    assert out["arena_finish_sent"] is True
    assert _StubArena.finished


def test_token_never_leaks_into_errors(arena_url):
    # 关闭 stub 后触发网络错误，断言异常文本里没有 token。
    ad = _adapter("http://127.0.0.1:1")
    with pytest.raises(ArenaProtocolError) as exc:
        ad.startup()
    assert TOKEN not in str(exc.value)


def test_on_demand_policy_skips_prewarm_and_starts_lazily(arena_url):
    ad = _adapter(arena_url, env_policy="on_demand")
    ad.startup()
    tasks = ad.read_tasks()
    assert tasks[0].challenge_id == "CH-1"
    assert "CH-1" not in _StubArena.started  # on_demand 不预热
    info = ad.ensure_environment("CH-1")
    assert "CH-1" in _StubArena.started  # 意图执行前按需拉起
    assert info.get("started") is True
    assert ad.ensure_environment("CH-1") is None  # 幂等：已启动不再发请求


def test_ensure_environment_tolerates_duplicate_409(arena_url):
    # 平台上已有该环境（另一实例/重启后 start 返回 409 duplicate）→ 视为在跑。
    _adapter(arena_url).startup()
    first = _adapter(arena_url, auto_start=True)
    first.startup()
    first.read_tasks()  # 预热启动 CH-1
    second = _adapter(arena_url, env_policy="on_demand")
    second.startup()
    second.read_tasks()
    assert second.ensure_environment("CH-1") is None  # duplicate 被收编，不抛


def test_ensure_environment_raises_on_environment_limit(arena_url):
    ad = _adapter(arena_url, env_policy="on_demand")
    ad.startup()
    ad.read_tasks()
    with pytest.raises(ArenaProtocolError):
        ad.ensure_environment("CH-LIMIT")


def test_submit_invalid_state_with_completed_challenge_is_accepted(arena_url):
    # 409 invalid_state + 列表证实已完成 → 幂等接受，不伪造 REJECTED。
    _StubArena.extra_challenges = True
    ad = _adapter(arena_url, auto_start=False, env_policy="on_demand")
    ad.startup()
    sub = Submission(challenge_id="CH-DONE", answer="flag{arena_stub_1}",
                     idempotency_key="k-done")
    receipt = ad.submit_answer(sub)
    assert receipt.status == "ACCEPTED"
    assert "already completed" in receipt.detail
    assert _StubArena.submissions == []  # 重发之外的对账不产生新提交


def test_submit_invalid_state_without_completion_stays_unknown(arena_url):
    # 409 invalid_state 但列表证实不了完成态 → UNKNOWN（诚实待对账），不是 REJECTED。
    _StubArena.extra_challenges = True
    ad = _adapter(arena_url, auto_start=False, env_policy="on_demand")
    ad.startup()
    sub = Submission(challenge_id="CH-CLOSED", answer="flag{arena_stub_1}",
                     idempotency_key="k-closed")
    receipt = ad.submit_answer(sub)
    assert receipt.status == "UNKNOWN"
    assert "invalid_state" in receipt.detail
