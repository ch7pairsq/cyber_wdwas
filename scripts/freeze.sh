#!/usr/bin/env bash
# 版本冻结（附录 F-9 / §1 冻结纪律）：记录代码版本与依赖锁
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p build
{
  date -u +"%Y-%m-%dT%H:%M:%SZ"
  git rev-parse HEAD 2>/dev/null || echo "no-git"
  shasum -a 256 pyproject.toml config/*.yaml 2>/dev/null || true
} > build/freeze.txt
if command -v python3 >/dev/null; then
  python3 -m pip freeze 2>/dev/null > build/requirements-frozen.txt || true
fi
echo "frozen -> build/freeze.txt"
