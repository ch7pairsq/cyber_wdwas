# 官方 Harness 接入清单（§12.5 阻塞项）

当前实现：`ctf_ext/harness/official.py` 是注入式适配器，只做方法集校验与逐调用转发。
SDK 缺失或方法不全时抛 `OfficialProtocolUnavailable`，`config.harness.protocol=official`
还要求 `official_sdk_ready=true` 才允许启动。这意味着：**没有 SDK 时系统不会假装能提交**。

## 1. 需要官方提供的最小信息集

| 类别 | 具体项 | 用途 |
| --- | --- | --- |
| SDK | 包名、版本、安装来源、Python 版本要求 | 依赖与镜像 |
| 鉴权 | 环境变量名已定为 BAIZHI_API_KEY（2026-11-18 用户确认，双通道共用）；剩余：凭据字段类型、有效期／刷新方式、容器重建时是否重注入 | 启动与轮换；重建不重注入且挂载持久卷时可启用 Key 落盘缓存（默认关闭） |
| 任务 | 拉取题目、附件引用格式、大小与限流 | bootstrap / 下载 |
| 提交 | 方法签名、幂等键规则、回执字段、状态枚举 | 提交状态机 |
| 查询 | 按幂等键查询回执、UNKNOWN 语义 | 附录 E：绝不重发 |
| 错误 | 错误码表、可重试/不可重试、限流响应 | 退避与熔断 |
| 生命周期 | 启动、健康检查、结束上报 | F-2 冒烟 |
| 样例 | 官方 fixture / 样例仓库 / 沙箱地址 | 契约测试 |

## 2. 适配器要求的方法（已固定）

- `startup()`
- `read_tasks()`
- `read_attachment(ref, *, max_bytes=None)`
- `submit_answer(submission)`
- `get_receipt(idempotency_key)`
- `finalize(summary)`

任一缺失即视为未就绪；接入方只需构造 `OfficialHarnessAdapter(sdk)`，不修改业务层。

## 3. 接入步骤

1. 安装官方 SDK，在 `config/competition.yaml` 置 `official_sdk_ready: true`。
2. 注入 SDK 后先跑 `capability_report()`，必须 `ready=true`。
3. 用官方 fixture 跑 read_tasks → read_attachment → submit → get_receipt 全链路。
4. 注入故障：429、超时、SENDING 中断、UNKNOWN，确认只查询不重发。
5. 双人复核后解除参赛锁；提交前保留 `competition_locked=true` 的演练记录。

## 4. 验收标准

- 官方 fixture 全部通过，字段零猜测；
- UNKNOWN/SENDING 路径不产生第二次提交；
- 限流时降并发而不是放大重试；
- 任何协议异常都不能被吞掉或降级为 mock 成功。

## 5. 明确禁止

- 静默回退到 Mock Harness；
- 拼 URL 猜端点或字段；
- 把未识别的响应状态当作成功；
- 在 `official_sdk_ready=false` 时启动官方协议。
