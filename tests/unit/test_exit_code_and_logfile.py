"""v25 退出码与日志落盘：实战"解出 5/8 题后容器提前死亡"修复。

- 退出码语义：旧 `0 if solved else 3` 让部分解出后的异常退场也 rc=0，
  supervisor 对 rc=0 无条件放行——解得越多越容易提前死亡且无救援；
  改为 solved==tasks 才 0，部分解出提前结束返回 3 触发有限重启续跑。
- supervisor --log-file：子进程输出透传平台的同时落盘独立副本。
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import supervise  # noqa: E402


def _out(tasks, solved_n):
    return {"tasks": tasks, "solved": ["c{}".format(i) for i in range(solved_n)]}


def test_exit_code_zero_only_when_all_solved():
    assert supervise is not None
    from ctf_ext.__main__ import _exit_code
    assert _exit_code(_out(8, 8)) == 0  # 全部解出：真完成
    assert _exit_code(_out(8, 5)) == 3  # 部分解出提前退场：触发 supervisor 重启
    assert _exit_code(_out(8, 0)) == 3  # 零解出
    assert _exit_code({"tasks": 0, "solved": []}) == 3  # 空赛程保守按未完成


def test_supervisor_tees_child_output_to_log_file(tmp_path):
    log = tmp_path / "agent.log"
    rc = supervise.run(["/bin/sh", "-c", "echo hello-out; echo hello-err >&2"],
                       max_restarts=0, log_file=str(log))
    assert rc == 0
    text = log.read_text()
    assert "hello-out" in text and "hello-err" in text
    assert "===== supervisor launch" in text
