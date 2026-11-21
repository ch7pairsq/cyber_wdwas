"""Arena harness 单元测试：stub HTTP 服务验证映射、409 语义、token 不泄漏。

不访问真实网络；token 用假值，断言它不出现在任何异常文本中。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tests.framework_golden import golden_response
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
    # 旧协议模拟：集合内的题目提交返回字符串 status/result（默认空 = 全部
    # 用 2026-11-21 平台实测 schema：布尔 correct + 计分字段）。
    legacy_submit: set = set()
    # start 响应可配置：测试 env_note 的 container_addr 有/无两种分支。
    start_payload: dict = {"started": True}

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
            return self._json(dict(_StubArena.start_payload))
        if self.path.startswith("/openapi/v1/challenges/submit"):
            code = self.path.split("unique_code=")[-1].split("&")[0]
            if code in _StubArena.invalid_state:
                return self._json({"code": "invalid_state",
                                   "message": "the attempt or challenge is not in a state "
                                              "that allows this operation"}, 409)
            if code not in _StubArena.started:
                return self._json({"error": "challenge_not_started"}, 409)
            _StubArena.submissions.append(payload)
            if code in _StubArena.legacy_submit:
                if payload.get("flag") == "flag{arena_stub_1}":
                    return self._json({"status": "ACCEPTED"})
                return self._json({"status": "REJECTED", "detail": "flag mismatch"})
            if payload.get("flag") == "flag{arena_stub_1}":
                # 2026-11-21 正式赛实测回执形状（fixtures/golden）：布尔 correct
                # + 计分字段；数值按 stub 题分（100）调整。
                receipt = dict(golden_response("submit_correct"),
                               awarded=100, cumulative_score=100)
                return self._json(receipt)
            return self._json(dict(golden_response("submit_wrong"),
                                   cumulative_score=100))
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
    _StubArena.legacy_submit = set()
    _StubArena.start_payload = golden_response("start_no_addr")
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


def test_submit_logs_flag_and_verdict(arena_url, capsys):
    # 错题观测地基：[submit] 行必须包含 flag 原文与平台回执（2026-11-21 正式赛
    # 3 道错题只留下传输日志、回执无处可查的教训）；幂等重放也带标记。
    ad = _adapter(arena_url)
    ad.startup()
    ad.read_tasks()
    sub = Submission(challenge_id="CH-1", answer="flag{arena_stub_1}",
                     idempotency_key="k-log")
    receipt = ad.submit_answer(sub)
    assert receipt.status == "ACCEPTED"
    captured = capsys.readouterr()
    assert "[submit]" in captured.err
    assert "ch=CH-1" in captured.err and "flag=flag{arena_stub_1}" in captured.err
    assert "verdict=ACCEPTED" in captured.err
    ad.submit_answer(sub)
    captured = capsys.readouterr()
    assert "(idempotent replay)" in captured.err


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
    # 幂等：已启动不再发请求（started 集合不变），但仍返回已存 start_info
    # 并刷新描述（2026-11-21 正式赛：early return 曾让地址永远进不了描述）。
    assert ad.ensure_environment("CH-1") == info


def test_ensure_environment_tolerates_duplicate_409(arena_url):
    # 平台上已有该环境（另一实例/重启后 start 返回 409 duplicate）→ 视为在跑。
    _adapter(arena_url).startup()
    first = _adapter(arena_url, auto_start=True)
    first.startup()
    first.read_tasks()  # 预热启动 CH-1
    second = _adapter(arena_url, env_policy="on_demand")
    second.startup()
    second.read_tasks()
    # duplicate 被收编，不抛；返回本进程已知的 start_info（可能为空 dict），
    # 描述同步刷新（无地址时写明"平台未返回靶机地址"，不误报已启动）。
    assert second.ensure_environment("CH-1") == {}


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


def test_submit_platform_correct_schema_accepted(arena_url):
    # 平台真实回执（布尔 correct + 计分字段）必须解析为 ACCEPTED：v11 正式赛
    # 第 1 题 correct=true 已计 1000 分，仍因旧解析只认 status/result 字符串
    # 落 UNKNOWN 的教训。detail 带计分字段供 [submit] 日志观测。
    ad = _adapter(arena_url)
    ad.startup()
    ad.read_tasks()
    sub = Submission(challenge_id="CH-1", answer="flag{arena_stub_1}",
                     idempotency_key="k-new-ok")
    receipt = ad.submit_answer(sub)
    assert receipt.status == "ACCEPTED"
    assert "awarded=100" in receipt.detail
    assert "cumulative_score=100" in receipt.detail
    assert "is_completed=True" in receipt.detail


def test_submit_platform_correct_schema_rejected(arena_url):
    # correct=false 必须落 REJECTED（不得像 v11 那样丢进 UNKNOWN——REJECTED
    # 语义驱动编排层重开题目续解）。
    ad = _adapter(arena_url)
    ad.startup()
    ad.read_tasks()
    sub = Submission(challenge_id="CH-1", answer="flag{wrong_flag_x9}",
                     idempotency_key="k-new-bad")
    receipt = ad.submit_answer(sub)
    assert receipt.status == "REJECTED"
    assert "awarded=0" in receipt.detail
    assert "correct_flag_count=0" in receipt.detail


def test_submit_legacy_status_schema_still_supported(arena_url):
    # 旧协议（字符串 status/result）兼容保留：练习平台或降级版本可能仍返回。
    _StubArena.legacy_submit.add("CH-1")
    ad = _adapter(arena_url)
    ad.startup()
    ad.read_tasks()
    ok = ad.submit_answer(Submission(challenge_id="CH-1", answer="flag{arena_stub_1}",
                                     idempotency_key="k-legacy-ok"))
    bad = ad.submit_answer(Submission(challenge_id="CH-1", answer="flag{wrong_flag_x9}",
                                      idempotency_key="k-legacy-bad"))
    assert ok.status == "ACCEPTED"
    assert bad.status == "REJECTED"


def test_env_note_guides_arena_http_with_container_addr(arena_url):
    """正式赛教训（2026-11-21）：模型不知道靶机访问约定，40 轮只用 python_exec。
    env_note 必须把 arena_http 访问方式写进模型唯一可读面。"""
    import os
    _StubArena.start_payload = golden_response("start_with_addr")
    old = os.environ.get("ARENA_NETWORK_CIDR")
    os.environ["ARENA_NETWORK_CIDR"] = "10.201.0.0/16"
    try:
        ad = _adapter(arena_url, env_policy="on_demand")
        ad.startup()
        ad.read_tasks()  # 正式赛由编排层调用；此处补齐 _tasks 供 note append
        ad.ensure_environment("CH-1")
        desc = ad.task_description("CH-1")
        assert "题目服务地址: http://10.201.0.201:3000" in desc
        assert "优先用 arena_http 工具" in desc
        assert "requests/httpx 默认走平台 HTTP_PROXY 隧道" in desc  # 2026-11-22 认知修正
        assert "手写 socket" in desc
        assert "允许访问的目标网段" in desc
    finally:
        if old is None:
            os.environ.pop("ARENA_NETWORK_CIDR", None)
        else:
            os.environ["ARENA_NETWORK_CIDR"] = old


def test_env_note_pending_status_warns_not_ready(arena_url):
    """第三场实测（2026-11-21）：start 返回 status='pending' + expires_at=None
    ——容器未就绪。note 必须告知"拉起中，失败等 30-60s 重试，不要判未启动"。"""
    import os
    payload = dict(golden_response("start_no_addr"))
    payload["container_status"] = "pending"
    payload["expires_at"] = None
    _StubArena.start_payload = payload
    os.environ["ARENA_NETWORK_CIDR"] = "10.201.0.0/16"
    try:
        ad = _adapter(arena_url, env_policy="on_demand")
        ad.startup()
        ad.read_tasks()
        ad.ensure_environment("CH-1")
        desc = ad.task_description("CH-1")
        assert "容器状态: pending" in desc
        assert "尚未就绪" in desc
        assert "不要判定靶机未启动" in desc
        assert "容器租期至" not in desc  # expires_at=None 不写租期节
    finally:
        os.environ.pop("ARENA_NETWORK_CIDR", None)


def test_env_note_expires_at_warns_lease(arena_url):
    """第三场实测：weak-password status='available' + expires_at 有值
    （约 start+30min 租期）。就绪态不写 pending 提示，但租期要告知。"""
    import os
    payload = dict(golden_response("start_no_addr"))
    payload["container_status"] = "available"
    payload["expires_at"] = "2026-11-21T12:45:11.912219+08:00"
    _StubArena.start_payload = payload
    os.environ["ARENA_NETWORK_CIDR"] = "10.201.0.0/16"
    try:
        ad = _adapter(arena_url, env_policy="on_demand")
        ad.startup()
        ad.read_tasks()
        ad.ensure_environment("CH-1")
        desc = ad.task_description("CH-1")
        assert "容器租期至 2026-11-21T12:45:11" in desc
        assert "到期平台可能回收容器" in desc
        assert "容器状态: available" not in desc  # 就绪态不写状态节
        assert "尚未就绪" not in desc
    finally:
        os.environ.pop("ARENA_NETWORK_CIDR", None)


def test_env_note_without_container_addr_warns_model(arena_url):
    """start 响应无 container_addr（正式赛 static_file_server 疑似形态）：
    描述必须显式告知模型缺地址 + 给出推导与工具路径，而不是让模型
    自己得出"靶机未启动"的错误结论。"""
    import os
    _StubArena.start_payload = golden_response("start_no_addr")
    old = os.environ.get("ARENA_NETWORK_CIDR")
    try:
        os.environ.pop("ARENA_NETWORK_CIDR", None)
        ad = _adapter(arena_url, env_policy="on_demand")
        ad.startup()
        ad.read_tasks()  # 正式赛由编排层调用；此处补齐 _tasks 供 note append
        ad.ensure_environment("CH-1")
        desc = ad.task_description("CH-1")
        assert "平台未返回靶机地址" in desc
        assert "arena_help 推导" in desc  # 无地址时给推导路径
        assert "arena_http" in desc
        assert "requests/httpx 默认走平台 HTTP_PROXY 隧道" in desc
        assert "允许访问的目标网段" not in desc  # CIDR unset 时不写该节
    finally:
        if old is not None:
            os.environ["ARENA_NETWORK_CIDR"] = old


def test_start_diag_prints_container_addr(arena_url, capsys):
    """start 诊断必须单独打印 keys 与 container_addr（stdout 是比赛唯一
    可观测面；desc 占满截断窗口导致地址是否存在无法判断）。"""
    import sys
    from contextlib import redirect_stderr
    import io
    _StubArena.start_payload = dict(golden_response("start_with_addr"),
                                    container_addr="10.201.0.9:80",
                                    container_status="running",
                                    expires_at="2026-11-22T12:00:00Z")
    ad = _adapter(arena_url, env_policy="on_demand")
    ad.startup()
    buf = io.StringIO()
    with redirect_stderr(buf):
        ad.ensure_environment("CH-1")
    out = buf.getvalue()
    assert "container_addr='10.201.0.9:80'" in out
    assert "container_status='running'" in out
    assert "expires_at='2026-11-22T12:00:00Z'" in out
    assert "keys=" in out


def test_arena_http_accepts_multi_cidr_targets():
    """正式赛 ARENA_NETWORK_CIDR unset 曾让 arena_http 全程 DENY。
    多网段逗号分隔必须支持；网段外仍拒绝。"""
    import os
    import subprocess
    import sys
    tool = "ctf_ext/gateway/arena_http.py"
    env = dict(os.environ)
    env["ARENA_PROXY"] = "socks5://127.0.0.1:1"  # 不存在的代理：网段校验通过后死在连接
    env["ARENA_NETWORK_CIDR"] = "10.201.0.0/16,192.168.0.0/16"
    inside = subprocess.run([sys.executable, tool, "--host", "192.168.5.5", "--port", "80"],
                            capture_output=True, text=True, timeout=30, env=env)
    assert "target outside" not in inside.stdout + inside.stderr
    outside = subprocess.run([sys.executable, tool, "--host", "8.8.8.8", "--port", "80"],
                             capture_output=True, text=True, timeout=30, env=env)
    assert "DENY: target outside attempt network" in outside.stdout + outside.stderr


def test_ensure_environment_refreshes_note_after_start_returns_addr(arena_url):
    """正式赛 bug 回归（2026-11-21）：on_demand 下 read_tasks 先写"平台未返回
    靶机地址"note，ensure_environment 幂等 early return 导致 start 真拿到的
    container_addr 永远进不了模型描述。必须刷新为最新地址。"""
    import os
    _StubArena.start_payload = golden_response("start_with_addr")
    old = os.environ.get("ARENA_NETWORK_CIDR")
    os.environ["ARENA_NETWORK_CIDR"] = "10.201.0.0/16"
    try:
        ad = _adapter(arena_url, env_policy="on_demand")
        ad.startup()
        ad.read_tasks()
        first = ad.task_description("CH-1")
        assert "平台未返回靶机地址" in first          # read_tasks 时还没有 start_info
        ad.ensure_environment("CH-1")                  # start 成功返回地址 → 必须刷新
        desc = ad.task_description("CH-1")
        assert "题目服务地址: http://10.201.0.201:3000" in desc
        assert "平台未返回靶机地址" not in desc        # 旧 note 已被替换
        assert desc.count("[Arena 运行环境]") == 1     # 幂等：不重复叠加
    finally:
        if old is None:
            os.environ.pop("ARENA_NETWORK_CIDR", None)
        else:
            os.environ["ARENA_NETWORK_CIDR"] = old
