# 参赛镜像与公司模型约束

## 已确认的交付方式

采用 Harness 托管。交付入口按用户 2026-11-18 决定改为**双轨支持**，最终采用哪种待比对便利性后确定：

- 镜像仓库：需要平台可访问的仓库地址／命名空间，以及推送／拉取凭据。仓库地址指容器镜像引用（仓库、镜像名、标签或摘要），不是 Git 代码地址，也不是网页浏览链接。
- 文件上传：`docker save` 把成品镜像打包为 tar（可压缩），经平台通道上传。需确认接受格式与加载方式（docker load）、大小限制、上传截止时间、是否可重传；交付时同时提供 tar 文件的 sha256 供平台核对。注意 tar 文件摘要与镜像摘要不是同一个值，两者都要记录；上传完成后应在干净环境 load 回来复跑 `check_competition.py`，确认交付物本身可回放。

无论哪种入口，比赛时平台只拉取／加载成品镜像并启动，不能执行 pip install、apt install、uv sync 或其他补装步骤。Docker Engine 与 Compose 已经存在；此前失败发生在基础镜像拉取，不是缺少 Docker。

## 已确认的模型配置

- 主通道（2026-11-20 用户确认切换）：DeepSeek 官方 API，OpenAI Chat Completions 兼容协议（POST /chat/completions、Bearer 鉴权），Base URL 固定 https://api.deepseek.com ，model ID 仅允许 deepseek-flash／deepseek-v4-pro（已真实调用验收）。双模型换挡：`model.pro_model=deepseek-v4-pro` 启用，stage≥pro_from_stage 的换路径回合与提交前评审路由 Pro，调用失败/每题次数超限回退 flash（fail-open）。切换只改配置 `model.provider`（`deepseek_official_openai` ↔ `baizhi_anthropic`），业务代码零改动；出口白名单须覆盖 api.deepseek.com。
- 备选通道（保留）：公司百智云网关，Base URL https://ai-api-gateway.app.baizhi.cloud/api/anthropic ，Anthropic Messages 兼容协议（POST /v1/messages、x-api-key、anthropic-version），唯一 model ID deepseek-flash。
- 两条通道互斥；`config/base.yaml`、`arena.yaml`、`official-arena.yaml` 当前默认均走官方通道，百智网关保留为备选（切换只改 `model.provider`）。两通道执行同样的传输纪律：不继承任意 HTTP_PROXY、不跟随重定向、校验 TLS、不发送 tools/web_search 定义、响应大小上限，对非文本工具响应和模型 ID 不一致的响应拒绝处理。两条通道均已经过真实调用：百智通道在 2026-11-19 arena 平台演练，官方通道为 2026-11-20 切换后验收。
- 凭据环境变量固定为 BAIZHI_API_KEY（2026-11-18 用户确认，两条通道共用；切换通道时变量名不变，值对应所选通道的 Key；2026-11-20 起默认值为 DeepSeek 官方 API key）。Key 只能运行时注入，不打入镜像，不写配置文件与数据库快照。
- 可选 Key 落盘缓存（2026-11-18 新增，默认关闭）：环境变量在进程重启与容器重启时都会保留，只有"平台重建容器且不重注入、并挂载持久卷"的场景才需要落盘兜底。启用后缓存文件 0600 原子写入，不进 SQLite／证据／报告／日志，本轮运行正常结束时清理；是否启用待平台确认挂载与重启语义后再定。

接口路径名称中的 anthropic 代表兼容协议，不代表调用 Claude；两条通道请求中的模型都是用户指定的 DeepSeek 系列。Provider 不发送 tools/web_search 定义，对非文本工具响应和模型 ID 不一致的响应拒绝处理。

## 镜像内容和阶段

同一个 Dockerfile 包含公共 runtime 阶段，以及两个交付目标：

| 目标 | 用途 | 包含内容 | 能否直接参赛 |
| --- | --- | --- | --- |
| development | 赛前开发、离线测试、断点调试 | 运行依赖、Cairn、pytest、httpx、debugpy、安装器 | 不能 |
| competition | 参赛候选镜像 | 应用、提示词、配置、Cairn 全部运行依赖、预装系统工具；移除常见安装器和调试模块 | 入口已接线 Arena 协议（2026-11-20 验收）；待真实平台联调与交付 |

依赖清单分别为 docker/requirements-runtime.lock 与 docker/requirements.lock。所有依赖均在 docker build 阶段安装；CMD/ENTRYPOINT 不执行安装操作。固定的基础镜像摘要已下载并校验。

不打包其他 Docker 镜像、其他容器、宿主机虚拟环境、历史数据卷、模型权重、模型 Key 或 Git 凭据。镜像内的 docker Python 包只是 Cairn 的依赖，不包含 Docker 守护进程和服务器原有镜像。

系统工具在构建期通过 Debian 官方 apt 源预装，清单固定在 `docker/toolchain.json`：core 层包含 `file`、`binutils`、`unzip/zip`、`xz/bzip2/zstd`、`openssl`、`xxd`、`jq` 等；ctf 层包含 `binwalk`、`foremost`、`sleuthkit`、`exiftool`、`p7zip`、`steghide`、`pngcheck`、`gdb`、`strace`、`ltrace`、`qemu-user`、`poppler-utils`、`qrencode`、`zbar`，以及 Reverse 用的 `pwntools`、`ROPgadget`、`cstool`、`checksec`、`patchelf`、`z3` 等。工具是镜像内容，不是运行时下载的插件；`sage`、`ghidra`、`sagemath` 等重型工具暂不进入默认镜像。平台架构、镜像大小限制与完整题目工具集合仍待验收，不能宣称镜像已覆盖所有题型工具。详见 [离线工具链](TOOLCHAIN.md)。

## 搜索与网络边界

- 禁止比赛时使用 web_search、浏览器检索、其他搜索 API 或利用模型代理转发搜索。
- 工具注册执行层拒绝搜索／浏览器类名称与通用 network 工具；模型 Provider 只接受纯文本消息，不发送工具定义。
- 工具命令策略拒绝 pip/uv/apt 等安装器以及 python -m pip/ensurepip 等直接入口；参赛目标额外移除 pip/ensurepip/uv/apt 等常见运行时入口。
- 默认开发 Compose 使用 internal 网络，无公共互联网出口；Mock 服务间通信仍可用。
- **应用检查不等于不可绕过的网络隔离。** 允许执行 Python/题目代码时仍可能绕过命令名检查。正式环境必须由平台在网络出口层限制为公司模型网关，以及官方协议明确要求的任务／附件／提交端点；这些平台端点不属于公共网页搜索，但必须有官方授权。
- 不能只写环境变量或提示词就宣称已经封禁全部出站访问。现阶段未配置平台出口白名单，也未开放开发容器的模型网络出口。

## 当前启动门禁（2026-11-20 更新）

competition 目标的默认入口已从旧的退出 78 占位改为 `scripts/run_official.sh`：supervisor（心跳判活 + ≤3 次重启 + 总墙钟）包裹 `python -m ctf_ext run --mode llm --config config/official-arena.yaml`，对接实际平台的 Arena 协议（Harness 托管拉起镜像）。旧模块 `python -m ctf_ext.competition`（退出 78）保留为手动门禁，不再是容器入口。

无凭据注入时的预期行为：缺 `ARENA_SOLVER_TOKEN` 即 SystemExit 快速失败，supervisor 重启 ≤3 次后以非零码退出；不回退 Mock、不安装包、不触发提交。正式运行依赖平台托管注入 `BAIZHI_API_KEY`、`ARENA_SOLVER_TOKEN`、`ARENA_BASE_URL`（可选 `ARENA_PROXY` / `ARENA_NETWORK_CIDR`）。注意 `app.mode` 保持 `practice`：`app.mode=competition` 会触发遗留校验（强制 `protocol=official`）导致无法启动。

无凭据快速失败已在宿主机冒烟验证（短窗口单次失败、300s 窗口重启 3 次后退出 1）；镜像重建与断网复验见下文验收记录。

## 等待用户／官方提供

1. 交付入口二选一（双轨均已支持，用户比对便利性后定）：仓库方案需平台可访问的镜像仓库／命名空间、推送／拉取凭据；文件方案需上传通道、接受格式（docker load）、大小限制、上传截止与重传规则。目前不推送镜像、不上传文件。
2. 平台 Harness 托管细节确认：**官方契约已确认（2026-11-20 官方注入说明）**：
   API 根 = `BENCHMARK_BASE_URL`（`http://10.201.0.9:8090`，追加 `/openapi/v1/...`）；
   token = `ARENA_SOLVER_TOKEN`；赛题靶机只经隧道可达（WireGuard 或 SOCKS5，
   "Challenge targets are reachable only through the same access method"），
   model-proxy 服务的 `SOCKS`/`HTTP_PROXY` 命名端口即隧道入口；
   409 duplicate=已计数、409 challenge_not_started=先 start；finish 在全部题
   有结论后才调；凭据绝不打印/发给靶机。适配器解析顺序
   `ARENA_BASE_URL -> BENCHMARK_BASE_URL -> ARENA_GATEWAY_URL -> k8s service -> config`，
   `setup_platform_network_env()` 导出 `ARENA_PROXY`/`HTTP(S)_PROXY`（幂等），
   工具子进程白名单放行代理地址（剥离 user:pass@）。历史实测：
   **已实测确认（2026-11-20 harness 日志）**：平台不注入 `ARENA_BASE_URL`；改用 `ARENA_GATEWAY_URL` + `ARENA_GATEWAY_TOKEN_FILE`（token 文件）+ k8s service 变量（`MODEL_PROXY_*_SERVICE_HOST` / 命名端口 `CHALLENGES`），并预注入 `CYBER_ARENA_HELP_MARKDOWN`、`BAIZHI_API_KEY`、`NO_PROXY`。适配器已按 `resolve_arena_base_url/resolve_arena_token` 自动探测（env -> gateway -> k8s service -> config），token 支持 env 与文件双来源、401 自动换源。启动命令可覆盖、模型走 model-proxy 侧车（端口 18081/18082 用途待确认）。
3. DeepSeek 官方通道（现主通道）的平台出口白名单覆盖 api.deepseek.com 与使用额度确认；如回切百智网关，其 Messages 路径、鉴权头和使用额度另行与公司侧确认。
4. 平台 CPU 架构、资源／镜像大小限制、网络出口规则，以及最终必须预装的工具集合。

官方协议接线与候选镜像离线验收已完成（2026-11-20，见下文记录）；上述信息确认后完成真实平台联调、镜像发布（或文件上传）与摘要固定；不要在比赛过程中安装依赖或修改协议。

## 本地构建候选镜像与离线验收

以下只在赛前开发主机执行：

~~~bash
docker build --target competition \
  --build-arg TOOLCHAIN_TIER=ctf \
  -t cyber-wdwas:competition-candidate .
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  --entrypoint python cyber-wdwas:competition-candidate /app/docker/check_competition.py

# 2026-11-20 起默认入口为 run_official.sh；无凭据时预期有限重启后非零退出。
docker run --rm --network none --read-only --cap-drop ALL \
  cyber-wdwas:competition-candidate
~~~

构建与离线测试可以验证包是否预装、安装器是否移除、工具链是否完整、默认门禁是否生效；不能证明真实模型可用、出口策略通过或官方平台可以解题。构建默认使用 `--build-arg TOOLCHAIN_TIER=ctf`；`check_competition.py` 会读取 `/opt/toolchain-installed.json` 并逐个校验工具二进制，缺失即失败。

### 2026-11-17 实测记录

> 以下为**加入离线工具链之前**的镜像记录；本轮新增 `docker/toolchain.json` 与构建期安装脚本后尚未重新构建，不能把旧镜像 ID 当成新工具链镜像。

- 远程 development 镜像已构建：`cyber-wdwas:dev`，修正测试夹具与 DAP 验证后的最终本地镜像 ID `sha256:c1457b69ccc9d16d49edc99636e34e9f6b519428079f9d47a9a1ea6ca2c0009b`。
- competition 候选已从独立 runtime 分支构建：`cyber-wdwas:competition-candidate`，本地镜像 ID `sha256:63f46f916294a4973aa98cc793ee9d72c0b7527ace987deb802925bf2f289eb5`，不是继承 development 的测试依赖。以上不是已发布的镜像仓库摘要。
- 候选在 `--network none --read-only --cap-drop ALL --security-opt no-new-privileges:true` 下通过 `check_competition.py`：Cairn 0.2.1 和运行依赖可定位，指定 pip／uv／ensurepip／apt／dpkg 入口及 pytest／debugpy 不可用；未挂 Docker socket、未发现 `/app/.env`。
- 候选默认启动实测退出 **78**，只报告官方 Harness 未接通；这是拒绝启动验证通过，不是参赛验收通过。未注入 Key、未调用模型、未推送镜像或提交比赛。
- 日志位于 `/root/cyber-wdwas-docker/logs/build-competition-final.log`、`container-competition-check-final.log`、`container-competition-entry-final.log`、`container-competition-exit-final.log`；实际容器限制与镜像 ID 另见 `container-security-final.json`。
- 主机提示不支持 swap 限额；内存限额不能被描述为已同时验证 swap 限额。平台资源和网络约束仍待正式验收。

## 2026-11-20 入口接线与冒烟记录

### 改动

- Dockerfile competition 阶段：`ENTRYPOINT` 由 `python -m ctf_ext.competition`（退出 78 占位）改为 `bash /app/scripts/run_official.sh`；release-status 标签更新为 `official-arena-entry-wired`。旧门禁模块保留，手动执行 `python -m ctf_ext.competition` 仍退出 78（已复验）。
- `scripts/run_official.sh` 增加 `cd "$ROOT"`：`-m ctf_ext` 的模块解析不再依赖平台继承的 workdir 或 PYTHONPATH。
- README 与本文件"当前启动门禁"描述同步更新。

### 远程构建主机源码重同步

- 旧源码目录整体 `mv` 备份至 `/root/cyber-wdwas-docker/backup-source-20261120`（保留 inode，未中断运行中的 ctf-debug 容器挂载），并清理了此前误同步到根目录的散落文件。
- 按 `git ls-files` 同步 322 个跟踪文件；**此前远程 `config/` 缺少 `official-arena.yaml`**（正式赛入口依赖它），本次已补齐。

### 验证结果（dev 镜像挂载新源码；--network none、只读根文件系统、cap-drop ALL、非 root）

| 场景 | 结果 |
| --- | --- |
| 30s 窗口、无凭据 | banner → `arena harness requires ARENA_BASE_URL and ARENA_SOLVER_TOKEN` → 退出 1，无重启循环 |
| 300s 窗口、无凭据 | 同上失败信息，supervisor 重启 3/3 后退出 1 |
| 假凭据（fake token/key）、断网 | 配置解析 → Provider 构造 → Arena `startup()` GET `/openapi/v1/challenges/help` → `Network is unreachable` → `ArenaProtocolError` → 退出 1；无提交、无 Mock 回退、无挂起 |
| 手动门禁模块 | `python -m ctf_ext.competition` → 退出 78（语义保留） |

假凭据仅为占位字符串，断网环境下未触达任何真实服务。

### 镜像重建与离线验收（2026-11-20，代理恢复后完成）

- 出站恢复方式：用户提供公司代理 `http://proxy.in.chaitin.net:8123`。宿主机与默认网桥容器内均实测 `deb.debian.org`=200、`pypi.org`=200 后，仅以 **build-arg** 方式传入构建（`HTTP_PROXY/HTTPS_PROXY/http_proxy/https_proxy`）；代理地址不进镜像层与 history（已核对成品镜像 env 无代理变量）。
- 重建成功：`cyber-wdwas:competition-candidate`，本地镜像 ID `sha256:ebffa5e49fe4484f087018258b9a6a2ad0782cdbac67f9948906b663a0afb96d`（短 ID `ebffa5e49fe4`），linux/amd64，Python 3.12.16，工具链 ctf 层 33 个工具。构建日志：`/root/cyber-wdwas-docker/logs/build-competition-arena-20261120-proxy.log`。
- 断网加固复验（`--network none --read-only --cap-drop ALL --security-opt no-new-privileges:true`）：`check_competition.py` 通过（offline_check=passed、cairn 0.2.1、无 pip/uv/ensurepip/apt 入口、无 docker socket、无 /app/.env）。
- 成品镜像入口核对：`ENTRYPOINT=["bash","/app/scripts/run_official.sh"]`、USER 10001:10001、标签 `official-arena-entry-wired`。
- 成品镜像入口冒烟（均 `--network none`）：30s 无凭据 → banner + ARENA_SOLVER_TOKEN 缺失 → 退出 1；300s 无凭据 → supervisor 重启 3/3 后退出 1；假凭据 → Arena `startup()` GET `/openapi/v1/challenges/help` → `Network is unreachable` → `ArenaProtocolError` → 退出 1。假凭据为占位字符串，未触达真实服务。

### 工具链扩展：Java/Go（2026-11-20 第二次构建）

- 目标：补齐 Java（jar 运行/编译/反编译/apk 解包）与 Go（本地单文件编译）解题能力。
- Debian trixie 官方源核实（代理 `apt-cache policy` 实测）：`jadx`/`cfr`/`smali`/`baksmali`/`dex2jar` 均无包；可用替代 `default-jdk-headless`（OpenJDK 21.0.13.1）、`procyon-decompiler`（Debian 包二进制名是 **`procyon`**）、`apktool` 2.7.0、`golang-go`（Go 1.24.4）。
- 清单与白名单：`docker/toolchain.json` ctf 层 +4 条目（37 工具 / 36 apt 包）；`command_allowlist` +7 命令（java/javac/jar/procyon/apktool/go/gofmt），base/arena/official-arena 三份同步。
- 构建失败教训：`procyon-decompiler` 包的二进制名不是包名而是 `/usr/bin/procyon`，首次构建 `verify_tools` 按 bins 校验失败退出 1；修正 manifest bins 后重建通过（构建日志：`logs/build-competition-arena-20261120-v2-javago.log`）。
- 重建成功：镜像 ID `sha256:4ba923a41a23`，大小 2.52GB（v1 1.55GB，增量约 +1.0GB，来自 JDK+Go 及依赖；平台镜像大小限制仍未确认，需留意）。
- 断网加固复验（`--network none --read-only --cap-drop ALL --security-opt no-new-privileges:true`）：`check_competition.py` 通过（offline_check=passed、toolchain_tools=37、runtime_installers=false、无 pip/apt 入口）。
- 容器内工具实测（均 `--network none`）：`java -version`/`javac`/`jar` = 21.0.13；`procyon` 反编译 javac 产物输出源码；`apktool --version` = 2.7.0；`go run` 编译运行 gob 编码单文件成功（gob_len=27）。
- 注意：apktool 的 Debian 包装器会在 HOME 下建目录，`--read-only` 且 /tmp 不可写时版本自检失败（mkdir 报错）；比赛容器 /tmp 可写，不受影响。Go 测试同理需可写可执行的 /tmp（Docker `--tmpfs` 默认 noexec，测试时要显式加 `exec`）。
- 入口冒烟复验（同 v1 口径）：无凭据 → banner + ARENA gate 报错 → supervisor 重启 3/3 → 退出 1；假凭据（占位字符串）断网 → Arena startup 失败 → 退出 1。行为与 v1 一致，入口代码未变。

### 解题思路记忆（2026-11-20 第三次构建加入）

- 机制：平台回执 `ACCEPTED` 后，orchestrator 从该题的获胜尝试（路线/成立条件/步骤/已排除）、
  提交引用证据对应的完整成功命令链（command_log argv basename）、本题教训蒸馏出一份整题
  思路，写入运行库 `solutions` 表（migration v3）；每个意图开始时按 **同题精确命中 > 同域
  + 题名/关键词** 检索最相关 ≤2 条注入 prompt（标注"历史经验，先验证再使用"），命中即
  复用计数 +1。
- 边界：FLAG/候选答案复用 lessons 的 sanitizer 脱敏后才入库；跨域不共享（同 lessons 口径，
  防止泄露其他题结论）；记录/检索全部 fail-open，异常只记 `solution_record_failed` 事件，
  不影响主循环。
- 持久化：记忆存在运行库（`data/practice.db` / `data/official.db`）里，同一数据卷的后续
  运行直接受益；平台重建容器且不挂数据卷时记忆随库重置。
- 验证：`tests/unit/test_framework_solutions.py`（蒸馏脱敏/幂等/检索优先级/跨域不泄露/
  worker 注入 fail-open 共 7 例）；全量 `run_checks.py` 347 passed / 1 skipped。
- 代码审查修复（2026-11-20 复查）：① recall 回调改走 `board.read_view()`（共享 sqlite
  连接的读必须与写事务共用 RLock，原实现绕锁违反并发前提）；② `search_solutions` 收紧
  跨域边界——关键词命中的跨域行一律不注入（原实现 score>0 即放行，存在跨域泄露可能），
  与 lessons"跨题共享只匹配同一领域"口径一致。
- v4 重建与验收：镜像 ID `sha256:352410f88eee`（2.52GB）；断网 `check_competition.py`
  通过（37 工具、offline_check=passed）；入口无凭据冒烟 banner → gate 报错 → 重启 3/3 →
  退出 1；镜像内已确认审查修复代码。构建日志：
  `logs/build-competition-arena-20261120-v4-reviewfix.log`。交付镜像
  `portus.in.chaitin.net/yuqiao.zhang/cyber-wdwas:competition-20261120`
  （2026-11-20 已推送成功，registry 返回 digest
  `sha256:352410f88eeebff09f4037ebc5d019fd5e53a623a3bf26a50d7a022fd88d87cc`；
  首推因 dockerd 代理配置指向已不存在的本地代理 127.0.0.1:18023 失败，以本地转发器
  18023→proxy.in.chaitin.net:8123 兜底完成；daemon.json 已把 portus 加入 no-proxy，
  备份 `.bak-20261120`，下次 daemon 重启后可直连）。

### 2026-11-20 平台首跑 503 与 startup 重试修复（v5）

- **现象**：平台托管首跑，镜像启动/配置/凭据解析全部正常（base 取自
  `BENCHMARK_BASE_URL` k8s service、token 取自 env、direct 路由可达），但平台后端
  `GET /openapi/v1/challenges/help -> 503 {"code":"resource_unavailable",
  "message":"platform is unavailable"}`（0.01s 即返回，属平台侧不可用）。
- **我方缺口**：startup 对平台 5xx 无重试，supervisor 3 次重启在数秒内烧光退出，
  剩余 ~3h50m 尝试窗口全部浪费。
- **修复**：`ArenaProtocolError` 携带 HTTP status；startup 对 5xx（未就绪/过载）
  退避重试（默认 20 次 × 30s ≈ 10 分钟，配置键 `harness.arena.startup_retry_attempts`
  / `startup_retry_seconds`，official-arena.yaml 已固化），supervisor 3 次重启合计
  ~30 分钟容错；连接层失败与 4xx（凭据/协议错误）仍立即失败，断网冒烟语义不变。
- **验证**：stub 服务 3 个新测试（503 重试后成功 / 预算耗尽报错 / 401 不重试立即失败）；
  全量 `run_checks.py` 350 passed / 1 skipped。
- v5 镜像 ID `sha256:050b1a84ff8e`（2.52GB）：断网 check_competition 通过、无凭据冒烟
  快速失败行为与 v4 一致、startup 重试代码确认在镜像内。构建日志：
  `logs/build-competition-arena-20261120-v5-startupretry.log`。已覆盖推送
  `portus.in.chaitin.net/yuqiao.zhang/cyber-wdwas:competition-20261120`（重跑尝试
  需平台重新拉取/发起）。

### 2026-11-20 平台重跑 SSLError 烧光窗口：平台 CA 合并信任 + 传输层退避重试（v6）

- **现象**：v5 推送后平台重跑，startup 成功但解题循环中模型调用全部
  `SSLError`（direct/proxy 双路由都挂）：每个 intent round 0 即死 →
  重新 claim → 死循环烧光 13800s。日志归档还显示更早的 previous 实例
  503 一次即烧光（3 秒重启间隔证明跑的是**不含 v5 重试的旧镜像**，
  平台拉取时机早于 v5 推送）。
- **根因（实锤）**：`openai_compat.py`/`deepseek.py` 均 `trust_env=False +
  verify=True`，requests 只认 certifi，平台注入的
  `SSL_CERT_FILE/REQUESTS_CA_BUNDLE/CURL_CA_BUNDLE/NODE_EXTRA_CA_CERTS`
  全部失效；平台出口为 TLS-MITM 型（direct 路由也被透明拦截），拦截链
  不被信任 → SSLError。旧日志只打异常类 docstring，看不到底层真实原因。
- **修复**（经确认三方案）：
  1. `provider.platform_ca_bundle()`：certifi 公共 CA + 平台注入 PEM 合并为
     一次性 bundle（0600）交给 `verify=<路径>`；证书链校验完整保留（非禁用
     验证），无注入时返回 True 行为不变；按环境快照缓存。
  2. 连接类快速失败（SSL/连接拒绝）退避重试 3 次（5/15/30s）；黑洞路由类
     超时保持快速判死不重试；HTTP 状态错误不重试；startup 断网快速失败语义不变。
  3. 底层异常消息（`exc`）替代 `type(exc).__doc__` 打印，可直接看到
     CERTIFICATE_VERIFY_FAILED 等真实原因（key 在 header，无凭据泄漏风险）。
- **验证**：新增 `test_llm_transport_tls.py` 13 个测试（CA 合并/OpenSSL 加载/
  verify 联动/重试成功/耗尽报错/超时不重试，两通道覆盖）；全量
  `run_checks.py` 363 passed / 1 skipped。
- **构建教训**：旧 `overlay.Dockerfile`（`FROM ebffa5e49fe4`，v1/33 工具）构建出的
  叠加镜像会回退 v4 的 Java/Go 工具链（37→33）；作废，改用
  `overlay-v6.Dockerfile`（`FROM 050b1a84ff8e` + 3 个 llm 文件 COPY）重建。
- v6 镜像 ID `sha256:d194b5e5eae5`（基于 v5，37 工具）：断网 check_competition
  通过（offline_check=passed、37 工具、无安装器）、容器内确认
  `platform_ca_bundle/_send_with_retry/TRANSPORT_RETRY_DELAYS` 在镜像且无注入时
  verify=True、无凭据冒烟 gate 报错 → 重启 3/3 → 退出 1（与 v4/v5 一致）。构建日志
  `logs/build-competition-arena-20261120-v6-sslretry.log`，已覆盖推送
  `portus.in.chaitin.net/yuqiao.zhang/cyber-wdwas:competition-20261120`
  digest `sha256:d194b5e5eae5892acf5c584c8f9f18e3b076af474fe3a6c137d6e1ff52c57522`
  （重跑需平台重新拉取新 digest，避免再调度到旧镜像实例）。

### v7 镜像：算法题计算库离线叠加（2026-11-20）

- **目标**：正式赛算法题所需的 Python 计算库：z3-solver 4.15.4.0 / sympy 1.14.0 /
  pycryptodome 4.5.0 / gmpy2 2.3.2 / numpy 2.5.5 / scipy 1.18.1 / pwntools 4.15.1 /
  pillow 13.0.0，另含 pytest 8.3.3 便于容器内自检。清单见 `requirements-arena.txt`；
  requests 保持锁版 2.32.3 不变。
- **离线构建法**（devbox 无外网）：Mac 用阿里云镜像 `pip download --platform
  manylinux2014_x86_64 --python-version 312 --only-binary=:all:` 预下载 44 个 wheel
  （含全部依赖与 pip-26.0.1 自身）→ rsync 至 `/root/cyber-wdwas-docker/wheels/` →
  overlay 层安装。base 镜像 venv 无 pip，用
  `python3 /tmp/wheels/pip-26.0.1-py3-none-any.whl/pip install` 直接从 wheel 运行。
- **overlay.Dockerfile 关键点**（上下文 `/root/cyber-wdwas-docker`）：
  1. `COPY source/ /app/` **全量同步当前源码**（配 `.dockerignore` 排除
     `source/.git`、`source/.monkeyai`、`source/.pytest_cache`、`source/data`、
     `__pycache__`；源码真实体积 19MB，`source/data` 运行期工作区垃圾 1.3G）。
     逐文件 COPY 会漏新增文件并造成新旧版本倾斜——v7 前的候选镜像因旧
     `provider.py` 缺 `TRANSPORT_RETRY_DELAYS`、缺新增 `store/solutions.py`
     在无凭据冒烟中 ImportError，全量 COPY 后消除。
  2. base 镜像默认 USER 为运行身份 10001:10001 且 venv 归 root：pip 安装层
     前后必须 `USER root` / `USER 10001:10001`，否则 `Permission denied` 且
     运行身份漂移。
- **验证**：候选镜像内全量 pytest 364 passed / 1 skipped / 0 failed；算法冒烟
  （z3 约束求解、sympy 素数、AES 加解密）通过；13 个库全部导入成功；无凭据
  冒烟 rc=1 且尾行为预期 fail-closed 提示；运行身份 10001:10001、entrypoint
  `bash /app/scripts/run_official.sh` 不变；22 层。
- **推送**：`docker save` 626MB tar → `scripts/push_tar.py` 推
  `portus.in.chaitin.net/yuqiao.zhang/cyber-wdwas:dev`（manifest PUT → 201，
  base 层全部 exists 复用，仅上传源码/wheels/pip 三层）；registry config digest
  `sha256:beb875094a076e4bd…` 与本地 tar 一致（用户/entrypoint 已比对）。

### v8 镜像：预算与 stage 晋升校准（2026-11-21）

- **问题诊断**（正式赛首轮 1/2 题复盘）：static_file_server 三次尝试合计 46 步
  未解出，全部 `budget_exceeded` 止损。根因有二：
  1. 步数窗口按意图重置（`reset_intent_window`），而阈值 `[30,50,70,100]`
     下 pro 档（stage2=50 步）在单意图预算内永远触达不了——全程 stage0
     盲试（日志全程 `thinking=0` 印证）；
  2. 单题累计 token 上限 600k 太紧（约 20-26 步即耗尽）。
- **arena.yaml 调整**（用户确认）：
  - `controller.thresholds: [8,16,24,32]`——8 步轻思考、16 步启用 pro 换路径、
    24 步深水、32 步心跳止损；
  - `controller.max_steps: 40`（与 tool_rounds=40 对齐）；
  - `budget.per_challenge_seconds: 2400→12000`、`claim_timeout_seconds:
    1500→3600`（时间限制保留，接近运行总时长，absolute_deadline 兜底）；
  - `budget.max_model_tokens_per_intent: 600k→5M`、
    `max_tool_calls_per_intent: 300→1000`；
  - `budget.max_model_tokens_total: 2000万→0`（**0 = 不设全局 token 上限**，
    用户决定 300 元额度只靠时间+步数限制约束；用量账本仍持久化）；
  - 防死循环护栏保留：stall_steps=30、repeat_limit=6、stage4 heartbeat。
- **验证**：364 passed / 1 skipped / 0 failed；容器内 load_config 确认生效值
  （budget 12000/3600/5M/global=0，controller [8,16,24,32]/40）；nocred
  rc=1 fail-closed 正常；镜像 `20defc2cabd9`（22 层，10001:10001 不变）。
- **推送**：`yuqiao.zhang/cyber-wdwas:dev` manifest PUT → 201，registry config
  digest `sha256:ff128135b628…` 与本地 tar 一致（已比对）。

### 仍未验收

- 真实凭据与平台的端到端联调（BAIZHI_API_KEY 真实调用、Arena 任务/附件/提交回执）未执行，需要明确授权后在正式平台进行。
- 镜像交付方式未定：仓库推送（`portus.in.chaitin.net`，v7 已验证可用：命名空间 `yuqiao.zhang/cyber-wdwas`、`scripts/push_tar.py` 推送、`:dev` tag）或 `docker save` 文件上传（需平台通道与大小限制）。最终比赛交付 tag 待平台确认后推送。

## 2026-11-22 测试体系升级：接线/回放/减法/守门（v15 后补强）

正式赛 2 题复盘暴露"8 题自测拦不住线上事故"的结构性原因：自建测试
验证自建假设，sim8 只测解题编排语义，靶机交互路径从未通电。四层补强：

1. **接线测试**（tests/unit/test_wiring.py）：arena 协议 build_runtime
   装配断言（authorized_arena_targets → ARENA_NETWORK_CIDR 注入、
   harness/worker 类型）；平台材料（start_info/arena_help）进模型首条
   消息的可见面断言。
2. **契约回放库**（tests/fixtures/golden/ + tests/framework_golden.py）：
   平台真实响应形状固化（submit_correct/submit_wrong/start_with_addr/
   start_no_addr/platform_env_snapshot），每条标注 source 与 confidence
   （observed/shape-inferred）。stub 回放真形状而非理想化假设。真环境
   观测到新字节后回填本目录。
3. **减法环境测试**：测试环境变量集合 ⊆ 生产快照
   （platform_env_snapshot.json）；arena_http 在 CIDR unset 时拒绝信息
   必须明确（"ARENA_NETWORK_CIDR is required"）。
4. **结论守门**（llm_worker + tests/unit/test_claim_guard.py）：远程题
   （有 start_info/help/运行环境 note）工具循环 REMOTE_NUDGE_AT=5 轮
   0 次 arena_http → 注入 REMOTE_NUDGE；0 次 arena_http 却声称
   "靶机未启动/不可达" → UNREACHABLE_GUARD 拒收重写一次（不消耗契约
   重试预算），二次放行留痕。有 arena_http 证据的不可达结论不拦。

验证：421 passed / 1 skipped（+7：守门 4 + 接线 3 + golden 接入改写
stub）。无真环境（用户确认），比赛协议 v2 的开局探针阶段为下一优先级。

## 2026-11-21 第二场复盘 + v16 交付

第二场（2 题）v15 镜像 2/2 全解（weak-password ACCEPTED 1000 分；
static_file_server 前 25 轮零 arena_http 全 python_exec 仍解出）。
三事实与修复（commit 8504d45）：

1. **契约谜底**：start 响应 `container_addr=[]` 空数组（两题皆然）——
   平台不返回地址，模型靠 v15 注入的 arena_help + 题面占位符推导成功。
2. **认知修正**：python requests/httpx 默认走平台预置 HTTP_PROXY(18082)
   隧道可达；真正不可靠的是手写 socket/禁用代理直连。v15"python 网络
   请求不可靠"措辞过度保守 → env_note/DEFAULT_SYSTEM/NUDGES 改三分类
   （requests 环境代理可用 / socket 直连不可达 / arena_http 最可靠优先）。
3. **险情**：start 拉容器实测 32.75s vs client_timeout=40s 贴线 → 90s。

start 诊断补 container_status/expires_at；golden 回填实测形状。
预算校准（740e07e）：per_challenge_seconds 12000→4500、claim_timeout
3600→1800（19 题/4h 口径，全链覆盖核查含 supervise.py --seconds 注入、
热更字段、三处 3 环境一致、run_official.sh 注释漂移修正、arena.yaml
死配置同步）。

**v16 交付**（镜像 1191b877be42 = 5f982ec+6cd3dc7+740e07e+8504d45）：
镜像内 pytest 421 passed / 1 skipped；无凭据 rc=1 fail-closed（token
from: none 明确报错）；push portus dev manifest PUT → 201；内容级对账
PASS（user=10001:10001、entrypoint=[bash, /app/scripts/run_official.sh]、
RootFS 层数含顺序全一致；Id 不同系 containerd image store 正常现象）。

留观项：remote nudge / unreachable guard 触发情况、container_status
实测值、ClaimTimeout 频度（如频现先回调 claim_timeout）、正式考题数
是否 19 以验证预算口径。
