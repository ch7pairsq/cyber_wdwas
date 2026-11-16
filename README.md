# ctf-agent · CTF AI Agent 工程框架

按《CTF AI Agent 框架开发设计文档》附录 A（目录蓝图）、附录 B（接口契约）、附录 F（Day-0 清单）初始化的工程骨架。设计文档见画布节点 d6900d3d 或任务目录 DESIGN.md。

## 快速开始

```bash
cd ctf-agent
python3 scripts/run_checks.py     # 零依赖自检（推荐先跑）
python3 scripts/preflight.py      # Day-0 赛前自检
# pytest 用户：python3 -m pytest tests -q
```

## 目录 → 设计文档映射

| 目录 | 文档依据 |
| --- | --- |
| ctf_ext/harness/ | §12 Harness 适配层、附录 B.1（adapter/schema/submit/mock） |
| ctf_ext/domain/ | §17 数据结构：五张扩展表 migration + 领域对象与 attempt_key |
| ctf_ext/store/ | §7 共享记忆：黑板（租约/幂等/去重）、证据库、教训蒸馏 |
| ctf_ext/runtime/ | §8/§14：调度轮转、五级 deadline、看门狗 |
| ctf_ext/gateway/ | §15/§16：策略引擎、Fail-Closed 下载、进程沙箱、工具注册表 |
| ctf_ext/llm/ | §11：Provider 抽象、DeepSeek Flash 实现、上下文压缩 |
| ctf_ext/skills/ | §10：索引先行按需加载 |
| ctf_ext/prompts/ | 附录 B.2 Worker 输出契约 |
| config/ | §13 分层配置（base / practice / competition） |
| tests/ | 附录 D 不变量 + Mock 端到端 |
| scripts/ | 附录 F-9/10：冻结与赛前自检 |

## 已验证行为（run_checks.py 全绿）

- 双 Worker 并发领取互不重复（附录 D.1-1，BEGIN IMMEDIATE + changes()==1）
- 结果幂等写入：同 idempotency_key 只落库一次，重复返回首次回执（D.1-2）
- 策略去重：attempt_key 重复的 Intent/结果被拒绝（D.3 参数规范化）
- 租约过期由看门狗回收，崩溃 Worker 的任务可被接续；过期写入拒绝（stale_write）
- 下载 Fail Closed：X 未配置（0）时拒绝且**不发起连接**；单文件/总配额双重校验；重定向阻断
- 命令白名单：未列入的命令被拒；参赛锁：competition_locked=true 时提交被拒（SUBMIT_LOCK）
- Mock Harness 端到端：正确 flag ACCEPTED、错误 REJECTED、同键重复提交幂等
- UNKNOWN 状态只查询回执，绝不自动重发（附录 E）
- 调度按期望得分/Token 排序 + 单题并发上限，防单题垄断（§23.2-4）

## 与设计文档的差异（初始化期优化）

1. **Python 版本**：文档目标 ≥3.12（随 Cairn）；本机为 3.9.6，代码按 3.9 兼容编写并通过全部检查。生产镜像仍须用 3.12，preflight 会给出 WARN。
2. **依赖懒加载**：pyyaml / requests 声明在 pyproject 但本机未安装；config 加载与 DeepSeek 调用为懒导入，缺依赖时给出明确错误而不是崩溃。run_checks.py 不依赖任何第三方包。
3. **上游 Cairn 未 vendored**：ctf_ext 不 import cairn（扩展层独立可测）。接入上游时执行：git clone https://github.com/oritera/Cairn 到 cairn/ 并记录 commit SHA（AGPL-3.0 合规确认归负责人）。
4. **官方 Harness 未实现**：OfficialHarnessAdapter 一律 fail closed，等 SDK/样例后按 §12.5 清单实现。

## 下一步（附录 F 剩余项）

- [ ] F-2 跑通上游 Cairn serve + dispatch（需 vendored）
- [ ] F-5 DeepSeek 真实连通性验证（tool calls / JSON / 429 行为）
- [ ] F-8 注入一次真实 Worker 崩溃演练（当前看门狗逻辑已由测试覆盖）
- [ ] §12 官方协议映射表（阻塞项见文档 §22）
