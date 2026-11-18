# cyber-wdwas · CTF AI Agent 工程框架

面向授权 CTF 场景的 Python 工程框架。应用包名为 `ctf_ext`，包含任务领取、证据登记、结果契约、提交状态机、模型适配与运行指标；`cairn/` 保留固定版本的上游 Cairn 源码。

> **状态：工程骨架与模块测试可用，尚未完成真实解题和生产部署验收。** Mock 成功不等于真实模型可解题；代码审查发现的主流程接线与安全边界缺口见下文。仅用于拥有明确授权的题目和目标。

## 工程优化 1.0（2026-11-17 增量）

已接入声明式 FLAG 与可追溯校验、原子提交 Outbox、非阻塞提交/重启只查询、结构化状态/可逆归档、步数调度/预算/缓存、租约心跳及显式保底入口。本轮又补了单题硬超时、失败退避、技能按需加载、同领域教训共享、心跳 supervisor 与异常隔离。累计新增 106 项回归，Python 3.12.16 为 **205/205**，本地 CI 通过；20 题离线夹具含 5 个对抗输入，零误提交。未调用真实模型或官方接口，未复建/发布镜像。

- [架构实施记录与能力边界](docs/FRAMEWORK-OPTIMIZATION-1.0.md)
- [A–L 全量用例映射、实际结果与未验收项](docs/FRAMEWORK-TEST-MATRIX-1.0.md)
- 保底入口：`python3 -m ctf_ext run --mode fallback`，复用原数据库/证据；仅验证 Mock 简单题，不在官方模式自动回退 Mock。
- 当前只允许公司固定模型，50 步换策略而非换厂商/模型；默认阈值需真实题集校准，多 Agent 未启用。
- 运行报告新增 `execution`：`mode=serial`、`effective_parallel=1`、`claim_timeout_seconds`、`retry_backoff_seconds`、`isolation.container=not_verified`。`concurrency.max_workers=4` 只是容量上限，不代表已并行。
- 单题硬超时只在主线程用 `SIGALRM` 生效；非主线程调用 `step()` 时不会强制中断，必须由外部 supervisor/进程超时兜底。
- 本地执行不等于容器沙箱：要求隔离时本地命令直接拒绝。每题真容器、真实解题工具循环、官方 Harness、全赛程/首杀与 24h 冻结仍未验收，**不满足全 P0 参赛准出**。

Docker 与参赛状态中的容器记录为此前结果，不包含本次尚未打包的增量。

## 离线工具链（构建期打包）

工具随镜像构建，比赛时只调用，不下载、不安装。清单固定在 `docker/toolchain.json`，`core` 覆盖文件/压缩/编码/二进制基础分析，`ctf` 增加 `binwalk`、`foremost`、`sleuthkit`、`exiftool`、`7zip`、`steghide`、`pngcheck`、`gdb`、`strace/ltrace`、`qemu-user`、`poppler`、QR 工具，以及 Reverse 用的 `pwntools`/`ROPgadget`/`cstool`/`checksec`/`patchelf`/`z3`；`sage`、`ghidra`、`nuclei` 等重型或主动扫描工具不进入默认镜像。构建阶段从 Debian 官方 apt 源安装并复制包内版权说明，运行阶段移除 pip/apt/uv/dpkg 入口；`check_competition.py` 与 `preflight.py --strict` 会校验工具二进制，缺失即失败。`ToolRegistry` 已接入 `--mode llm` 工具循环：模型只能调用注册表内的 18 个结构化只读/受控工具（侦察、压缩包、隐写、PDF、QR、反汇编、ROP），执行仍经过策略、预算、工作目录、command_log 与证据登记；默认沙箱要求为开启，缺少真实隔离时工具调用 fail-closed；网关暂不支持 Anthropic `tools` 时可用 `model.tools_enabled=false` 关闭工具定义传输。内置技能库现有 10 节，覆盖 crypto/reverse/pwn/forensics/misc 五个域并支持分类别名。来源、体积取舍和验证命令见 [离线工具链](docs/TOOLCHAIN.md)。

## Docker 与参赛状态

**应用已完成 Docker 开发运行、容器测试和真实 SSH 断点调试验收；尚不能正式参赛。** 根目录 Dockerfile／compose.yaml 区分 development 调试镜像与 competition 参赛候选镜像。后者已构建并通过断网检查，但官方 Harness 未接通，默认启动明确退出 78；未发布镜像。历史宿主机结果与本次容器结果分开记录。运行说明见 [Docker 运行与调试](docs/DOCKER.md)，参赛规则见 [公司模型与参赛约束](docs/COMPETITION.md)。

| 层级 | 当前状态 | 边界 |
| --- | --- | --- |
| `ctf_ext` 应用 | 最终开发镜像内 79/79 检查、CLI Mock、真实断点与退出验证通过 | CLI 一轮完成后退出；Mock 不代表真实解题 |
| Docker 运行依赖 | Python 3.12.16；构建时 pip check 通过，依赖赛前预装 | 不使用宿主机虚拟环境；保留镜像内安装清单 |
| Cairn 上游 | 最终开发镜像内 98/98 测试、实际 API／Mock Dispatcher 调度通过 | API 与 Dispatcher 保持运行；未接通 `ctf_ext` 的业务链路 |
| 参赛候选镜像（变更前） | 断网检查通过，安装器入口移除，默认退出 78 | 未接通官方 Harness，不能发布为可参赛成品 |
| `cairn/docker-compose.yaml` | 上游 Docker 启动参考，尚未验证 | 不代表已将 `ctf_ext` 接入容器中的 Dispatcher |

上游 Compose 包含 server 和 dispatcher，dispatcher 会挂载宿主机 Docker socket，且需要准备 `cairn/dispatch.yaml`。不要将它当成默认安全沙箱，也不要在未核对配置、网络、权限、持久化与资源限制前直接部署。

## 赛前开发：隔离的 Mock 验证

以下安装命令仅用于比赛开始前的开发环境。比赛时禁止 pip/apt/uv 安装；平台应从镜像仓库拉取已预装依赖的成品镜像。镜像仓库地址与 Harness 规范待提供。

运行目标为 Python **3.12+**（与 Cairn 一致）；不再保留 Python 3.9 兼容。

~~~bash
git clone https://git.in.chaitin.net/yuqiao.zhang/cyber-wdwas
cd cyber-wdwas

# 将虚拟环境放在源码目录外，避免现有 CI 语法扫描递归进入依赖目录。
python3.12 -m venv ../cyber-wdwas-venv
source ../cyber-wdwas-venv/bin/activate
python -m pip install -r requirements-lock.txt

# 不调用真实模型，不向官方平台提交。
python -m ctf_ext run --seconds 30 \
  --db build/practice.db --evidence build/evidence \
  --report build/report.json --dashboard build/dashboard.html

# 单元、集成、故障注入检查；预检；本地 CI。
python scripts/run_checks.py
python scripts/preflight.py
bash scripts/ci.sh
~~~

若系统 Python 未提供 `venv/ensurepip`，可使用已安装的 `virtualenv` 或 `uv` 创建独立环境；不要为了验证直接改动系统 Python。上述命令假定你有仓库读取权限和依赖下载权限。

`scripts/ci.sh` 执行语法检查、直接依赖锁核对、当前 186 项检查、小规模 SQLite 模拟压测和预检。`.github/workflows/ci.yml` 是 GitHub Actions 配置，**不会自动成为 GitLab 流水线**；当前不宣称 GitLab CI 已接通。

### 可选：安装 Cairn 并准备本地冒烟

~~~bash
# 在同一独立虚拟环境中安装固定源码包及上游测试依赖。
python -m pip install -r requirements-lock.txt ./cairn/cairn 'httpx>=0.27'
python -m pip check
mkdir -p build
python -m pip freeze > build/installed-freeze.txt

# 上游测试与本应用检查分开运行。
(cd cairn/cairn && python -m pytest -q)

# 仅监听本机回环；启动后在另一终端做检查。
cairn serve --host 127.0.0.1 --port 8000 --db-path build/cairn.db
~~~

调度前须准备并校验 DispatchConfig。仓库的 `cairn/dispatch.local.example.yaml` 默认使用宿主机真实 Agent CLI，而 `cairn/dispatch_mock.yaml` 默认是容器执行且包含随机故障配置；**两者都不能不加区分地作为无付费调用的冒烟配置**。无模型验证应显式使用 `runtime.execution: local`、`prompt_group: mock` 和 `type: mock` Worker。

## 真实模型与官方平台的边界

- `--mode llm` 是实验入口。已接通 Provider 请求、响应契约校验、修复重试与本地只读工具循环；但完整附件读取、真实网关的 `tools`/`tool_use` 兼容、可信容器沙箱和官方平台仍未验收，**不是补上 Key 就能正式解题**。
- 模型固定为公司百智云代理上的 `deepseek-flash`，采用 Anthropic Messages 兼容格式；禁止厂商直连或其他模型。`BAIZHI_API_KEY` 只在运行时注入，不写入镜像、配置或报告。公司网关真实协议／鉴权仍待联调，本轮没有真实调用。
- 比赛禁止 `web_search`、其他搜索 API 和运行时安装。已增加工具层拒绝与参赛候选镜像安装器移除；任意 Python 代码的出站访问仍须平台网络策略约束，不能把应用检查当作完整沙箱。
- 官方 Harness 的 SDK、fixture 和字段映射尚未验收；CLI 的官方模式仍明确拒绝启动。适配器存在不代表 CLI 已完成接线。
- 下载限额 0 的设计语义是禁止下载，但热更新与共享计数仍有下述缺口；不能将模块保护等同于完整出口隔离。
- 不确定提交状态只查询、不重发；当前已接启动恢复、原子 Outbox 与实际进程中断测试，正式平台回执协议仍未验收。

## 目录结构

| 路径 | 职责 |
| --- | --- |
| `ctf_ext/harness/` | Mock/官方协议边界、输出契约、提交状态机 |
| `ctf_ext/domain/` | 领域对象、去重键、数据库迁移 |
| `ctf_ext/store/` | SQLite 黑板、租约、证据、事实与教训 |
| `ctf_ext/runtime/` | 调度、看门狗、Worker、编排、指标和静态看板 |
| `ctf_ext/gateway/` | 策略判定、下载控制、命令执行、工具注册、ZIP 检查 |
| `ctf_ext/llm/` | Provider 适配与上下文处理 |
| `ctf_ext/config.py`、`config/` | 配置归一、校验、热更新与分层配置 |
| `cairn/` | 固定提交的上游源码、原始许可、示例与测试 |
| `tests/` | 扩展层单元、集成和故障注入测试 |
| `scripts/` | 既有检查、预检、模拟压测、看板和源码固定脚本 |
| `docs/` | 历史核对记录、指标口径、官方接入清单 |

## 验证记录与口径

### 2026-11-17 容器验收

- 开发镜像 `cyber-wdwas:dev`：本地镜像 ID `sha256:c1457b69ccc9d16d49edc99636e34e9f6b519428079f9d47a9a1ea6ca2c0009b`，linux/amd64，Python 3.12.16。
- 容器内项目 CI **79/79 通过**；预检 **16 PASS / 2 WARN / 0 FAIL**，警告为未预装 binwalk／sage 和未注入公司模型 Key。Cairn **98/98 通过**，保留一条 Starlette/httpx 弃用警告。
- CLI Mock 完成；API 与 Mock Dispatcher 实际调度项目 `proj_002` 达到 completed、包含 2 个 Intent。未调用模型或官方提交接口。
- 通过实际 SSH 隧道访问 API 返回 HTTP 200；DAP 命中 `/app/ctf_ext/__main__.py:120` 后继续，收到 terminated，再独立确认容器退出码 **0**、生成本轮 Mock 报告。临时验证隧道已关闭，调试容器已退出，不处于等待连接状态。
- 参赛候选 `cyber-wdwas:competition-candidate`：本地镜像 ID `sha256:63f46f916294a4973aa98cc793ee9d72c0b7527ace987deb802925bf2f289eb5`；断网检查与默认退出 **78** 验证通过。这不是官方 Harness 验收，也不是镜像仓库已发布摘要。
- 最终运行约束已核对：UID/GID 10001、只读根目录、cap_drop ALL、禁止提权、无 Docker socket，实际 cgroup 为 512 MiB／1 CPU／256 PID；internal 网络无默认路由。正式比赛出口白名单仍待平台落实。
- 过程中的失败、修正及日志目录见 [Docker 验收记录](docs/DOCKER.md)。完整网关协议、平台约束和镜像仓库仍待提供；本轮未提交或推送代码。

### 历史宿主机基线

以下为基线代码 `2e831a0` 的旧结果，不能替代上述容器验收：

- 本地 Python 3.12.16：**66/66 既有检查通过**；预检 **12 PASS / 2 WARN / 0 FAIL**。警告为缺 PyYAML 和模型 Key，因此 YAML 配置分支当时没有执行。
- 此前 Python 3.9.6：66/66 通过；预检 11 PASS / 4 WARN / 0 FAIL。
- 远程 Python 3.12.3：32 个依赖包安装成功，兼容性检查通过；项目 CI 的 **66/66 检查通过**，预检 **16 PASS / 2 WARN / 0 FAIL**。警告为缺少 `unzip/file/binwalk/sage` 与模型 Key；未自动安装大型工具。
- Cairn 上游：**98/98 测试通过**，无失败、错误或跳过；有一条 Starlette/httpx 弃用警告，未为消除警告而擅自更换依赖。
- Cairn 实际冒烟：回环 HTTP 服务、启动健康检查、空队列 `dispatch --once`、Mock bootstrap 调度和 `completed` 结果落库全部通过；不调用真实模型。测试后已发送 SIGTERM，日志确认正常关闭且端口不再监听；未部署常驻服务。
- 首次复测串联命令存在引号错误，导致上游测试文本日志保存失败、冒烟未执行；98 项测试结果由 JUnit XML 核对，冒烟随后单独补跑通过。未将首次整体退出码 1 隐瞒为成功。
- `preflight` 的“Cairn 可运行”只核对 Python 版本，不证明依赖齐全或 `serve/dispatch` 已成功。
- `scripts/bench.py` 直接对 Blackboard 做多线程 SQLite 模拟，不经过完整调度链；4/6/8 线程结果不代表真实平台吞吐、模型效率或解题得分。
- 当前看板是静态快照，不是实时监控。视觉使用近黑底与克制青紫辉光，生成结果位于被 Git 忽略的 `build/`。

## 11 个风险问题的直接结论（离线）

| # | 问题 | 当前结论 |
|---|---|---|
| 1 | 异常覆盖 | 单题超时、Worker 异常、契约失败、Gate 超时、Gateway pump、结果写入和连续调度异常都有独立事件与失败关闭路径；连续 3 次调度异常才 `run_aborted`。不是所有 `BaseException` 都兜底，`KeyboardInterrupt` 仍向上抛。 |
| 2 | Skill 加载与解题速率 | 已改为索引先行、按领域/关键词选择、最多 2 节、单节 8KB/总计 12KB、进程内缓存；索引损坏降级为空技能包。只内置 2 个领域章节，真实模型提速未测。 |
| 3 | 卡死与外部阻塞 | 主线程单题 `ClaimTimeout`、Guard 超时、Provider 超时、LeaseHeartbeat、提交 Outbox 与心跳 supervisor 已接；外部平台长期卡死仍可能拖住提交线程，应用层没有强制网络取消。 |
| 4 | 重启恢复 | 会话/尝试/结果/Outbox/教训/证据持久化，过期租约回收，真实进程退出后 DRAFT 继续、SENDING/UNKNOWN 只查询。未完成“工具调用中途”细粒度断点。 |
| 5 | 解题过慢 | 有上下文压缩、技能预算、阈值路由和工具缓存；没有真实题集时延/步数基准，不能宣称已解决速度问题。 |
| 6 | 并行与瓶颈 | 当前固定串行，`effective_parallel=1`；SQLite 写入、模型全局限额和单题消费者是瓶颈。没有真正并行执行器，压测只证明 8 路 SQLite 调度不超卖。 |
| 7 | 沙箱隔离与 Agent 通信 | 每题工作区 0700、拒绝 symlink、`SandboxSpec` 参数失败关闭；本机无 Docker，H-02/03/04 未实测。Agent 间没有直连通信，只通过黑板/教训交接包。 |
| 8 | 记忆落盘 | 会话 JSON、尝试、教训、证据和提交 Outbox 均落 SQLite/内容寻址文件，证据 fsync 原子替换；持久层通过重启用例。 |
| 9 | 跨会话记忆 | 同一数据库重启可恢复步骤、路线、证据和教训；没有跨运行的自然语言长期记忆或向量检索。 |
| 10 | 失败经验的跨 Agent 共享 | 教训蒸馏为“成立条件/失败原因/已排除/下一步”，FLAG 先脱敏；只按同 `category` 共享，交接包不返回其他题号或候选答案。当前只有一个串行 Worker，实际共享范围有限。 |
| 11 | 还需补充的测试 | 真实 Docker 隔离、真实 `deepseek-flash` 工具循环、官方 Harness/回执、4 小时全赛程、20 道真实题、非主线程超时、提交线程网络卡死、工具调用中途崩溃、上下文窗口极限。 |

完整用例映射与未验收项见 [测试矩阵](docs/FRAMEWORK-TEST-MATRIX-1.0.md)。

## 外部 supervisor

主进程可用心跳文件配合 `scripts/supervise.py` 做有限重启；它只能处理进程级卡死，不能证明容器隔离或平台回执正确。

~~~bash
python3 scripts/supervise.py --heartbeat build/agent.heartbeat --stale-seconds 90 --max-restarts 3 -- \
  python3 -m ctf_ext run --mode fallback --seconds 300 --heartbeat-file build/agent.heartbeat
~~~

## 已知缺口与下一步

测试通过仅证明已覆盖路径。以下为源码审查确认的待补齐事项，尚未用新增故障实验逐项复现：

| 优先级 | 缺口 | 完成标准 |
| --- | --- | --- |
| 高 | LLM Worker 的工具循环已接离线测试，但完整附件读取、真实网关 `tools`/`tool_use`、可信容器执行与真实模型效果未验收 | 从空数据库走真实入口，在隔离环境生成可验证证据和候选答案 |
| 高 | 单题硬超时只在主线程生效，阻塞型 C 扩展不保证可中断 | 生产进程使用 supervisor + 外部超时；补非主线程与 C 扩展实测 |
| 部分补齐 | 单题 wall-clock、Token、工具次数预算进入每次调用；超时写可重试失败并退避 | 真实模型下的取消、压缩与恢复仍需联调 |
| 已补齐（离线） | 结果/Gate/Outbox 原子提交，DRAFT 重启发送，SENDING/UNKNOWN 重启只查询 | 官方平台断线回执仍需联调 |
| 已补齐（JSON） | 非法结构/数值热更新拒绝；缺失额度重置 0 | YAML 分支受本轮 PyYAML 缺失限制未实跑 |
| 高 | 下载总额度缺少原子共享与逐块限制 | 并发与未知长度下载均不能绕过累计配额 |
| 部分补齐 | 命令输出有界采集、隔离要求默认失败关闭；ZIP 保护未接入所有工具路径 | 统一容器执行/解压与出口仍待完善 |
| 已补齐（离线） | 拒绝已到期写回/续租；执行与校验期间独立连接心跳 | 心跳与过期写回回归通过 |
| 部分补齐 | 调度容量检查与 claim 已同事务，8 路并发测试通过；扩展解题编排仍为单引擎 | 多 Agent 继续关闭，真实模型并发吞吐未验收 |
| 验收 | Docker 开发运行、测试、SSH 调试和候选拒绝启动已验收；官方协议与真实模型未验收 | 上游 Mock 成功不替代扩展层接线、安全边界及真实解题验收 |

本仓库目前不应被描述为“仅剩环境问题”或“已完成生产部署”。

## 依赖、来源与许可

- `requirements-lock.txt` 当前只固定三项直接依赖，不是全部传递依赖的完整锁；部署时需保留实际安装清单并补齐可复现性验证。
- 上游 Cairn 固定提交：`8e7e0ea67552383851dfcabfba0c4e9c8d007878`，包版本 0.2.1；来源与校验说明见 [cairn/VENDORED.md](cairn/VENDORED.md)。
- Cairn 原始 AGPL-3.0 许可保留在 [cairn/LICENSE](cairn/LICENSE)。本说明不为扩展层另行授予许可。
- [历史核对记录](docs/REVIEW-2026-11-17.md) 记录先前阶段结果，不能覆盖本 README 的当前已知缺口；另见 [指标口径](docs/METRICS.md) 和 [官方接入清单](docs/HARNESS-CHECKLIST.md)。
