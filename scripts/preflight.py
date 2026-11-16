#!/usr/bin/env python3
"""Day-0 赛前自检（附录 F-10）：环境、配置、Fail-Closed、存储、Key。

只读检查 + 临时目录验证，不产生付费调用；退出码 0 = 可启动。
"""
import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

results = []


def check(name, ok, detail="", warn=False):
    tag = "PASS" if ok else ("WARN" if warn else "FAIL")
    results.append((tag, name, detail))
    print("[{}] {} {}".format(tag, name, detail))
    return ok or warn


def main() -> int:
    v = sys.version_info
    check("python>=3.9", v >= (3, 9), "{}.{}.{}".format(v.major, v.minor, v.micro))
    if v < (3, 12):
        check("python>=3.12 (production target)", False,
              "local {} for skeleton only; production image must use 3.12".format(
                  "{}.{}".format(v.major, v.minor)), warn=True)
    try:
        import yaml  # noqa: F401
        check("pyyaml", True)
        yaml_ok = True
    except ImportError:
        check("pyyaml", False, "pip install pyyaml", warn=True)
        yaml_ok = False

    du = shutil.disk_usage(ROOT)
    check("disk>1GB", du.free > 1 << 30, "free={}MB".format(du.free // (1 << 20)))

    if yaml_ok:
        from ctf_ext.config import load_yaml, validate_config, deep_merge
        base = load_yaml(ROOT / "config" / "base.yaml")
        overlay = load_yaml(ROOT / "config" / "practice.yaml")
        cfg = deep_merge(base, overlay)
        errors = validate_config(cfg)
        check("config valid", not errors, "; ".join(errors))
        dl = cfg["download"]
        check("download fail-closed", dl["max_single_bytes"] == 0 and dl["max_total_bytes"] == 0,
              "X 未配置 → 禁止下载")
        check("competition lock explicit", "competition_locked" in cfg["harness"])

    # migration + 证据库可写
    tmp = tempfile.mkdtemp(prefix="preflight-")
    conn = sqlite3.connect(os.path.join(tmp, "p.db"), isolation_level=None)
    conn.row_factory = sqlite3.Row
    from ctf_ext.domain.migrate import migrate
    migrate(conn)
    check("migrate ok", True, "tables={}".format(
        len(conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())))

    from ctf_ext.gateway.policy import PolicyConfig, PolicyDenied, PolicyEngine
    eng = PolicyEngine(PolicyConfig(authorized_scope=("example.com",)))
    try:
        eng.enforce(eng.decide_download("https://example.com/x"))
        check("policy fail-closed", False, "download allowed with unset limits!")
    except PolicyDenied as exc:
        check("policy fail-closed", exc.reason_code == "QUOTA_UNSET", exc.reason_code)

    key = os.environ.get("DEEPSEEK_API_KEY", "")
    check("DEEPSEEK_API_KEY", bool(key), "set before running real model calls", warn=True)

    failed = [r for r in results if r[0] == "FAIL"]
    print("-" * 50)
    print("pass={} warn={} fail={}".format(
        sum(1 for r in results if r[0] == "PASS"),
        sum(1 for r in results if r[0] == "WARN"),
        len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
