# CTF Agent 源码级框架分析

> 本文面向工程评审与新成员上手，从源码角度说明每个模块的职责、每个类的用途与端到端调用链路。
> 代码由两部分组成：**自研 CTF 求解扩展层 `ctf_ext/`**（本项目的核心资产）与 **内嵌调度框架**（随仓库携带的通用任务调度与执行底座，本层不感知 CTF 业务，仅提供"项目—事实—意图"图与 worker 进程调度）。

---

## 1. 总体架构

```
┌────────────────────────────────────────────────────────────────────┐
│                          入口 __main__.main                        │
│        读配置 → 建 harness → 建 store/gateway → build_runtime       │
└───────────────┬────────────────────────────────────────────────────┘
                │
┌───────────────▼────────────────────────────────────────────────────┐
│  ctf_ext.runtime.Orchestrator（自研编排主循环，状态机 + 资源守护）     │
│    Scheduler 排题 → Blackboard.claim_intent 领取 → Worker.work      │
│    → LlmWorker（提示词/上下文 → 模型 → 结构化工具循环 → 契约校验）     │
│    → FlagGate 三门评审 → SubmitGateway Outbox 幂等提交               │
└───────┬──────────────────────────────────┬─────────────────────────┘
        │ 共享 SQLite（WAL）               │ 受控工具执行
┌───────▼──────────┐            ┌──────────▼──────────────────────────┐
│ store 层          │            │ gateway 层                          │
│ session/evidence/ │◄──────────►│ ToolRegistry → PolicyEngine         │
│ lessons/blackboard│  evidence  │ → sandbox/execution → 命令日志       │
└──────────────────┘            └─────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────┐
│ 内嵌调度框架（与本层松耦合，可 mock 独立运行）                         │
│ server（FastAPI + SQLite 原生表）◄──HTTP── 协议客户端回写          │
│ DispatcherLoop：bootstrap/explore/reason 三类任务                   │
│   → worker_select 选人 → ContainerManager/LocalBackend 执行         │
│   → WorkerDriver（claudecode/codex/pi/mock 四适配器）                │
│ ctf_ext 不 import 内嵌框架；两者只通过适配层/脚本对接                  │
└────────────────────────────────────────────────────────────────────┘
```

两条独立主线：

1. **自研求解主线**（`ctf_ext`）：题目输入 → 意图驱动求解 → 证据约束 → 三门评审 → 幂等提交。
2. **内嵌调度主线**（内嵌框架）：把"项目"当作事实图，向 worker 进程派发三类任务，worker 以 CLI/容器进程形态执行并把结论经 HTTP 回写。本项目把它作为"多机/多 worker 扩展执行底座"内嵌携带。

---

## 2. 目录结构

```
ctf-agent/
├── ctf_ext/                    # 自研求解扩展层（下文 §4-§11）
│   ├── __init__.py             # 版本与包描述
│   ├── __main__.py             # CLI 入口与全链路装配
│   ├── config.py               # 配置加载（YAML/JSON，深合并，别名归一）
│   ├── domain/                 # 领域模型与扩展表迁移、竞赛标识
│   ├── store/                  # 状态持久层（黑板/会话/证据/教训）
│   ├── runtime/                # 编排/调度/预算/看门狗/仪表盘
│   ├── gateway/                # 工具目录与安全护栏（策略/沙箱/下载/解压…）
│   ├── llm/                    # 模型通道抽象与两家 Provider、上下文压缩
│   ├── harness/                # 平台协议适配（mock/arena/official）+ 契约 + 门控 + 提交
│   └── skills/                 # 技能库索引与按需加载
├── dispatcher/                 # 内嵌调度框架（下文 §12-§13）
│   ├── server/                 # FastAPI 服务：项目/事实/意图/提示/导出/设置
│   └── dispatcher/             # 调度器：三类任务 + 执行后端 + worker 适配器
├── tests/                      # unit/integration/chaos 三层测试（230 用例）
├── docs/                       # 本文档与其他运维/评审文档
└── README.md
```

---

## 3. 入口与装配（`ctf_ext/__main__.py`）

`main()` 是唯一的进程入口，职责是把散件装配成可运行系统并处理退出码：

1. 解析 CLI 参数（`serve`/单轮等模式、配置路径、日志级别）。
2. `load_config` 读配置并做必需键校验。
3. 按协议选择 harness 适配器：`v1-mock` → `MockHarnessAdapter`；`v1-arena` → `ArenaHarnessAdapter`；`official` → `OfficialHarnessAdapter`（fail-closed）。
4. 打开共享 SQLite 连接 → `StateStore.run_migrations`（扩展表 v1/v2）→ 构造 `Blackboard`、`EvidenceStore`、`LessonStore`。
5. 构造 `PolicyEngine`/`ToolRegistry`/`SubmitGateway`/`FlagGate`（注入模型审阅者）。
6. `build_runtime(...)` 组装 `Orchestrator` 并 `run()`。

退出码契约在 `harness/schema.py`：`0` 正常、`2` 配置错误、`3` 无任务、`4` 超时、`5` 提交错误。协议版本不识别直接失败，不做静默降级。

---

## 4. 配置层（`ctf_ext/config.py`）

| 名称 | 职责 |
| --- | --- |
| `ConfigError` | 配置缺失/非法的统一异常 |
| `AppConfig` | 顶层配置容器（协议、平台、模型、预算、策略等子段） |
| `load_yaml / load_json / load_file` | 按扩展名选择解析器；YAML 缺依赖时明确报错 |
| `load_config(*paths)` | 多文件深合并（后者覆盖前者），键别名归一后构造 `AppConfig` |

---

## 5. 领域层（`ctf_ext/domain/`）

| 名称 | 职责 |
| --- | --- |
| `models.py` | 领域数据类：挑战、意图、尝试、证据、提交等的内存形状，与库表一一对应 |
| `migrate.py` | 扩展表迁移 v1/v2（`challenges/intents_ext/attempts/evidence/lessons/submissions/results_ext/facts/events/config_snapshots` 等），幂等执行；不触碰内嵌框架的原生表 |
| `competition.py` | 竞赛上下文标识（当前竞赛 id 等），供 harness 与黑板联动 |

---

## 6. 持久层（`ctf_ext/store/`）

所有状态共享同一个 SQLite 连接（WAL，`busy_timeout=5000`），读写走快照视图，写走 `BEGIN IMMEDIATE` 事务。

### session.py
| 名称 | 职责 |
| --- | --- |
| `SessionStore` | 每题一份解题会话（路由、阶段、假设、已试路径、证据 id 集）；`validate` 强 schema 校验；`compact` 归档完整 state 到证据库、正文只留近 10 条；`restore_evidence` 做路径逃逸与哈希校验后还原 |

### evidence.py
| 名称 | 职责 |
| --- | --- |
| `EvidenceStore` | 内容寻址证据库：`register` 写 `<id[:2]>/<id>` 并校验 sha256；`restore` 读回；归档永不截断（尾部线索可回读、可过门控） |

### lessons.py
| 名称 | 职责 |
| --- | --- |
| `LessonStore` | 教训库：跨题沉淀"什么策略在什么条件下无效"；写入前做敏感信息脱敏（`sanitize_lesson_text`），防止其他题号等泄漏进提示词 |

### blackboard.py（核心）
| 名称 | 职责 |
| --- | --- |
| `DuplicateStrategy` | 同题同策略键重复尝试（含阻塞来源） |
| `StaleWrite` | 乐观并发校验失败（attempt_no 过期） |
| `EvidenceNotRegistered` | 引用了未登记/哈希不符的证据 |
| `IntentDraft / Claim / WorkerResult` | 数据类：意图草稿、领取凭据（带 `attempt_no` 做乐观锁）、worker 产出（statuses: completed/partial/blocked/failed） |
| `Blackboard` | 黑板门面：`plan_intent / claim_intent / write_result / write_facts / handoff / snapshot / append_event` 等。要点：<br>• `claim_intent` 用租约（默认 180s）+ 心跳续期实现互斥领取<br>• `write_result` 幂等：同 attempt_key 重复写忽略；支持 `transaction_hook` 在同一事务内原子落门控记录与提交 Outbox<br>• `handoff` 产出限量脱敏交接包（近期尝试/教训/开意图） |

---

## 7. 运行时层（`ctf_ext/runtime/`）

| 名称 | 职责 |
| --- | --- |
| `orchestrator.py` `Orchestrator` | **编排主循环/状态机**：选取下一题 → 生成意图 → 领取 → 派发 worker → 处理结果 → 门控 → 提交 → 推进阶段。内含 deadline 守护（收尾宽限 30s，等待平台回执）、停滞检测、五阶段推进、`persist_gate` 事务钩子（门控记录 + 提交入 Outbox 原子落库） |
| `scheduler.py` `Scheduler` | 排题策略：`eligible_challenges` 做背压与排除（已确认/已停止/零槽位），`rank` 按"意图优先级 × 五阶段权重（开局难度加成）→ 公平轮转 → 题号"排序 |
| `control.py` `GlobalBudget / SessionControl` | 预算纪律：全局 token 账本（事务式读写）+ 每题会话控制（步骤/工具/令牌预留与扣减、80% 阈值触发压缩、换挡路由与停滞判定） |
| `budget.py` | 顶层预算配置解析与校验 |
| `llm_worker.py` `LlmWorker` | 模型求解 worker：构建提示词与上下文包 → 经 Provider 补全 → 解析工具调用 → `ToolRegistry.execute` → 循环直至产出候选答案；输出过契约校验后才可入黑板 |
| `worker.py` `Worker(ABC)` | worker 抽象（`work()` 契约）；`RuleBasedWorker` 为规则兜底实现（mock 场景） |
| `watchdog.py` `DeadlineGuard` | SIGALRM 硬超时（仅主线程，不支持的平台降级软超时）；`recover_expired_leases` 回收过期租约 |
| `lease.py` | 租约工具：领取/续期/过期的统一语义 |
| `heartbeat.py` | 求解过程心跳：推进进度基线、上报存活 |
| `planner.py` | 意图规划器：把题目/会话状态折叠为下一批意图草稿（策略键 `domain.technique.variant`） |
| `metrics.py` | 指标采集（尝试数/工具调用/token/门控通过率） |
| `dashboard.py` | `collect_snapshot` 聚合运行快照 → `render_dashboard` 自包含 HTML → `write_dashboard` 落盘（KPI 卡片 + 状态表 + worker 基准表） |

### 主调用链（自研主线）

```
__main__.main
  └─ build_runtime → Orchestrator.run
       ├─ Scheduler.eligible_challenges / rank        # 选下一题
       ├─ Blackboard.claim_intent                     # 租约互斥领取
       ├─ Worker.work → LlmWorker.solve               # 模型求解循环
       │    ├─ SessionControl.reserve                 # 预算预留
       │    ├─ ModelProvider.complete                 # 模型补全
       │    └─ ToolRegistry.execute_arguments         # 结构化工具循环
       ├─ FlagGate.evaluate                           # 三门评审（见 §10）
       ├─ Orchestrator.persist_gate(事务钩子)          # 决策落库 + 提交入 Outbox
       └─ SubmitGateway.submit（后台守护线程）          # 平台提交 + 回执
```

### 工具调用链

```
LlmWorker（模型给出结构化参数 JSON）
  └─ ToolRegistry.execute_arguments(tool, args)
       ├─ PolicyEngine.decide_tool_name → 七级判定（deny/allow/…）
       ├─ _check_cwd（工作区约束）→ 并发信号量 → 预算/缓存
       ├─ sandbox.run_command → execution.capture    # 有界子进程，杀进程组
       ├─ EvidenceStore.store_evidence               # 全文归档（内容寻址）
       └─ Blackboard 命令日志（argv/截断标记/证据 id）
```

---

## 8. 安全网关层（`ctf_ext/gateway/`）

| 名称 | 职责 |
| --- | --- |
| `policy.py` `PolicyEngine / PolicyConfig / PolicyDenied` | 硬规则引擎：工具名/命令白黑名单、下载限额（0=fail-closed）、私网阻断（SSRF）、竞技场目标白名单；默认全部收紧 |
| `tools.py` `ToolSpec / ToolResult / ToolRegistry` | 结构化工具注册表：模型只给 JSON 参数，不给 shell 命令。`execute` 护栏链：名称判定 → 注册检查 → 风险级 → 工作目录 → 并发 → 预算/缓存 → 执行 → 证据归档 → 命令日志 |
| `catalog.py` | 内建工具目录（22 个）：strings/hexdump/objdump/checksec/ROPgadget/file/7z/binwalk/exiftool/pngcheck/pdftotext/zbarimg/steghide 等；每个工具绑定固定主命令 + 参数白名单（路径限工作区、整数区间、字符串限长禁换行）+ 只读风险级 + 原始证据策略 |
| `execution.py` `capture / SandboxSpec / CommandExecutor` | 有界子进程执行（白名单环境变量、超时杀进程组、截断标记）；docker 只读沙箱规格（pin sha256 镜像、禁网络、非特权 uid）；受信命令执行器（仅固定离线 base64 解码器，不执行模型代码） |
| `sandbox.py` `run_command` | 策略 → 本地有界执行的薄入口（本地执行器不是安全沙箱，容器模式才是） |
| `download.py` `DownloadController / QuotaExceeded` | 受控下载：URL 预检（拒绝时不建连）→ 重定向全阻断 → Content-Length 预检 + 流式计数 + 总配额复核 |
| `archive.py` `ArchiveLimits / ArchiveRejected` | 解压防护：条目数/单条/总量/深度/压缩比/路径长/算法与符号链接禁令；先整体判定再写盘 |
| `python_exec.py` | 受限 Python 执行器脚本：模型代码经 stdin 送入，资源限额（CPU/内存/进程/文件），异常 traceback 回显供自修 |
| `pdf_images.py` | pdfimages 封装（list/extract） |
| `evidence_digest.py` | 证据摘要脚本：按 id 汇总、哈希校验防路径逃逸、超预算取头尾节选 |
| `arena_http.py` | 竞技场受限 HTTP：强制 SOCKS5 代理、目标限白名单网段、方法白名单、响应限量 |
| `git_history.py` | 只读 git 历史检查器：log/show/fsck 三模式、rev 白名单拦截选项注入、受控环境变量 |

---

## 9. 模型通道层（`ctf_ext/llm/`）

| 名称 | 职责 |
| --- | --- |
| `provider.py` `ModelProvider(ABC)` | 统一补全接口；`ProviderResponse` 统一形状（正文/用量/工具调用）；异常族 `RateLimited/ProviderTimeout/ProviderConfigError/ProviderError` |
| `provider.py` `RateLimiter` | 全局并发信号量 + 指数退避（1→60s，成功减半） |
| `deepseek.py` `DeepSeekProvider` | 公司网关通道：base_url/model 白名单、思考预算钳制、工具消息规范化、脱敏 repr |
| `openai_compat.py` `DeepSeekOfficialProvider` | 官方 API 通道：OpenAI 兼容协议、模型白名单、禁用环境代理继承 |
| `compress.py` | 上下文压缩：token 估算、工具输出预览（截断 + 关键观察提取，指向证据 id）、`build_context` 按预算装配 |

双模型换挡：阶段 2 起路由到更强模型（配置键 `pro_from_stage`，fail-open：取不到配置沿用默认）。

---

## 10. 平台适配与门控（`ctf_ext/harness/`）

### 协议与适配器
| 名称 | 职责 |
| --- | --- |
| `schema.py` | 数据契约（`HarnessContext/ChallengeInput/Submission/SubmitReceipt/RunSummary`）与退出码；`parse_challenge` 校验必需字段；仅支持 `v1-mock`/`v1-arena` |
| `adapter.py` `HarnessAdapter(ABC)` | 唯一平台边界：startup/read_tasks/submit_answer/finalize；附件与回执查询为可选钩子 |
| `mock.py` `MockHarnessAdapter` | 内置练习题 + 固定 flag 判定 + 提交幂等（同幂等键返回首次回执） |
| `arena.py` `ArenaClient / ArenaHarnessAdapter` | 竞技场协议：Bearer 鉴权（可 SOCKS5 代理，token 脱敏）；环境策略 prewarm/on_demand（并发上限 3、按 unique_code 去重防重复启动）；提交对账：409 duplicate → 幂等 ACCEPTED、409 invalid_state → 只读探测对账，其余 UNKNOWN，绝不伪造 REJECTED |
| `official.py` `OfficialHarnessAdapter` | 官方协议 fail-closed：SDK 方法集不全即抛 `OfficialProtocolUnavailable`，只转发不兜底 |

### 输出契约与门控
| 名称 | 职责 |
| --- | --- |
| `contract.py` `validate_worker_output / ContractViolation` | 附录 B.2 契约：task_id/状态/summary 1..5 条/策略键三段式/facts 证据齐全/资源用量数值/候选答案必须形如 `flag{...}` 且有证据支撑；不合格输出不得入库，重写一次仍失败记 `failed_contract` |
| `flag_gate.py` `FlagGate / GateDecision / ProviderReviewer / MockReviewer` | **三门评审**：<br>① grounding——证据落地：只认当前题由执行边界记录的完整成功命令输出，逐字比对声明，诱饵词/低熵/多匹配降级拒绝<br>② negation——三票反驳（L2 每票独立模型审阅，≥2 票反对即否决）<br>③ interrogation——追问三项（来源可靠/输出唯一/格式正确）<br>L1（逐字完整命中且唯一）走确定性快路径，三门由代码判定，不消耗模型；评审前后两次校验 FLAG 声明未被篡改 |
| `gateway.py` `SubmitGateway` | 持久化提交网关（Outbox 模式）：幂等键 `sha256(challenge_id|normalized_flag)`；主线程写库、守护线程发网络请求；仅三门全过才入队；UNKNOWN 按幂等键只查询不重发 |

---

## 11. 技能层（`ctf_ext/skills/`）

| 名称 | 职责 |
| --- | --- |
| `loader.py` `SkillLoader / SkillNotFound` | 技能库：索引 `index.json` 常驻内存（严格校验文件名不逃逸 sections/），正文按需读取缓存；`select` 按"领域匹配+名称匹配+关键词命中"打分挑技能；`index_text` 生成字节预算内的目录文本（fail-open） |

---

## 12. 内嵌调度框架——服务端（`server/`）

FastAPI + SQLite（WAL）。核心抽象是**事实图**：项目由事实（facts）组成，意图（intents）从一组事实出发、完成后指向新事实；`goal` 是每个项目固定的目标伪事实。

| 名称 | 职责 |
| --- | --- |
| `db.py` | `configure` 建表（8 张原生表：settings/projects/facts/intents/intent_sources/hints/counters/scoped_counters）+ 幂等列迁移；`get_conn` 短连接上下文（WAL + 外键，提交/回滚/关闭） |
| `models.py` | pydantic 契约：`Settings/Fact/Intent/Hint/ProjectReason/ProjectMeta/ProjectSummary/ProjectDetail` 与各请求/响应模型；文本字段统一"去空白非空"校验 |
| `services.py` | 领域服务：scoped id 生成、项目状态守卫（active/completed）、事实存在性校验、Intent 可领取/可释放判定（先过期失效租约）、行→模型装配、`expire_workers`（心跳超时隐式释放）、`expire_reason_leases` |
| `app.py` | FastAPI 装配 + 生命周期初始化 + 内嵌静态管理界面 |
| `routers/projects.py` | 项目 CRUD、状态切换（停用时释放全部租约与 reason 锁）、reason 锁 claim/heartbeat/release、`complete`（写指向 goal 的完成 Intent 并置 completed）、`reopen`（删完成 Intent、写反馈事实、状态回 active） |
| `routers/intents.py` | Intent 创建/心跳（兼领取）/释放/conclude（写结论事实并回填 `to_fact_id/concluded_at`） |
| `routers/hints.py` | 追加人工提示 |
| `routers/settings.py` | 全局超时读写 |
| `routers/export.py` | 项目导出：yaml 全量快照 / timeline 事件流文本 |

并发模型：Intent 租约（单持有者 + 心跳超时回收）与项目级 reason 锁（同一时刻仅一个 worker 做规划）。

---

## 13. 内嵌调度框架——调度器（`dispatcher/`）

| 名称 | 职责 |
| --- | --- |
| `config.py` | `DispatchConfig` 全量校验：worker 名唯一、单项目并发 ≤ 总并发、执行模式一致性（container 必须有容器配置与 LLM 环境键；local 补默认）、提示词组资源完整性、mock 行为键校验 |
| `contracts.py` | 三类任务输出契约：`validate_reason_payload`（rejected/complete/intents/noop 四态 + 上限）、`validate_bootstrap_execute/conclude_payload`、`validate_explore_payload`；兼容包裹形 `{"accepted":bool,"data":{…}}` |
| `models.py` | `RunningTask`（在途任务与取消控制柄）、`ReasonCheckpoint`（启动计数快照，回写时对比新增量） |
| `output_parser.py` | 从模型输出容错提取首个 JSON 对象（围栏段 → 逐位置 raw_decode） |
| `prompting.py` | 提示词模板加载/渲染（`{key}` 占位）与事实/意图/提示的 JSON 块格式化 |
| `logging.py` | 调度器日志格式化（短模块名；bare 模式只输出正文） |

### 调度主循环
| 名称 | 职责 |
| --- | --- |
| `scheduler/loop.py` `DispatcherLoop` | 主循环：启动预检 → 每拍拉取项目列表 → 为可派发项目选 worker → 提交线程池执行 → 回收 Future 与清理容器。派发优先级：bootstrap（冷启动）> reason（项目规划）> explore（意图执行） |
| `scheduler/worker_select.py` `choose_worker` | worker 排序：优先级 → 当前负载 → 随机抖动 |
| `startup_healthcheck.py` | 并行探活全部 worker（进程内 HTTP ping，非容器内 curl），失败汇总快速失败 |

### 执行底座（`runtime/`）
| 名称 | 职责 |
| --- | --- |
| `backend.py` `ExecutionBackend(Protocol)` | 执行底座协议：`container_name/ensure_running/build_exec_process/write_text_file/清理四件套/close`。两个实现经 `runtime.execution` 一键切换 |
| `containers.py` `ContainerManager` | 容器实现：每项目一容器（统一前缀命名，同名冲突复用）；生命周期/孤儿清理/文本注入（tar 归档）/exec 进程（命令外包 `timeout` 保底强杀） |
| `process.py` `ProcessResult / ExecProcess / ManagedProcess` | 进程抽象与容器 exec 实现：后台线程采集输出、超时强杀（按 exec PID）、取消 |
| `local_process.py` `LocalProcess` | 宿主子进程实现：独立进程组、SIGTERM→宽限→SIGKILL 关停 |
| `local_backend.py` `LocalBackend` | 本地实现：每项目一工作目录，继承宿主环境与 CLI 凭证，无容器（清理接口空实现） |
| `heartbeat.py` `HeartbeatLease` | 后台心跳线程：周期续期租约；连续失败（宽限期 = 2×周期）或 403/409 终态失败时杀 worker 进程止损 |
| `cancellation.py` `TaskCancellation` | 取消控制柄：幂等记录原因并强杀挂接进程；对后挂接的进程补生效 |

### 任务执行体（`tasks/`）
| 名称 | 职责 |
| --- | --- |
| `common.py` | 任务公共件：`run_worker_process`（起进程 + 挂接租约/取消 + 等待）、超时/取消归一、`write_conclude_result(_with_fact_id)`（结论回写与失败释放）、图快照注入、mock 行为解析 |
| `bootstrap.py` `run_bootstrap_task` | 冷启动任务：首条事实 + 完成描述（两阶段 execute/conclude） |
| `reason.py` `run_reason_task` | 项目级规划：领取 reason 锁 → 输出新 Intent 批（或宣告完成） |
| `explore.py` `run_explore_task` | 意图执行：完成单个 Intent 并把结论写回事实 |

### Worker 适配器（`workers/`）
| 名称 | 职责 |
| --- | --- |
| `base.py` `WorkerDriver(ABC) / DriverResult / SeedSessionDriver / RegexSessionDriver` | 适配器抽象：健康探活 + 执行/收尾命令构造 + 会话与响应提取；两种会话 mixin（预生成 UUID / stderr 正则提取） |
| `health.py` `HealthResult / http_ping` | 进程内探活：小请求按 HTTP 状态判健康（2xx 即可用） |
| `registry.py` `get_driver` | 按类型与执行模式取适配器单例（local 变体复用宿主 CLI 配置） |
| `adapters/claudecode.py` | claude CLI：Anthropic /v1/messages 探活；`claude --session-id …` 执行、`claude -r <session>` 收尾 |
| `adapters/codex.py` | codex CLI：/responses 探活；`codex exec`（容器模式注入自定义模型供应商参数）/ `exec resume` |
| `adapters/pi.py` | pi CLI：按 provider API 类型选端点探活；JSON 模式执行；从事件流提取会话 id 与最后一条 assistant 消息 |
| `adapters/mock.py` | mock：内嵌 python3 脚本按配置概率产出结果（延迟/结果分布可配），用于端到端联调与混沌测试 |

### 内嵌框架调用链

```
dispatcher CLI serve/dispatch
  └─ DispatcherLoop.run
       ├─ run_startup_healthchecks                    # worker 探活
       ├─ 协议客户端.list_projects                   # 拉取项目
       ├─ _dispatch_available → _try_dispatch_project # 按 bootstrap>reason>explore 派发
       │    └─ choose_worker                          # 选 worker
       ├─ ThreadPoolExecutor.submit(run_*_task)       # tasks/{bootstrap,reason,explore}
       │    └─ run_worker_process
       │         ├─ ContainerManager.ensure_running   # 容器/本地执行单元
       │         ├─ WorkerDriver.build_execute        # 构造 argv
       │         ├─ HeartbeatLease.start              # 心跳续租
       │         └─ ManagedProcess/LocalProcess.communicate
       ├─ contracts.validate_*_payload                # 输出契约
       └─ 协议客户端.create_intent / conclude / complete  # HTTP 回写
```

---

## 14. 关键设计约束（评审要点）

1. **自研层与内嵌框架解耦**：`ctf_ext` 不 import 内嵌框架模块；本层所有状态在自研扩展表内，迁移不触碰框架原生表。两套状态库共享同一 SQLite 连接以减少写冲突，但表空间分离。
2. **证据强约束**：模型口头报告不构成证据；一切结论必须挂接内容寻址、哈希校验过的命令输出。
3. **幂等贯穿**：提交幂等键（sha256）、尝试键幂等写、回执幂等查询，保证重试/崩溃恢复不产生副作用放大。
4. **预算纪律**：全局账本 + 每题会话控制 + 工具级预留扣减；80% token 触发上下文压缩而非硬中断。
5. **fail-closed 优先**：未知协议、未配置策略项、缺 SDK 方法、下载限额为 0 等，一律拒绝而非降级；唯一例外是模型换挡配置（fail-open 保持可用性）。
6. **可观测**：事件表追加写 + 仪表盘快照 + 命令日志（argv/截断标记/证据 id）+ 门控决策审计表。

---

## 15. 测试与验证基线

- 单元/集成/混沌三层共 **230 个用例**全部通过（本仓库历史基线）。
- `ToolRegistry` 内建 **22 个结构化工具**（18 个核心 + 平台周边）。
- 本层静态检查：全部 Python 文件通过 `py_compile`；`ctf_ext` 与内嵌框架的公开类/方法均有中文 docstring（AST 扫描 0 缺失）。
- 运行时验证需容器环境（本机无 Docker 时仅可静态验证，见 README 运行章节）。
