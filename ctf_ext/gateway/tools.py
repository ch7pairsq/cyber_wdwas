"""工具注册表：ToolSpec / ToolResult 与执行护栏（附录 B.3、§15.3、§16.2）。

执行前必须通过：注册检查 → 风险评估 → 工作目录约束 → 并发上限 → 命令白名单。
证据策略：raw 存原文；summary 存截断预览；hash_only 只登记哈希。
"""
from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Sequence

from .policy import REQUIRE_APPROVAL, Decision, PolicyDenied, PolicyEngine
from .sandbox import run_command

SUMMARY_PREVIEW_BYTES = 12000


@dataclass
class ToolSpec:
    name: str
    version: str = "0.1.0"
    description: str = ""
    risk_level: str = "read"  # read | compute | network | execute
    input_schema: Dict = field(default_factory=dict)
    output_schema: Dict = field(default_factory=dict)
    allowed_scope: str = "challenge"  # challenge | target_allowlist | none
    timeout_seconds: float = 30.0
    max_output_bytes: int = 200_000
    concurrency_limit: int = 1
    idempotent: bool = True
    evidence_policy: str = "summary"  # raw | summary | hash_only


@dataclass
class ToolResult:
    status: str  # ok | partial | timeout | denied | error
    summary: str = ""
    output: str = ""
    truncated: bool = False
    duration_ms: int = 0
    evidence_id: Optional[str] = None
    bytes_read: int = 0
    bytes_written: int = 0
    retryable: bool = False
    error_code: str = ""


class ToolRegistry:
    def __init__(self, policy: PolicyEngine, evidence_store=None, work_root: Optional[str] = None):
        self._policy = policy
        self._evidence = evidence_store
        self._work_root = Path(work_root).resolve() if work_root else None
        self._specs: Dict[str, ToolSpec] = {}
        self._semaphores: Dict[str, threading.BoundedSemaphore] = {}
        self._lock = threading.Lock()

    def register(self, spec: ToolSpec) -> None:
        with self._lock:
            self._specs[spec.name] = spec
            self._semaphores[spec.name] = threading.BoundedSemaphore(max(1, spec.concurrency_limit))

    def specs(self):
        return list(self._specs.values())

    def _check_cwd(self, cwd: Optional[str]):
        if not cwd or self._work_root is None:
            return
        target = Path(cwd).resolve()
        if self._work_root not in target.parents and target != self._work_root:
            raise PolicyDenied("PATH", "cwd outside work root: {}".format(target))

    def execute(self, name: str, argv: Sequence[str], cwd: Optional[str] = None) -> ToolResult:
        spec = self._specs.get(name)
        if not spec:
            return ToolResult(status="denied", error_code="TOOL_UNAVAILABLE")
        decision = self._policy.decide_tool_risk(spec.risk_level)
        if decision.action == REQUIRE_APPROVAL:
            return ToolResult(status="denied", error_code=decision.reason_code,
                              summary="tool {} requires approval".format(name))
        try:
            PolicyEngine.enforce(decision)
            self._check_cwd(cwd)
        except PolicyDenied as exc:
            return ToolResult(status="denied", error_code=exc.reason_code)
        sem = self._semaphores.get(name) or threading.BoundedSemaphore(1)
        if not sem.acquire(blocking=False):
            return ToolResult(status="denied", error_code="TOOL_BUSY", retryable=True)
        started = time.time()
        try:
            raw = run_command(argv, self._policy, timeout=spec.timeout_seconds,
                              max_output_bytes=spec.max_output_bytes, cwd=cwd)
            duration = int((time.time() - started) * 1000)
            output = str(raw["output"])
            evidence_id = self._store_evidence(spec, output)
            return ToolResult(
                status="timeout" if raw["status"] == "timeout" else "ok",
                summary=output[:200], output=output,
                truncated=bool(raw["truncated"]), duration_ms=duration,
                evidence_id=evidence_id, bytes_read=len(output.encode("utf-8")),
                retryable=bool(raw["status"] == "timeout" and spec.idempotent),
            )
        except PolicyDenied as exc:
            return ToolResult(status="denied", error_code=exc.reason_code)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(status="error", error_code=type(exc).__name__, retryable=spec.idempotent)
        finally:
            sem.release()

    def _store_evidence(self, spec: ToolSpec, output: str) -> Optional[str]:
        if self._evidence is None or not output:
            return None
        data = output.encode("utf-8")
        kind = "tool:" + spec.name
        if spec.evidence_policy == "raw":
            return self._evidence.put_bytes(data, kind=kind)
        if spec.evidence_policy == "hash_only":
            return self._evidence.put_hash(hashlib.sha256(data).hexdigest(), kind="hash:" + spec.name)
        return self._evidence.put_bytes(data[:SUMMARY_PREVIEW_BYTES], kind="summary:" + spec.name)
