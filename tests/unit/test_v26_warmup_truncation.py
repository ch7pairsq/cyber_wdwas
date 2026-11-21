"""v26：实战"reasoning 吃满 mt→args 截断卡死→supervisor 快速重启循环"三连修复。

- supervisor progress 预热期：progress 文件跨重启持久，上次卡死的旧 mtime
  让新进程启动几秒内被判死（实战 660/662/664s 三次重启 10 秒烧光）——
  运行超过 progress_stale_seconds 仍无推进才判卡。
- 截断的工具调用拦截：stop_reason=length 且带 tool_calls 时注入精简提示
  in-claim 重试（限 1 次），不让残缺 args 进入执行路径。
- supervisor terminate 前 SIGUSR2 触发 faulthandler 线程栈 dump（落
  agent.log），"活着但不推进"的卡点归因从猜测变成看栈。
"""
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import supervise  # noqa: E402

from tests.unit.test_llm_worker import _payload, _resp, FakeProvider, _setup
from ctf_ext.runtime.llm_worker import LlmWorker


def test_progress_warmup_prevents_instant_kill_of_restarted_process(tmp_path):
    """重启后 progress 旧 mtime 不再立即判死：新进程在预热期内正常跑完。"""
    progress = tmp_path / "progress"
    progress.write_text("stale")  # 模拟上次卡死留下的旧推进标记
    old = time.time() - 7200
    import os as _os
    _os.utime(progress, (old, old))  # mtime 2 小时前，age 远超阈值
    rc = supervise.run(["/bin/sh", "-c", "sleep 0.3; echo done"],
                       progress=str(progress), progress_stale_seconds=10,
                       stale_seconds=180, max_restarts=3)
    assert rc == 0  # 旧实现会在几秒内 stale 重启 3 次后非 0 退出


def _truncated_tool_response():
    from ctf_ext.llm.provider import ProviderResponse
    return ProviderResponse(text="", stop_reason="length", tool_calls=[
        {"name": "python_exec", "input": {"code": "print(1)"}, "id": "t1"}])


def test_truncated_tool_calls_retried_once():
    """stop_reason=length 且带 tool_calls：拦截残缺参数，注入精简提示重试。"""
    board, ev, claim, sha = _setup()
    provider = FakeProvider([_truncated_tool_response(), _resp(_payload(sha))])
    result = LlmWorker(provider, evidence_check=ev.verify).work(claim, {})
    assert result.status == "completed" and result.outcome == "success"
    assert len(provider.calls) == 2
    last_user = [m for m in provider.calls[1]["messages"] if m.get("role") == "user"][-1]
    assert "max_tokens" in last_user["content"]  # 截断纠正提示已注入


def test_truncated_tool_calls_second_time_fails():
    """纠正后仍截断：走失败路径，不无限重试。"""
    board, ev, claim, sha = _setup()
    provider = FakeProvider([_truncated_tool_response(), _truncated_tool_response()])
    result = LlmWorker(provider, evidence_check=ev.verify).work(claim, {})
    assert result.status == "failed"
    assert len(provider.calls) == 2
