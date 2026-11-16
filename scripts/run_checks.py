#!/usr/bin/env python3
"""零依赖测试运行器：发现 tests/unit/test_*.py 并执行 test_* 函数。

用法：python3 scripts/run_checks.py
也可用 pytest：python3 -m pytest tests -q
"""
import importlib
import pathlib
import sys
import traceback

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "unit"))

def main() -> int:
    total = 0
    failures = []
    for path in sorted((ROOT / "tests" / "unit").glob("test_*.py")):
        mod_name = "tests.unit." + path.stem
        mod = importlib.import_module(mod_name)
        for name in sorted(dir(mod)):
            if not name.startswith("test_"):
                continue
            fn = getattr(mod, name)
            if not callable(fn):
                continue
            total += 1
            try:
                fn()
                print("PASS {}:{}".format(path.stem, name))
            except Exception:
                failures.append((path.stem + ":" + name, traceback.format_exc()))
                print("FAIL {}:{}".format(path.stem, name))
    print("-" * 60)
    print("total={} passed={} failed={}".format(total, total - len(failures), len(failures)))
    for name, tb in failures:
        print("\n== {} ==\n{}".format(name, tb))
    return 1 if failures else 0

if __name__ == "__main__":
    raise SystemExit(main())
