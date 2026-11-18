"""工具路径隔离：相对路径以当前题工作区为基准，绝对路径不得跨题。"""
import os
import tempfile

from _support import add_challenge, new_board

from ctf_ext.gateway.catalog import default_specs
from ctf_ext.gateway.policy import PolicyConfig, PolicyEngine
from ctf_ext.gateway.tools import ToolRegistry
from ctf_ext.store.evidence import EvidenceStore
from ctf_ext.store.session import StateStore


class _Control:
    challenge_id = "c1"

    def reserve(self, *args, **kwargs):
        return None

    def progress(self, *args, **kwargs):
        return None


def _file_registry(store, evidence):
    policy = PolicyEngine(PolicyConfig(command_allowlist=("file",), sandbox_required=False))
    registry = ToolRegistry(
        policy,
        evidence_store=evidence,
        work_root=str(store.root),
        state_store=store,
        environment="test",
    )
    registry.register(next(spec for spec in default_specs() if spec.name == "file_info"))
    return registry


def test_relative_path_uses_challenge_workspace():
    board, conn = new_board()
    add_challenge(board, "c1")
    evidence = EvidenceStore(os.path.join(tempfile.mkdtemp(prefix="ev-"), "store"), db=conn)
    store = StateStore(board, evidence, os.path.join(tempfile.mkdtemp(prefix="ws-"), "workspaces"))
    workspace = store.workspace("c1")
    with open(os.path.join(workspace, "FLAG"), "w", encoding="utf-8") as handle:
        handle.write("flag{c1}")

    registry = _file_registry(store, evidence)
    result = registry.execute_arguments("file_info", {"path": "FLAG"}, cwd=str(workspace),
                                         control=_Control())
    assert result.status == "ok", (result.status, result.error_code)


def test_absolute_path_cannot_cross_challenge_workspace():
    board, conn = new_board()
    add_challenge(board, "c1")
    add_challenge(board, "c2")
    evidence = EvidenceStore(os.path.join(tempfile.mkdtemp(prefix="ev-"), "store"), db=conn)
    store = StateStore(board, evidence, os.path.join(tempfile.mkdtemp(prefix="ws-"), "workspaces"))
    c1 = store.workspace("c1")
    c2 = store.workspace("c2")
    with open(os.path.join(c1, "FLAG"), "w", encoding="utf-8") as handle:
        handle.write("flag{c1}")
    with open(os.path.join(c2, "FLAG"), "w", encoding="utf-8") as handle:
        handle.write("flag{c2}")

    registry = _file_registry(store, evidence)
    outside = registry.execute_arguments(
        "file_info", {"path": os.path.join(c2, "FLAG")}, cwd=str(c1), control=_Control()
    )
    assert outside.status == "denied"
    assert outside.error_code == "INVALID_ARGUMENTS"


def test_path_tool_without_workspace_fails_closed():
    board, conn = new_board()
    evidence = EvidenceStore(os.path.join(tempfile.mkdtemp(prefix="ev-"), "store"), db=conn)
    policy = PolicyEngine(PolicyConfig(command_allowlist=("file",), sandbox_required=False))
    registry = ToolRegistry(policy, evidence_store=evidence, work_root=None, environment="test")
    registry.register(next(spec for spec in default_specs() if spec.name == "file_info"))

    result = registry.execute_arguments("file_info", {"path": "FLAG"})
    assert result.status == "denied"
    assert result.error_code == "PATH"
