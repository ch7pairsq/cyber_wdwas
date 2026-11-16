#!/usr/bin/env python3
"""零依赖测试运行器：执行 tests/{unit,integration,chaos}/test_*.py 中的 test_* 函数。

用法：python3 scripts/run_checks.py [suite ...]
也可用 pytest：python3 -m pytest tests -q
"""
import importlib
import pathlib
import sys
import traceback

ROOT = pathlib.Path(__file__).resolve().parents[1]
SUITES = ("unit", "integration", "chaos")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "unit"))


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    suites = tuple(s for s in argv if s in SUITES) or SUITES
    total = 0
    failures = []
    for suite in suites:
        for path in sorted((ROOT / "tests" / suite).glob("test_*.py")):
            mod_name = "tests.{}.{}".format(suite, path.stem)
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
                    print("PASS {}/{}:{}".format(suite, path.stem, name))
                except Exception:
                    failures.append(("{}/{}:{}".format(suite, path.stem, name), traceback.format_exc()))
                    print("FAIL {}/{}:{}".format(suite, path.stem, name))
    print("-" * 60)
    print("total={} passed={} failed={}".format(total, total - len(failures), len(failures)))
    for name, tb in failures:
        print("\n== {} ==\n{}".format(name, tb))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
