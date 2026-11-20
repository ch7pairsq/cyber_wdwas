#!/usr/bin/env python3
"""Day-0 赛前自检（附录 F-10）：环境、配置、策略、存储、工具、密钥。

只读检查 + 临时目录验证，不产生付费调用、不联网；退出码 0 = 可启动。
"""
import os
import pathlib
import re
import shutil
import sqlite3
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "docker"))

STRICT = "--strict" in sys.argv
results = []


def check(name, ok, detail="", warn=False):
    tag = "PASS" if ok else ("WARN" if warn else "FAIL")
    results.append((tag, name, detail))
    print("[{}] {} {}".format(tag, name, detail))
    return ok or warn


def main() -> int:
    v = sys.version_info
    check("python>=3.12", v >= (3, 12), "{}.{}.{}".format(v.major, v.minor, v.micro))
    try:
        import yaml  # noqa: F401
        check("pyyaml", True)
        yaml_ok = True
    except ImportError:
        check("pyyaml", False, "pip install pyyaml", warn=True)
        yaml_ok = False

    du = shutil.disk_usage(ROOT)
    check("disk>1GB", du.free > 1 << 30, "free={}MB".format(du.free // (1 << 20)))

    cfg = {}
    if yaml_ok:
        from ctf_ext.config import deep_merge, load_yaml, validate_config
        base = load_yaml(ROOT / "config" / "base.yaml")
        overlay = load_yaml(ROOT / "config" / "practice.yaml")
        cfg = deep_merge(base, overlay)
        errors = validate_config(cfg)
        check("config valid", not errors, "; ".join(errors))
        dl = cfg["download"]
        check("download fail-closed", dl["max_single_bytes"] == 0 and dl["max_total_bytes"] == 0,
              "X 未配置 → 禁止下载")
        check("competition lock explicit", "competition_locked" in cfg["harness"],
              "locked={}".format(cfg["harness"].get("competition_locked")))
        allowlist = (cfg.get("policy") or {}).get("command_allowlist") or []
        missing_allow = [b for b in allowlist if shutil.which(b) is None]
        try:
            from install_toolchain import load_manifest, verify_tools
            manifest = load_manifest(ROOT / "docker" / "toolchain.json")
            missing_toolchain = verify_tools(manifest, "ctf")
            check("toolchain manifest", True, "{} tools; build-time install only".format(
                len(manifest["tools"])))
        except Exception as exc:  # noqa: BLE001
            missing_toolchain = ["manifest:" + type(exc).__name__]
            check("toolchain manifest", False, str(exc), warn=not STRICT)
        missing = sorted(set(missing_allow + missing_toolchain))
        check("allowlisted tools installed", not missing,
              "missing={} (工具随镜像构建，不临时下载；--strict 时缺失即失败)".format(missing),
              warn=not STRICT)

    # migration + 证据库可写
    tmp = tempfile.mkdtemp(prefix="preflight-")
    conn = sqlite3.connect(os.path.join(tmp, "p.db"), isolation_level=None)
    conn.row_factory = sqlite3.Row
    from ctf_ext.domain.migrate import migrate
    migrate(conn)
    check("migrate ok", True, "tables={}".format(
        len(conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())))

    from ctf_ext.store.evidence import EvidenceStore
    ev = EvidenceStore(os.path.join(tmp, "evidence"), db=conn)
    sha = ev.put_bytes(b"preflight")
    check("evidence store writable", ev.verify(sha))

    # §15.1 解压炸弹防护：路径穿越与压缩比必须在写出前拦截
    import io
    import zipfile
    from ctf_ext.gateway.archive import ArchiveRejected, extract_zip
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../escape.txt", b"x")
    try:
        extract_zip(buf.getvalue(), os.path.join(tmp, "unz"))
        check("archive guard", False, "path traversal accepted!")
    except ArchiveRejected as exc:
        check("archive guard", exc.reason_code == "ZIP_TRAVERSAL", exc.reason_code)

    # §12.5 官方协议未就绪时必须 fail closed，不能静默降级
    from ctf_ext.harness.official import OfficialHarnessAdapter
    report = OfficialHarnessAdapter().capability_report()
    check("official harness fail-closed", report["ready"] is False,
          "missing methods={}".format(len(report["missing"])))

    # §13 示例键名 → 规范键
    from ctf_ext.config import normalize_config
    mapped = normalize_config({"downloads": {"max_single_download_bytes": 123}})
    check("config aliases", mapped["download"]["max_single_bytes"] == 123, "downloads.* → download.*")

    # F-1：上游 Cairn 已 vendored，运行同样要求 >=3.12
    cairn_pyproject = ROOT / "cairn" / "cairn" / "pyproject.toml"
    check("cairn vendored", cairn_pyproject.exists(), "commit=8e7e0ea")
    if cairn_pyproject.exists():
        match = re.search(r'requires-python\s*=\s*"([^"]+)"',
                          cairn_pyproject.read_text(encoding="utf-8"))
        check("cairn requires-python", bool(match), match.group(1) if match else "unknown")
        check("cairn runnable here", sys.version_info >= (3, 12),
              "serve/dispatch needs >=3.12; local {}".format(
                  "{}.{}".format(v.major, v.minor)), warn=True)

    from ctf_ext.gateway.policy import PolicyConfig, PolicyDenied, PolicyEngine
    eng = PolicyEngine(PolicyConfig(authorized_scope=("example.com",)),
                       resolver=lambda host: ["93.184.216.34"])
    try:
        eng.enforce(eng.decide_download("https://example.com/x"))
        check("policy fail-closed", False, "download allowed with unset limits!")
    except PolicyDenied as exc:
        check("policy fail-closed", exc.reason_code == "QUOTA_UNSET", exc.reason_code)

    private = PolicyEngine(PolicyConfig(authorized_scope=("internal.example",)),
                           resolver=lambda host: ["10.0.0.5"])
    try:
        private.enforce(private.decide_download("https://internal.example/x"))
        check("ssrf private-network blocked", False, "private address allowed!")
    except PolicyDenied as exc:
        check("ssrf private-network blocked", exc.reason_code == "PRIVATE_NETWORK", exc.reason_code)

    key = os.environ.get("BAIZHI_API_KEY", "")
    check("BAIZHI_API_KEY", bool(key), "运行时注入 DeepSeek 官方 API key", warn=True)

    failed = [r for r in results if r[0] == "FAIL"]
    print("-" * 50)
    print("pass={} warn={} fail={}".format(
        sum(1 for r in results if r[0] == "PASS"),
        sum(1 for r in results if r[0] == "WARN"),
        len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
