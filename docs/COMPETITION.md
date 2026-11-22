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

## 2026-11-21 第三场复盘 + v17 交付

第三场 2/2 全解（v16 首战，attempt 01a1fc4f）。v16 四项修复实测生效：
预算 4500/1800（[run] budget/[orch] 行确认）、client_timeout=90s
（**立功**：static_file_server start 实测 40.63s，40s 上限必超）、
start 诊断新字段拿到真实值、remote nudge round 5 正常触发。

新契约发现：**start 返回 ≠ 容器就绪**——weak-password
status='available'+expires_at 有值（约 start+30min 租期）；
static_file_server status='pending'+expires_at=None（拉起中）。
本次成功靠模型先分析附件拖过拉起窗口；正式赛开局即请求靶机有
"未就绪→探测失败→误判未启动"重演风险。

修复（7b4b7af，纯信息增量不改控制流——重复 start 幂等性未知，
轮询不做）：env_note 写入容器状态（非就绪提示"等 30-60s 重试，
不要判未启动"）与租期；DEFAULT_SYSTEM 补 JSON 数组格式要求
（两题各浪费一次 preconditions 契约修复调用）；golden 回填两态
实测；+2 测试。423 passed。

**v17 交付**（镜像 943a094227f7 = 8504d45+7b4b7af）：镜像内
pytest 423 passed；无凭据 rc=1（token from: none 明确报错）；
push portus dev manifest PUT → 201；内容级对账 PASS（user/
entrypoint/cmd/env/RootFS 层数含顺序全一致）。

## 2026-11-21 第四场复盘 + v18 交付

第四场 2/2 全解（累计 2000 分，attempt 01a1fc6a）。亮点：round 17
首次出现 arena_http 调用（remote nudge 后第 12 轮）——守门机制
首次引发模型行为改变；随后模型回退 python_exec 并解出。

两个交付级问题（16bbffa）：
1. **v17 P2 落点错误**：生产系统提示是 ctf_ext/prompts/worker.md
   （2952 chars，PROMPT_PATH 加载），DEFAULT_SYSTEM（496 chars）
   只是无 prompt_path 时的回退——数组格式提示打在回退文件上，
   对生产无效，契约违规照旧。教训：改系统提示必须先确认生产
   加载路径；覆盖核查要包含"修改是否真的到达生效路径"。
2. **版本不可考**：四场日志 system_chars 恒 2952 与版本无关，
   无法归因某场跑的哪个镜像。修复：仓库根 VERSION 文件（与
   镜像 tag 对应）+ run_official.sh 首行打印 image_version。

v18 修复：worker.md 输出契约段补类型约束（preconditions/actions/
ruled_out 必须 JSON 字符串数组；strategy_key 恰好三段
domain.technique.variant）+ VERSION 版本横幅。423 passed。

**v18 交付**（镜像 db7686a0a8d3 = 7b4b7af+16bbffa）：镜像内
pytest 423 passed；无凭据 rc=1；push manifest PUT → 201；对账
PASS（六项全 OK）。下次日志首行 [run_official] image_version=v18
即可归因版本。

注：第四场无法确定跑的是 v16 还是 v17（env_note pending 提示
是否已生效未知）；v18 起版本可归因。v18 已包含 v17 全部修复。

## 2026-11-21 第五场：v18 全项生效验证（2/2 全解）

attempt 01a1fc7b。三项 v18 修复实测生效：
1. `[run_official] image_version=v18`——版本归因根治；
2. `system_chars=3117`（此前恒 2952）——worker.md 修改到达
   生产路径；weak-password `contract ok (attempt 1)` 一次通过，
   **首场零契约违规**（前四场每题浪费一次修复调用）；
3. weak-password 2 轮解决（前几场 3 轮 + 违规修复）——prompt
   约束直接收益。

static_file_server 第三次稳定复现 pending+expires_at=None 形态；
remote nudge round 5 正常触发；30+ 轮全 python_exec 无 arena_http
仍解出（python_exec + 平台 HTTP_PROXY 隧道路径稳定可用）。结局
用户确认解出，2/2（累计再 +2000）。

结论：v18 无回归，连续三场 2/2，修复链稳定。正式考（19 题/4h）
前无需新改动；待观察项只剩 19 题场景的预算口径与 endgame 实战。

## 2026-11-21 第六轮：v19 工具补齐交付（对照参考工程清单审计）

用户对照两份其他工程参考清单要求审计工程缺口。以 v18 镜像逐项实测为准
（两份清单互相矛盾且不完全对应当前镜像；清单来源确认为"其他工程参考"）。

**v18 镜像审计结果**：24 个注册工具的外部命令 20/21 OK、41 个 Python 库
15 OK；git 实际存在（清单说无是错的）。缺口分级：P0 = tesseract 缺失
（ocr_text 已注册但主命令不存在，调用必败；且默认 lang='eng+chi_sim' 必须
连中文语言包一起装）；P1 = skills 已承诺场景缺弹药（pwn.mips64/
reverse.riscv.firmware 的 qemu 多架构/gdb-multiarch/unsquashfs，
forensics.pcap.triage 的 tshark，crypto.lattice.lll 的 fpylll，
web.java.deser 的 Java 环境）；P2 = 纯 Python 库（ecdsa/py_ecc/bs4/lxml/
httpx/scapy/pefile/primefac/qiling/LIEF/cysignals）。用户确认 P0+P1+P2 全修。

**v19 交付**（镜像 87dfcd2e92ae）：纯环境增量，零代码改动（唯一代码变更是
预埋测试 test_ocr_pdf_tools 的接线修复：渲染子进程 SYSTEM_PY→sys.executable
——PIL 在 /opt/venv 而系统 python3 没有，v18 时该测试因 tesseract 缺失
被 skip 掩盖）。基础镜像实为 Debian trixie（deb13），v12 构建时删除了
apt/dpkg 二进制——deb 安装采用"trixie 容器清空 status 全量闭包收集
（262 deb，314MB）+ ar 引导 dpkg + dpkg 三连跑（--force-depends + configure -a）"
离线方案。工具矩阵 32/32 OK（tesseract+chi_sim/eng、gdb-multiarch、
qemu-x86_64/aarch64/mipsel/riscv64、unsquashfs、tshark、java/javac、ruby、
one_gadget）；新增 Python 库 import 11/11 OK；site-packages 清单对比
关键库 13/13 SAME 零移除（unicorn/capstone/pyelftools/pycryptodome/sympy/
z3/gmpy2/pwntools/requests/numpy/scipy/pillow/pyyaml——契约防御真绿灯，
注意 venv 无 pip 模块，freeze 比对是空对空假绿灯，必须用目录清单比对）；
镜像内 pytest 424 passed（423 基线 + OCR 端到端激活通过）；无凭据 rc=1。

**跳过项（P3 待办）**：seccomp-tools（racc gem 需 native 编译链，
ruby-dev 闭包 + gcc 风险大）、CFR 反编译 jar（下载失败）、JNDIMap（repo
未定位）、qilingrootfs（rootfs 资源未下载，qiling 本体已装）、fpylll
（无 cp312 wheel）、hashpumpy（C 扩展无 wheel，python_exec 可手写替代）；
反编译器（Ghidra headless/radare2）需要 catalog 新增工具 spec，另行开发。

**本轮教训**：a) Dockerfile RUN 链尾部的 `|| true` 会吞掉全链失败
（首版 build "成功"但 apt/pip/gem 全没装——bash 无此文件、无 apt 二进制
全被掩盖）；b) 版本对齐必须先实测基础镜像发行版（trixie vs bookworm 差
一个世代，闭包混装被 breaks 拦截）；c) devbox 磁盘 40G 是硬约束——
build 中间层每轮 ~1.5GB，失败重试前必须 prune（本轮清理后 82%→73%）。

## 2026-11-21 第七轮：第 19 题正式赛复盘与 v20 稳定性修复

19 题/4h 正式赛（用户中途主动停止）：解 6 错 4，35 分钟无模型调用。
日志复盘确认三层故障叠加，均为 2 题小场从未触发的长跑缺陷：

1. **fork EAGAIN 根因**：python_exec 解题脚本 spawn 的孙进程（qemu/JVM）
   退出后成孤儿僵尸，容器 PID1（bash）不收割 → 进程表耗尽 → fork 阻塞
   （BlockingIOError）→ 工具层持续 TOOL_BUSY → worker 全卡。
2. **发现层盲区**：supervisor 只判存活心跳（pump 空转时也在刷），而
   claim_timeout(SIGALRM)=1800s 要 30 分钟才打断一次卡死 worker，打断后
   重开 intent 又卡——"35 分钟无模型调用"与 30 分钟硬杀线精确吻合。
3. **预算浪费主凶**：max_tokens=4096 截断（finish_reason=length）8 次，
   worker 无 in-claim 重试直接报废 attempt，同题 3 连败
   provider_error_exhausted blocked；平台环境问题（container_addr 未返回、
   ${challenge_host} 占位符未展开、附件缺失）另烧多题 blocked。

**v20 修复**（430 passed 1 skipped，较 v19 基线 +7 个新测试）：
- `runtime/progress.py` 推进心跳：模型调用/工具完成/结果落库才刷新，
  与存活心跳分离；`supervise.py --progress --progress-stale-seconds 600`
  停更即重启（"活着但不推进"兜底，任何未知卡死分钟级可发现）；
- `runtime/reaper.py` PR_SET_CHILD_SUBREAPER + waitpid 轮询线程：收养并
  收割孤儿僵尸，治 fork EAGAIN 根因（macOS fail-open 返回 unavailable）；
- max_tokens 4096→8192 + worker 截断精简重试（限 1 次，防循环烧 attempt）；
- blocked 调度设计复查：intent 级熔断 + planner 终态不扩展已健全，不改。

**教训**：长跑正式赛（19 题/4h）是独立于小场 2/2 的测试面——资源累积、
预算口径、平台环境数据问题都只在长跑暴露；supervisor 的"活性"与
"推进"必须分开度量，存活不等于前进。

## 第十七轮（v30，2026-11-22 模拟赛日志复盘：flag 前缀通用化与 4 小时持续在途）

**背景：模拟赛（2026-11-22）跑不满 4 小时的归因**。用户定调：不是解题不行，
是 agent 设计跑不完 4 小时、被各种限制打断。日志复盘（20+ 无时间戳 llm/orch
流）三条根因：

1. **提交错误 = 我们的规则毁掉正确答案**。blocked reason 里明确显示
   `0xfun{...}`／`csawctf{...}` 前缀被硬编码 `flag{` 模板拒收/改写（
   TLSB、1black0white 等 5 道提交错误题中至少 3 道因此错失）——worker.md
   提交纪律段“必须形如 flag{...}”“硬凑格式禁令”是模型 blocked 的直接
   指令来源，契约层 FLAG_RE 与 gate 层 FLAG_PATTERN 同罪。
2. **吞吐衰减 = 单 claim 轮次过深上下文滚雪球**。round≥30 出现 126 次、
   msgs=48、单请求上行 22-54K token、P99 28.9 秒；budget_exceeded 仅 1 次
   ——不是预算掊的。
3. **意图供给枯竭**：max_intents_per_challenge=12 中段烧完后题进入静默，
   剩余数小时无意图在途；endgame 授权一次后 pass2 再 blocked 就永久作废。

**改动一：flag 前缀通用化（匹配宽松 + 提交保真）**。

- 匹配层（FLAG_PATTERN/EVIDENCE_PATTERN/契约 FLAG_RE）：任意 CTF 前缀
  `<pre>{...}`（首字符字母数字 + 字母数字连字符，总长 1-31）+
  IGNORECASE（大写 FLAG{ 形态照识别）；**排除“以 flag 结尾的拼贴前缀”**
  （K01 对抗案例 prefixflag{...}：错前缀提交会被平台判 REJECTED 关闭
  题目，宁可漏提取也不提交错前缀）。
- 提交层（normalized_flag）：前缀与大小写逐字保留（case_mode='preserve'
  语义落到归一层）——旧版硬写成 flag{ 并 casefold 会把正确答案改错。
- 命中判定/幂等去重换新函数 flag_identity：方括号包装归一为花括号 +
  casefold（C03 语义不变：大小写/包装变体走 L2 审阅而非直接拒收；同源
  变体幂等键一致防重复提交）。
- gateway 4 处比对/幂等键（key、同答去重、反篡改声明比对、平台格式修正
  比对）全部换 flag_identity；worker.md 提交纪律与第 4 条命中规则改为
  任意前缀逐字保留、禁止改写前缀。
- 不变量三确认：C03 变体仍需全票 L2；C01 缺证据仍 L4；幂等键同源同键。

**改动二：endgame 从“一次作废”改“冷却循环”**。

- _endgame_granted 由 set 改 Dict[cid→授权时刻]，同题两次授权最小间隔
  900s（budget.endgame_cooldown_seconds 热配置可调）；冷却期内跳过、
  期满重新纳入候选（pass2 消费完的 intent 不占 attempt_key，blackboard
  只拦 READY/RUNNING/FAILED_RETRYABLE——已验证）。DuplicateStrategy 分支
  用 setdefault 保留更早的真实授权时刻。4 小时内题不再永久静默。
- max_intents_per_challenge 12→50（config.py 校验上限 20→100）：短
  followup 成本低，配合 best-effort 提交 + REJECTED 重开 + endgame 冷却
  形成持续意图供给；token/时间预算墙仍兑底。

全量 462 passed 4 skipped（新增 test_flag_prefix_generalized、
test_endgame_cooldown_allows_regrant；K01/C03 等框架测试在双分支正则与
identity 拆分后全部原生通过）。遗留（赛后项）：单 claim 上下文膨胀治理
（接力机制细化）、prewarm/files 清单并行拉取、镜像瘦身、crypto 技能卡补
AMM 纯 Python r-th root 片段（sqrt163 类题靠 gmpy2+sympy，不装 sage）。

## 第十六轮（v29，2026-11-22 技能别名审计：泛词挤槽修复与中文触发词落地）

**技能卡中文别名机制（63 卡 aliases + 域归一）审计落地**。打分链：题面
抽 ASCII 词（≥3 字符、去重）给卡打分，域精确 +6、keywords +3、aliases
反向扫题面 +7（每卡一次），≥5.0 入围。审计结论：边缘策略（ASCII 侧补
词边界、中文侧不补，"Shiro反序列化" 粘连命中、"javascript" 不误伤
"java"、"self" 不命中 "elf"）实测全对；主要缺陷是 **5 张卡配泛中文词
alias 会跨域挤槽**——misc 纯中文题提"编码"→ crypto.encoding 排第 1
挤掉 misc.unicode；pwn 题提"反汇编"→ reverse.elf.static 占第 2 槽。
修复：

- 删泛词 alias（编码/解码/静态分析/反汇编/压缩包/随机数/流量分析/数据包
  及分类词"逆向"），保留窄专名；判定标准 = "aliases 回答'这题就是考这
  个'"，泛过程词回答不了；
- web.intranet.relay 的 domain "渗透"→"web"（打分归一无碍，但
  index_text 目录按原始 domain 分组会分裂出独立"渗透"行）；
- 评测集 web_intranet_relay 用例的 forbid web.ssrf→crypto.rsa.small_e：
  relay 卡 trigger 本就是"SSRF 链"，同域 ssrf 作为第 2 候选正当——旧
  约束是域分裂时代的产物（relay 域"渗透"时 ssrf 从不同入围）。该回归
  由未提交的 alias 改动自带，首次全量（460）才暴露；
- 新增 6 个回归测试：粘连命中、ASCII 边界不误伤、泛词不挤 misc/pwn 槽
  （2 例）、整词 rust 合法命中、substring 防误伤改用纯 "trust" 验证
  （原断言依赖零命中侥幸通过，alias "rust" 落地后必然翻车）。

全量 460 passed 4 skipped。验证复核：misc 纯中文题提"编码"→
crypto.encoding 落选、misc.unicode 第 1；pwn 题提"反汇编"→
reverse.elf.static 落选。

## 第十五轮（v28，2026-11-22 终局吞吐对齐与大文件读取纪律）

**endgame 授权按空闲槽位封顶**。11-21 复盘：剩 20 分钟时一次抬升 14 题，
实际并发只有 3——11 题的预算抬升等于没发生，还占住槽位让长跑线无法正常
收尾。改动：

- Orchestrator._grant_endgame 拆为 _endgame_grant_limit（显式配置
  budget.endgame_max_grants 优先；0=自动取调度器空闲槽位 free_slots()=
  max_running_projects − 有在跑意图的题数；查询失败退回线程数 fail-open）
  与 _endgame_candidates（候选判定沿用 v12/v21 语义，新增 points_hint
  降序——同分按题号）；
- 无空槽时本轮不授权（返回 False 不写事件——监控线程每 poll 调用，写
  事件会刷爆表），槽位回收后由下一轮 poll 补授；
- 审计修复：bootstrap 的 _challenges 缓存此前没有 points_hint 键，候选
  排序会静默退化成 challengeId 字典序——高分优先在生产不生效（测试手动
  塞键所以绿）。补键修复。

**大文件读取策略入 Worker 提示词**。事实层：单次工具输出只有前 4000 字节
进上下文（与 max_tool_result_bytes 一致，测试锁死防漂移），完整原文只在
证据库。纪律层：先定位再取值（python_exec 匹配只 print 命中行）、反汇编
走 symbols→按地址区间读 hex→disasm_bytes 取片段（disassemble 全量输出
注定截断）、strings/hexdump 先收窄、同一文件不重复读且结论立刻落 facts。
不含 shell 教唆（环境无 shell）。

测试：free_slots 三态、授权封顶+高分优先+幂等补授、显式配置覆盖、提示词
预算一致性；全量 455 passed 4 skipped。

## 第十四轮（v27，2026-11-22 四份归档对照定案）：非攻坚轮显式 thinking disabled

用户提供 4 份平台归档（trajectory / attempt-archive 157MB / model-calls /
11-19 对照场）逐条对照后定案。attempt 01a1fe63（11-21，20 题 5 解出，
weightedScore 5000，tokensIn 40.25M）的 1568 次调用统计：

- **95% 的"未下发 thinking"轮实际在思考**（1450 次里 1380 次 reasoning>0）
  ——官方文档确认 thinking 默认 enabled 且默认 effort=high，不传参数
  ≠ 关闭，thinking_by_stage 的 0 档形同虚设；
- **58 次 finish_reason=length 截断里 54 次来自未下发轮**（enabled 轮仅
  4 次）——v24 无响应侧拦截，每次截断报废一个 attempt；23:31:50 最后
  一次响应即此形态（reasoning 7865/8192、python_exec args 截断于 data[）；
- 1567 次 deepseek 调用零 400：官方文档称工具场景不回传 reasoning_content
  会 400，实测未触发——按实际行为降为观察项。

修复（6a822c1）：openai_compat 非 0 预算下发 enabled、0/未传显式
`{"thinking":{"type":"disabled"}}`；公司网关通道（Anthropic 风格
budget_tokens）不受影响。深度思考集中在 stage1/2 攻坚档（117 次 enabled
轮照旧），侦察/收尾轮不再自主烧 mt——54 次截断的来源从源头消失；残余
（enabled 轮 effort 非硬上限）由 v26-C 响应侧拦截兜成可恢复重试。

风险声明：stage3/4 复盘轮偶尔确需推理，若下场比赛侦察质量下降（ruled_out
变浅、路径多样性差），回退为 stage3 给低预算 enabled。验证指标：下场比赛
agent.log `tool_calls truncated` 应个位数或零，非攻坚轮 reasoning_tokens
应归零。

镜像交付：competition-v26 push portus dev 对账三层一致（RootFS 40 层）
+ 远端复测 455 passed 后被 v27 覆盖；v27（FROM v26 只重跑 COPY 层）build
→ 镜像内 pytest → push → 对账 → 复测同批完成。过程坑：devbox 磁盘两次
耗尽（v24+镜像 6.2G×N 堆积），`docker image prune -af` 误删 overlay 基础
镜像 v18/v24——从 portus dev 拉回 v24 改 FROM 续建；prune 前必须核对
FROM 依赖链。

## 第十三轮（v26，2026-11-22 平台日志归因）：reasoning 截断卡死与重启循环

平台侧数据还原故障链（23:30:57 → 23:42）：最后一次模型调用 stage2
reasoning 吃掉 7865/8192 tokens，python_exec 的 args 以 data[ 结尾被
max_tokens 掐断（finish_reason=length）；此后 11 分钟零模型请求（卡点
无栈可查，pod 回收后日志丢失）；supervisor 判 stale progress 660s 重启
3 次（660/662/664s）——重启间隔仅 2s，新进程从未获得机会，exit 241
（SIGTERM -15 无符号透传）。

- 修 A（supervisor 预热期，实锤）：progress 文件跨重启持久，旧 mtime 让
  新进程启动几秒内被判死。改为运行超过 progress_stale_seconds 仍无推进
  才判卡——重启后获得完整 600s 预热。
- 修 B（栈 dump 归因）：supervisor terminate 前先发 SIGUSR2，agent 入口
  faulthandler.register(SIGUSR2) 把全部线程栈 dump 到 stderr → agent.log。
  "活着但不推进"的卡点从猜测变成看栈。
- 修 C（截断 tool_calls 拦截）：stop_reason=length 且带 tool_calls 时注入
  TRUNCATION_CORRECTION in-claim 重试（限 1 次），不让残缺 args 进执行路径。
- 修 D（thinking 降档）：thinking_by_stage [0,4000,8000,0,0] →
  [0,3000,5500,0,0]（mt=8192 硬上限下 stage2=8000 只给 args 剩 192 tokens，
  必然截断）；thinking 轮 request timeout 联动放大（180+thinking/20，
  上限 455s < progress_stale 600s - 工具超时 30s）。
- v25 的退出码修复（solved==tasks 才 0）与本事故独立但同批交付。

教训：慢响应/截断/卡死三类问题的归因都依赖"死前的栈"——faulthandler
信号 dump 是比日志更根本的观测面。

## 第十二轮（v25，2026-11-22 实战"5/8 题解出后容器提前死亡"）：退出码与日志落盘

实战现象：解出 5 题（总题数>5）后容器退出、无日志、剩余约 10 分钟模型零调用。

- 根因一（实锤）：main() 退出码旧语义 `0 if solved else 3`——部分解出后的
  任何退场（worker 全止损/intent 终态/异常）都 rc=0，supervisor 对 rc=0
  无条件放行不重启 → 容器提前死亡。解得越多越容易提前死亡且无救援。
  修复：solved==tasks 才 0；部分解出提前结束返回 3 → supervisor 有限重启
  续跑（deadline 与 min_remaining 兜底不超比赛窗口）。
- 根因二（代价放大器）： supervise.py 无日志落盘——平台 pod 回收后日志
  随之丢失（"没有日志"），退场前的异常类型无法归因。修复：supervisor
  --log-file 把子进程 stdout/stderr 透传平台的同时追加写
  RUN_DIR/agent.log，含每次 launch 分隔行。
- 待日志确认：worker 全退的异常类型（三线程各自 3 连异常止损？intent
  全终态空转？）——日志到位后针对性加固。

## 第十一轮（v24，2026-11-21 镜像工具补齐）：wasm2wat/ffprobe/procyon 落镜像

v23 镜像实测 which 验证 tshark/mmls/fls OK，但 wasm2wat/ffprobe/procyon
MISSING——toolchain.json 声明不等于安装，三个工具不在 deb 闭包（bookworm/trixie
无 wabt/procyon 包）。补齐路线：

- ffmpeg：devbox 内网镜像 mirrors.dev.in.chaitin.net/debian 可达，trixie 容器
  换源 --download-only 拉 ffmpeg 闭包 281 deb 并入 debs/（447 个）；
- wasm2wat：wabt 1.0.34 release 静态 ELF（4.8MB）→ opt/wabt/，Dockerfile
  symlink /usr/local/bin；
- procyon：mstrobel v0.6.0 jar（2MB）→ opt/procyon/，java -jar wrapper
  （java-jdk 已装）；
- overlay.Dockerfile 增 USER root RUN 装配层（v23 tool binaries 标记），build
  时自检三命令版本输出。

教训：镜像工具验证必须 which + 实跑，不能只看 toolchain.json。

## 第十一轮（v31，2026-11-22 模拟赛长跑复盘）：并行度 3 拉满与资源不空转

模拟赛日志复盘后的六处改动，目标：parallel=3 全程拉满、无空转、稳定跑满 4 小时：

1. **no_signal 链深限制**（planner）：NO_SIGNAL_CHAIN_LIMIT=3，无信号分支不再
   无限延伸烧预算。
2. **话痨截断**（llm_worker）：CHATTY_TOOL_TEXT_LIMIT=6000，工具轮附带长散文
   硬截断进历史（实测单轮 15337 chars、40 轮滚到 112K，P99 28.9s 主成分）；
   最终 JSON 输出不受限。
3. **契约修复轮轻量化**（llm_worker）：CONTRACT_COMPACT_REPAIR 只带失败原因与
   上一轮输出原文（12000 chars 上限），tools 置 None；不再全量重发 40 轮历史
   （每题省 1-3 次全量调用）。附 evidence_id 只引真实返回的 64 位 hex 规则提示
   （实测模型编造 id 触发 unregistered 拒收）。
4. **收尾边界修复**（llm_worker）：`if not tools or tool_rounds >= max_tool_rounds`
   ——无工具配置时也能进收尾路径。
5. **契约错误熔断**（orchestrator）：contract_error_max_attempts=2，同题契约
   连续失败熔断进 blocked，不再无限烧。
6. **endgame 三修**（orchestrator+scheduler）：
   - `backfill_slots()`（scheduler）：空闲槽位扣除可领取的普通队列，尾盘不抢占；
   - `_grant_endgame` 授权额度累加 `grants[cid] += bonus`、`limit=min(grant_limit, available)`；
   - `_endgame_candidates` 冷却循环 + drained 判定：pass2 消费完 + 冷却期满可
     再次授权，普通队列耗尽的未解题不再永久静默（授权 1 次后 pass2 blocked
     导致题数小时无意图的实测问题）。

配套：worker.md 并行工具调用引导；新技能卡 crypto.amm.root.md（AMM 类密码题）
+ index.json（64 条）；`_cleanup_finished_workspaces` 仅在有 ACCEPTED 提交时
删工作区（防误删未提交证据）。

测试：test_planner +2、test_llm_worker +2（话痨截断/compact 修复）、
test_provider_error_breaker +3（熔断）、test_concurrency endgame 冷却循环 +
槽位封顶共 24 项针对性回归通过。

已知限制：串行 `_run_serial` 的 endgame 冷却空洞未修（has_waiting_work=True
时永不调 _grant_endgame）——实际比赛 parallel_agents=3 走并行路径，监控线程
每个 poll 调 _grant_endgame，无此空洞；时间紧不修。镜像内 sympy/gmpy2 依赖
只能在 devbox 构建链验证（macOS 无该环境）。

## 第十轮（v23，2026-11-22 工具包扩展）：只读工具 24→30 与技能 8 条

实测复盘（pcap/EXE/流量题反复用 python_exec 手写 tshark/反汇编逻辑，40 轮磨满
仍 partial）后扩展专用工具面：

- 新增 6 个只读固定 argv 工具：pcap_summary(tshark io,phs)、
  wasm_translate(wasm2wat)、java_decompile(procyon)、media_probe(ffprobe)、
  disk_partitions(mmls)、disk_files(fls，offset 定位分区)；
- policy command_allowlist 补 tshark/wasm2wat/ffprobe（procyon/mmls/fls 原有）；
  全部 risk_level=read 不触发沙箱审批；
- toolchain.json：tshark/ffmpeg/wabt allowlist=true（镜像层默认安装）；
- skills index 新增 8 条（web.xss/xxe/upload、pwn.format_string/ret2libc、
  reverse.go、misc.unicode、crypto.aes.nonce_reuse），sections/*.md 全部落盘；
- 测试：test_readonly_tool_pack.py ×8（镜像内全激活）。

日志复核修正：v22 轮曾将"tool_round_limit 收尾轮 tool_calls"判为实际发生——
复核确认该 ok 行（p=38945）与收尾调用（chars=104660→p≈5 万+）不匹配，属并行
题交织误配对；pcap 题实际成功走完收尾（partial）。v22 纠正修复保留为防御性。
transport failed ×3 同瞬报废与 TOOL_BUSY 实锤不变。

## 第九轮（v22，2026-11-22 实测日志两缺口）：传输瞬态与收尾轮纠正

实测日志（三并行 worker 同时 transport failed + pcap 题 tool_round_limit 收尾轮
模型仍返回 tool_calls）暴露 v21 未覆盖的两个 attempt 报废路径，同款 in-claim
限 1 次重试补齐：

1. **传输类瞬态重试**（llm_worker）：ProviderError 含 transport/connection/
   temporarily 时 in-claim 重试一次（请求本身没问题直接重发）；此前三个并行
   attempt 同瞬间报废。
2. **无工具收尾轮纠正**（llm_worker）：tool_round_limit 强制无工具总结后模型
   仍返回 tool_calls（tools=0 请求下实测发生）→ 注入 FINAL_SUMMARY_CORRECTION
   重试一轮（限 1 次），不再直接报废 40 轮工作；二次仍违规保留 tool_round_limit
   失败码。

测试：test_v21_resilience.py 增至 11 个；本地 441 passed 1 skipped。

## 第八轮（v21，2026-11-21 审计后续）：四条灾难路径关闭

第二轮日志审计（事件频谱 + 逐题命运 + 代码对照）确认 19 题赛三类故障的完整机制链并修复：

1. **协作式 claim 超时**（llm_worker）：DeadlineGuard 的 SIGALRM 只在主线程生效，
   parallel=3 时 worker 全在子线程 → claim_timeout 30 分钟硬杀静默失效（watchdog
   降级现打一次性告警行）。work() 循环每轮开头自检墙钟，超 min(claim_timeout,
   claim.budget_seconds) 返回可重试超时（claim_deadline）；轮间打断最坏延迟一次
   模型/工具调用（各 120s 超时）。
2. **worker 止损隔离**（orchestrator._worker_loop）：并行模式单线程连续 3 次 step
   异常只退出自己（run_aborted isolated=true），不再 _stop.set() 杀全场；串行语义不变。
3. **python_exec 并发 1→3**（catalog._spec 加 concurrency_limit 参数）：parallel=3
   争抢时未抢到的立即拿 TOOL_BUSY 烧整轮（b49c4e1fd53f partial 死因），提到 3
   每 worker 各占一槽；其余工具默认仍 1。
4. **blocked 题复活**（blackboard.challenges_with_blocked_intents + endgame）：
   planner 对 blocked 不扩展、intents 又未打满，endgame 与 planner 互相推诿 →
   3 个平台问题题永久搁置、剩余时间空转。endgame 授权范围纳入存在终态 FAILED
   intent 的题（限一次，DuplicateStrategy 兜底）。

测试：tests/unit/test_v21_resilience.py ×7；本地 437 passed 1 skipped。
