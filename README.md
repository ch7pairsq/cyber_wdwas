# ctf-agent · CTF AI Agent 工程框架

按《CTF AI Agent 框架开发设计文档》附录 A（目录蓝图）、附录 B（接口契约）、附录 F（Day-0 清单）实现的工程骨架。设计文档见画布节点 d6900d3d；本轮核对与修复记录见 [docs/REVIEW-2026-11-17.md](docs/REVIEW-2026-11-17.md)。

## 快速开始

```bash
cd ctf-agent
python3 -m ctf_ext run --db /tmp/p.db --evidence /tmp/ev   # 端到端冒烟（mock 题，无需 Key）
python3 scripts/run_checks.py     # 41 项检查：unit + integration + chaos
python3 scripts/preflight.py      # Day-0 赛前自检
# pytest 用户：python3 -m pytest tests -q
```

## 目录 → 设计文档映射

| 目录 | 文档依据 |
| --- | --- |
| ctf_ext/harness/ | §12 协议边界；B.1 adapter/submit、B.2 contract 校验、mock 端到端 |
| ctf_ext/domain/ | §17 数据结构：五张扩展表 migration + 领域对象与 attempt_key |
| ctf_ext/store/ | §7 共享记忆：黑板（租约/幂等/去重）、证据库、教训蒸馏 |
| ctf_ext/runtime/ | §8/§14/§18：调度轮转、五级 deadline、看门狗、编排器、Worker、指标 |
| ctf_ext/gateway/ | §15/§16：策略引擎、Fail-Closed 下载、进程沙箱、工具注册表 |
| ctf_ext/llm/ | §11：Provider 抽象、DeepSeek Flash 实现、上下文压缩 |
| ctf_ext/skills/ | §10：索引先行按需加载 |
| ctf_ext/prompts/ | 附录 B.2 Worker 输出契约 |
| config/ | §13 分层配置（base / practice / competition） |
| tests/ | unit + integration（端到端）+ chaos（故障注入） |
| scripts/ | 附录 F-9/10：冻结与赛前自检 |
| docs/ | 核对报告与剩余差距 |

## 已验证行为（run_checks.py 全绿）

- 双 Worker 并发领取互不重复（附录 D.1-1，BEGIN IMMEDIATE + changes()==1）
- 结果幂等写入：同 idempotency_key 只落库一次，重复返回首次回执（D.1-2）
- 策略去重：attempt_key 重复的 Intent/结果被拒绝（D.3 参数规范化）
- 租约过期由看门狗回收，崩溃 Worker 的任务可被接续；过期写入拒绝（stale_write）
- 下载 Fail Closed：X 未配置（0）时拒绝且**不发起连接**；单文件/总配额双重校验；重定向阻断
- 命令白名单：未列入的命令被拒；参赛锁：competition_locked=true 时提交被拒（SUBMIT_LOCK）
- Mock Harness 端到端：正确 flag ACCEPTED、错误 REJECTED、同键重复提交幂等
- UNKNOWN 状态只查询回执，绝不自动重发（附录 E）
- 调度按期望得分/Token 排序 + 单题并发上限 + 并发题目数封顶（§23.2-4、§5.4）
- 证据不变量：结果与 Fact 只能引用已登记且哈希匹配的证据（D.1-3）；附件复制入库、源文件保留
- 下载策略额外拦截私网/回环/DNS 失败目标（§15.1 SSRF）
- 工具护栏：白名单、execute 需审批、工作目录约束、并发上限、raw/summary/hash_only 三种证据策略
- 提交状态机：SENDING/UNKNOWN 只查询不重发，重启先恢复已发送事实（附录 E）
- B.2 契约校验：strategy_key、facts 证据、候选答案证据，不合格记 failed_contract
- 端到端：CLI 一次运行完成 读附件→解码→flag→证据→事实→ACCEPTED，重启不重放、不重复提交

## 与设计文档的差异（初始化期优化）

1. **Python 版本**：文档目标 ≥3.12（随 Cairn）；本机为 3.9.6，代码按 3.9 兼容编写并通过全部检查。生产镜像仍须用 3.12，preflight 会给出 WARN。
2. **依赖懒加载**：pyyaml / requests 声明在 pyproject 但本机未安装；config 加载与 DeepSeek 调用为懒导入，缺依赖时给出明确错误而不是崩溃。run_checks.py 不依赖任何第三方包。
3. **上游 Cairn 未 vendored**：ctf_ext 不 import cairn（扩展层独立可测）。接入上游时执行：git clone https://github.com/oritera/Cairn 到 cairn/ 并记录 commit SHA（AGPL-3.0 合规确认归负责人）。
4. **官方 Harness 未实现**：OfficialHarnessAdapter 一律 fail closed，等 SDK/样例后按 §12.5 清单实现。
5. **配置键命名**：本项目用 concurrency/budget/download/harness，§13 示例用 runtime/budgets/downloads；语义一致但未做别名映射。
6. **无凭证时的 Worker**：RuleBasedWorker 只覆盖 base64 类题目，用于打通链路，不代替正式 LLM Worker。

## 下一步（附录 F 剩余项）

- [x] F-4 Mock 端到端（CLI + integration/chaos 测试）
- [x] F-8 崩溃恢复（chaos：租约回收后接续、过期写入拒绝、SENDING 恢复）
- [ ] F-1/F-2 vendored Cairn + serve/dispatch 冒烟，记录 commit SHA
- [ ] F-5 DeepSeek 真实连通性验证（tool calls / JSON / 429 行为）
- [ ] LLM Worker 接入（prompts/worker.md → contract → 黑板）
- [ ] 压缩炸弹与解压计数（§15.1）
- [ ] 动态配置热更新与 config_snapshots 写入（§13）
- [ ] §18 指标导出与 4/6/8 并发压测
- [ ] CI：依赖锁、契约校验、migration 一致性
- [ ] §12 官方协议映射表（阻塞项见文档 §22）
