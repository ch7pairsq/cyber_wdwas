# Docker 赛前开发、运行与调试

本页仅适用于比赛开始前的 development 镜像；不是正式 Harness 参赛流程。比赛时不得安装依赖。参赛候选镜像、禁止搜索与待确认的入口规范见 [COMPETITION.md](COMPETITION.md)。

## 开发镜像包含什么

- 项目源码、提示词、配置、测试与运行脚本。
- 仓库固定版本的 Cairn 0.2.1、其完整 Python 依赖，以及项目依赖。
- pytest、httpx、debugpy；第三方 Python 包版本集中在 docker/requirements.lock。
- Cairn 原始 `dispatch.local.example.yaml` 仅作为上游配置解析测试夹具加入 development；不会使用它启动真实模型 CLI，competition 不复制此文件。
- Python 3.12、Debian 精简系统、file、unzip、binutils、openssl 和证书。

不会把宿主机已有的其他 Docker 镜像、容器、数据卷、虚拟环境或密钥打进去。构建使用白名单 .dockerignore 和明确 COPY；基础镜像来自 Astral 官方 GHCR，并非宿主机业务镜像。Dockerfile 默认固定本次校验的 Linux amd64 镜像摘要，实际部署仍须记录成品镜像 ID／仓库摘要并核对平台架构。

这里的“全部依赖”是应用与 Cairn 的 Python 依赖，不是宿主机全部软件。sage、binwalk 不在本精简镜像中；它们涉及额外安装与空间，预检仍会提示。容器化不补齐真实模型、官方 Harness、预算、安全工具闭环等已知业务缺口。

## 构建与启动

在本仓库根目录执行：

~~~bash
docker compose config --quiet
docker compose build cairn-server
docker compose up -d --no-build --wait cairn-server cairn-dispatcher
docker compose ps

# 项目本身是有限时长的 CLI；一轮完成后正常退出，不伪装为常驻服务。
docker compose run --rm ctf-agent
docker compose run --rm ctf-agent cat /data/report.json
~~~

所有服务共用同一 cyber-wdwas:dev 镜像。常驻的是 Cairn API 与其 Mock Dispatcher；ctf_ext 通过一次性容器运行，与 Cairn 的业务接线仍是原代码边界，不宣称二者已自动完成集成。

默认仅运行 Mock，没有注入模型凭据或官方提交配置。Cairn 的 local 模式表示 Worker 子进程在 Dispatcher 容器内执行，不会回到宿主机，也不创建子 Docker 容器。

## 容器内复测

~~~bash
docker compose run --rm tests pip check
docker compose run --rm tests
docker compose run --rm tests bash -c 'cd cairn/cairn && python -m pytest -q -p no:cacheprovider --junitxml=/app/build/cairn-junit.xml'
docker compose run --rm tests python scripts/container_smoke.py
~~~

最后一条要求 Cairn API 和 Dispatcher 已启动，会创建一个独立 Mock 项目验证调度与结果落库。测试结果放在本项目的 test-results 命名卷；没有从宿主机复制旧的测试结果作为容器验收。

测试服务的 `TMPDIR=/app/build` 指向专用 test-results 卷，让上游测试执行其临时假 CLI；公共 `/tmp` 仍为 noexec，不改其他服务的临时路径。测试中的假 `claude` 只是本地输出固定 JSON 的脚本，不安装、不调用 Claude 或任何真实模型。

通过 SSH heredoc 或脚本串行执行时，为每条 `docker compose run` 加上 `-T --interactive=false`，并将标准输入重定向为 `/dev/null`。Compose 默认保持标准输入，可能读走后续脚本，导致只执行第一项却返回 0；必须逐项检查日志与结果，不能仅凭外层退出码判断整批成功。

## 断点调试

~~~bash
docker compose --profile debug up -d --no-build --pull never --force-recreate ctf-debug
docker compose logs ctf-debug
~~~

ctf-debug 默认在 wait-for-client 阶段等待调试器，未开始执行 Mock 代码。它不是 HTTP 服务；不要用浏览器访问调试端口。

1. 在 VS Code 中打开 ctf-agent 仓库根目录，并启用 Python Debugger 扩展。
2. 将 docker/launch.json 的配置合并到自己的 .vscode/launch.json；不要覆盖已有调试配置。
3. 在 ctf_ext/__main__.py 的 build_runtime 调用附近设置断点，执行“连接 Docker 内的 CTF Mock 调试器”。
4. 调试结束后重新创建本项目调试容器，即可进行下一轮；临时数据库随容器重建清空。

自动验证可运行 `docker compose run --rm -T --interactive=false tests python scripts/debug_smoke.py`：它通过 DAP 设置断点、核对暂停栈、继续执行并等待 exited／terminated。attach 会话的 terminated 不含退出码，必须另用 `docker wait cyber-wdwas-ctf-debug-1` 确认 **0**，并核对 debug-data 中本轮 `debug-report.json`。只看到 wait-for-client 或连接关闭，都不算完整调试验证通过。

源码和配置以只读绑定方式挂入调试容器；修改宿主机源码后重启该调试服务即可，不必重装 Python 依赖。调试数据库与证据在容器临时目录，每轮重启重建；调试报告保存到 debug-data 卷。普通 ctf-agent 的运行数据库则持久化；重复练习需要显式选择新的数据库与证据路径，不能把已有已完成任务当成新的一轮。

本机连接远程服务器上的容器时，先建立 SSH 隧道：

~~~bash
server_ip=$(ssh devbox_agent "docker inspect cyber-wdwas-cairn-server-1 --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'")
debug_ip=$(ssh devbox_agent "docker inspect cyber-wdwas-ctf-debug-1 --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'")
test -n "$server_ip" && test -n "$debug_ip" || exit 1
ssh -N -o ExitOnForwardFailure=yes \
  -L "127.0.0.1:18000:${server_ip}:8000" \
  -L "127.0.0.1:15678:${debug_ip}:5678" devbox_agent
~~~

保持隧道开启，再用上述 VS Code 配置连接本机 15678；Cairn API 可在本机 18000 访问。容器重建后 IP 可能改变，须重新读取 IP 并重开隧道。远程部署目录为 /root/cyber-wdwas-docker/source；本机源码改动不会自动同步到服务器，须先同步对应文件再重启调试容器。例如：

~~~bash
scp ctf_ext/runtime/worker.py devbox_agent:/root/cyber-wdwas-docker/source/ctf_ext/runtime/worker.py
~~~

调试端口能执行 Python 代码，不应暴露公网。本机 Docker 的 internal 网络下，声明的宿主 ports 实测未生效；因此 Compose 不再声明宿主端口，而由 SSH 服务端访问真实容器 IP。隧道仅绑定开发机的 127.0.0.1，不开放公网端口，不依赖失效的远程回环映射。

## 隔离、持久化与停止

- 默认非 root（UID/GID 10001），只读根文件系统，删除全部 Linux capabilities，禁止提权。
- 不挂载 /var/run/docker.sock，不使用 privileged 或 host 网络。
- 使用 internal 网络，仅供项目容器互通及宿主机访问；开发机通过 SSH 回环转发进入。真实模型所需出口必须另行评估与显式配置。
- 数据库、证据、工作目录分别进入本项目命名卷；临时目录为有大小限制的 tmpfs。
- 每个容器限制 512 MiB 内存、1 CPU 和 256 个进程；日志轮转每份 10 MiB、最多 2 份。
- 这些约束不是完整的对抗性代码安全沙箱；现有工具与预算代码缺口仍适用。

~~~bash
# 查看本项目日志
docker compose logs --tail 100 cairn-server cairn-dispatcher

# 仅停止本项目服务，保留数据卷和镜像
docker compose --profile debug down
~~~

不要使用 docker system prune、全局容器停止或 down -v 来处理本项目问题。容器化不要求删除、打包或重启服务器上的其他镜像与服务。

## 构建网络故障

如果 Docker 报告连接 127.0.0.1:18023 代理失败，问题来自宿主机 Docker 的既有代理配置，不是应用依赖缺失。不要重启 Docker 或改全局代理，以免影响其他服务。

本次 ctr 直连尝试因 TLS 握手超时失败，未用于成功下载。实际完成的是赛前通过 Python urllib 直连 GHCR，下载 manifest、config 与各层，逐个校验 SHA256，制作 OCI 归档后用 docker load 导入。来源记录位于远程 /root/cyber-wdwas-docker/logs/base-source.json，导入记录为 logs/base-load.log。

基础镜像已导入不等于应用已构建：BuildKit 仍可能尝试解析远端元数据并触发失效代理。本机已通过单条 FROM 探测，支持以下仅作用于当前命令的恢复方式；不改 Docker 守护进程配置，不拉取其他基础镜像。

~~~bash
base=ghcr.io/astral-sh/uv:0.13.8-python3.12-trixie-slim
expected=sha256:b1d470137757f15474bbcfd9596f49885f1d874f6a9e17d505c1e7e99fe2fc5b
test "$(docker image inspect "$base" --format '{{.Id}}')" = "$expected" || exit 1
docker tag "$base" cyber-wdwas-base:py312-b1d470137757
DOCKER_BUILDKIT=0 docker build --pull=false --force-rm \
  --memory 768m --memory-swap 768m --cpu-period 100000 --cpu-quota 100000 \
  --build-arg BASE_IMAGE=cyber-wdwas-base:py312-b1d470137757 \
  --build-arg HTTP_PROXY= --build-arg HTTPS_PROXY= --build-arg ALL_PROXY= \
  --build-arg http_proxy= --build-arg https_proxy= --build-arg all_proxy= \
  --target development -t cyber-wdwas:dev .
~~~

上述镜像 ID 比较适用于本次已核对的 containerd 镜像存储；其他环境须通过 RepoDigests／来源记录校验 manifest，不能把 config ID 与 manifest 摘要混为一谈。默认 Dockerfile 仍固定官方摘要，BASE_IMAGE 覆盖只用于已验证的本地镜像。旧构建器已弃用，仅作为当前主机的受限恢复路径。

这仅避免基础镜像元数据请求，**不是完全离线构建**：apt／pip 仍在赛前构建阶段联网获取依赖。安装过程不得放进容器入口。应用镜像和容器验收状态须以实际日志为准。

## 2026-11-17 最终验收记录

| 项目 | 实测结果 | 远程 logs/ 证据 |
| --- | --- | --- |
| 最终 development 镜像 | c1457b69ccc9；Python 3.12.16，linux/amd64 | build-development-final.log |
| 项目容器 CI | 79/79；预检 16 PASS、2 WARN、0 FAIL | container-project-ci-final.log |
| Cairn 容器测试 | 98/98；保留 1 条 Starlette/httpx 弃用警告 | container-cairn-tests-final.log |
| 应用 CLI Mock | 一题完成、Mock ACCEPTED，无真实提交 | container-agent-mock.log |
| Cairn 实际 Mock 调度 | proj_002 completed，2 个 Intent | container-cairn-mock-final.log |
| SSH 与真实调试 | API HTTP 200；断点第 120 行；terminated；独立退出码 0；本轮报告有效 | container-debug-ssh-final.log |
| 最终 competition 候选 | 63f46f916294；安装器检查通过；默认退出 78 | container-competition-check-final.log、container-competition-exit-final.log |
| 隔离与资源 | 非 root／只读／禁止提权／无 socket；实际 cgroup 512 MiB、1 CPU、256 PID；internal、无默认路由 | container-security-final.json |

日志根目录：/root/cyber-wdwas-docker/logs。完整镜像 ID 见 README 与 COMPETITION.md，它们是本地镜像标识，不是已发布的仓库摘要。Cairn JUnit 位于 test-results 卷的 /app/build/cairn-container-final-junit.xml；本轮应用报告在 agent-data，调试报告在 debug-data。

### 保留的失败与修复记录

1. 原 BuildKit 固定摘要解析走失效代理；基础镜像下载、摘要校验和导入已成功后，改用经验证的本地标签与单命令旧构建器。未更改全局代理或重启 Docker。
2. 首轮批处理只有 pip check 实际执行：Compose 默认标准输入读走了 heredoc 后续命令。关闭 interactive 并重定向 /dev/null 后补跑；未把外层退出 0 当成整批成功。
3. Cairn 首轮 95 通过、3 失败：开发镜像缺少原始样例，且临时假 CLI 位于 noexec 的 /tmp。仅 development 补齐样例，测试服务 TMPDIR 改用专用测试卷；未修改上游测试，未放开公共 /tmp 的 noexec。修复后 98/98 通过。
4. 首轮 DAP 断点已命中，但脚本只等待 exited，attach 实际发送 terminated 后断开。修正事件处理并强制另查容器退出码和报告；通过新建调试会话与真实 SSH 隧道复测，不把断开连接直接当成退出成功。
5. 此主机 internal 网络的宿主端口声明没有产生有效映射。移除无效 ports，改为 SSH 到当前容器 IP；实测 API 与 DAP 均可达，不改成可外出的公网桥接网络。

### 交付时状态与边界

- Cairn API 与 Mock Dispatcher 保持运行；ctf-debug 完成一次调试后 Exited (0)，不等待连接。临时验证 SSH 隧道已关闭；自行调试时按本页命令重建调试容器并建立新隧道。
- 磁盘剩余约 2.0 GB，仍有空间压力；没有删除其他镜像／卷、全局 prune 或更改其他业务容器。
- 主机不支持 swap 限额，不能将 memory.max 验证称为 swap 限额验证。预检仍提示缺少 binwalk／sage 和模型 Key；需要的工具必须赛前补齐，不在比赛中安装。
- 安装清单位于镜像 /opt/installed-freeze.txt。当前锁定了包版本，但未宣称全套系统包、构建依赖和 wheel 都已完成哈希级可复现构建。
- 文档在验收后更新并同步远程；已验收镜像中的 README 是构建时快照。该文档更新未触发重新打包，也没有改变已验证代码或虚构新的镜像 ID。
- 尚未连接真实公司模型、官方 Harness 或镜像仓库，没有推送镜像、提交比赛或提交／推送本轮代码。正式协议、网关鉴权、平台限制和业务闭环仍按 COMPETITION.md／README 的缺口处理。
