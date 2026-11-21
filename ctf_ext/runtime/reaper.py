"""孤儿进程收割：PR_SET_CHILD_SUBREAPER + 后台 waitpid 线程。

容器内 PID1（平台 entrypoint bash）不收割孤儿僵尸：python_exec 里解题
脚本 spawn 的子进程（qemu/JVM/tesseract 等）退出后先成孤儿、再成僵尸，
无人收割会让进程表耗尽、fork 返回 EAGAIN——2026-11-21 第 19 题正式赛
python_exec 无法 fork（BlockingIOError EAGAIN 后持续 TOOL_BUSY）的根因。
agent 主进程设 subreaper 后，孤儿被收养到本进程，由收割线程回收。

已知取舍：收割线程 os.waitpid(-1) 理论上可抢走 capture() 尚未 wait 的
已退出子进程，使该次命令误报 returncode=0（CPython subprocess 把
ChildProcessError 折算为 0）。收养只会发生在"父进程已死"的进程上，
而 capture 的直接子进程父进程（agent）始终存活——只有毫秒级的
"已退出未 wait"窗口可被抢占，误报仅影响 rc 元数据，output 证据仍真实。
仅 Linux 生效；prctl/线程失败只返回状态描述，不抛异常（fail-open）。
"""
from __future__ import annotations

import ctypes
import os
import threading
import time

_PR_SET_CHILD_SUBREAPER = 36  # linux/prctl.h


def install_subreaper(interval: float = 5.0) -> str:
    """设 subreaper 并启动收割线程；返回状态描述（不抛异常）。

    interval 为收割轮询间隔（秒）；间隔内孤儿暂存为僵尸，不影响 fork
    可用 pid（僵尸只占进程表项，实测 19 题/4h 量级远未触顶——收割只为
    防长跑累积）。非 Linux 平台（macOS 开发机）返回 unavailable。
    """
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
    except OSError:
        return "unavailable: libc.so.6 not loadable"
    try:
        if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
            return "unavailable: prctl errno={}".format(ctypes.get_errno())
    except Exception as exc:  # noqa: BLE001  prctl 签名异常按不可用处理
        return "unavailable: {}".format(type(exc).__name__)

    def _reap():
        while True:
            try:
                # WNOHANG 循环清干当前可收割的子进程；无子进程时抛 ChildProcessError。
                while os.waitpid(-1, os.WNOHANG)[0] != 0:
                    pass
            except (ChildProcessError, OSError):
                pass
            time.sleep(interval)

    threading.Thread(target=_reap, name="orphan-reaper", daemon=True).start()
    return "installed"
