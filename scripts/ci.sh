#!/usr/bin/env bash
# 本地 CI 聚合：语法 → 依赖锁 → 测试 → 小规模压测 → 预检
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"

"$PYTHON" - <<'PY'
import pathlib, sys
bad = []
for path in pathlib.Path('.').rglob('*.py'):
    try:
        compile(path.read_text(encoding='utf-8'), str(path), 'exec')
    except SyntaxError as exc:
        bad.append("{}:{} {}".format(path, exc.lineno, exc.msg))
if bad:
    print("\n".join(bad)); sys.exit(1)
print("syntax ok")
PY

"$PYTHON" scripts/check_lock.py
"$PYTHON" scripts/run_checks.py
"$PYTHON" scripts/bench.py --workers 2 --intents 8 --work-ms 1 --out build/bench-ci.json
"$PYTHON" scripts/preflight.py
