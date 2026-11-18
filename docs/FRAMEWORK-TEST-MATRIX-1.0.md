# 工程框架测试用例 1.0 · 覆盖与实测记录

日期：2026-11-18（增量）。工程目录：ctf-agent。实施解释见 [架构实施记录](FRAMEWORK-OPTIMIZATION-1.0.md)。

## 本轮结论

- 改动前基线：79/79 通过。
- 当前全量 **205/205**（unit 184 / integration 17 / chaos 4）。本增量新增 9 项：Planner 3 项、调度批量查询 3 项、配置校验 3 项；并用 Python 3.9.6 的零依赖 runner 实跑。
- 完整本地 runner：语法、直接依赖锁、205 项检查、2 Worker / 8 Intent SQLite 模拟压测和预检通过；预检 11 PASS / 4 WARN / 0 FAIL。Python 3.9.6 与 3.12.16 均为 204/204；pytest、ruff/mypy 与 YAML 配置分支本轮未实跑。
- 20 题离线夹具：15 个有效答案全部 ACCEPTED，5 个对抗输入均未提交，0 错误提交。命令日志 30 条。它不是按赛制全时长的真实题集模拟。
- 真实子进程 os._exit(23) 后，DRAFT 恢复提交且不重复解题；SENDING 恢复只查询、零 POST。
- 真实 CLI --mode fallback 通过，输出见 build/framework-fallback.json。
- 不调用付费模型、不联网、不向正式平台提交。当前无 Docker CLI；PyYAML、requests、pytest 在第二套 Python 环境中也未安装，使用现有零依赖运行器与 Fake Provider。

**准出判定：未达成正式参赛准出。** 自动化用例通过不代表文档全部 P0 已通过：H 真容器隔离、K 全真赛制、L-03 冻结纪律等未验证。真实首杀转化率及 P1 ≥90% 不可从 Mock 推断。

## 2026-11-18 增量

| 改动 | 验证 |
|---|---|
| 黑板公开 `transaction()` / `connection` / 命名查询，生产代码去掉 `_db`、`_write()` 直连 | `test_named_queries_cover_scheduler_and_runtime_needs`；全量回归 |
| 调度器每轮聚合查询 + `StateStore.load_many()`，消除按题 SQL | 12 道题上断言 SELECT ≤ 5：`test_scheduler_uses_constant_number_of_queries` |
| `_execute_claim` 拆分并接入最小 Planner | Planner 触发/上限/blocked/confirmed 3 项单测；端到端 no_signal 后新增 Intent |
| 去重：删除重复 `Submission`、占位 official adapter、生产未用 `Submitter` | 全量回归；离线适配器测试改用 `tests/framework_submitter.py` |
| 清理未接线配置键并新增校验 | `test_planner_limit_and_competition_sandbox_are_validated`、`test_removed_aliases_are_not_injected` |

## 复跑与证据

~~~bash
cd ctf-agent
python3 scripts/run_checks.py
python3.12 scripts/run_checks.py
bash scripts/ci.sh
~~~

日志：

- build/framework-checks-py39.log
- build/framework-checks-py312.log
- build/framework-ci.log
- build/framework-fallback.log / build/framework-fallback.json

build/ 保持 Git 忽略，不提交数据库、flag、凭据或生成结果。测试夹具和断言保存在以下源码，可脱离上述历史日志重新执行：

- tests/framework_support.py：临时数据库、实际固定命令、Fake 审阅与 Fake 回执。
- tests/unit/test_framework_gates.py：14 项，校验、注入、聚焦窗口、归档篡改。
- tests/unit/test_framework_gateway.py：11 项，Outbox、异步、恢复、幂等、格式修正和事务回滚。
- tests/unit/test_framework_control.py：17 项，调度、状态、预算、缓存、隔离参数及有界采集。
- tests/unit/test_framework_safety.py：6 项，默认隔离拒绝、凭据不继承、真实心跳、8 路调度与旧库恢复。
- tests/unit/test_framework_skills.py：5 项，索引不读正文、领域选择、字节预算、缓存、路径穿越降级。
- tests/unit/test_framework_lessons.py：7 项，脱敏、幂等、同领域共享、跨领域隔离、空信号与交接包脱敏。
- tests/unit/test_framework_memory.py：5 项，技能注入、交接包截断、状态压缩、技能索引损坏降级。
- tests/unit/test_framework_timeout.py：8 项，主线程超时、可重试失败、契约失败不停题、写入/网关/连续异常隔离。
- tests/unit/test_framework_recovery.py：6 项，心跳、supervisor 策略、工作区 0700/symlink、证据事务与压缩预算。
- tests/integration/test_framework_hardening.py：5 项，超时有界、退避窗口、执行报告、心跳刷新、重启记忆。
- tests/integration/test_framework_acceptance.py：8 项，主入口、20 题、保底与配置。
- tests/chaos/test_framework_process_recovery.py：2 项，真实进程骤停的 DRAFT / SENDING 恢复。
- tests/unit/test_llm_tool_loop.py：2 项，工具输出写入 command_log 并支撑 L1；越界 cwd、更换命令、路径穿越均拒绝。
- tests/unit/test_company_gateway.py：新增 2 项，声明工具随请求发送并解析 `tool_use`；`web_search` 定义在传输前被拒。
- tests/unit/test_tool_catalog.py：2 项，18 个工具的固定 argv 构造与越界/非法参数拒绝；execute 类工具标记为非幂等。
- tests/unit/test_llm_worker.py 与 test_contract.py：候选答案必须形如 `flag{...}` 才能通过契约（格式错误进入修复轮）。

## 对 11 个风险问题的结论

| # | 问题 | 结论与证据 |
|---|---|---|
| 1 | 异常覆盖 | 已覆盖单题超时、Worker 异常、契约失败、Gate 超时、Gateway pump、结果/事实/教训写入和连续调度异常；单次失败不停止题目，连续 3 次调度异常才 `run_aborted`。测试：test_framework_timeout.py、integration/test_framework_hardening.py。`KeyboardInterrupt` 仍按设计向上抛。 |
| 2 | Skill 加载与解题速率 | 已接索引先行、领域/关键词选择、最多 2 节、8KB/12KB 预算、缓存和损坏降级；`skills_loaded` 写入 `resource_usage`。测试：test_framework_skills.py、test_framework_memory.py。只内置 2 节，真实模型提速未测。 |
| 3 | 卡死与外部阻塞 | 主线程 `ClaimTimeout` 到点释放，Gate 超时、Provider 超时、LeaseHeartbeat、提交线程与 supervisor 心跳均有独立路径。测试：test_framework_timeout.py、test_framework_recovery.py、integration/test_framework_hardening.py。非主线程/C 扩展与真实平台网络取消未验收。 |
| 4 | 重启恢复 | SQLite 会话/尝试/结果/Outbox/证据/教训持久化；过期租约回收；真实进程 `os._exit(23)` 后 DRAFT 继续、SENDING 只查询。测试：test_framework_process_recovery.py、integration/test_framework_hardening.py。工具调用中途断点未做。 |
| 5 | 解题过慢 | 已有状态压缩、技能预算、阈值路由和命令缓存，但无真实题集时延/步数基准；不能把 205 项离线通过当成速度问题已解决。Planner 仅做确定性离线验证。 |
| 6 | 并行与瓶颈 | 固定串行，报告 `effective_parallel=1`；`max_workers=4` 只是容量上限。SQLite 写入、模型全局限额和单题消费者仍是瓶颈；8 路 SQLite 压测只验证不超卖。 |
| 7 | 沙箱隔离与 Agent 通信 | 每题工作区 0700、拒绝 symlink、`SandboxSpec` 参数失败关闭；本机无 Docker，H-02/03/04 未实测。Agent 无直连通信，仅共享黑板/教训交接包。 |
| 8 | 记忆落盘 | 会话 JSON、尝试、教训、证据、提交 Outbox 均可从 SQLite/内容寻址文件恢复；证据 fsync + 原子替换，目录 fsync。测试：test_framework_recovery.py、test_framework_lessons.py。 |
| 9 | 跨会话记忆 | 同一数据库重复 `build_runtime` 可恢复步骤、路线、证据和教训，且不重复提交；没有跨运行自然语言长期记忆或向量检索。测试：integration/test_framework_hardening.py。 |
| 10 | 失败经验的跨 Agent 共享 | 教训蒸馏为成立条件/失败原因/已排除/下一步，先脱敏 FLAG；只按同 `category` 检索，交接包限制 6 条且不返回其他题号或候选答案。测试：test_framework_lessons.py、test_framework_memory.py。当前只有一个串行 Worker，真实多 Agent 共享收益未测。 |
| 11 | 还需补充的测试 | 真实 Docker 隔离、真实 deepseek-flash 工具循环、官方 Harness/回执、4 小时全赛程、20 道真实题、非主线程超时、提交线程网络卡死、工具调用中途崩溃、上下文窗口极限、上下文压缩后继续解题。 |

## 逐项映射

“通过”只指下述离线构造与断言。“部分”表示有代码与离线证据，但没有达到原文全部环境/行为标准；不将部分计作准出通过。“未验收”不等于测试已执行失败。

| 用例 | 本轮输入 / 实际结果 | 判定 | 自动化证据或剩余项 |
|---|---|---|---|
| A-01 | 默认 base64 题经真实固定命令、FLAG、校验、异步提交 ACCEPTED，无人工 | 离线通过 | test_A01_A02_K04_full_entrypoint_has_traceable_gates_and_metrics；官方端到端未验收 |
| A-02 | 记录 gate → command_id → argv / 原始输出 / SHA256，篡改归档后拒绝 | 通过 | test_C02_exact_hit_uses_no_llm_and_records_trace、test_A02_tampered_archive_cannot_ground |
| A-03 | 注入有普通证据但无命令来源的候选，L4，零 submissions | 通过 | test_A03_main_entrypoint_rejects_injected_ungrounded_candidate |
| B-01 | 实际第 30 步状态变 hinted，route_changed 含提示 | 通过 | test_B01_B02_B03_steps_drive_persisted_routes |
| B-02 | 实际第 50 步变 alternate，仍为公司固定模型 | 部分 | 同上；未引入其他模型，真实解题引擎执行新策略效果未验收 |
| B-03 | 第 70 步 deep_water，同题最多一槽；100 步不新增调用 | 部分 | test_B03_deep_water_has_one_slot_and_heartbeat_none；低成本模型与算力收益未实测 |
| B-04 | 两道均标简单，一道已 70 步，优先执行另一道 | 通过 | test_B04_mislabeled_easy_challenge_is_demoted_by_actual_steps |
| B-05 | 开局简单题权重高于同优先级难题 | 部分 | test_B05_opening_window_weights_simple_challenges；真实首杀目标/名次未提供 |
| B-06 | 已确认 DRAFT 在下一次 pick_next 前已派发 SENDING | 通过 | test_B06_confirmed_outbox_is_dispatched_before_new_work |
| C-01 | FLAG 与普通附件匹配但无可信命令日志，L4 拒绝 | 通过 | test_C01_missing_command_evidence_is_L4 |
| C-02 | 完整逐字唯一命中 L1；审阅器设置为调用即报错仍确认 | 通过 | test_C02_exact_hit_uses_no_llm_and_records_trace；三门为代码判定 |
| C-03 | 大小写或方括号包裹差异 L2，3 票 + 全部追问才确认 | 通过 | test_C03_case_and_wrapper_hits_require_all_reviews |
| C-04 | session-id/诱饵/多候选/低熵，L3 不提交 | 通过 | test_C04_decoy_low_entropy_and_ambiguity_rejected |
| C-05 | 部分前缀、前后嵌入、更长串不构成逐字来源 | 通过 | test_C05_partial_or_embedded_match_is_not_grounding |
| C-06 | 三票中两票反驳，拒绝，不进入追问确认 | 通过 | test_C06_majority_negation_vetoes_candidate |
| C-07 | source/unique/format 任一 false 都拒绝；仅给聚焦窗口 | 通过 | test_C07_every_interrogation_item_is_required、test_C07_reviewer_receives_only_focused_window_and_no_other_commands |
| C-08 | 无服务、超时、畸形返回、过期结果均 pending | 通过 | test_C08_missing_timeout_or_malformed_reviewer_stays_pending、test_C08_late_review_result_cannot_confirm；真实服务未调用 |
| C-09 | 缺 FLAG / 符号链接 / 声明不符，拒绝入队 | 通过 | test_C09_requires_exact_regular_FLAG_declaration |
| D-01 | Fake 平台显式要求大写，修正后接受；不等价答案与第二次拒绝不再重试 | 离线通过 | test_D01_explicit_platform_format_correction_retries_once、test_D01_invalid_correction_or_second_rejection_never_loops；官方映射未提供 |
| D-02 | Fake REJECTED 生成含建议的 submission_diagnosis，零盲目重发 | 通过 | test_D02_rejection_has_diagnosis_without_blind_retry |
| D-03 | 重复候选只发送一次；进程中断 DRAFT/SENDING 分别处理；多网关不重复 POST | 通过 | gateway 单测、safety 多网关、process_recovery 双路径 |
| D-04 | 提交线程被事件阻塞，主线程可立即领取另一任务 | 通过 | test_D04_slow_submission_does_not_block_main_scheduler |
| D-05 | 同题第二个已校验候选返回原任务键，不新增提交 | 通过 | test_D03_duplicate_and_D05_second_candidate_not_enqueued |
| E-01 | 50 条路径压缩为 10 条热状态，原 state 与命令输出均可精确恢复 | 通过 | test_E01_E04_compaction_keeps_exact_state_and_command_archives |
| E-02 | 新连接恢复第 50 步 alternate 路线及证据，不清零 | 部分 | test_E02_E03_fresh_connection_recovers_steps_route_and_evidence；真实跨模型因公司策略未执行 |
| E-03 | 实际子进程退出码 23；重启精确恢复并处理 Outbox | 通过 | tests/chaos/test_framework_process_recovery.py；无真实模型工具循环恢复宣称 |
| E-04 | 从 state_archive 读出与压缩前 JSON 完全相同的对象 | 通过 | test_E01_E04_compaction_keeps_exact_state_and_command_archives |
| E-05 | 固定 schema、无效字段值拒绝，CAS 旧版本拒绝 | 通过 | test_E05_schema_and_stale_revision_are_rejected |
| F-01 | 同参达到重复限制触发 LoopDetected，停止并记事件 | 通过 | test_F01_repeated_call_signature_intervenes |
| F-02 | 80% Token 先压缩，超预算阻止后续调用；工具数与截止时间也限制 | 通过 | test_F02_budget_compacts_before_exhaustion_and_stops_calls、test_F02_deadline_and_tool_budget_are_enforced |
| F-03 | 长期无验证进展停止；confirmed 禁止新调用 | 通过 | test_F03_stall_and_confirmed_session_stop_work |
| G-01 | 同参第二次命中，替换 capture 为报错也不调用子进程 | 通过 | test_G01_cache_hit_skips_subprocess |
| G-02 | 目标/环境键改变不复用；过期/篡改证据无命中 | 通过 | test_G02_G03_cache_identity_contains_target_environment_version_and_input、test_G02_expired_or_tampered_cache_is_not_used |
| G-03 | 工具版本变化导致缓存键变化 | 通过 | test_G02_G03_cache_identity_contains_target_environment_version_and_input |
| H-01 | 每题目录不同；跨题命令证据不能 Grounding | 部分 | test_H_sandbox_spec_is_fail_closed_and_has_all_isolation_flags、test_A03_cross_challenge_and_incomplete_evidence_rejected；真容器副作用隔离未验收 |
| H-02 | SandboxSpec network=none；默认要求沙箱的本地执行被拒绝 | 未验收 | 参数/失败关闭有测试，但本机无 Docker，未实测容器外联 |
| H-03 | 参数 user 10001 / read-only / no-new-privileges；子进程不继承模型 Key | 未验收 | safety/controller 单测；真实提权/根文件系统写入实验未执行 |
| H-04 | 参数 cap-drop ALL | 未验收 | 参数断言不替代实际内核 capability 验证 |
| I-01 | 控制器支持固定同模型换策略，不允许其他模型 | 未实现原标准 | 公司限制只允许 deepseek-flash，不擅自启用强模型升级 |
| I-02 | 路线固定于数据库，修改阈值重启拒绝；热改模型/控制器被拒绝 | 通过 | test_I02_L03_route_is_pinned_across_restart、test_invalid_hot_config_and_missing_quota_fail_closed |
| I-03 | 人工切 --mode fallback 可复用状态、单槽、继续 Mock 简单题 | 部分 | test_L01_L02_fallback_reuses_state_and_preserves_full_gate_chain；不是多模型升级失败的自动回退 |
| J-01 | 默认且强制 parallel_agents=1，3 路配置拒绝 | 不适用（未启用） | test_I02_J01_parallel_is_explicitly_disabled_until_acceptance |
| J-02 | 不启用多路竞胜 | 不适用（未启用） | 单引擎全真验收前不增加并行付费路径 |
| J-03 | 无真实并行/单引擎成绩对比 | 不适用（未启用） | 不将 SQLite 压测当作 Agent 对冲收益 |
| K-01 | 20 题结构变化夹具 + 5 对抗场景，15 接受、0 错交 | 部分 | test_K01_offline_twenty_cases_including_five_adversarial_inputs；真实题集/全赛程/首杀目标未验收 |
| K-02 | 8 路真实 SQLite 连接同时调度，容量 1 时只一项 RUNNING；CI 压测 8 Intent 零重复/错误 | 部分 | test_K02_parallel_schedulers_atomically_respect_project_cap；真实模型资源利用率未验收 |
| K-03 | 持久状态、心跳、步数门禁与 long_sessions 报告已覆盖 | 部分 | controller/safety 测试；全赛程长会话卡点分析未执行 |
| K-04 | 平均步数、否决率、提交率/延迟、恢复事件可查；首杀率明确 null | 部分 | test_A01_A02_K04_full_entrypoint_has_traceable_gates_and_metrics；缺官方名次与真实恢复成功率分母 |
| L-01 | --mode fallback 显式切换，使用原库线索与全部校验链 | 离线通过 | test_L01_L02_fallback_reuses_state_and_preserves_full_gate_chain；不绕过 official 拒绝启动 |
| L-02 | 真实 CLI 保底 Mock 简单题 ACCEPTED | 离线通过 | build/framework-fallback.json；正式题目保底还需正式 Harness 与容器 |
| L-03 | 路线固定、防热改；既有 freeze.sh 可留档 | 部分 | 路线测试通过；未得到开赛时间，未执行正式 24 小时冻结，脚本不是变更审批系统 |

## 本轮发现与处理的缺陷

| 缺陷 | 级别 | 修复/验证 |
|---|---|---|
| 主流程绕过 Gate 直接提交，附件或模型自报可成为候选来源 | P0 | 主入口只经 Gate + Outbox；注入无证据候选零提交 |
| Gate 决策插入 8 个占位符对应 7 列，真实写入失败 | P0 | 更正为 7；每条端到端候选真实落库 |
| 结果完成与发出之间断电导致答案永久不发送 | P0 | 结果/Gate/Outbox 同事务；事务中断全部回滚，真实进程中断恢复 |
| 旧 Submitter 无数据库时发送异常未保留已发送事实 | P0 | 生产路径已删除 `harness/submit.py`，正式提交只走 `SubmitGateway`；离线适配器测试 helper 保留相同状态机，并由 gateway/chaos 测试覆盖 SENDING/UNKNOWN 只查询。 |
| Scheduler 每题重复查询（N+1） | P1 | 聚合查询 + `load_many` + 批量元数据；12 题断言 SELECT 次数不随题数增长。 |
| 跨模块直接访问黑板 `_db` / `_write` | P1 | 公开 `transaction()`、只读 `connection` 与命名查询；生产代码迁移完成。 |
| 配置含未接线键，易被误认为生效 | P2 | 移除 `run.*`、`per_strategy_seconds`、`tool_timeout`、`grace_seconds`、`allow_http/https` 等；新增 Planner 上限与 competition sandbox 校验。 |
| 工具输出先全量缓冲、EOF 后未等待进程退出 | P0/P1 | 统一有界采集、超时杀进程组；正常成功码等待；超限不作证据 |
| 本地命令执行默认被误当作安全沙箱 | P0 | 默认 sandbox_required 时拒绝本地执行；只有显式本地测试可绕开容器要求 |
| 租约过期仍可写回或心跳复活；调度检查与领取并非原子 | P1 | 增加到期约束、独立连接心跳、事务内容量复核及 8 路并发测试 |
| 热更新数值/结构错误抛出；删除配额保留旧额度 | P1 | 非法配置拒绝且保留旧值；缺额重置 0；JSON 分支实测 |
| 运行时连接默认自动事务，证据登记留下未提交事务，后续 `BEGIN IMMEDIATE` 报 `cannot start a transaction within a transaction` | P0 | `build_runtime` 改 `isolation_level=None`；证据登记只在自身事务提交；回归 `test_evidence_registration_does_not_commit_board_transaction_early`，集成/混沌复跑通过 |
| Worker 异常或契约失败把整题永久 `stopped` | P0 | 单次异常/契约失败改为可重试失败，保留状态；仅预算/停滞策略决定终态 |
| 单题超时后立即重领，单题饿死其他题、故障时热循环 | P1 | 超时写 FAILED_RETRYABLE，增加 `retry_backoff_seconds` 冷却；`has_waiting_work()` 让主循环等待而非退出 |
| 模型超时/限流返回 `blocked`，黑板当 READY 立即重领 | P1 | LLM Worker 改为 `failed` + 退避；Provider 测试同步更新 |
| 技能索引存在但从未被 Worker 调用；教训蒸馏/检索未接线 | P1 | `LlmWorker` 注入最多 2 节技能；`Orchestrator` 每 Attempt 蒸馏教训，`handoff` 注入同领域教训 |
| 上下文压缩把指针计入预算，可能超出硬上限 | P1 | `build_context` 预算硬收敛并保留 `[summary:*]` 指针；`test_context_budget_never_exceeds_limit_and_keeps_pointer_ids` |

过程记录：新增测试第一次合并后 130/131 通过，旧 test_command_allowlist 依赖默认可本地运行，按新的失败关闭边界改为显式 sandbox_required=False；新增独立测试保证默认 True 必须拒绝。既有 echo 注册测试也显式声明本地测试模式。未放宽生产默认权限，最终双 Python 与 CI 全通过。
