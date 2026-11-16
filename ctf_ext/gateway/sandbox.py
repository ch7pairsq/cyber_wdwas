"""进程沙箱：命令白名单 + 超时 + 输出截断（§16、附录 B.3）。"""
from __future__ import annotations

import os
import signal
import subprocess
from typing import Dict, List, Optional, Sequence

from .policy import PolicyEngine


def run_command(argv: Sequence[str], policy: PolicyEngine, timeout: float = 30.0,
                max_output_bytes: int = 200_000, cwd: Optional[str] = None) -> Dict[str, object]:
    """先过策略，再执行；超时杀整个进程组；输出超限截断并标记。"""
    policy.enforce(policy.decide_command(argv))
    proc = subprocess.Popen(
        list(argv), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        cwd=cwd, start_new_session=True,
    )
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        out, _ = proc.communicate()
    raw = out or b""
    truncated = len(raw) > max_output_bytes
    return {
        "status": "timeout" if timed_out else "ok",
        "returncode": proc.returncode,
        "output": raw[:max_output_bytes].decode("utf-8", errors="replace"),
        "truncated": truncated,
        "bytes": len(raw),
    }
