# §18 指标与压测基线（2026-11-17）

## 1. 指标口径

| 指标 | 来源 | 用途 |
| --- | --- | --- |
| intents.claimed / completed / duplicate_write | 进程内 Metrics + `intents_ext` | 调度吞吐与幂等 |
| knowledge.facts | `facts` 表 | 共享记忆增量 |
| submissions.accepted / rejected / unknown | `submissions` 表 | 提交状态机健康度 |
| model.tokens | `attempts.resource_usage.model_tokens` | §23：Token 为一等指标 |
| tools.calls | `attempts.resource_usage.tool_calls` | 工具预算 |
| config.reloads / config_snapshots | `events` + `config_snapshots` | 动态配置审计 |
| claim 互斥 / database-locked | bench 结果 | 并发正确性 |

看板是静态快照而非实时监控：`python3 scripts/dashboard.py --db data/practice.db --out build/dashboard.html --bench build/bench.json`。

## 2. 本次实测（单机 mock，无模型调用）

命令：`python3 scripts/bench.py --workers 4,6,8 --intents 48 --work-ms 5`

| Workers | Intents | 耗时 | 领取/秒 | 占用率 | 重复领取 | 错误 | 结果 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 4 | 48 | 0.108s | 443.7 | 56% | 0 | 0 | PASS |
| 6 | 48 | 0.086s | 560.3 | 47% | 0 | 0 | PASS |
| 8 | 48 | 0.101s | 477.1 | 30% | 0 | 0 | PASS |

结论边界：这验证的是黑板 + SQLite 的互斥与写入正确性（无重复领取、无 database-locked、无丢结果），
**不是**平台压测，也不代表真实模型吞吐。占用率随并发下降来自单写者串行化，属预期行为。

## 3. 与公开基线对比

- 榜首（7/15 题、845,910 tokens、5,112 s）：单题均值约 12 万 tokens、730 s。
- 本项目 P0 目标：单题 Token 控制在 10 万量级；`budget.max_model_tokens_per_intent` 可配置上限。
- 当前框架尚无真实模型跑分：mock 链路 token 为 0，因此**不能**声称达标，只能声明门禁已就位
  （契约校验会拒收超预算/无证据输出）。

## 4. 复现

    python3 scripts/bench.py --workers 4,6,8 --intents 48 --work-ms 5 --out build/bench.json
    python3 scripts/dashboard.py --db build/demo.db --out build/dashboard.html --bench build/bench.json
