#!/usr/bin/env bash
# 正式赛启动入口（平台托管 harness 调起本镜像后执行）。
#
# 结构：supervisor（心跳判活 + 有限重启 + 总墙钟约束）包裹 agent。
# - 总墙钟默认 13800s = 4h - 10min 收尾余量：orchestrator 在 deadline 后
#   还有 ≤30s 的在途提交排空宽限（final_drain_seconds），不能顶着 4h 硬顶跑。
# - DB/证据/心跳全部放 /tmp（平台 10GiB 临时盘，官方建议大文件用 /tmp）。
# - 崩溃/心跳卡死 → supervisor 杀进程组并重启，--seconds 自动收缩为剩余
#   时间；重启后 Orchestrator 从同一 DB 断点恢复（session_resumed）。
#
# 用法： run_official.sh [总秒数]     # 默认 13800
# 环境要求：BAIZHI_API_KEY / ARENA_SOLVER_TOKEN / ARENA_PROXY /
#           ARENA_NETWORK_CIDR / ARENA_BASE_URL 由 harness 注入。
set -euo pipefail

TOTAL_SECONDS="${1:-13800}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN_DIR="${RUN_DIR:-/tmp/ctf-official}"
mkdir -p "$RUN_DIR"
# -m ctf_ext 依赖 cwd 或 PYTHONPATH；显式 cd 仓库根，不依赖平台继承的 workdir。
cd "$ROOT"

echo "[run_official] total_wall=${TOTAL_SECONDS}s run_dir=${RUN_DIR}"

exec python3 "$ROOT/scripts/supervise.py" \
  --heartbeat "$RUN_DIR/agent.heartbeat" \
  --stale-seconds 180 \
  --max-restarts 3 \
  --deadline-seconds "$TOTAL_SECONDS" \
  --min-remaining 120 \
  -- \
  python3 -m ctf_ext run \
    --mode llm \
    --config "$ROOT/config/official-arena.yaml" \
    --db "$RUN_DIR/official.db" \
    --evidence "$RUN_DIR/evidence" \
    --heartbeat-file "$RUN_DIR/agent.heartbeat" \
    --report "$RUN_DIR/report.json"
# claim 超时不传 CLI：official-arena.yaml budget.claim_timeout_seconds=3600 是
# 唯一事实源（v11 硬编码 --claim-timeout 1200 与配置打架，[run] 打印 1200、
# [orch] 热配置覆盖成 3600，排查时两处对不上）。
