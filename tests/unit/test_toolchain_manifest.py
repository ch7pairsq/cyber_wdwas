"""离线工具链清单：层级、二进制校验和配置白名单一致性（构建期约束）。"""
import copy
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "docker"))

import install_toolchain as toolchain  # noqa: E402


def _manifest():
    return toolchain.load_manifest(ROOT / "docker" / "toolchain.json")


def test_manifest_has_core_and_ctf_tools():
    manifest = _manifest()
    assert manifest["schema_version"] == 1
    assert len(manifest["tools"]) >= 20
    names = {tool["name"] for tool in manifest["tools"]}
    assert {"file", "binutils", "binwalk", "gdb", "qemu-user"} <= names
    assert {"reverse-pwntools", "ropgadget", "checksec", "patchelf", "capstone-tool", "z3"} <= names
    assert "radare2" not in names and "rizin" not in names
    assert all(tool["bins"] for tool in manifest["tools"])


def test_ctf_tier_is_cumulative_and_has_no_duplicate_packages():
    manifest = _manifest()
    core = toolchain.apt_packages(manifest, "core")
    ctf = toolchain.apt_packages(manifest, "ctf")
    assert set(core) <= set(ctf)
    assert len(ctf) == len(set(ctf))
    assert "binutils" in core and "binwalk" in ctf
    assert "upx-ucl" not in ctf
    assert {"python3-pwntools", "python3-ropgadget", "checksec", "patchelf", "capstone-tool", "z3"} <= set(ctf)


def test_missing_binary_is_reported():
    manifest = _manifest()
    original = toolchain.shutil.which
    toolchain.shutil.which = lambda name: None if name == "binwalk" else original(name)
    try:
        missing = toolchain.verify_tools(manifest, "ctf")
    finally:
        toolchain.shutil.which = original
    assert "binwalk:binwalk" in missing


def test_duplicate_tool_name_is_rejected():
    manifest = _manifest()
    broken = copy.deepcopy(manifest)
    broken["tools"].append(copy.deepcopy(broken["tools"][0]))
    try:
        toolchain.validate_manifest(broken)
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate tool name accepted")


def test_config_allowlist_is_covered_by_manifest():
    manifest = _manifest()
    allowed = {
        binary
        for tool in manifest["tools"] if tool.get("allowlist")
        for binary in tool["bins"]
    }
    text = (ROOT / "config" / "base.yaml").read_text(encoding="utf-8")
    block = re.search(r"command_allowlist:\s*\n((?:\s+- .+\n)+)", text)
    assert block, "command_allowlist block not found"
    configured = {
        line.split("- ", 1)[1].strip()
        for line in block.group(1).splitlines() if "- " in line
    }
    assert configured <= allowed, sorted(configured - allowed)
