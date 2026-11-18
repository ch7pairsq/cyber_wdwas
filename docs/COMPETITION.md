# 参赛镜像与公司模型约束

## 已确认的交付方式

采用 Harness 托管。交付入口按用户 2026-11-18 决定改为**双轨支持**，最终采用哪种待比对便利性后确定：

- 镜像仓库：需要平台可访问的仓库地址／命名空间，以及推送／拉取凭据。仓库地址指容器镜像引用（仓库、镜像名、标签或摘要），不是 Git 代码地址，也不是网页浏览链接。
- 文件上传：`docker save` 把成品镜像打包为 tar（可压缩），经平台通道上传。需确认接受格式与加载方式（docker load）、大小限制、上传截止时间、是否可重传；交付时同时提供 tar 文件的 sha256 供平台核对。注意 tar 文件摘要与镜像摘要不是同一个值，两者都要记录；上传完成后应在干净环境 load 回来复跑 `check_competition.py`，确认交付物本身可回放。

无论哪种入口，比赛时平台只拉取／加载成品镜像并启动，不能执行 pip install、apt install、uv sync 或其他补装步骤。Docker Engine 与 Compose 已经存在；此前失败发生在基础镜像拉取，不是缺少 Docker。

## 已确认的模型配置

- 主通道：公司百智云网关，Base URL https://ai-api-gateway.app.baizhi.cloud/api/anthropic ，Anthropic Messages 兼容协议（POST /v1/messages、x-api-key、anthropic-version），唯一 model ID deepseek-flash。
- 备选通道（2026-11-18 用户新增）：DeepSeek 官方 API，OpenAI Chat Completions 兼容协议（POST /chat/completions、Bearer 鉴权），Base URL 固定 https://api.deepseek.com ，model ID 仅允许 deepseek-chat／deepseek-reasoner。切换只改配置 `model.provider`（`baizhi_anthropic` ↔ `deepseek_official_openai`），业务代码零改动；切换前须确认平台出口白名单覆盖 api.deepseek.com，真实调用另行明确授权。本条修订此前"不直连厂商"的限制。
- 两条通道互斥，参赛候选当前默认仍走公司网关。两通道执行同样的传输纪律：不继承任意 HTTP_PROXY、不跟随重定向、校验 TLS、不发送 tools/web_search 定义、响应大小上限，对非文本工具响应和模型 ID 不一致的响应拒绝处理。网关服务端实际模型路由仍须公司侧确认；两条通道的真实联调均未执行，本轮没有读取、使用或提交真实 Key，也没有付费调用。
- 凭据环境变量固定为 BAIZHI_API_KEY（2026-11-18 用户确认，两条通道共用；切换通道时变量名不变，值对应所选通道的 Key）。Key 只能运行时注入，不打入镜像，不写配置文件与数据库快照。
- 可选 Key 落盘缓存（2026-11-18 新增，默认关闭）：环境变量在进程重启与容器重启时都会保留，只有"平台重建容器且不重注入、并挂载持久卷"的场景才需要落盘兜底。启用后缓存文件 0600 原子写入，不进 SQLite／证据／报告／日志，本轮运行正常结束时清理；是否启用待平台确认挂载与重启语义后再定。

接口路径名称中的 anthropic 代表兼容协议，不代表调用 Claude；两条通道请求中的模型都是用户指定的 DeepSeek 系列。Provider 不发送 tools/web_search 定义，对非文本工具响应和模型 ID 不一致的响应拒绝处理。

## 镜像内容和阶段

同一个 Dockerfile 包含公共 runtime 阶段，以及两个交付目标：

| 目标 | 用途 | 包含内容 | 能否直接参赛 |
| --- | --- | --- | --- |
| development | 赛前开发、离线测试、断点调试 | 运行依赖、Cairn、pytest、httpx、debugpy、安装器 | 不能 |
| competition | 参赛候选镜像 | 应用、提示词、配置、Cairn 全部运行依赖、预装系统工具；移除常见安装器和调试模块 | 当前不能，官方接线待完成 |

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

## 当前启动门禁

competition 目标的默认入口为 python -m ctf_ext.competition，当前明确退出 78，说明官方 Harness 规范与入口尚未接通。它不会偷偷回退 Mock、安装包、联网或触发比赛提交。即使人为设置 official_sdk_ready=true，现有 CLI 官方接线也仍然未完成。

这是候选镜像的误提交保护，不是生产可用的参赛入口。禁止把它作为成功运行的参赛镜像发布。

## 等待用户／官方提供

1. 交付入口二选一（双轨均已支持，用户比对便利性后定）：仓库方案需平台可访问的镜像仓库／命名空间、推送／拉取凭据；文件方案需上传通道、接受格式（docker load）、大小限制、上传截止与重传规则。目前不推送镜像、不上传文件。
2. 第 2.2 节 Harness 托管全文：平台启动命令、协议／SDK、注入变量、任务／附件／提交方法、结束／重启行为。
3. 公司网关确认的 Messages 路径、鉴权头和使用额度；如启用 DeepSeek 官方备选通道，其真实联调、出口白名单与额度另行明确授权。
4. 平台 CPU 架构、资源／镜像大小限制、网络出口规则，以及最终必须预装的工具集合。

上述信息齐全后再完成官方接线、赛前全链路验证、镜像发布（或文件上传）与摘要固定；不要在比赛过程中安装依赖或修改协议。

## 本地构建候选镜像与离线验收

以下只在赛前开发主机执行：

~~~bash
docker build --target competition \
  --build-arg TOOLCHAIN_TIER=ctf \
  -t cyber-wdwas:competition-candidate .
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  --entrypoint python cyber-wdwas:competition-candidate /app/docker/check_competition.py

# 当前预期退出 78；不是运行成功，也不能作为参赛通过依据。
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
