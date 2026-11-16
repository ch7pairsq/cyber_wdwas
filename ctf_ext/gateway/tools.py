"""工具注册表：ToolSpec / ToolResult（附录 B.3）。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Sequence

from .policy import PolicyDenied, PolicyEngine
from .sandbox import run_command


@dataclass
class ToolSpec:
    name: str
    version: str
    description: str
    risk_level: str  # read | compute | network | execute
    timeout_seconds: float = 30.0
    max_output_bytes: int = 200_000
    concurrency_limit: int = 1
    idempotent: bool = True
    evidence_policy: str = "summary"  # raw | summary | hash_only


@dataclass
class ToolResult:
    status: str  # ok | timeout | denied | error
    output: str = ""
    truncated: bool = False
    duration_ms: int = 0
    evidence_id: Optional[str] = None
    error_code: str = ""


class ToolRegistry:
    def __init__(self, policy: PolicyEngine, evidence_store=None):
        self._policy = policy
        self._evidence = evidence_store
        self._specs: Dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._specs[spec.name] = spec

    def execute(self, name: str, argv: Sequence[str], cwd: Optional[str] = None) -> ToolResult:
        spec = self._specs.get(name)
        if not spec:
            return ToolResult(status="denied", error_code="TOOL_UNAVAILABLE")
        try:
            self._policy.decide_sandbox(available=spec.risk_level != "execute")
            started = time.time()
            raw = run_command(argv, self._policy, timeout=spec.timeout_seconds,
                              max_output_bytes=spec.max_output_bytes, cwd=cwd)
            duration = int((time.time() - started) * 1000)
            evidence_id = None
            if self._evidence is not None and raw["output"]:
                data = raw["output"].encode("utf-8")
                if spec.evidence_policy == "hash_only":
                    evidence_id = self._evidence.put_bytes(b"", kind="hash:" + name)
                else:
                    evidence_id = self._evidence.put_bytes(data, kind="tool:" + name)
            return ToolResult(
                status="timeout" if raw["status"] == "timeout" else "ok",
                output=str(raw["output"]), truncated=bool(raw["truncated"]),
                duration_ms=duration, evidence_id=evidence_id,
            )
        except PolicyDenied as exc:
            return ToolResult(status="denied", error_code=exc.reason_code)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(status="error", error_code=type(exc).__name__)
