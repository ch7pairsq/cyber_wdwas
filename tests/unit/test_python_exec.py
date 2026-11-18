"""python_exec 工具：注册、stdin 管道、脚本资源限额与错误回传。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.framework_support import fixture
from ctf_ext.gateway.catalog import _PYTHON_EXEC_SCHEMA, _python_exec_argv, default_specs
from ctf_ext.gateway.tools import ToolRegistry


def _registry():
    from ctf_ext.gateway.policy import PolicyConfig, PolicyEngine
    policy = PolicyEngine(PolicyConfig(command_allowlist=("python3",),
                                      sandbox_required=False))
    registry = ToolRegistry(policy)
    for spec in default_specs():
        registry.register(spec)
    return registry


def test_python_exec_registered_with_stdin_pipe():
    registry = _registry()
    spec = registry._specs.get("python_exec")
    assert spec is not None
    assert spec.risk_level == "execute" and spec.command == "python3"
    assert spec.stdin_argument == "code" and spec.stdin_max_bytes == 20000
    argv = _python_exec_argv({"code": "print(1)"}, None)
    assert argv[0].endswith("python3") and argv[1].endswith("python_exec.py")
    assert _PYTHON_EXEC_SCHEMA["required"] == ["code"]


def test_python_exec_stdin_guard(tmp_path):
    registry = _registry()
    # 缺 code / 空 code / 超长 code 都拒绝
    for arguments in ({}, {"code": "   "}, {"code": "x" * 20001}):
        result = registry.execute_arguments("python_exec", arguments, cwd=str(tmp_path))
        assert result.status == "denied" and result.error_code == "INVALID_ARGUMENTS"
    # argv 不携带代码体（走 stdin）
    argv = _python_exec_argv({"code": "print('hi')"}, tmp_path)
    assert "print" not in " ".join(argv)


def test_python_exec_script_runs_and_reports_errors(tmp_path):
    script = Path(__file__).resolve().parents[2] / "ctf_ext" / "gateway" / "python_exec.py"
    py = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
    # 正常执行：print 回传
    out = subprocess.run([py, str(script)], input=b"print(6*7)",
                         capture_output=True, timeout=30)
    assert b"42" in out.stdout
    # 代码报错：traceback 回传 stdout（capture 合并 stderr），退出码非 0
    out2 = subprocess.run([py, str(script)], input=b"1/0",
                          capture_output=True, timeout=30)
    assert b"ZeroDivisionError" in out2.stdout + out2.stderr and out2.returncode != 0
    # 空代码 / 超长代码拒绝
    out3 = subprocess.run([py, str(script)], input=b"   ", capture_output=True, timeout=30)
    assert out3.returncode == 2
    out4 = subprocess.run([py, str(script)], input=b"x" * 20001,
                          capture_output=True, timeout=30)
    assert out4.returncode == 2


def test_python_exec_end_to_end_via_registry(tmp_path):
    """registry 全链路：stdin 管道把 code 送到脚本，输出含执行结果。"""
    (tmp_path / "data.txt").write_bytes(b"secret=1337")
    registry = _registry()
    result = registry.execute_arguments(
        "python_exec",
        {"code": "print(open('data.txt').read().strip()[7:])"},
        cwd=str(tmp_path))
    assert result.status == "ok"
    assert "1337" in result.output
    # 未挂 evidence_store 时 evidence_id 按设计为 None（_store_evidence 返回 None）
