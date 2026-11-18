"""工具目录：固定 argv、路径约束与 schema 拒绝；不执行真实命令。"""
import tempfile
from pathlib import Path

from ctf_ext.gateway.catalog import default_specs, register_default_tools
from ctf_ext.gateway.policy import PolicyConfig, PolicyEngine
from ctf_ext.gateway.tools import ToolRegistry

EXECUTE_TOOLS = {"archive_extract", "binwalk_extract", "steghide_extract"}


def _registry():
    policy = PolicyEngine(PolicyConfig(command_allowlist=("7z", "binwalk", "steghide", "cstool"),
                                       sandbox_required=False))
    registry = ToolRegistry(policy, work_root=tempfile.mkdtemp())
    register_default_tools(registry)
    return registry


def test_catalog_covers_ctf_recon_tools_and_hides_builders():
    registry = _registry()
    names = {spec.name for spec in registry.specs()}
    assert {"rop_gadgets", "archive_list", "archive_extract", "binwalk_scan", "binwalk_extract",
            "metadata", "png_check", "pdf_text", "qr_decode", "steghide_extract",
            "disasm_bytes"} <= names
    for spec in registry.specs():
        if spec.name in EXECUTE_TOOLS:
            assert spec.risk_level == "execute" and not spec.idempotent
        elif spec.name == "arena_http":
            assert spec.risk_level == "arena_target"
        else:
            assert spec.risk_level == "read"
    assert all("argv_builder" not in definition for definition in registry.tool_definitions())


def test_catalog_builders_produce_fixed_argv_and_reject_escapes():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        specs = {spec.name: spec for spec in default_specs()}
        assert specs["archive_list"].argv_builder({"path": "a.7z"}, root) == \
            ["7z", "l", "-slt", str((root / "a.7z").resolve())]
        assert specs["pdf_text"].argv_builder({"path": "a.pdf"}, root) == \
            ["pdftotext", str((root / "a.pdf").resolve()), "-"]
        assert specs["steghide_extract"].argv_builder({"path": "a.jpg", "passphrase": "hint"}, root)[-4:] == \
            ["-p", "hint", "-xf", "stego.out"]
        assert specs["disasm_bytes"].argv_builder({"arch": "x86", "hex": "90c3"}, root) == \
            ["cstool", "x86", "90c3"]

        registry = _registry()
        outside = registry.execute_arguments("strings", {"path": "../../etc/passwd"}, cwd=str(root))
        assert outside.status == "denied" and outside.error_code == "INVALID_ARGUMENTS"
        bad_dir = registry.execute_arguments("archive_extract", {"path": "a.7z", "dir": "../evil"},
                                             cwd=str(root))
        assert bad_dir.status == "denied" and bad_dir.error_code == "INVALID_ARGUMENTS"
        bad_hex = registry.execute_arguments("disasm_bytes", {"arch": "x86", "hex": "90zz"},
                                             cwd=str(root))
        assert bad_hex.status == "denied" and bad_hex.error_code == "INVALID_ARGUMENTS"
        extra = registry.execute_arguments("archive_extract", {"path": "a.7z", "shell": "sh"},
                                           cwd=str(root))
        assert extra.status == "denied" and extra.error_code == "INVALID_ARGUMENTS"
