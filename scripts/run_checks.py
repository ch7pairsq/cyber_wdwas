#!/usr/bin/env python3
"""测试运行器：执行 tests/{unit,integration,chaos}。

优先委托 pytest；未安装 pytest 时退回内置的零依赖执行器。
用法：python3 scripts/run_checks.py [suite ...]
也可直接用：python3 -m pytest tests -q
"""
import importlib
import inspect
import pathlib
import shutil
import sys
import tempfile
import traceback

ROOT = pathlib.Path(__file__).resolve().parents[1]
SUITES = ("unit", "integration", "chaos")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "unit"))


def _call_test(fn):
    """调用无 pytest 环境下的测试；只支持最常见的 tmp_path fixture。"""
    params = inspect.signature(fn).parameters
    if not params:
        return fn()
    if set(params) == {"tmp_path"}:
        tmp_path = pathlib.Path(tempfile.mkdtemp(prefix="ctf-check-")).resolve()
        try:
            return fn(tmp_path=tmp_path)
        finally:
            shutil.rmtree(tmp_path, ignore_errors=True)
    raise TypeError("unsupported pytest fixture(s): {}".format(", ".join(sorted(params))))


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    suites = tuple(s for s in argv if s in SUITES) or SUITES
    try:
        import pytest  # noqa: F401
    except ModuleNotFoundError:
        pytest = None
    if pytest is not None:
        targets = [str(ROOT / "tests" / suite) for suite in suites]
        return int(pytest.main(["-q", "--rootdir", str(ROOT)] + targets))
    total = 0
    failures = []
    for suite in suites:
        for path in sorted((ROOT / "tests" / suite).glob("test_*.py")):
            mod_name = "tests.{}.{}".format(suite, path.stem)
            try:
                mod = importlib.import_module(mod_name)
            except ModuleNotFoundError as exc:
                if exc.name == "pytest":
                    print("SKIP {} (pytest not installed; use .venv/bin/python -m pytest)".format(mod_name))
                    continue
                raise
            for name in sorted(dir(mod)):
                if not name.startswith("test_"):
                    continue
                fn = getattr(mod, name)
                if not callable(fn):
                    continue
                total += 1
                try:
                    _call_test(fn)
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
