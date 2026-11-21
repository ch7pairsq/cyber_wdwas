"""推进心跳：真实解题活动（模型调用/工具完成/结果落库）的时间戳文件。

与 HeartbeatFile（进程存活）分离：pump 空转时存活心跳仍在刷，但推进
心跳停更——supervisor 据此发现"活着但不推进"的卡死。2026-11-21 第 19
题正式赛实测：worker 卡在 fork 阻塞（fork EAGAIN），30 分钟 claim_timeout
(SIGALRM) 才打断一次、重开后又卡，35 分钟无模型调用且 supervisor 无感知。

写入点刻意避开 orchestrator pump 主循环（空转 poll 不算推进）：只有
模型调用、工具执行、结果落库这类真实前进动作才刷新。未配置
CTF_PROGRESS_HEARTBEAT 时全部 touch 为 no-op（本地/练习环境零影响）。
"""
from __future__ import annotations

import os
import time
from typing import Callable, Optional

ENV_NAME = "CTF_PROGRESS_HEARTBEAT"


def progress_path() -> Optional[str]:
    """推进心跳文件路径；未配置返回 None。"""
    value = (os.environ.get(ENV_NAME) or "").strip()
    return value or None


def touch_progress(clock: Callable[[], float] = time.time) -> bool:
    """原子刷新推进心跳（.tmp 写入后 os.replace）；未配置或写失败返回 False。

    失败只静默返回：推进判定是运维信号，不能让解题主流程因心跳写失败中断。
    """
    path = progress_path()
    if not path:
        return False
    try:
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as fh:
            fh.write(str(clock()))
        os.replace(temporary, path)
        return True
    except OSError:
        return False
