# 工程框架优化 1.0 · 实施记录

日期：2026-11-17。依据用户提供的《工程框架优化 1.0》和《工程框架测试用例 1.0》。

## 结论与范围

本轮完善的是现有 ctf_ext 轻量控制器，不重写 vendored Cairn 的 ReAct/上下文运行时。不调用真实模型、不向官方平台提交、不安装依赖、不推送代码。仓库已有未提交改动原样保留并增量接线；本轮新增 48 项回归，测试总数 **186/186**，最终日志与逐项结论见 [验收矩阵](FRAMEWORK-TEST-MATRIX-1.0.md)。

**本地离线验证通过，不等于正式参赛准出。** 官方 SDK/协议、真实模型与工具循环、生产每题容器、按赛制全时长模拟仍未验收。

## 架构与真实调用链

~~~text
CLI build_runtime
  ├─ StateStore：固定 JSON schema / CAS / 命令与证据归档 / 缓存
  ├─ Scheduler：实际步数 + 开局权重 + 数据库内原子并发门禁
  └─ Orchestrator
       ├─ LeaseHeartbeat：独立 SQLite 连接续租，覆盖执行和校验阶段
       ├─ SessionRunner（现有 Worker 协议）
       │    ├─ RuleBasedWorker：仅 Mock 的固定离线解码程序
       │    ├─ SkillLoader：索引先行、领域/关键词选择、最多 2 节、字节上限、缓存
       │    └─ LlmWorker：现有 Provider 契约、状态交接、技能注入与预算限制
       ├─ FlagGate：FLAG 声明 → Grounding → Negation → Interrogation
       ├─ 单事务：结果 + GateDecision + submission_jobs / DRAFT
       ├─ ClaimTimeout：主线程 SIGALRM 单题硬超时，超时写可重试失败
       └─ SubmitGateway
            主线程 SQLite Outbox → 守护线程传输 → 回执队列
            DRAFT → SENDING → ACCEPTED / REJECTED / UNKNOWN
            SENDING / UNKNOWN：只查询；绝不猜测是否送达并重发
~~~

### 1. FlagGate / 声明式提交（R1/R2）

- 只接受工作区内唯一、限长、非符号链接/非硬链接的 FLAG 文件，与 Worker 候选必须一致。
- 只认当前题的完整、退出码为 0 的真实命令输出；普通附件、模型自报和 hash-only 不可充当 Grounding。
- 拒绝前缀/嵌入串、低熵、session-id、request-id、诱饵及多候选歧义。
- **文档冲突解释**：4.1 要求三门，4.2 和 C-02 又要求 L1 不耗 LLM。本实现保留三门布尔结果：L1 的完整逐字、唯一、非诱饵命中走代码判定；L2 大小写/包裹归一必须三票反驳与 source/unique/format 追问。若赛事要求 L1 也调用模型，应在赛前修改策略并重验，不能赛中静默更改。
- 审阅异常/格式异常/超时、校验途中 FLAG 改变均不得 confirmed；模型只能看来源命令及聚焦输出窗口。
- 修复原有 gate_decisions 插入占位符数目错误；保存来源 command_id 与 evidence_ids，支持回放。

### 2. SubmitGateway（R7）

- 主循环不执行平台网络调用，不做阻塞式重试；最末只等待最多 1 秒且受剩余总预算限制，未完成任务持久化保留。
- 结果与已确认候选 Outbox 同事务，解决“结果已完成但尚未发送就崩溃”的丢提交窗口。
- DRAFT 只有抢占成功后可 POST；SENDING、UNKNOWN 重启只查询。旧库不确定记录也只挂接查询，不补造校验。
- 同题确认后不再接收第二候选；发送前再次校验声明、命令及归档哈希。
- 格式修正仅接受平台结构化 corrected_answer，且只允许保持归一值不变的大小写/包裹修正；最多另发一次。不从自然语言错误猜测新 flag。官方协议尚无字段映射，不能宣称真实平台格式重试已验收。
- REJECTED 记录诊断与建议；异常正文不写入未知传输诊断，避免泄漏凭据。
- 守护线程不共享 SQLite 连接；正式适配器仍必须落实有界网络超时。当前是一条传输通道，平台长期卡死不会阻塞解题，但会延迟其后的发送；不能把它称为高可用多通道网关。

### 3. StateStore / LoopDetector / Cache（R4/R6）

- 固定状态包含题目、步数、已试路径、证据指针、假设、Token/工具/时间预算、路由、签名和状态；CAS 防止过期版本覆盖。
- 原始输出内容寻址，恢复核对哈希与路径；压缩先精确归档旧状态，再裁剪热上下文，不以摘要替代原始证据。
- 调度接入 30/50/70/100 候选阈值，持久记录路线与提示；深水区限一槽，心跳阶段不新增调用。
- 公司策略只允许 deepseek-flash，因此 50 步执行**同模型换策略**，未擅自加入其他模型；多 Agent 显式禁止，单引擎验收前不开启。
- 阈值与模型路线绑定运行数据库，重启不允许静默改动；热更新只接受合法可调项，模型/控制器/策略/Harness 本轮固定。
- 同参签名重复或长期无已验证进展触发干预；Token 达 80% 先归档，耗尽停止；LLM 调用使用剩余时间/输出 Token 上限。
- 缓存键包含题目、目标版本、环境、工具名/版本、参数与输入哈希；TTL 或归档失效不命中。实测通过“第二次不启动子进程”证明加速，不依赖不稳定耗时比较。
- 租约写回和续租拒绝已到期所有者；调度容量在 BEGIN IMMEDIATE 内再次判定，避免多连接检查后超卖。
- 单题超时由 `DeadlineGuard` 在主线程触发 `ClaimTimeout`（继承 `BaseException`，不会被 Worker 的 `except Exception` 吞掉）；结果标为可重试失败，不再把整题永久 `stopped`。
- FAILED_RETRYABLE 进入 `runtime.retry_backoff_seconds` 冷却，调度器在冷却期内不重领；主循环仍有开放 Intent 时轮询等待，避免单题饿死其他题，也不会在无任务时空转。
- 单步异常隔离：Gateway pump、结果写入、事实写入、教训写入分别捕获并记事件；连续 3 次调度异常才 `run_aborted`。契约失败只让本次 Attempt 失败，不直接停止题目。

### 4. Sandbox（R3）

- SandboxSpec 校验不可变镜像摘要、真实绝对工作目录与合法名字，固定 network=none、UID/GID 10001、read-only、cap-drop ALL、no-new-privileges 和资源限制。
- 本地 run_command 在 sandbox_required=True 时拒绝执行，不能把白名单、cwd 或进程组当作安全沙箱。
- Mock 固定程序才可显式本地执行；进程输出有界采集，超量/超时结束进程组；默认不向子进程继承模型凭据。
- **未完成生产每题容器执行适配**：当前环境无 docker 命令；本轮只验证参数与失败关闭，不冒充真实网络/权限/跨题隔离验证。正式模式仍禁止启动。
- 每题工作区为 0700，拒绝 symlink；证据文件 fsync + 原子替换，目录 fsync；证据登记只在自身开启事务时提交，避免污染黑板事务。

### 5. Observability / 保底（R5）

- 记录命令、校验级别/原因/各门结果、路由变更、循环干预、恢复及提交回执/诊断。
- 报告 quality 包含平均步数、各门否决率、接受率、发送延迟、恢复事件与长会话清单；未拿到官方名次，first_kill_rate 明确为 null。accepted_first_30m_conversion 仅为平台接受比例，不是首杀率。
- 一键保底：--mode fallback，复用同一数据库/证据，单题单槽，保持完整校验链；仅能处理 Mock 已支持的简单附件题，绝不在 official 下静默降级 Mock。
- 运行报告新增 `execution`：`mode=serial`、`effective_parallel=1`、`configured_max_workers`、`claim_timeout_seconds`、`retry_backoff_seconds`、`isolation.agent=per_challenge_workspace`、`isolation.container=not_verified`。配置中的 `max_workers=4` 只是容量上限，不是并行证据。
- 外部 supervisor 用心跳文件判活并有限重启；它只处理进程级卡死，不改变数据库或提交状态。

~~~bash
python3 scripts/supervise.py --heartbeat build/agent.heartbeat --stale-seconds 90 --max-restarts 3 -- \
  python3 -m ctf_ext run --mode fallback --seconds 300 --heartbeat-file build/agent.heartbeat
~~~

### 6. Skill 与跨题教训

- SkillLoader 启动时只读 `index.json`；按题目 `category` 和名称/描述关键词打分，最多注入 2 节，单节默认 8KB、总计 12KB，重复读取走进程内缓存。
- 索引或章节损坏、路径穿越、超预算都跳过该节并继续解题；不会把技能正文当系统指令执行。
- 每次 Attempt 蒸馏为“成立条件 / 失败原因 / 已排除 / 下一步”，先脱敏 FLAG 再入库；同 `category` 才可跨题检索，返回内容不含来源题号或候选答案。
- Worker 交接包限制近期尝试 5 条、教训 6 条、开放 Intent 5 条；压缩续跑状态只带最近 10 条假设/证据指针，不带完整 evidence 列表。
- 当前只内置 2 个技能章节、单一串行 Worker；机制有离线测试，真实模型解题提速和跨 Agent 共享收益未测。

~~~bash
# 无模型、无官方提交的保底入口；复用原运行库时将路径换成原路径。
python3 -m ctf_ext run --mode fallback --seconds 30 \
  --db build/fallback.db --evidence build/fallback-evidence \
  --report build/fallback-report.json
~~~

## 仍需外部或后续工程完成的事项

1. 官方 Harness 的任务/附件/回执/格式修正字段与真实鉴权超时联调；competition 继续退出 78。
2. 成熟解题引擎接入可信容器命令执行与证据记录；现有 LlmWorker 已有离线只读工具循环，但可信容器执行、真实网关 `tools`/`tool_use` 与完整附件读取未验收，单给 API Key 仍不能正式解题。模型选择受公司固定策略限制，不实现未经批准的升级链。
3. 真实 10–20 题的步数分布校准、生成策略持续运行、全局资源/Token 预算以及 70 步低成本模型资源策略；当前是单模型步数/并发门禁与单题预算，不等同于完整多引擎资源调度。
4. 与比赛一致的容器安全实测；20 题夹具为直读/base64 结构变化而非真实混合解题难度，不可用于参赛成绩推断。
5. 正式开赛时间、计分规则与首杀目标未提供，不能宣称 K-01、P1 ≥90% 或全 P0 准出达成。
6. 冻结前运行 scripts/freeze.sh 留档并审查未提交差异，只允许标明理由的 P0 修复。现有冻结脚本并非强制变更审批系统，L-03 的 24 小时组织纪律仍需实际执行。
7. 当前宿主缺 PyYAML，YAML 解析/热更新分支本轮未实跑；JSON 热更新已覆盖。未自动安装依赖，也未复建容器镜像。
8. 单题硬超时依赖主线程 `SIGALRM`；非主线程、阻塞型 C 扩展或整个进程卡死仍需外部 supervisor/平台超时兜底，不能把 `DeadlineGuard` 当作万能取消。
9. 真正并行执行器未实现；当前串行报告 `effective_parallel=1`，8 路 SQLite 压测只证明容量门禁不超卖。
10. 跨会话记忆只覆盖数据库中的状态、证据、教训和 Outbox；没有跨运行的自然语言长期记忆、向量检索或工具调用中途断点。
