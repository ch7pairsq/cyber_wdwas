#!/usr/bin/env python3
"""构建期安装并校验离线 CTF 工具链。

工具是镜像内容，不是运行期可下载的插件：
- 只在 docker build 阶段调用 apt-get；
- 运行期不提供 pip/apt/uv/npm 入口；
- 安装完成后写入 /opt/toolchain-installed.json 与许可文件副本；
- 校验失败返回非零，避免带着缺失工具进入比赛；
- 二进制用 PATH 探测，Python 库用 /usr/bin/python3 做 import 探测。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

DEFAULT_MANIFEST = Path(__file__).with_name("toolchain.json")
DEFAULT_METADATA = Path("/opt/toolchain-installed.json")
DEFAULT_LICENSES = Path("/opt/toolchain-licenses")
TIER_ORDER = ("core", "ctf", "optional")


def load_manifest(path: Path = DEFAULT_MANIFEST) -> Dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_manifest(data)
    return data


def validate_manifest(data: Dict) -> None:
    if data.get("schema_version") != 1:
        raise ValueError("toolchain manifest schema_version must be 1")
    if tuple(data.get("tiers") or ()) != TIER_ORDER:
        raise ValueError("toolchain tiers must be core, ctf, optional")
    tools = data.get("tools")
    if not isinstance(tools, list) or not tools:
        raise ValueError("toolchain tools must be a non-empty list")
    names = set()
    for tool in tools:
        if not isinstance(tool, dict):
            raise ValueError("tool entry must be an object")
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("tool name is required")
        if name in names:
            raise ValueError("duplicate tool name: " + name)
        names.add(name)
        if tool.get("tier") not in TIER_ORDER:
            raise ValueError("invalid tier for " + name)
        if tool.get("source") not in ("apt", "base", "manual"):
            raise ValueError("invalid source for " + name)
        bins = tool.get("bins")
        if not isinstance(bins, list) or any(not isinstance(x, str) or not x for x in bins):
            raise ValueError("tool bins must be a string list: " + name)
        if not isinstance(tool.get("installed_by_default", True), bool):
            raise ValueError("installed_by_default must be bool: " + name)
        if not isinstance(tool.get("allowlist", False), bool):
            raise ValueError("allowlist must be bool: " + name)
        # 纯 Python 库（python3-*）没有独立二进制：bins 为空时必须提供 modules 做 import 探测。
        modules = tool.get("modules", [])
        if not isinstance(modules, list) or any(not isinstance(x, str) or not x for x in modules):
            raise ValueError("tool modules must be a string list: " + name)
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", m) for m in modules):
            raise ValueError("invalid python module name: " + name)
        if not bins and not modules:
            raise ValueError("tool needs non-empty bins or modules: " + name)


def _tier_index(tier: str) -> int:
    if tier == "full":
        return TIER_ORDER.index("optional")
    if tier not in TIER_ORDER:
        raise ValueError("unknown tier: " + tier)
    return TIER_ORDER.index(tier)


def tools_for_tier(data: Dict, tier: str, *, include_optional: bool = False) -> List[Dict]:
    """返回该层应安装/应校验的工具；默认不要求 optional。"""
    limit = _tier_index(tier)
    result = []
    for tool in data["tools"]:
        if not tool.get("installed_by_default", True) and not include_optional:
            continue
        if tool["tier"] == "optional" and not include_optional and tier != "full":
            continue
        if TIER_ORDER.index(tool["tier"]) <= limit:
            result.append(tool)
    return result


def apt_packages(data: Dict, tier: str) -> List[str]:
    packages = []
    seen = set()
    for tool in tools_for_tier(data, tier, include_optional=(tier == "full")):
        if tool["source"] != "apt":
            continue
        package = tool.get("package")
        if package and package not in seen:
            seen.add(package)
            packages.append(package)
    return packages


def verify_tools(data: Dict, tier: str = "ctf") -> List[str]:
    """检查该层所有默认工具的二进制与 Python 模块；返回 name:binary 缺失项。"""
    missing = []
    for tool in tools_for_tier(data, tier, include_optional=(tier == "full")):
        for binary in tool["bins"]:
            if shutil.which(binary) is None:
                missing.append("{}:{}".format(tool["name"], binary))
        for module in tool.get("modules", []):
            if not _module_importable(module):
                missing.append("{}:python3-import:{}".format(tool["name"], module))
    return missing


def _module_importable(module: str) -> bool:
    """用 /usr/bin/python3 探测 import（apt python3-* 的目标解释器，与 python_exec 同环境）。"""
    try:
        probe = subprocess.run(
            ["/usr/bin/python3", "-c", "import " + module],
            capture_output=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def _apt_env() -> Dict[str, str]:
    env = dict(os.environ)
    env["DEBIAN_FRONTEND"] = "noninteractive"
    return env


def install_apt(packages: Sequence[str]) -> None:
    if not packages:
        return
    subprocess.check_call(["apt-get", "update"], env=_apt_env())
    subprocess.check_call(
        ["apt-get", "install", "-y", "--no-install-recommends", *packages],
        env=_apt_env(),
    )


def _dpkg_version(package: str) -> str:
    try:
        return subprocess.check_output(
            ["dpkg-query", "-W", "-f=${Version}", package],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def write_metadata(data: Dict, tier: str, metadata_path: Path) -> None:
    installed = tools_for_tier(data, tier, include_optional=(tier == "full"))
    payload = {
        "tier": tier,
        "install_phase": "docker-build",
        "runtime_install": False,
        "packages": {
            tool["package"]: _dpkg_version(tool["package"])
            for tool in installed if tool.get("package")
        },
        "tools": {
            tool["name"]: list(tool["bins"]) for tool in installed
        },
        "modules": {
            tool["name"]: list(tool["modules"])
            for tool in installed if tool.get("modules")
        },
        "missing": verify_tools(data, tier),
    }
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def copy_licenses(data: Dict, tier: str, license_dir: Path = DEFAULT_LICENSES) -> None:
    """把 Debian 包内 copyright 复制进镜像，保留 GPL/BSD 等分发所需说明。"""
    license_dir.mkdir(parents=True, exist_ok=True)
    for tool in tools_for_tier(data, tier, include_optional=(tier == "full")):
        package = tool.get("package")
        if not package:
            continue
        source = Path("/usr/share/doc") / package / "copyright"
        if source.is_file():
            shutil.copy2(source, license_dir / (package + ".copyright"))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="构建期安装离线 CTF 工具链")
    parser.add_argument("--tier", choices=("core", "ctf", "full"), default="ctf")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--check-only", action="store_true", help="只校验，不安装")
    args = parser.parse_args(argv)

    manifest = load_manifest(args.manifest)
    if not args.check_only:
        packages = apt_packages(manifest, args.tier)
        print("[toolchain] installing {} apt packages for tier {}".format(len(packages), args.tier))
        install_apt(packages)
        copy_licenses(manifest, args.tier)
        write_metadata(manifest, args.tier, args.metadata)
    missing = verify_tools(manifest, args.tier)
    if missing:
        print("[toolchain] missing: " + ", ".join(missing), file=sys.stderr)
        return 1
    print("[toolchain] ok tier={} tools={}".format(args.tier, len(tools_for_tier(manifest, args.tier))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
