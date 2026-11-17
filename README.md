# cyber-wdwas · CTF AI Agent 工程框架

面向授权 CTF 场景的 Python 工程框架。应用包名为 `ctf_ext`，包含任务领取、证据登记、结果契约、提交状态机、模型适配与运行指标；`cairn/` 保留固定版本的上游 Cairn 源码。

> **状态：工程骨架与模块测试可用，尚未完成真实解题和生产部署验收。** Mock 成功不等于真实模型可解题；代码审查发现的主流程接线与安全边界缺口见下文。仅用于拥有明确授权的题目和目标。

## 当前是否运行在 Docker 中？

**不是。当前远程验证采用宿主机 Python 3.12.3 + 独立虚拟环境。** 本地已在 Python 3.12.16 下通过 66 项既有检查。

| 层级 | 当前状态 | 边界 |
| --- | --- | --- |
| `ctf_ext` 应用 | 从源码目录运行 | 尚无本应用专用的完整 Docker 部署方案 |
| 远程依赖环境 | 32 个包安装成功，依赖兼容性检查通过 | Python 3.12.3，保留实际安装清单；不是系统级安装 |
| Cairn 上游 | 98 项测试通过；回环服务与 local/mock 调度冒烟通过 | 仅验证 Mock，不代表真实模型、Docker 或生产验收 |
| `cairn/docker-compose.yaml` | 上游 Docker 启动参考，尚未验证 | 不代表已将 `ctf_ext` 接入容器中的 Dispatcher |

上游 Compose 包含 server 和 dispatcher，dispatcher 会挂载宿主机 Docker socket，且需要准备 `cairn/dispatch.yaml`。不要将它当成默认安全沙箱，也不要在未核对配置、网络、权限、持久化与资源限制前直接部署。

## 快速开始：隔离的 Mock 验证

推荐 Python **3.12+**。扩展代码保留 Python 3.9 兼容，但 Cairn 要求 3.12+。

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

`scripts/ci.sh` 执行语法检查、直接依赖锁核对、66 项检查、小规模 SQLite 模拟压测和预检。`.github/workflows/ci.yml` 是 GitHub Actions 配置，**不会自动成为 GitLab 流水线**；当前不宣称 GitLab CI 已接通。

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

- `--mode llm` 是实验入口。目前只完成 Provider 请求、响应契约校验和修复重试，尚未接通完整附件读取、工具循环与证据登记闭环，**不是补上 Key 就能正式解题**。
- 模型凭据只通过受控环境提供；不要提交到仓库、示例配置或运行报告。当前验证不包含真实模型调用。
- 官方 Harness 的 SDK、fixture 和字段映射尚未验收；CLI 的官方模式仍明确拒绝启动。适配器存在不代表 CLI 已完成接线。
- 下载限额 0 的设计语义是禁止下载，但热更新与共享计数仍有下述缺口；不能将模块保护等同于完整出口隔离。
- 不确定提交状态必须只查询，不重发；启动恢复接线尚待补齐。

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

截至 2026-11-17，基线代码 `2e831a0`：

- 本地 Python 3.12.16：**66/66 既有检查通过**；预检 **12 PASS / 2 WARN / 0 FAIL**。警告为缺 PyYAML 和模型 Key，因此 YAML 配置分支当时没有执行。
- 此前 Python 3.9.6：66/66 通过；预检 11 PASS / 4 WARN / 0 FAIL。
- 远程 Python 3.12.3：32 个依赖包安装成功，兼容性检查通过；项目 CI 的 **66/66 检查通过**，预检 **16 PASS / 2 WARN / 0 FAIL**。警告为缺少 `unzip/file/binwalk/sage` 与模型 Key；未自动安装大型工具。
- Cairn 上游：**98/98 测试通过**，无失败、错误或跳过；有一条 Starlette/httpx 弃用警告，未为消除警告而擅自更换依赖。
- Cairn 实际冒烟：回环 HTTP 服务、启动健康检查、空队列 `dispatch --once`、Mock bootstrap 调度和 `completed` 结果落库全部通过；不调用真实模型。测试后已发送 SIGTERM，日志确认正常关闭且端口不再监听；未部署常驻服务。
- 首次复测串联命令存在引号错误，导致上游测试文本日志保存失败、冒烟未执行；98 项测试结果由 JUnit XML 核对，冒烟随后单独补跑通过。未将首次整体退出码 1 隐瞒为成功。
- `preflight` 的“Cairn 可运行”只核对 Python 版本，不证明依赖齐全或 `serve/dispatch` 已成功。
- `scripts/bench.py` 直接对 Blackboard 做多线程 SQLite 模拟，不经过完整调度链；4/6/8 线程结果不代表真实平台吞吐、模型效率或解题得分。
- 当前看板是静态快照，不是实时监控。视觉使用近黑底与克制青紫辉光，生成结果位于被 Git 忽略的 `build/`。

## 已知缺口与下一步

测试通过仅证明已覆盖路径。以下为源码审查确认的待补齐事项，尚未用新增故障实验逐项复现：

| 优先级 | 缺口 | 完成标准 |
| --- | --- | --- |
| 高 | LLM Worker 缺完整题目上下文、附件/工具/证据闭环 | 从空数据库走真实入口，生成可验证证据和候选答案 |
| 高 | Intent 完成与发送之间、SENDING/UNKNOWN 重启恢复未接线 | 明确区分未发送和可能已发送，重启只查询已发送记录 |
| 高 | 单题时间、Token、工具次数预算未强制执行 | 剩余预算进入每次调用，截止时取消并保存可恢复状态 |
| 高 | 热更新校验异常可逃逸；缺失额度可能保留旧值 | 非法配置不终止运行，额度缺失严格关闭下载 |
| 高 | 下载总额度缺少原子共享与逐块限制 | 并发与未知长度下载均不能绕过累计配额 |
| 高 | 输出先全量缓冲再截断；ZIP 保护未接入所有工具路径 | 有界输出读取、资源隔离、统一受控解压与出口 |
| 中 | 租约写回未比较到期时间，运行期心跳未接线 | 拒绝已到期写回；长任务可靠续租与恢复 |
| 中 | 扩展编排仍串行，调度检查与 claim 非原子 | 用真实调度入口验证并发上限、名额占用和公平性 |
| 验收 | Cairn local/mock 冒烟通过；Docker、官方协议与真实模型尚未验收 | 上游 Mock 成功不替代扩展层接线、安全边界及真实解题验收 |

本仓库目前不应被描述为“仅剩环境问题”或“已完成生产部署”。

## 依赖、来源与许可

- `requirements-lock.txt` 当前只固定三项直接依赖，不是全部传递依赖的完整锁；部署时需保留实际安装清单并补齐可复现性验证。
- 上游 Cairn 固定提交：`8e7e0ea67552383851dfcabfba0c4e9c8d007878`，包版本 0.2.1；来源与校验说明见 [cairn/VENDORED.md](cairn/VENDORED.md)。
- Cairn 原始 AGPL-3.0 许可保留在 [cairn/LICENSE](cairn/LICENSE)。本说明不为扩展层另行授予许可。
- [历史核对记录](docs/REVIEW-2026-11-17.md) 记录先前阶段结果，不能覆盖本 README 的当前已知缺口；另见 [指标口径](docs/METRICS.md) 和 [官方接入清单](docs/HARNESS-CHECKLIST.md)。
