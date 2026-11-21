"""工具注册表：ToolSpec / ToolResult 与执行护栏（附录 B.3、§15.3、§16.2）。

执行前必须通过：注册检查 → 风险评估 → 工作目录约束 → 并发上限 → 命令白名单。
证据策略：raw/summary 均归档原文；summary 只在活跃上下文截断；hash_only 只登记哈希。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

from .policy import REQUIRE_APPROVAL, Decision, PolicyDenied, PolicyEngine
from .sandbox import run_command
from ..runtime.control import BudgetExceeded, LoopDetected
from ..runtime.progress import touch_progress

@dataclass
class ToolSpec:
    """工具描述：模型可见的 schema + 服务端强制约束（风险/超时/并发/证据策略）。"""
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
    # 固定主命令可防止同名工具被替换为另一个白名单命令。
    command: str = ""
    # 只接受结构化参数，由调用方在进入 shell 前构造固定 argv。
    argv_builder: Optional[Callable[[Dict[str, Any], Optional[Path]], Sequence[str]]] = None
    # 指定某个字符串参数经 stdin 传给固定 argv（如 python_exec 的代码体），
    # 避免代码进 argv 撞引号/长度限制。空串 = 不经 stdin。
    stdin_argument: str = ""
    stdin_max_bytes: int = 0


@dataclass
class ToolResult:
    """工具执行结果：状态、预览与全文、证据 id 与命令归档 id。"""
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
    command_id: Optional[str] = None


class ToolRegistry:
    """结构化工具注册表与执行护栏：模型只给 JSON 参数，不直接提供 shell 命令。"""
    def __init__(self, policy: PolicyEngine, evidence_store=None, work_root: Optional[str] = None,
                 state_store=None, environment: str = "offline-image",
                 target_revision: str = "runtime"):
        """policy 为硬规则引擎；evidence_store/state_store 提供证据与缓存。"""
        self._policy = policy
        self._evidence = evidence_store
        self._work_root = Path(work_root).resolve() if work_root else None
        self._state = state_store
        self._environment = environment
        self._target_revision = target_revision
        self._specs: Dict[str, ToolSpec] = {}
        self._semaphores: Dict[str, threading.BoundedSemaphore] = {}
        self._lock = threading.Lock()

    def register(self, spec: ToolSpec) -> None:
        """注册工具并建并发信号量；同名覆盖（启动期行为，不做热更）。"""
        with self._lock:
            self._specs[spec.name] = spec
            self._semaphores[spec.name] = threading.BoundedSemaphore(max(1, spec.concurrency_limit))

    def specs(self):
        """全部已注册 ToolSpec 列表（运维/审计用）。"""
        return list(self._specs.values())

    def tool_definitions(self):
        """供模型使用的工具定义；不暴露 argv 构造器。"""
        return [{"name": spec.name, "description": spec.description,
                 "input_schema": dict(spec.input_schema)} for spec in self._specs.values()]

    def _check_cwd(self, cwd: Optional[str]):
        """工作目录约束：必须位于 work_root 内，防路径逃逸。"""
        if not cwd or self._work_root is None:
            return
        target = Path(cwd).resolve()
        if self._work_root not in target.parents and target != self._work_root:
            raise PolicyDenied("PATH", "cwd outside work root: {}".format(target))

    def execute_arguments(self, name: str, arguments: Dict[str, Any],
                          cwd: Optional[str] = None, *, control=None) -> ToolResult:
        """把模型给的 JSON 参数转成固定 argv；未知工具/参数一律拒绝。"""
        spec = self._specs.get(name)
        if not spec:
            return ToolResult(status="denied", error_code="TOOL_UNAVAILABLE")
        if not isinstance(arguments, dict) or spec.argv_builder is None:
            return ToolResult(status="denied", error_code="INVALID_ARGUMENTS")
        schema = spec.input_schema or {}
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        if any(name not in arguments for name in required) or any(name not in properties for name in arguments):
            return ToolResult(status="denied", error_code="INVALID_ARGUMENTS")
        # 路径必须以当前题目工作区为基准，而不是全局 work_root：
        # 否则相对路径会落到共享根目录，绝对路径还能跨题读取。
        root = self._work_root
        challenge_id = getattr(control, "challenge_id", None)
        if challenge_id and self._state is not None:
            try:
                root = self._state.workspace(challenge_id)
            except (OSError, ValueError):
                return ToolResult(status="denied", error_code="PATH")
        if root is None and any(key in properties for key in ("path", "file", "dir")):
            # 没有工作区基准时不能安全解析路径，模型调用必须拒绝而不是放行。
            return ToolResult(status="denied", error_code="PATH")
        try:
            argv = [str(item) for item in spec.argv_builder(arguments, root)]
        except (TypeError, ValueError, KeyError):
            return ToolResult(status="denied", error_code="INVALID_ARGUMENTS")
        if not argv or (spec.command and Path(argv[0]).name != spec.command):
            return ToolResult(status="denied", error_code="TOOL_COMMAND_MISMATCH")
        stdin_data = b""
        if spec.stdin_argument:
            value = arguments.get(spec.stdin_argument)
            if not isinstance(value, str) or not value.strip():
                return ToolResult(status="denied", error_code="INVALID_ARGUMENTS")
            if spec.stdin_max_bytes and len(value.encode("utf-8")) > spec.stdin_max_bytes:
                return ToolResult(status="denied", error_code="INVALID_ARGUMENTS")
            stdin_data = value.encode("utf-8")
        return self.execute(name, argv, cwd=cwd, control=control, stdin_data=stdin_data,
                            args_json=json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False))

    def execute(self, name: str, argv: Sequence[str], cwd: Optional[str] = None, *,
                control=None, stdin_data: bytes = b"", args_json: str = "") -> ToolResult:
        """执行护栏链：名称黑名单 → 注册 → 风险 → 工作目录 → 并发 → 预算/缓存 →
        沙箱执行 → 证据归档 → 命令日志。任何一环不过都返回 denied。
        """
        name_decision = self._policy.decide_tool_name(name)
        if name_decision.action != "allow":
            return ToolResult(status="denied", error_code=name_decision.reason_code)
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
            if cwd is None and self._state is not None and control is not None:
                cwd = str(self._state.workspace(control.challenge_id))
            # input_hash 必须覆盖真实参数（如 python_exec 的代码），否则同一工具的
            # 所有调用共享签名与缓存：重复调用被误判为 loop，且新代码会拿到旧缓存。
            input_hash = hashlib.sha256(json.dumps(
                {"argv": list(argv), "cwd": str(cwd or ""), "input": args_json},
                sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            cache_key = None
            if self._state is not None and control is not None:
                cache_key = self._state.cache_key(
                    control.challenge_id, self._target_revision, self._environment,
                    spec.name, spec.version, argv, input_hash)
                try:
                    control.reserve(spec.name, spec.version, argv, input_hash)
                except BudgetExceeded:
                    return ToolResult(status="denied", error_code="BUDGET_EXCEEDED")
                except LoopDetected:
                    return ToolResult(status="denied", error_code="LOOP_DETECTED")
                cached = self._state.cached(cache_key)
                if cached:
                    output = self._state.restore_evidence(cached["evidence_id"]).decode("utf-8", "replace")
                    return ToolResult(status="ok", summary=output[:200], output=output,
                                      evidence_id=cached["evidence_id"], command_id=cached["id"],
                                      bytes_read=len(output.encode("utf-8")))
            raw = run_command(argv, self._policy, timeout=spec.timeout_seconds,
                              max_output_bytes=spec.max_output_bytes, cwd=cwd,
                              input_data=stdin_data)
            duration = int((time.time() - started) * 1000)
            output = str(raw["output"])
            evidence_id = self._store_evidence(spec, output)
            complete = (raw["status"] != "timeout" and raw["returncode"] == 0
                        and not raw["truncated"])
            command_id = self._record_command(spec, argv, cwd, evidence_id, cache_key, complete,
                                             raw["returncode"], control)
            if complete and evidence_id and control is not None:
                control.progress(evidence_id, "tool:" + spec.name)
            status = "timeout" if raw["status"] == "timeout" else ("ok" if complete else "error")
            return ToolResult(
                status=status,
                summary=output[:200], output=output,
                truncated=bool(raw["truncated"]), duration_ms=duration,
                evidence_id=evidence_id, command_id=command_id,
                bytes_read=len(output.encode("utf-8")),
                error_code="" if complete else ("TIMEOUT" if raw["status"] == "timeout" else "COMMAND_FAILED"),
                retryable=bool(raw["status"] == "timeout" and spec.idempotent),
            )
        except PolicyDenied as exc:
            return ToolResult(status="denied", error_code=exc.reason_code)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(status="error", error_code=type(exc).__name__, retryable=spec.idempotent)
        finally:
            sem.release()
            # 工具完成也是真实推进：fork 阻塞/子进程 hang 时不会执行到这里，
            # 推进心跳停更，supervisor 可与模型调用停更一起判定卡死。
            touch_progress()

    def _record_command(self, spec: ToolSpec, argv, cwd, evidence_id, cache_key, complete,
                        returncode, control) -> Optional[str]:
        """把工具执行写入 command_log；FlagGate 只认这张表里的完整成功输出。"""
        if self._state is None or control is None or not evidence_id:
            return None
        import uuid
        command_id = "cmd-" + uuid.uuid4().hex
        with self._state.board.transaction() as db:
            db.execute("INSERT INTO command_log VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (command_id, control.challenge_id, evidence_id, json.dumps(list(argv)),
                        spec.version, self._target_revision, self._environment,
                        returncode, int(bool(complete)), time.time()))
            if complete and cache_key:
                db.execute("INSERT OR REPLACE INTO tool_cache VALUES(?,?,?)",
                           (cache_key, command_id, time.time() + 300))
        return command_id

    def _store_evidence(self, spec: ToolSpec, output: str) -> Optional[str]:
        """按证据策略归档工具输出；归档永不截断（尾部线索可回读/过 Gate）。"""
        if self._evidence is None or not output:
            return None
        data = output.encode("utf-8")
        kind = "tool:" + spec.name
        if spec.evidence_policy == "raw":
            return self._evidence.put_bytes(data, kind=kind)
        if spec.evidence_policy == "hash_only":
            return self._evidence.put_hash(hashlib.sha256(data).hexdigest(), kind="hash:" + spec.name)
        # 预览在 Worker 层生成；归档不能截断，否则尾部线索无法回读或通过 Gate。
        return self._evidence.put_bytes(data, kind="tool:" + spec.name)
