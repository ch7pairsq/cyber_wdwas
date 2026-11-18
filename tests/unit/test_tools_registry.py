"""工具护栏：风险评估、工作目录约束、并发上限、证据策略（附录 B.3、§15.3）。"""
import os
import tempfile

from _support import new_board

from ctf_ext.gateway.policy import PolicyConfig, PolicyEngine
from ctf_ext.gateway.tools import ToolRegistry, ToolSpec
from ctf_ext.store.evidence import EvidenceStore


def _registry(evidence=None, work_root=None, **policy_kwargs):
    # 本组只测试本地 echo 的注册/证据；真实隔离要求由框架安全回归单独覆盖。
    kwargs = dict(command_allowlist=("echo",), sandbox_required=False)
    kwargs.update(policy_kwargs)
    policy = PolicyEngine(PolicyConfig(**kwargs))
    return ToolRegistry(policy, evidence_store=evidence, work_root=work_root)


def _spec(**overrides):
    base = dict(name="echo", version="1.0.0", description="echo", risk_level="read",
                timeout_seconds=10, max_output_bytes=1000, evidence_policy="summary")
    base.update(overrides)
    return ToolSpec(**base)


def test_unknown_tool_denied():
    reg = _registry()
    out = reg.execute("no-such-tool", ["echo", "x"])
    assert out.status == "denied" and out.error_code == "TOOL_UNAVAILABLE"


def test_command_denied_by_allowlist():
    reg = _registry()
    reg.register(_spec())
    out = reg.execute("echo", ["ls"])
    assert out.status == "denied" and out.error_code == "COMMAND_NOT_ALLOWED"


def test_execute_requires_approval_when_configured():
    reg = _registry(require_approval_for_execute=True)
    reg.register(_spec(risk_level="execute"))
    out = reg.execute("echo", ["echo", "hi"])
    assert out.status == "denied" and out.error_code == "TOOL_APPROVAL"


def test_cwd_must_stay_inside_work_root():
    root = tempfile.mkdtemp(prefix="workroot-")
    outside = tempfile.mkdtemp(prefix="outside-")
    reg = _registry(work_root=root)
    reg.register(_spec())
    assert reg.execute("echo", ["echo", "hi"], cwd=root).status == "ok"
    blocked = reg.execute("echo", ["echo", "hi"], cwd=outside)
    assert blocked.status == "denied" and blocked.error_code == "PATH"


def test_evidence_policies_do_not_collide():
    board, conn = new_board()
    ev = EvidenceStore(tempfile.mkdtemp(), db=conn)
    reg = _registry(evidence=ev)
    reg.register(_spec(name="echo", evidence_policy="hash_only"))
    first = reg.execute("echo", ["echo", "alpha"])
    second = reg.execute("echo", ["echo", "beta"])
    assert first.evidence_id != second.evidence_id  # 不同输出必须不同哈希
    assert ev.verify(first.evidence_id) is True

    reg.register(_spec(name="echo", evidence_policy="raw"))
    raw = reg.execute("echo", ["echo", "gamma"])
    assert raw.status == "ok" and raw.evidence_id
    row = conn.execute("SELECT bytes FROM evidence WHERE sha256=?", (raw.evidence_id,)).fetchone()
    assert row["bytes"] > 0
