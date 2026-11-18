"""进程存活心跳文件，供外部 supervisor 发现卡死并重启。

心跳只用于运维判定，不参与解题状态；DB/证据仍是唯一事实来源。
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional


class HeartbeatFile:
    def __init__(self, path: Optional[str], clock=time.time):
        self.path = Path(path).resolve() if path else None
        self.clock = clock
        self.last_error = ""

    def touch(self) -> bool:
        if self.path is None:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(str(self.clock()), encoding="utf-8")
            os.replace(str(temporary), str(self.path))
            return True
        except OSError as exc:
            self.last_error = type(exc).__name__
            return False

    @staticmethod
    def age(path: Optional[str], now: Optional[float] = None) -> Optional[float]:
        if not path:
            return None
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            return None
        return max(0.0, (now if now is not None else time.time()) - mtime)

    @classmethod
    def is_stale(cls, path: Optional[str], max_age: float,
                 now: Optional[float] = None, running: bool = True) -> bool:
        """心跳文件缺失或超过 max_age 即视为卡死；进程已退出时另由 supervisor 处理。"""
        if not running or not path or max_age <= 0:
            return False
        age = cls.age(path, now=now)
        return age is None or age > max_age
