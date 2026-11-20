"""进程沙箱：命令白名单 + 超时 + 输出截断（§16、附录 B.3）。"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from .policy import PolicyEngine


def run_command(argv: Sequence[str], policy: PolicyEngine, timeout: float = 30.0,
                max_output_bytes: int = 200_000, cwd: Optional[str] = None,
                input_data: bytes = b"") -> Dict[str, object]:
    """受控命令执行入口：策略白名单 → 本地有界执行（非安全沙箱）；见 SandboxSpec。"""
    from .execution import capture
    policy.enforce(policy.decide_command(argv))
    # 本地执行器不是安全沙箱，不能把 cwd/进程组当作容器隔离。
    policy.enforce(policy.decide_sandbox(available=False))
    result = capture(argv, cwd=cwd, timeout=timeout, limit=max_output_bytes,
                     input_data=input_data)
    return {
        "status": "timeout" if result["timed_out"] else "ok",
        "returncode": result["returncode"],
        "output": result["output"].decode("utf-8", errors="replace"),
        "truncated": result["truncated"],
        "bytes": result["bytes"],
    }
