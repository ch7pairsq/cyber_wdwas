# ctf-agent · CTF AI Agent 工程框架

按《CTF AI Agent 框架开发设计文档》附录 A（目录蓝图）、B（接口契约）、F（Day-0 清单）实现的工程骨架。
设计文档见画布节点 d6900d3d；核对与修复记录见 [docs/REVIEW-2026-11-17.md](docs/REVIEW-2026-11-17.md)，
指标与压测口径见 [docs/METRICS.md](docs/METRICS.md)。

## 快速开始

```bash
cd ctf-agent
python3 -m ctf_ext run --db /tmp/p.db --evidence /tmp/ev        # 端到端冒烟（mock，无需 Key）
python3 -m ctf_ext run --mode llm --config config/base.yaml     # 需要 DEEPSEEK_API_KEY
python3 scripts/run_checks.py     # 65 项零依赖检查（unit + integration + chaos）
python3 scripts/bench.py --workers 4,6,8 --intents 48          # 并发压测 → build/bench.json
python3 scripts/dashboard.py --db /tmp/p.db --out build/dashboard.html --bench build/bench.json
python3 scripts/preflight.py      # Day-0 赛前自检
bash scripts/ci.sh                # 本地 CI：语法 → 依赖锁 → 测试 → 压测 → 预检
# pytest 用户：python3 -m pytest tests -q
```

## 目录 → 设计文档映射

| 路径 | 文档依据 |
| --- | --- |
| ctf_ext/harness/ | §12 协议边界；B.1 adapter/submit、B.2 contract 校验、mock 端到端；official.py 显式 fail closed |
| ctf_ext/domain/ | §17 扩展表 migration（v1 八表 + v2 facts/events/config_snapshots）、领域对象与 attempt_key |
| ctf_ext/store/ | §7 共享记忆：黑板（租约/幂等/去重）、证据库、事实层、教训蒸馏 |
| ctf_ext/runtime/ | §8/§14/§18：调度、看门狗、编排器、规则/LLM Worker、指标、热配置接线、看板 |
| ctf_ext/gateway/ | §15/§16：策略引擎（SSRF）、Fail-Closed 下载、沙箱、工具护栏、解压防护 |
| ctf_ext/llm/ | §11：Provider 抽象、DeepSeek Flash（total_tokens 统计）、上下文压缩 |
| ctf_ext/config.py | §13：别名归一、环境变量 Fail Closed、校验、热更新与快照 |
| config/ | §13 分层配置（base / practice / competition） |
| cairn/ | F-1：vendored 上游（固定 commit，AGPL-3.0，见 cairn/VENDORED.md） |
| tests/ | unit + integration（端到端/热更新）+ chaos（故障注入） |
| scripts/ | F-9/10：冻结、预检、压测、看板、依赖锁、CI |
| docs/ | 核对报告、指标基线 |

## 已验证行为（65/65 全绿）

- 双 Worker 并发领取互不重复；结果幂等写入；attempt_key 去重（D.1–D.3）
- 租约回收清空 lease_until，崩溃任务可立即接续；过期写入拒绝（D.4）
- 证据不变量：结果与 Fact 只能引用已登记且哈希匹配的证据（D.1-3）
- 下载 Fail Closed + 私网/回环/DNS 失败拦截（§15.1）；重定向阻断
- 解压防护：路径穿越、条目数、单条/总字节、深度、压缩比、符号链接全部拒绝（§15.1）
- 工具护栏：白名单、execute 审批、工作目录约束、并发上限、三种证据策略
- 提交状态机：SENDING/UNKNOWN 只查询不重发（附录 E）
- B.2 契约校验 + LLM Worker：无效 JSON/契约失败各重写一次，仍失败记 failed_contract；Provider 超时/限流不误重试
- 官方 Harness：SDK 缺失或方法不全时抛 OfficialProtocolUnavailable，绝不静默降级
- §13 热更新：配置变更即时生效，非法配置保留旧值并记录 last_error，快照写入 config_snapshots
- §18 指标：Token/工具调用/提交状态落库，静态暗色看板与 4/6/8 并发压测（无重复领取、无 database-locked）

## 本轮收尾（2026-11-17）

| 原差距 | 现状 | 证据 |
| --- | --- | --- |
| Cairn 未 vendored | 已 vendored 固定 commit `8e7e0ea`，含 AGPL 许可与重拉脚本 | cairn/VENDORED.md、scripts/vendor_cairn.sh、preflight |
| 压缩炸弹防护 | 已实现 inspect_zip/extract_zip 六类上限 | ctf_ext/gateway/archive.py + 7 项测试 |
| 动态配置热更新 | 已实现别名/校验/热更新/快照/调度与限额生效 | ctf_ext/config.py + tests/integration |
| LLM Worker 接入 | 已实现 prompt→Provider→契约→一次修复 | ctf_ext/runtime/llm_worker.py + 7 项测试 |
| 指标看板与压测 | 已实现静态看板 + 4/6/8 压测 | docs/METRICS.md、build/dashboard.html |
| CI 与锁文件 | 已加 requirements-lock、check_lock、ci.sh、GitHub Actions | .github/workflows/ci.yml |
| §13 键名映射 | runtime/budgets/downloads/app 已映射到规范键 | tests/unit/test_config.py |
| 官方 Harness | 仍阻塞：SDK/样例缺失，保持 fail closed | ctf_ext/harness/official.py（注入式，方法不全即拒绝） |
| 真实模型连通性 | 未验证：无 DEEPSEEK_API_KEY | preflight WARN、cli --mode llm 明确报错 |

## 与设计文档的差异

1. **Python 版本**：文档目标 ≥3.12；本机 3.9.6，代码按 3.9 兼容并通过全部检查，生产镜像仍须 3.12（preflight WARN）。
2. **Cairn 运行环境**：上游 requires-python >=3.12 且依赖 fastapi/uvicorn/docker；本机无 3.12 与 Docker，serve/dispatch 冒烟无法执行（源码已 vendored，非代码缺陷）。
3. **官方 Harness**：协议细节未公开，适配器只做注入式转发 + 就绪门禁，未臆造字段映射。
4. **依赖懒加载**：pyyaml/requests 未在本机安装；加载器与 DeepSeek 为懒导入，缺依赖给出明确错误。
5. **压测口径**：bench 是单机 SQLite 模拟并发，验证互斥与正确性，不代表平台吞吐或真实解题速度。
6. **无凭证 Worker**：RuleBasedWorker 只覆盖 base64 类题目，用于打通链路，不代替正式 LLM Worker。

## 下一步（附录 F 剩余）

- [x] F-1 vendored Cairn（源码 + SHA + 重拉脚本）
- [x] F-4 Mock 端到端（CLI + integration/chaos）
- [x] F-8 崩溃恢复（租约回收、过期写入、SENDING 恢复）
- [ ] F-2 serve/dispatch 冒烟：需 Python ≥3.12 + 依赖或 Docker
- [ ] F-5 DeepSeek 真实连通性（tool calls / JSON / 429）
- [ ] 官方协议映射表（阻塞项见文档 §22）
