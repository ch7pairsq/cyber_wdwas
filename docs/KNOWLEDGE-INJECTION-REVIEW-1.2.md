# CTF 智能体知识注入评审与优化 1.2

日期：2026-11-19。范围：仅授权 CTF；评审“轻量索引、四要素技能卡、规则回写、阶段提示词、质量闸”这一组建议，对照当前仓库代码与文档给出可落地的优化。本轮只新增本评审文档，不修改运行代码、技能正文、提示词或配置，也不调用真实模型。

## 1. 结论摘要

建议的方向成立：先把知识变成可路由的索引，再把索引指向可执行卡片，最后把赛后经验沉淀成受约束的规则，而不是把 1324 篇 writeup 直接塞进上下文。这与当前仓库的演进方向一致，但仓库现状与建议之间有三处关键偏差。

第一，当前不是“索引先行”，而是“按题目分类预加载最多 2 节正文”。ctf_ext/skills/loader.py:54 的 list_index 只被测试调用，生产路径从未把索引交给 Worker；ctf_ext/runtime/llm_worker.py:299-315 按题目分类和名称/描述关键词选最多 2 节，整段注入，且注入发生在任何工具调用之前。也就是说，模型看到的是“被选中的两节正文”，而不是“有哪些方法可选”。这正是建议第一层要解决的问题，但当前恰好没有接线。

第二，8.9 个百分点的那篇负结果不能被读成“技能无用”。它是对一项 180 次运行、15 道题、单一 Claude Sonnet 4.5、MCP 工具层的既有研究的再分析；No-Skills 基线已有 77.8%，完整技能 86.7%，差值 8.9pp，95% 置信区间 [−6.8, +24.6]，卡方 p=0.71，趋势检验 p=0.25。作者自己写的是“不能声称因果上为零”，并把“环境反馈带宽”作为可证伪假设，而不是已证实的定律。更关键的是，该研究的四个条件都把文档前置加载，明确没有实现 SKILL.md 的按需加载语义；它测试的不是“常驻轻索引 + 按需一两张卡”，而是 12k 到 36k token 的全量文档。用它的数字反对索引加卡片，是把实验条件换掉了。

第三，29 个 genuine solves 的数据也不能证明“深层文档没用”。原文的准确表述是：29 个 genuine solves 都在第一层（pack index in system prompt）暴露，14 个打开了第二层 note，15 个没有打开任何 note。索引是全员暴露，因此“29/29 都看到了索引”是设计使然，不是因果证据；打开 note 是自选择的，难题更可能打开，因此 15 比 14 不能说明 note 无价值。可迁移的是“索引常驻、深层按需”的架构，不是“深层文档可以删掉”。

因此，本仓库真正该做的不是再写一份更长的知识库，而是：让索引真正进入 Worker；把现有 10 节从“散文步骤”改成“可执行工具调用 + 判据 + 踩坑”；用质量闸挡住错误卡片和错误教训；用确定性控制器执行止损；最后才考虑 writeup 蒸馏与语义检索。

## 2. 研究依据核查与边界

### 2.1 “技能只带来 8.9pp”的真实条件

可核验来源：Chacko、Hugglestone、Islam、Liu，《When Skills Don't Help: A Negative Result on Procedural Knowledge for Tool-Grounded Agents in Offensive Cybersecurity》，arXiv:2605.20023v2，https://arxiv.org/abs/2605.20023 。

原文关键事实：

- 180 次运行 = 15 道题 × 4 个文档条件 × 3 次重复；模型固定为 Claude Sonnet 4.5（extended thinking）；工具层是 Nmap、Ghidra、Angr、GDB，经 MCP 严格 JSON schema 返回；每条约 17 到 20 分钟。
- 四个条件分别是 No-Skills 55 行/591 token、Experiential 1478 行/12865 token、Curated 1976 行/17253 token、Comprehensive 4147 行/36001 token。
- 通过率 77.8%（35/45）、82.2%（37/45）、84.4%（38/45）、86.7%（39/45）；相对 No-Skills 分别 +4.4、+6.6、+8.9pp。
- 统计：4×2 卡方 p=0.71；Cochran–Armitage 趋势 Z=1.15、p=0.25；六组两两 Cohen's h 有五组低于 0.2，只有 No-Skills 对 Comprehensive 为 0.23；解题时长 Kruskal–Wallis p=0.77。
- 作者明确写：“We do not claim a causal demonstration of zero effect; we claim only that any effect in this domain is below the resolution of a 180-run controlled study.” 局限包括 N=15、单模型、天花板效应（No-Skills 已 77.8%）、以及条件并非 SKILL.md 的按需加载。
- 时序侧信道子任务：Comprehensive 1/3、Curated 2/3、No-Skills 0/3；作者把原因归为 Experiential 教训把模型推向符号执行，即“false lesson propagation”，并明确说 n=3、只作示例，不作稳健效应。
- 成本：完整条件约 61 倍 token 换 +8.9pp；作者认为即使接受这个点估计，No-Skills 在成本感知下也在 Pareto 前沿。

对本仓库的含义：

- 这项研究支持“不要把 36k token 的文档前置塞进上下文”，不支持“连 500 token 的索引也不要”。
- 它支持“教训蒸馏可能造成负迁移”，因此赛后回写必须有质量闸；这是本评审最强的一条实证支撑。
- 它不能直接外推到本仓库，因为工具带宽、模型、题目难度都不同。原文的 H1 把带宽分为确定性、schema 保真度、时延三个轴；本仓库的输入参数是严格 schema，但工具输出是文本、超时 30 到 120 秒、沙箱默认 fail-closed、没有 Ghidra/Angr，带宽明显低于原文环境。按 H1，本仓库的技能边际价值应该更高，而不是更低。但本仓库模型是 deepseek-flash，能力与 Claude Sonnet 4.5 不同，题目也不是同一套；所以这只是一个待测假设，不能反过来宣称“技能一定有用”。

### 2.2 “29 个解出、15 个没打开文档”的真实含义

可核验来源：Amine 与 Saab Jr.，《Planner-directed multi-agent LLMs with on-demand skill-equipped specialists for CTF-based autonomous penetration testing》，International Journal of Information Security 25:183 (2026)，https://link.springer.com/article/10.1007/s10207-026-01350-x 。

原文关键事实：

- NYU CTF Bench 200 题；平台接受 41 个，经来源审计后 29 个 genuine solves（14.5%）；对照 3/200。
- 技能语料是 CTF-SKILLS，118 个 note、11 个 pack，采用 progressive disclosure：system prompt 只放 pack index，note 由专家按需打开。
- 原文句子：“All 29 genuine solves were exposed at the first layer and 14 at the second, with 15 opening no notes.”
- 升级规则在 200 题中触发 199 题，升级模型占花费 72.5%；匹配预算实验未发现架构主效应（摘要口径 OR 1.17，p=0.34），升级本身才是主要收益来源（OR 8.79）。
- 92.5% 的委派与类别提示一致，说明分类路由有效；但这不等于“技能卡有效”。
- 泄漏审计：在 118 个 note 中检索基准题名、flag 包裹、测试集文件名/主机/端口/常量，CSAW 出现在 31 个 note 中，3 个测试题名带技术名；这是真实的污染风险。

另需区分来源：本轮可核到的是 Springer 文的 29 个 genuine solves 与 CTF-SKILLS 的 118 个 note/11 个 pack；“1324 篇 writeup”和“Claude Opus 4.5 实测全部 29 个解出”不在我核到的摘要与关键段落中。若它们来自其他材料，应单独给出处；不能把 1324 篇、29 个解出、Opus 4.5 三个数字拼成同一实验的结论。

对本仓库的含义：

- “索引常驻”有直接的架构先例；但“29/29 都看到索引”不构成因果证据，因为索引对全部 episode 都可见。
- “15 个没打开 note”不能证明 note 无用；打开行为与题目难度相关，是自选择。真正应学的是“先给目录，再让执行者在需要时打开”，而不是“删掉深层内容”。
- 12 次动作触发升级在 199/200 题触发、占 72.5% 花费，说明“按步数触发”很容易变成普遍升级，不是精准止损。本仓库的 30/50/70/100 也只能作为候选阈值，不能当作普适常数。
- 赛前必须做污染审计；不能把包含基准题名、flag 样例、测试集常量的 writeup 直接蒸馏进卡片。

### 2.3 哪些结论可迁移，哪些不可迁移

| 建议中的说法 | 评审 | 可迁移的修订 |
| --- | --- | --- |
| 技能全量注入无用，应改为轻量索引 | 部分成立 | 反对的是 12k 到 36k token 的前置全量文档；500 token 索引加 1 到 2 张按需卡不是同一条件 |
| 索引要放进 system prompt | 成立 | 但必须是内部、经过质量闸的索引；外部 writeup 文本不能提升为 system 指令 |
| 深层文档没人看，所以可以删 | 不成立 | 29 个解出中 14 个打开了 note；打开是自选择，不能推出无价值 |
| 赛后把轨迹蒸馏成规则 | 有条件成立 | 只能生成候选规则；必须经过重复证据、离线质量闸和可撤销晋升，不能自动进入比赛上下文 |
| 用 30 步提示做止损 | 不成立为通则 | 按步数触发在原文中 199/200 都触发；应改用无新证据、重复签名、预算耗尽和明确子目标失败等可观测信号 |
| 按阶段拆分提示词 | 部分成立 | worker.md 已有五步路径；拆成三份完整提示会重复契约、增加漂移；应做一份基础契约加三段短附加 |
| 质量闸和空白识别 | 成立 | 应提前到写卡片之前，而不是最后；错误卡片的负迁移比没有卡片更糟 |
| 用 RAG 检索 1324 篇 writeup | 暂不成立 | 当前瓶颈不是检索，而是工具可执行性、质量、真实模型联调和评测；RAG 放 P3 |

## 3. 本仓库现状核对

以下均为静态阅读结论；本轮未运行测试，未调用模型。

| 能力 | 位置 | 现状 | 缺口 |
| --- | --- | --- | --- |
| 索引 | ctf_ext/skills/loader.py:54-57 | list_index 返回名称、领域、前置、tokens_hint、风险；不读正文 | 生产路径从未调用；Worker 看不到目录 |
| 选择 | loader.py:65-98 | domain 命中 +6；query 只取 [a-z0-9]+ 的词；关键词命中 +2/+3；默认 limit=2 | 中文题面无法产生词项；domain 单独命中就能占满两个名额；没有触发条件 |
| 加载 | loader.py:100-142 | 单节 8KB、总计 12KB、缓存、路径穿越校验、损坏降级 | load_for 返回 skipped，但调用方丢弃；没有证据触发；没有按轮预算 |
| 注入 | llm_worker.py:150、299-315 | 在第一条 challenge 消息之后注入“内置方法参考”，整段正文保留到本轮结束 | 前置加载；没有索引；卡片会随每轮请求重复计费；没有卸载 |
| 阶段 | llm_worker.py:103-120、196；planner.py:76-84 | 按 route.stage 选 Pro/思考预算；followup 写 params.phase | Worker 不读 params.phase；没有阶段提示词路由 |
| 提示词 | __main__.py:39、229 | 只加载 prompts/worker.md | prompts/planner.md、prompts/handoff.md 存在但无代码引用；不是“已生效” |
| 止损 | control.py:101-155 | max_steps=100；重复签名 3 次停；20 步无新证据停；stage 4 禁新调用 | 20 步停滞可能早于 30 步提示；steps 同时计模型调用和工具调用；没有 warn 到 switch 的中间态 |
| 教训 | lessons.py:44-74、111-153 | 四要素（成立条件/失败原因/已排除/下一步）、FLAG 脱敏、同域检索、6 条 | 只按 source_attempt_id 去重，不按文本；没有上限/多样性；supersede 无调用；最新条目可能挤掉多样经验 |
| 事实 | blackboard.py:492-517 | 写入前校验 evidence_ids；有 confidence | 没有读取方；statement 未脱敏；没有版本/前提/TTL；不能称为已接线的记忆层 |
| 交接 | blackboard.py:446-469；llm_worker.py:318-331 | 5 条尝试、6 条教训、5 个 Intent；逐字符串脱敏；6000 字节检查 | 检查的是裁剪前的 JSON；裁剪后没有复核，仍可能超 6000 |
| 工具 | catalog.py:275-316；tools.py:93-204 | 21 个注册工具；输入严格 schema；错误码枚举；输出原文归档 | 输出是纯文本预览；没有恢复提示；python_exec 可 import subprocess；真实网关 tools 兼容未联调 |
| 文档 | README.md:38、158；docs/TOOLCHAIN.md:126-135；docs/FRAMEWORK-TEST-MATRIX-1.0.md:68、156 | 声称 10 节、18 工具、只内置 2 节等，互相不一致 | 文档漂移；测试矩阵自己已记录“索引存在但从未被 Worker 调用” |
| 测试 | tests/unit/test_framework_skills.py（6 项）；tests/unit/test_framework_memory.py（5 项） | 覆盖索引元数据、选择、预算、缓存、损坏降级、注入、脱敏 | 没有索引可见性、卡片质量、触发精确率、延后加载、handoff 硬上限的回归 |

补充事实：

- 10 节正文合计约 4.9KB，单节 382 到 668 字节；内容偏薄，例如 crypto.rsa.small_e.md 只有 402 字节，forensics.zip.md 只有 382 字节，没有具体 argv、期望输出、失败分支和来源。
- forensics.zip.md 写的是 unzip -l，但注册表里没有 unzip 工具，只有 archive_list（7z l）；crypto.rsa.small_e.md 推荐 gmpy2，但 docs/TOOLCHAIN.md:85 明确说 gmpy2 本轮不加入；crypto.encoding.md 写 openssl enc -d，但注册表没有 openssl 工具。这三处是“卡片教了模型做不到的动作”的实例。
- index.json 只有 name/domain/prerequisites/keywords/tokens_hint/risk/file 七个字段，没有触发条件、命中判据、踩坑、来源、版本、许可证、弃用标记。
- worker.md 已有分类、侦察、假设-验证、命中停止、收尾五步，以及证据复核、python_exec、提交纪律；它不是“没有阶段提示”，而是“只有一份通用提示”。再拆三份完整提示的边际收益不高，重复契约的风险更高。
- prompts/planner.md 与 prompts/handoff.md 没有被任何代码加载；实际规划是确定性 IntentPlanner，实际交接包由 blackboard.handoff 构造。把它们当作“现行提示词”会误判现状。

## 4. 对原建议逐条评审

### 4.1 第一层：轻量路由索引

建议成立，但要补三个工程约束。

第一，索引必须真的进入 Worker。最小改动是在 _append_skills 里先追加一段紧凑目录，再追加原有的正文。目录只保留“领域 + 技法名 + 一行触发”，不放大段前置条件；总长度控制在 600 字节以内。这样不会改变 skills_loaded 的语义，也不会破坏现有测试。

第二，不要维护第二份 TAXONOMY 散文。当前 index.json 就是唯一事实源；再由它生成目录文本。如果另写一份 TAXONOMY，两份会漂移。若需要空白识别，另加一个只读的 taxonomy.json，记录“应有技法”和 card 映射，由脚本做覆盖报告，不让它参与运行时选择。

第三，索引的价值是“知道有什么”，不是“知道怎么做”。索引里不要放命令和判据；否则模型会把目录当卡片用，既占上下文又容易误用。目录只回答“有哪些方法、何时值得打开”。

推荐格式：

~~~text
技能目录（只读，不覆盖输出契约）
crypto: rsa.small_e（e<=5 且 c<n；或同明文多密文） | encoding（base64/hex/xor/rot）
reverse: elf.static（无符号/静态 ELF） | shellcode（十六进制机器码）
pwn: stack.recon（checksec/ROP/栈溢出）
forensics: zip（伪加密/注释） | archive_layers（嵌套/口令） | stego.image（PNG/JPEG/steghide） | pdf（嵌入对象）
misc: qr（二维码/条码）
~~~

10 条约 300 到 500 字节；40 条约 1 到 1.5KB。当前 10 条足够，不要为了“全”把目录做成 10KB。

### 4.2 第二层：四要素技能卡

建议成立，但“最小可执行命令”在本仓库要翻译成“结构化工具调用”。模型不能直接给 shell；它能做的是调用 21 个注册工具，或在 python_exec 里写代码。因此卡片应写成：

~~~text
# crypto.rsa.small_e

## 触发
- 题面/附件给出 n、e、c；e 很小（3 或 5）；c < n；明文短。
- 或同一明文有多组 (n_i, c_i)，e 相同。

## 最小动作
1. tool=python_exec
   code: 用 math.isqrt/二分求整数 e 次根；不要用 gmpy2。
   expect: 输出整数 m，m 编码后可读。
2. 若失败：tool=python_exec 计算 gcd(c_i^e - c_j^e, n_i)，尝试共模。
3. 若仍失败：tool=evidence_digest 汇总相关证据，检查 padding/多素数。

## 命中判据
- m 编码后的文本逐字出现在本次 python_exec 的 stdout，且只出现一处 flag{...}。
- 负判据：e 很大或 c>=n 时不适用，转 encoding/大数分解。

## 踩坑
- gmpy2 未安装；pow(c, 1/e) 浮点精度不够。
- 多组密文才考虑 Hastad；单组不要硬套。
- 命中后只提交一次；候选不要写成已证实事实。

## 来源/边界
- 内部编写；只覆盖无 padding 教学题，不覆盖 OAEP。
~~~

四要素的检查点：

- 触发条件必须是“可观察的”：文件类型、工具输出、题面字段、附件扩展名；不能写“遇到密码题”。
- 最小动作必须是“可执行的”：tool 名必须在 default_specs 中；python_exec 代码只能用镜像内库；不能写 unzip、openssl、gdb 这类没有注册为工具的命令。
- 命中判据必须是“可核验的”：逐字命中、唯一性、来源 evidence_id；不能只写“得到 flag”。
- 踩坑清单必须包含“负判据/停止条件”：什么情况下这张卡不适用，避免模型硬套。

卡片长度建议单张不超过 2KB，一次只加载 1 张。当前 10 节内容太薄，改造时应优先补“具体工具调用、期望输出、失败分支”，而不是扩写背景知识。

### 4.3 第三层：规则回写

建议成立，但必须把“模型自主回写”降级为“候选规则 + 受控晋升”。原文的时序侧信道例子说明：从少量轨迹里提炼出的“教训”可能把模型推向错误方法（符号执行），造成负迁移；SkillsBench 自生成技能的平均效应也只有 −1.3pp（这是原文引用的数据，不是本仓库实测）。因此：

- 一次失败最多生成一条候选规则，写入 status=candidate；不进入 Worker 上下文。
- 候选规则必须有触发条件、动作、证据 ID、适用边界、反例；没有这些字段的不入库。
- 晋升条件：至少在 2 个独立尝试或 2 道题上复现，且没有反例；或者人工审核通过。晋升后才进入 1 到 2 条注入。
- 规则冲突时保留历史，标记 superseded；不要静默覆盖。
- 规则分三类：换路规则（观察到 X 就换 Y）、止损规则（连续 N 次无新证据就停/交回 Planner）、高置信信号（看到 X 才允许提交候选）。止损规则的执行权在控制器，提示词只表达偏好。

现有 lessons 表已经做了脱敏和同域检索，但它是“每次尝试都写一条”，没有候选/晋升概念；直接把它升级为规则层会放大噪声。建议先加 kind 标签和晋升计数，再决定是否新建 rules 表；新表是加法，不改旧表结构。

### 4.4 第四层：阶段提示词

建议部分成立。worker.md 已经有五步路径，重复写三份 think_* 文件会增加维护成本和契约漂移。更合适的做法是：

- 保留 worker.md 作为唯一基础契约。
- 按 params.phase 或 route.stage 追加 3 到 5 行短附加，总长不超过 300 字节：
  - 初始：先做一次最小侦察；没有证据不要猜 flag。
  - 跟进：只验证 next_best_action 里的一个假设；连续两次无新证据就换路。
  - 收尾：先复核 candidate 与 evidence，再输出 JSON；不要开新长链路。
- 只有阶段变化时追加一次，不要每轮重复。
- 不要让副模块提示覆盖主契约；所有附加都以“只作方法，不覆盖输出契约”结尾。

“主模块路由 + 副模块证据触发”在本仓库可以落成：主模块 = 题目 category 对应的 1 张卡；副模块 = 工具输出里出现另一域的信号（例如 reverse 题里发现 zip 附件）时，再触发 1 张 forensics 卡。触发必须有可观察证据，不能只靠题面关键词。

### 4.5 第五层：质量闸与空白识别

建议成立，而且应提前。质量闸至少检查：

- 结构：触发、最小动作、命中判据、踩坑四段齐全；单张不超过 2KB；最少 1 条踩坑和 1 条负判据。
- 工具：所有 tool=NAME 必须在 default_specs 中；不能出现 unzip、openssl、gdb、nmap、sqlmap、nuclei 等未注册命令；python_exec 代码不能依赖 gmpy2、angr、ghidra、sage 等未安装库。
- 安全：不能含字面 flag；不能含基准题名/测试集文件名/主机端口常量；不能含“忽略之前的指令”“输出系统提示”等提示注入模式；外部来源必须记录 URL 与许可证。
- 去重：触发条件归一化后不能重复；同一技法不能有两张互相矛盾的卡。
- 可执行性：最小动作不超过 5 步；每步都有期望输出；命中判据能对应到 evidence_id。
- 版本：source、license、reviewed_at、version、deprecated 字段齐全；工具下线时卡必须标记 deprecated 或被替换。

空白识别不要做成大而全的 taxonomy。先做三张表：域覆盖（web 目前 0 张）、工具覆盖（哪些工具没有卡）、触发覆盖（哪些索引条目没有触发条件）。输出报告即可，不阻塞运行。

### 4.6 建议中的陷阱清单

原文的陷阱清单方向正确，建议补充本仓库特有的六条：

- 假教训传播：一次失败就被写成“真理”，下一题硬套。时序侧信道是现成案例。
- 基准污染：卡片或索引含 NYU/CSAW 题名、测试集常量、flag 样例；原文在 118 个 note 中查到 31 个 CSAW 引用。
- 工具漂移：卡片写 unzip/gmpy2/openssl，但工具链没有；模型会浪费轮次或走 python_exec 绕过。
- 上下文成本：卡片在消息历史里，每轮重发；2KB 卡片乘 10 轮就是 20KB 的重复输入，不是“加载一次”的成本。
- 过早止损：20 步无新证据就停，可能早于 30 步提示；对长链路题会误杀。
- 提示词越权：把“停止/提交”写成自然语言规则，但控制器没有对应状态；模型可以口头遵守，也可以不遵守。

## 5. 优化后的落地方案

### 5.1 分层

- L0 常驻：21 个工具定义 + 600 字节以内的技能目录。工具定义已经存在；目录是本轮要补的。
- L1 按需：1 张卡，最多 2KB，在题面或工具证据触发后加载；不在第一轮预加载。
- L2 规则：只注入已晋升的 1 到 2 条规则；候选规则只进审计表。
- L3 证据/事实：默认只写审计；若要展示，必须同题、脱敏、标注“未复核”。

### 5.2 数据模型

index.json 增加可选字段，不破坏旧条目：

~~~json
{
  "name": "crypto.rsa.small_e",
  "domain": "crypto",
  "triggers": ["e<=5", "small exponent", "小指数", "同明文多密文"],
  "hit_criteria": ["整数 e 次根可读", "flag{...} 唯一"],
  "tools": ["python_exec"],
  "prerequisites": ["python3"],
  "tokens_hint": 400,
  "risk": "compute",
  "file": "crypto.rsa.small_e.md",
  "source": "internal",
  "license": "internal",
  "reviewed_at": "2026-11-19",
  "version": 1,
  "deprecated": false
}
~~~

兼容性：loader 对缺失字段用 get 默认值；旧 index 仍可加载。list_index 改成只用 get，不再对 prerequisites/tokens_hint/risk 做硬索引，避免新增可选字段时 KeyError。

规则记录建议：

~~~text
id, challenge_id, kind(switch|stop|signal), trigger, action,
confidence, evidence_ids, status(candidate|promoted|retired),
observations, created_at, promoted_at, hits
~~~

新表用 CREATE TABLE IF NOT EXISTS，不改旧表；没有读取方时不影响现有流程。

### 5.3 运行时改动（按风险从低到高）

1. SkillLoader 增加 index_text()：由 index.json 生成紧凑目录；跳过 deprecated；对缺失字段用默认值；输出总长受 max_bytes 限制。list_index 保留给测试/工具。
2. LlmWorker._append_skills 先追加目录，再保留原有正文注入；resource_usage 增加 skills_indexed（名称列表）和 skills_skipped（原因计数）。这一步不改变 skills_loaded，现有测试可通过。
3. 增加可选 skills.mode：preload（默认，保持现状）、index（只注入目录）、deferred（目录 + 证据触发 1 张卡）。默认不变，A/B 后再切换。
4. deferred 模式下，在每次工具结果之后做一次廉价触发匹配：只看 result.summary、文件类型、错误码和前 500 字节预览；命中后追加 1 张卡，记录 skills_triggered、skills_card_bytes、skills_loaded。加载失败 fail-open，不阻断。
5. 工具结果头部增加恢复提示：error_code=PATH 时提示“路径必须在题目工作区内”；TOOL_BUSY 时提示“稍后重试或换工具”；TIMEOUT 时提示“缩小输入或换更小的工具”。只加一行，不改变 Gate。
6. SessionControl 增加 hint_stage 字段和 hint_injected 事件；同一阶段只提示一次；把“重复签名/无新证据/预算不足”分别映射到换路、收尾、停止；保留现有 max_steps/repeat_limit/stall_steps 默认值。
7. lessons 增加文本归一化去重、每题上限、同域多样性；supersede 至少在人工/确定性冲突路径接线；候选规则不注入。
8. _bounded_handoff 在裁剪后重新计算 JSON 长度，仍超 6000 就继续丢 lessons/attempts，直到达标；这是现有软上限的 bug 修复。
9. 配置新增可选键：skills.mode、skills.index_max_bytes、skills.card_max_bytes、skills.max_cards_per_intent、skills.trigger_min_score；默认值等于当前行为；validate_config 对未知键保持宽容。状态字段（如 hint_stage）在 load 时用 setdefault 补齐，旧 DB 不需要迁移；不要新增必填字段。

### 5.4 质量闸脚本与测试

新增 scripts/check_skills.py（只读，CI 运行）：

- 加载 index.json 和 default_specs；检查 name 唯一、file 存在、domain 合法、卡片四段齐全、单张不超过 2KB。
- 扫描 tool=NAME、python_exec 代码块、禁用库/命令、字面 flag、基准题名、提示注入模式、外部来源和许可证。
- 对每个 trigger 做归一化去重；输出“无触发条件”“无命中判据”“无踩坑”的列表。
- 失败返回非零；运行时仍由 loader 逐节降级，不让质量闸阻塞解题。

新增回归：

- tests/unit/test_skill_quality.py：好卡通过；缺判据/禁用命令/字面 flag/超长/重复触发分别失败。
- tests/unit/test_framework_skills.py 增加：index_text 不含正文、跳过 deprecated、缺失可选字段不崩。
- tests/unit/test_framework_memory.py 增加：index 模式下 skills_loaded 仍只表示已加载正文（目录不计入），目录可见；deferred 模式在第一轮不注入卡片、工具命中后注入；Broken loader 仍降级。
- tests/unit/test_framework_control.py 增加：hint 每阶段一次；handoff 裁剪后不超过 6000。

兼容性重点：现有 test_worker_injects_selected_skill_and_domain_lesson_but_not_full_state 断言“内置方法参考”和 skills_loaded 精确相等；test_provider_timeout_still_reports_loaded_skill 也依赖预加载。因此 deferred 必须做成开关，默认 preload；不要直接把默认路径改成延后加载。test_handoff_is_bounded 使用短字符串，硬上限修复不会破坏它。

### 5.5 评测设计

在没有真实模型调用授权之前，只能做离线代理指标：

- 索引可见性：单测断言 Worker 的 messages 含目录，且目录不含任何卡片正文。
- 触发精确率/召回率：准备 30 到 50 条本地题面/附件描述，标注应触发的卡片；统计误触发和漏触发。这比“模型觉得好不好”更可复现。
- 预算：用 FakeProvider 统计每轮 messages 的字节数，估算目录/卡片在 10 轮、20 轮、40 轮下的重复输入成本；设上限。
- 质量闸：对现有 10 节跑 lint，修完再谈扩充。
- handoff 上限、lessons 去重、hint 事件：纯离线单测。

真实 A/B 的前提是：真实网关 tools/tool_use 已联调、有一组带已知 flag 的本地题、模型调用获授权。设计三臂：A 无技能（仅工具定义和契约）、B 仅目录、C 目录 + 证据触发卡片；同模型、同提示、同预算、同工具；每题至少 3 次；报告每题配对差、置信区间、token、工具调用、首次证据步数、重复调用率、卡片打开率、条件解出率。不要用 15 题 180 次运行去宣称 10pp 的差异；原文自己都说这个样本量分辨不出 8.9pp。更现实的是看机制指标：卡片是否改变下一步动作、是否减少重复调用、是否引入错误换路。

### 5.6 优先级重排

| 优先级 | 工作包 | 准出证据 |
| --- | --- | --- |
| P0 | 目录真正进入 Worker；现有 10 节质量闸；handoff 硬上限；索引/跳过/提示事件埋点 | 单测看到目录；lint 对 10 节全过；handoff 裁剪后 <=6000；事件表有记录 |
| P0 | 修现有卡片的不可执行内容：unzip→archive_list、gmpy2→stdlib、openssl→明确边界或新增工具 | 每张卡的工具名都能在 default_specs 找到；无禁用库 |
| P1 | 证据触发 1 张卡（deferred 开关，默认 preload）；工具错误恢复提示；阶段 hint 事件 | 第一轮不加载卡片；命中后只加载 1 张；错误码有恢复提示 |
| P1 | lessons 文本去重、每题上限、多样性；supersede 接线 | 重复失败不产生重复教训；跨题注入有上限 |
| P2 | 规则表与候选/晋升；阶段短附加；facts 展示或明确只审计 | 候选规则 0 注入；晋升规则 1 到 2 条；阶段附加 <=300 字节 |
| P3 | writeup 蒸馏、语义检索、1324 篇 RAG | 仅在 P0/P1 有真实模型数据后评估；先过许可证/污染/质量闸 |

这个顺序与用户建议的差别只有两点：质量闸从最后提前到 P0；提示词拆分从第四位降到 P2。理由分别是：坏卡片的负迁移比没有卡片更贵；worker.md 已有通用路径，拆提示词的边际收益低于让目录和卡片真正可执行。

## 6. 风险与边界

| 风险 | 触发条件 | 缓解 |
| --- | --- | --- |
| 假教训传播 | 单次失败写成规则；跨题硬套 | 候选/晋升；至少 2 次复现；supersede；质量闸 |
| 基准污染 | 卡片含 NYU/CSAW 题名、测试常量、flag 样例 | 质量闸扫描；来源审计；不使用测试集写卡 |
| Flag 泄漏 | 卡片/事实/教训含字面 flag | 统一 sanitize；质量闸；facts 展示前再脱敏 |
| 提示注入 | 外部 writeup 文本进入卡片 | 卡片只放方法；外部文本不升 system；扫描 ignore/system 等模式 |
| 工具漂移 | 卡片写未注册命令/未安装库 | lint 对 default_specs；版本/reviewed_at；工具下线即弃卡 |
| 上下文成本 | 2KB 卡片 × 多轮重发 | 单卡 2KB、一次 1 张、延后加载；索引 <=600B；用 FakeProvider 估算 |
| 过早止损 | 20 步 stall 早于 30 步提示 | 先 warn 再 switch 再 stop；按重复签名/无新证据分流；保留预算硬停 |
| 评测欠功效 | 15 题 180 次运行 | 报告 CI；看机制指标；不宣称小差异；真实 A/B 前先联调 |
| 模型不可外推 | Claude Sonnet 4.5 对 deepseek-flash | 本地配对实验；按题类分层；不直接搬结论 |
| 执行边界 | python_exec 可 import subprocess；本地执行器不是安全沙箱 | 卡片不教 shell；外部卡片视为不可信；单独做容器隔离验收 |

## 7. 本轮未做

- 未修改任何运行代码、技能正文、提示词或配置。
- 未新增技能卡，未导入 CTF-SKILLS 或其他 writeup。
- 未调用真实模型、官方平台或外部题目。
- 未运行测试；现有 230 项离线通过等数字来自 README，不是本轮复测。
- 未验证真实网关的 tools/tool_use 兼容；这是任何“可执行卡片”评测的前置条件。

## 8. 关键代码位置

- ctf_ext/skills/loader.py:54：list_index；:65-98：select；:123-142：load_for。
- ctf_ext/runtime/llm_worker.py:150、299-315：技能注入；:318-331：handoff 软上限。
- ctf_ext/runtime/control.py:17-30：StepPolicy；:101-155：止损；:180-196：意图窗口。
- ctf_ext/store/lessons.py:44-74：蒸馏；:111-153：检索；:156-163：supersede。
- ctf_ext/store/blackboard.py:446-469：handoff；:492-517：facts。
- ctf_ext/runtime/planner.py:76-84：followup phase。
- ctf_ext/gateway/catalog.py:275-316：21 个工具；ctf_ext/gateway/tools.py:93-204：执行与错误码。
- ctf_ext/prompts/worker.md：唯一被加载的 system prompt；planner.md 与 handoff.md 未被代码引用。
- tests/unit/test_framework_skills.py、tests/unit/test_framework_memory.py：现有断言与兼容性约束。

## 9. 参考

- arXiv:2605.20023v2，https://arxiv.org/abs/2605.20023 （负结果、180 次运行、8.9pp、CI、局限、H1）。
- International Journal of Information Security 25:183 (2026)，https://link.springer.com/article/10.1007/s10207-026-01350-x （29 个 genuine solves、14 个 note、15 个未打开、199/200 升级、72.5% 花费、污染审计）。
- SkillsBench，arXiv:2602.12670（由上述论文引用；+16.2pp、16/84 负向、自生成技能 −1.3pp；本评审未直接复核该文全文）。
- 本仓库 README.md、docs/TOOLCHAIN.md、docs/FRAMEWORK-TEST-MATRIX-1.0.md、docs/FRAMEWORK-OPTIMIZATION-1.0.md、docs/COMPETITION-ROBUSTNESS-1.1.md。
## 10. P0–P4 落地分级（1.3 补充）

本节按“是否改变默认行为、复杂度、不可预见性”重新分级；第 5.6 节是按价值排序。两者冲突时，以本节作为落地决策，以 5.6 节作为价值参考。

### 10.1 总原则

1. 默认路径不变：新增能力默认关闭；默认配置下，prompt、技能选择、预算、Gate 行为保持现状。
2. 只加不改：新函数、新脚本、新字段、新配置；不改 select 评分、load_for 预算、max_skills、StepPolicy 阈值、FlagGate、ToolRegistry 策略和 validate_worker_output。
3. 一次一个开关：每个行为变化都有独立开关和一行回滚；不把索引、卡片、规则、RAG 绑在一起上线。
4. 失败开放：索引、卡片、质量闸失败都不能阻断解题；不新增必填字段，不做数据库迁移。
5. 不做自动学习：P3 只做离线报告；模型写的规则不自动进入比赛上下文。
6. 先观测再开行为：没有 token、触发率、合同失败率这些数据，不把开关默认打开。

### 10.2 分级与建议

| 级别 | 内容 | 默认行为 | 代码/数据面 | 复杂度 | 不可预见性 | 建议 |
| --- | --- | --- | --- | --- | --- | --- |
| P0 | 观测、纯函数、质量闸报告 | 零变化 | loader 纯函数、scripts、tests | S | 低 | 立即落地，全部合并 |
| P1 | 索引可见 | 默认 preload；index 需显式开 | _append_skills、可选 config、tests | S | 中低 | 落地开关，默认关；先 canary |
| P2 | 10 张卡改成可执行卡；单卡仅可选 | 卡片数量不变；内容发布会改变 prompt | sections、lint、tests | S/M | 中 | 先改内容，单卡等数据 |
| P3 | 规则候选报告 | 仅离线，0 注入 | 只读脚本 | M | 高 | 只做报告，不接线 |
| P4 | writeup 蒸馏/RAG | 无运行时 | 独立离线管线 | L | 很高 | 暂缓，先 3 张样例 |

### 10.3 P0：零行为变化的基座

目标：把“能不能做”变成“可验证”，不改运行时。

具体做：

- 在 SkillLoader 增加 index_text(max_bytes=600) 纯函数：由 index.json 生成紧凑目录，跳过 deprecated，缺失字段用默认值，超长截断并标注。只新增方法，不调用。
- 新增 scripts/check_skills.py，先只做 report 模式：读 index.json、sections 和 default_specs，报告缺段、禁用命令、未安装库、字面 flag、基准题名、重复触发、超长；有问题是 warning，退出码 0。
- 新增 tests/unit/test_skill_quality.py 和 test_skill_index_text.py：覆盖好卡、缺判据、禁用命令、字面 flag、超长、重复触发、index_text 不含正文。
- handoff 的 6000 上限修复单列：它是 bug 修复，只影响超过 6000 的输入；如果要求默认行为字节级不变，就把它放到 P1 并加开关；不要静默改。

不做：不注入目录、不改卡片、不改配置、不建表、不动 worker.md。

失败模式：lint 误报、index_text 对旧 index 的 KeyError。前者用 report 模式；后者用 get 默认值兜底。

验收：现有测试全部通过；新增测试通过；运行时文件 diff 为零（handoff 修复除外）；对现有 10 节能生成完整报告；报告中的问题数量作为 P2 的输入。

做到什么程度：100% 合并；用户不可见；没有收益宣称。

### 10.4 P1：索引可见，但默认关

目标：让模型知道“有哪些方法”，不改变默认选择。

具体做：

- 增加可选配置 skills.mode，取值 preload（默认，现状）、index（只注入目录）、index_preload（目录加现有卡片）、off（A/B 基线）。非法值回退 preload。
- _append_skills 只在 mode 为 index/index_preload 时追加一条目录消息；目录不超过 600 字节；不包含命令、判据、flag。
- mode 为 preload/index_preload 时，仍走原来的 select/load_for，选择算法、上限、缓存、预算都不变。
- resource_usage 增加 skills_indexed（名称列表）；skills_loaded 仍只表示卡片名，语义不变。
- __main__ 从 cfg 读取 skills 段；不把 skills 加入 validate_config 的必填 sections；读不到或非法就回退 preload。

不做：不减少 max_skills，不改 select 评分，不做证据触发，不加阶段提示，不动 handoff（除 P0 的 bugfix）。

失败模式：目录占 token、模型过度关注目录、目录与卡片重复。缓解：600 字节上限、只放名称和一行触发、canary 对比 token 和合同失败率。

验收：默认 preload 下，messages 与改动前逐字节一致（新测试）；index 模式只多一条目录消息；skills_loaded 不变；无合同新失败；canary 2 道题，记录 token 增量；rollback 只需把 mode 改回 preload。

做到什么程度：开关可用、可观测、可回滚；默认不开；不宣称解题率提升。是否默认开启，等 P1 canary 和真实 A/B 数据。

### 10.5 P2：卡片可执行，数量不变

目标：把 10 张卡从“散文步骤”改成“结构化工具调用 + 判据 + 踩坑”，先不改加载数量。

具体做：

- 逐张改写现有 10 节；每张 0.8 到 1.2KB，硬上限 2KB；四段齐全：触发、最小动作、命中判据、踩坑；加来源/边界。
- 最小动作只写注册工具（default_specs）或 python_exec；不写 unzip、openssl、gdb 等未注册命令；不依赖 gmpy2、angr、ghidra、sage。
- 命中判据必须能落到 evidence_id，写清负判据/停止条件。
- 全部通过 check_skills.py --strict 后，再把 strict 加进 CI；在未修完前保持 report 模式，避免 CI 先红。
- 可选 P2B：把 max_skills 从 2 降到 1，只作为 opt-in；默认仍 2；只有数据表明第二张卡长期无关时才考虑。

不做：不加 web 域、不加新工具、不做动态证据触发、不改 select 评分、不改 max_skill_bytes。

失败模式：卡片变长导致 token 上升；工具名写错；命中判据诱导模型提前提交。缓解：长度上限、lint、canary、token 对比；任何一项异常就 git revert 内容。

验收：10 节全部 strict 通过；工具名都能在 default_specs 找到；无字面 flag/基准题名；预加载总字节不超过 2.5KB（当前约 1KB）；现有测试通过；canary 至少 2 道题，记录 token 和合同失败率。

做到什么程度：10 张卡达标即可，不追求覆盖所有技法；web 仍为 0，等工具和授权策略明确后再加；动态触发不放进 P2。

### 10.6 P3：规则只做离线报告

目标：让赛后经验可审计，但不自动进入比赛上下文。

具体做：

- 新增 scripts/rules_report.py，只读 attempts/lessons，按 domain、strategy_key、outcome 聚类，输出候选规则：触发、动作、证据、出现次数、反例、建议状态。
- 只写报告文件，不写数据库，不建 rules 表，不注入 Worker。
- 人工审核候选；至少 2 个独立尝试或 2 道题复现，才允许后续讨论晋升。

不做：不做运行时规则注入，不做自动晋升，不做 supersede 自动化，不建表，不做动态证据触发。

失败模式：把相关性当因果、把单题偶然当规则、跨题泄露。缓解：报告标注证据数量/反例；人工审核；不进入运行时。

验收：生成 5 条有证据的候选；运行时 diff 为零；数据库 schema 不变；没有新增 prompt。

做到什么程度：离线报告；如果未来要做运行时规则，另开实验，默认 off，且必须先有 P1/P2 数据。

### 10.7 P4：writeup/RAG 只做安全样例

目标：证明 writeup 能安全蒸馏成卡片，而不是把 1324 篇塞进上下文。

具体做：

- 写一份离线蒸馏规范：来源、许可证、脱敏、去基准题名、四段结构、工具映射、人工审核。
- 从有明确许可证、非基准题集的材料里做 3 张样例卡；每张记录 URL/commit、许可证、reviewed_at。
- 用 P0 的 check_skills.py 做 gate；不接入运行时。

不做：不做 1324 篇批量入库，不建向量库，不接 RAG，不调用模型生成卡片，不改运行时。

失败模式：许可证不清、基准污染、提示注入、卡片质量低。缓解：3 张样例先过人工和 gate；来源可追溯；不合规就停止。

验收：3 张样例卡通过 gate；来源/许可证完整；无基准题名/字面 flag；运行时 diff 为零。

做到什么程度：规范加样例；不进入比赛镜像；等 P1/P2 有真实数据、工具联调完成后再评估。

### 10.8 复杂度与不可预见性对照

| 变更 | 默认影响 | 主要风险 | 规避 |
| --- | --- | --- | --- |
| index_text 纯函数 | 无 | 旧 index 缺字段 KeyError | get 默认值；单测 |
| 质量闸 report | 无 | 误报、CI 先红 | report 模式；修完再 strict |
| skills.mode 索引 | 默认无；开启后多一条消息 | token 上升、模型分心 | 600B 上限；canary；开关回滚 |
| 卡片改写 | 有，prompt 内容变化 | token 上升、工具写错、提前提交 | 1KB 目标、lint、canary、git revert |
| 单卡 max_skills=1 | 默认无；opt-in 后变化 | 漏掉第二张相关卡 | 只做 opt-in；数据支持再开 |
| 动态证据触发 | 默认无；开启后变化 | 误触发、上下文顺序、状态复杂 | 不放进 P2；未来用静态映射，默认 off |
| 规则注入 | 默认无；开启后变化 | 假教训、污染、迁移 | P3 只离线报告；不建表不注入 |
| writeup/RAG | 无 | 许可、污染、成本 | P4 只做 3 张样例；不接运行时 |

### 10.9 什么时候停止

出现以下任一情况，停止该项并回滚：

- 默认 preload 的 messages 与改动前不一致。
- skills_loaded 语义或内容变化（不是新增字段）。
- 现有测试失败，或新合同失败率上升。
- 预加载/索引 token 增量超过基线的 20%。
- handoff 裁剪后仍超过 6000。
- 卡片引用未注册工具或未安装库。
- 质量闸 strict 未通过却要开 CI。
- 任何规则自动进入 prompt，或任何 writeup 未经人工审核进入镜像。

### 10.10 结论

- P0 应全部落地：它只增加可验证性，不改变行为。
- P1 应落地为默认关闭的开关：它是“索引可见”的最小增强，回滚成本最低。
- P2 应先做 10 张卡的内容改写；单卡是可选实验，不是默认。
- P3 只做离线候选报告；动态证据触发和运行时规则都不在这一批。
- P4 只做 3 张安全样例；1324 篇和 RAG 暂缓。

一句话：增强应表现为“目录可见、卡片可执行、观测可审计”，而不是新增一套自动学习/自动路由的控制流。默认路径不变，才有资格谈增强。



## 11. 实施记录（2026-11-19）

本轮按“只加不改、默认不变、独立开关、失败开放”落地 P0–P3；P4 仍暂缓。

### 11.1 P0：可验证性

- `scripts/check_skills.py`：默认 report，`--strict` 才返回非零；检查四段结构、工具白名单、禁用命令、字面 flag、长度、prerequisites。
- `tests/unit/test_skill_quality_gate.py`：覆盖合法卡、未知工具、缺段、字面 flag、原始命令。
- handoff 6000 字节从“软上限”修成裁剪后复核的硬上限；极端输入继续降级到空集合，见 `tests/unit/test_handoff_cap.py`。

### 11.2 P1：索引可见（默认关闭）

- `ctf_ext/skills/loader.py` 新增纯函数 `index_text(max_bytes=600)`：只读索引元数据，不读 section 正文；按字节截断；过滤工具名关键词。
- `skills.mode` 取值 `preload`（默认，输出与改动前逐字一致）、`index`（只目录）、`index_preload`（目录 + 卡片）、`off`；非法值回退 `preload`。示例：`{"skills": {"mode": "index_preload", "index_max_bytes": 600}}`。
- `skills_loaded` 语义不变；新增独立字段 `skills_indexed`，只表示目录覆盖的技能名。
- 默认目录实测 331 字节；不含 7z/binwalk 等命令名。
- `tests/unit/test_skill_index_text.py`、`test_skills_mode.py` 覆盖默认不变、模式切换、目录失败仍给卡片。

### 11.3 P2：卡片可执行

- 10 张 section 改写为“触发 / 最小动作 / 命中判据 / 踩坑”；工具名全部来自 `default_specs`，不教 shell。
- `index.json` 的 prerequisites 从旧命令名改为注册工具名；质量闸新增 prerequisites 校验。
- `check_skills.py --strict` 结果：10 checked, 0 errors, 0 warnings。

### 11.4 P3：离线观测

- `scripts/rules_report.py` 以 `file:...?mode=ro` 只读打开 DB，输出候选规则；报告明确 `injection: none; candidates require human review`。
- `tests/unit/test_rules_report.py` 覆盖候选输出、只读性和缺库返回非零。

### 11.5 P4：暂缓

本轮没有引入 1324 篇 writeup、向量库或 RAG。原因是没有可核验许可证、非基准题集的外部来源；按 §10.7 的停止线，不把无来源材料放进运行时，也不伪造 3 张“外部样例”。需要提供有许可证的材料后再做样例卡。

### 11.6 验证与限制

- 全量 pytest：313 项通过、2 项跳过、0 失败；跳过的是未安装 tesseract 的 OCR 端到端，以及 PATH 中没有 `pdfimages` 的 PDF 图片端到端。
- 把 `/opt/homebrew/bin` 加入 PATH 后（本机已装 poppler 26.11.0），全量为 314 项通过、1 项跳过、0 失败；剩余跳过仅为 OCR 外部栈缺失。
- 定向测试覆盖技能目录、模式切换、质量闸、交接硬上限、规则报告、Worker 和教训路径，全部通过。
- 本地 `.venv` 使用 Python 3.12.16 与 pytest 8.3.3；`pyproject.toml` 的 `pythonpath = ["tests/unit"]` 解决 `from _support import ...` 的导入路径问题。`scripts/run_checks.py unit` 已委托 pytest，结果为 293 项通过、1 项跳过。
- 未调用真实模型、官方平台或网络服务。

## 12. 外部方法论吸收记录（2026-11-19）

本轮只吸收外部公开仓库的可迁移方法，不引入运行时依赖、脚本或自动路由。

### 12.1 来源与边界

- 来源：`https://github.com/ljagiello/ctf-skills`，MIT，固定提交 `20d182d9377f0467830f314bb0f843cae4dbf55e`。
- 审计清单：`ctf_ext/skills/sources.lock.json`；声明：`docs/THIRD_PARTY_NOTICES.md`；流程：`docs/skills-sources.md`。
- 运行时仍只读 `index.json` 与 `sections/*.md`；`sources.lock.json` 不被 `SkillLoader` 读取。
- 未复制 shell 命令、安装脚本、exploit payload 或大段正文；外部文本按不可信数据处理。

### 12.2 新增卡片（6 张）

| 卡片 | 吸收点 |
| --- | --- |
| `crypto.dh.degenerate` | 退化参数/全零共享值优先探测；先排除实现缺陷再上 DLP |
| `crypto.prng.mt19937` | 624 个 32 位输出恢复状态；时间种子小范围回放 |
| `crypto.rsa.common_modulus` | 同 n 多组 e/c 的 Bezout 组合；gcd>1 时停止并换路 |
| `reverse.bytecode.python` | .pyc/marshal 静态读取；版本/magic 与嵌套 code object |
| `forensics.pcap.triage` | 流量先分流再重组；字节序、snaplen、pcapng EPB 边界 |
| `reverse.vm.custom` | 自定义 opcode 的最小解释器与逐条 trace |

每张卡都写“触发 / 最小动作 / 命中判据 / 何时换路 / 踩坑 / 来源边界”，工具名只来自 `default_specs()` 或 `python_exec` 标准库。现有 10 张卡也补了 `## 何时换路`，只作文本规则，不改变 `select`/`load_for`。

### 12.3 元数据与质量闸

- `index.json` 从 10 条扩到 16 条；新条目含 `trigger`、`keywords`、可选的 `pivot` 与 `source`。
- `pivot` 只表达“下一张卡”的人工规则，不参与 `select`/`load_for` 控制流；质量闸校验目标是已知技能名。
- `check_skills.py` 新增：external source 必填字段、commit hex、reviewed_at 日期、pivot 形状/重复/悬空目标、危险代码调用（subprocess/os.system/socket/requests/eval/exec）与提示注入模式扫描。
- `python3 scripts/check_skills.py --strict`：`16 checked, 0 errors, 0 warnings`。

### 12.4 离线评测与草稿流程

- `scripts/eval_skills.py`：读取 JSONL 用例，只调用 `SkillLoader.select`，不调用模型；输出 top1/top2/forbidden/empty，`--strict` 作为门禁。
- `tests/fixtures/skill_selection_cases.jsonl`：10 条正/负例；当前结果 `cases=10 top1=10 top2=10 forbidden=0 empty=0`。
- `scripts/import_skill_draft.py`：外部 Markdown → `docs/skills-drafts/*.draft.md`；拒绝写 `ctf_ext/skills/sections`，去掉代码块、命令、URL 与字面 flag，要求固定 commit。
- 草稿不会自动进入 `index.json`；必须人工改写并通过 strict 质量闸。

### 12.5 验证与回滚

- 新增定向测试：`test_skill_source_gate.py`、`test_eval_skills.py`、`test_import_skill_draft.py`。
- pytest 全量：单元 293 项通过、1 项跳过（PATH 含 `/opt/homebrew/bin`）；无该 PATH 时单元 292 项通过、2 项跳过。单元+集成+混沌为 314/1（有 PATH）或 313/2（无 PATH），均 0 失败。18 个此前因缺 `pytest`/`_support` 而跳过的模块已全部实际执行。
- `scripts/run_checks.py unit` 在安装 pytest 后自动委托 pytest，不再在 `test_arena_harness.py` 的导入处中断；没有 pytest 时仍退回内置 runner 并对 pytest 模块打印 SKIP。
- 默认 `skills.mode=preload`、`select` 评分、`max_skills=2`、StepPolicy、FlagGate 均未改动；回滚只需移除新增 6 条索引与对应 section，或回退本轮提交。
