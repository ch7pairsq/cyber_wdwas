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
# 环境要求：BAIZHI_API_KEY / ARENA_SOLVER_TOKEN 由 harness 注入。
# 2026-11-21 正式赛实测契约：ARENA_BASE_URL 与 ARENA_NETWORK_CIDR 平台
# 都不注入——base 走 BENCHMARK_BASE_URL/k8s service 发现，CIDR 由装配层
# 从 official-arena.yaml 的 authorized_arena_targets 注入（v15 修复），
# ARENA_PROXY 隧道由 k8s service 端口自动导出（setup_platform_network_env）。
set -euo pipefail

TOTAL_SECONDS="${1:-13800}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN_DIR="${RUN_DIR:-/tmp/ctf-official}"
mkdir -p "$RUN_DIR"
# -m ctf_ext 依赖 cwd 或 PYTHONPATH；显式 cd 仓库根，不依赖平台继承的 workdir。
cd "$ROOT"

echo "[run_official] total_wall=${TOTAL_SECONDS}s run_dir=${RUN_DIR}"
# 版本横幅：四场正式赛无法从日志判断镜像版本（system_chars 相同），
# 每次交付前更新 VERSION 与镜像 tag 对应，日志首行即可归因。
echo "[run_official] image_version=$(cat "$ROOT/VERSION" 2>/dev/null || echo unknown)"
# 推进心跳（v20）：真实解题活动（模型调用/工具完成/结果落库）才刷新，
# 与存活心跳分离。supervisor 停更 600s 判定"活着但不推进"并重启
# （2026-11-21 第 19 题正式赛：worker 卡 fork 阻塞，35 分钟无模型调用
# 而 claim_timeout 30 分钟才打断一次、重开后又卡，supervisor 无感知）。
export CTF_PROGRESS_HEARTBEAT="$RUN_DIR/agent.progress"

exec python3 "$ROOT/scripts/supervise.py" \
  --heartbeat "$RUN_DIR/agent.heartbeat" \
  --log-file "$RUN_DIR/agent.log" \
  --stale-seconds 180 \
  --progress "$RUN_DIR/agent.progress" \
  --progress-stale-seconds 600 \
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
# claim 超时不传 CLI：official-arena.yaml budget.claim_timeout_seconds=1800 是
# 唯一事实源（v11 硬编码 --claim-timeout 1200 与配置打架，[run] 打印 1200、
# [orch] 热配置覆盖成 3600，排查时两处对不上；2026-11-22 调 1800 = 单意图
# 30 分钟硬杀线，正常 attempt（max_tool_rounds=40）10-20 分钟级自然收尾，
# 极端慢 attempt 被截断后走可重试失败，同题续跑状态与证据保留）。
