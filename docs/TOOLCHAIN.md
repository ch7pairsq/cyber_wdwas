# 离线 CTF 工具链

## 结论

工具**必须随镜像一起构建**，不能比赛时下载或安装。

- 工具是镜像的运行时依赖，属于交付物的一部分；
- 工具不是 `ctf_ext` 的 Python 源码，不把二进制 vendoring 进 Git 仓库；
- 构建阶段从 Debian 官方软件源安装，运行阶段只调用；
- 参赛镜像移除 `pip`、`apt`、`uv`、`dpkg` 等安装入口；
- 工具缺失时构建/参赛检查失败，不带着不完整工具链启动。

当前清单在 [docker/toolchain.json](../docker/toolchain.json)，安装脚本在
[docker/install_toolchain.py](../docker/install_toolchain.py)。

## 从哪里下载

| 类型 | 推荐来源 | 规则 |
| --- | --- | --- |
| 系统工具 | 基础镜像对应的 Debian 官方仓库 | 只用 `apt-get install --no-install-recommends`；不添加 PPA、Kali 源或第三方脚本 |
| Python 库 | Debian 官方 apt 的 `python3-*` 包 | 本轮使用 `python3-pwntools` 等发行版包；未进入官方源的库暂不引入。后续如用 PyPI，必须使用 `--require-hashes` 的完整锁文件 |
| 无发行版包的独立工具 | 项目官方 GitHub Releases | 固定 tag + SHA256，构建期下载；禁止 curl-to-bash、`npx` 或运行时下载 |
| 技能文本 | `ctf-skills` 等公开仓库 | 只作为离线文本，不是可执行工具；单独做许可与安全审查 |

本清单只使用 **Debian 官方 apt 包**。基础镜像本身已用 digest 固定；构建完成后把实际
`dpkg` 版本写入 `/opt/toolchain-installed.json`，作为本次镜像的版本记录。

如果平台要求逐包可复现，下一步应把 apt 源切到 Debian snapshot 并记录每个 `.deb` 的
SHA256；当前版本先保证“包名固定、构建期安装、运行期不可变”，不宣称已经做到逐包哈希锁。

## 默认工具

### core：最小可用

| 工具 | 包 | 用途 |
| --- | --- | --- |
| `file` | `file` | 文件类型 |
| `strings` / `objdump` / `readelf` / `nm` | `binutils` | 二进制快速分析 |
| `unzip` / `zip` | `unzip` / `zip` | ZIP |
| `xz` / `bzip2` / `zstd` | `xz-utils` / `bzip2` / `zstd` | 常见压缩 |
| `openssl` | `openssl` | 编码、摘要、证书 |
| `xxd` | `xxd` | 十六进制 |
| `jq` | `jq` | JSON |
| `python3` | 基础镜像 | 受控脚本入口 |
| `update-ca-certificates` | `ca-certificates` | TLS 根证书 |

### ctf：核心 CTF 工具

| 工具 | 包 | 主要场景 |
| --- | --- | --- |
| `binwalk` | `binwalk` | 固件、嵌套文件、提取 |
| `foremost` | `foremost` | 文件雕复 |
| `fls` / `icat` / `mmls` | `sleuthkit` | 磁盘镜像、文件系统取证 |
| `exiftool` | `libimage-exiftool-perl` | 图片/文档元数据 |
| `7z` | `p7zip-full` | 多格式归档 |
| `steghide` | `steghide` | 隐写 |
| `pngcheck` | `pngcheck` | PNG 结构 |
| `gdb` | `gdb` | 动态调试 |
| `pwn` | `python3-pwntools` | ELF/ROP/汇编辅助；同时带入 `python3-capstone`、`python3-ropgadget` 等依赖 |
| `ROPgadget` | `python3-ropgadget` | ROP gadget 搜索 |
| `cstool` | `capstone-tool` | Capstone CLI 反汇编 |
| `checksec` | `checksec` | NX/PIE/RELRO/Canary 检查 |
| `patchelf` | `patchelf` | ELF 解释器/RPATH 调整（受控 pwn 场景） |
| `z3` | `z3` | SMT 约束求解 |
| `strace` / `ltrace` | `strace` / `ltrace` | 系统/库调用 |
| `qemu-x86_64` / `qemu-aarch64` | `qemu-user` | 跨架构本地运行 |
| `pdftotext` / `pdfinfo` | `poppler-utils` | PDF |
| `qrencode` | `qrencode` | QR 生成 |
| `zbarimg` | `zbar-tools` | QR/条码识别 |
| `java` / `javac` / `jar` | `default-jdk-headless` | OpenJDK 21：运行/编译 Java（反序列化 payload、jar 分析） |
| `procyon` | `procyon-decompiler` | Java 字节码反编译（trixie 无 jadx/cfr；Debian 包二进制名为 `procyon`） |
| `apktool` | `apktool` | Android apk 解包（资源 + smali） |
| `go` / `gofmt` | `golang-go` | Go 1.24：编译/运行本地单文件（gob 构造等） |
| `tesseract` | `tesseract-ocr` + `tesseract-ocr-chi-sim` | OCR（注册工具 `ocr_text` 主命令；中英文） |
| `tshark` | `tshark` | pcap 解析（只读本地文件，不抓包） |
| `ffmpeg` / `ffprobe` | `ffmpeg` | 音视频解码、频谱图、抽帧 |
| `wasm2wat` | `wabt` | WASM 反汇编为文本 |
| `john` | `john` | 哈希/弱口令破解（社区版，无 *2john 提取脚本） |
| `photorec` / `testdisk` | `testdisk` | 文件雕刻、分区恢复 |
| `unrar` | `unrar` | RAR 解压（non-free 组件，许可以包内 copyright 为准） |
| `upx` | `upx-ucl` | UPX 脱壳（trixie main 4.2.4） |
| `scapy`/`numpy`/`sympy`/`PIL`/`Crypto`/`gmpy2`/`z3`/`unicorn`/`jwt` | `python3-scapy` 等 `python3-*` | `python_exec`（/usr/bin/python3）可 import 的解题库，用 `modules` 字段做 import 校验 |

`python3-pwntools` 会通过 Debian 依赖带入 `python3-pip`；镜像构建完成后仍由
`strip_installers.py` 删除 pip 入口，`pwn` 本身不依赖运行期 pip。构建验收需要确认
`pwn`/`ROPgadget`/`cstool`/`checksec`/`patchelf`/`z3` 六个主二进制以及 `tesseract`
都存在；纯 Python 库（scapy/numpy/sympy/PIL/pycryptodome/gmpy2/z3/unicorn/jwt）
由安装脚本用 `/usr/bin/python3` 做 import 探测，缺失即构建失败。

比赛容器无网络：`go` 仅限本地单文件编译（模块下载不可用）；`java` 运行 jar 同理，
不能从远程仓库拉依赖。

### 暂不放进默认镜像

以下工具体积大、依赖重或需要额外运行时，默认不打包；平台给出镜像大小限制后再决定：

- `sagemath`：密码学强，但体积和构建时间很大；
- `ghidra`：反编译强，但需要 JVM 和较大镜像；
- `radare2`、`rizin`：bookworm 官方源没有对应包（`packages.debian.org/bookworm/radare2` 返回不可用，`rizin` 无此包）；Reverse 主路径改用 `objdump` + `gdb` + `cstool` + `ROPgadget`，不引入非 Debian 源；
- `jadx`（trixie 官方源无此包，Java 反编译已用 `procyon-decompiler` 覆盖）、`angr`、`torch`：按题型和体积另行评估；
- `nmap`、`nuclei`、`sqlmap`：主动扫描器，不属于离线技能文本；需要明确授权和网络策略；
- `uncompyle6`、`decompyle3`、`pyinstxtractor_ng`：trixie 无 Debian 包；走 PyPI 必须先做 `--require-hashes` 锁文件，本轮不引入；
- `zip2john` 等 *2john 脚本：Debian `john` 是社区版（非 jumbo），不含这些提取脚本；需要提取哈希时在 `python_exec` 里手工构造。

纯 Python 库（`pycryptodome`、`gmpy2` 等）已改以 apt `python3-*` 包进入清单
（2026-11-20）：无独立二进制，用清单的 `modules` 字段做 import 校验，不走 pip。

`ctf-skills` 是方法文本，不是工具。它不能替代 `binwalk`、`gdb`、`qemu` 等实际执行能力；
反过来，只有工具没有技能文本也不会自动提高解题率。

## 构建

在赛前开发主机执行：

~~~bash
docker build --target competition \
  --build-arg TOOLCHAIN_TIER=ctf \
  -t cyber-wdwas:competition-candidate .
~~~

- `TOOLCHAIN_TIER=core`：只装最小工具，用于快速验证；
- `TOOLCHAIN_TIER=ctf`：默认，比赛候选使用；
- `TOOLCHAIN_TIER=full`：预留给后续可选层；当前没有默认 optional 包。

构建阶段会：

1. 读取 `docker/toolchain.json`；
2. 只安装 `core` + `ctf` 的 apt 包；
3. 复制各包 `/usr/share/doc/<pkg>/copyright` 到 `/opt/toolchain-licenses/`；
4. 写 `/opt/toolchain-installed.json`；
5. 校验每个工具的主二进制，缺失即构建失败。

## 运行期边界

- 运行期禁止 `pip`、`apt`、`uv`、`npm`、`dpkg`；`docker/strip_installers.py` 在参赛目标移除这些入口。
- 工具只能从镜像内的固定路径调用；模型不能要求“先安装一个工具”。
- 工具白名单在 `config/base.yaml` 的 `policy.command_allowlist`，与清单的 `allowlist=true` 条目同步。
- 任何工具执行仍必须经过策略层、工作目录约束、超时和输出上限；打包工具不等于放开任意命令执行。
- `ToolRegistry` 已接入 `--mode llm` 的工具循环：模型只能调用注册表中的结构化工具，参数由代码构造为固定 argv；每次调用仍经过 `policy.decide_tool_name` / `decide_tool_risk` / 命令白名单 / 工作目录约束 / 预算，并把完整输出写入 `command_log` 与证据库。
- 本地执行器仍不是安全沙箱：默认 `sandbox_required=true` 时工具调用会被拒绝；只有显式关闭沙箱要求的 mock/测试夹具才执行本地命令。真实比赛必须提供独立容器执行边界，不能用本机 `capture` 冒充隔离。
- 工具循环不会自动把工具输出当成答案：`candidate_answer` 仍必须引用已登记证据，`FlagGate` 仍只认当前题 `command_log` 中完整成功的输出。

## LLM 工具循环

`ctf_ext/gateway/catalog.py` 注册默认只读工具，`LlmWorker` 把工具定义随模型请求发送；模型返回
`tool_use` 后，`ToolRegistry` 用注册表校验工具名、把 JSON 参数转成固定 argv，再执行策略、预算、
工作目录、超时与输出上限检查。工具结果以 `tool_result` 回传，完整输出登记为证据；模型最终
必须引用 `evidence_id`，否则 B.2 契约拒收。

默认注册 24 个工具，不提供任意 shell/Python 执行：侦察类 `file_info`、`strings`、`hexdump`、
`metadata`、`png_check`、`binwalk_scan`；结构类 `elf_headers`、`symbols`、`checksec`、
`disassemble`、`disasm_bytes`（cstool）、`rop_gadgets`；内容提取类 `pdf_text`、`qr_decode`、
`archive_list`；受控执行类 `archive_extract`、`binwalk_extract`、`steghide_extract`（固定输出名、
非幂等、标记 risk_level=execute，生产 `sandbox_required=true` 时与所有本地命令一样 fail-closed）。
内置技能库 `ctf_ext/skills/` 现有 45 节，覆盖 crypto/reverse/pwn/forensics/misc 五个域，
按题目分类（含 `reversing→reverse` 等别名）与关键词选择。需要新增工具时，先确认
Debian 包、许可、体积和实际题型收益，再补 `ToolSpec` 与回归；不以“更多工具”为目标。

真实网关的 `tools`/`tool_use` 兼容尚未联调。若网关拒绝工具定义，可把 `model.tools_enabled`
设为 `false`，保留普通 JSON 调用；这不改变工具链镜像或本地测试，只是停止发送工具定义。

## 验证

构建后执行：

~~~bash
# 工具是否都在；只读，不安装
docker run --rm --network none --read-only --cap-drop ALL \
  --entrypoint python cyber-wdwas:competition-candidate \
  /app/docker/install_toolchain.py --check-only --tier ctf

# 参赛候选的完整离线检查（包含安装器移除、模型配置、工具链）
docker run --rm --network none --read-only --cap-drop ALL \
  --entrypoint python cyber-wdwas:competition-candidate \
  /app/docker/check_competition.py
~~~

宿主机开发环境没有这些 Linux 工具时，`python3 scripts/preflight.py` 只报 WARN；
在镜像或准出环境使用 `--strict` 时缺失即 FAIL：

~~~bash
python3 scripts/preflight.py --strict
~~~

## 许可

`docker/toolchain.json` 中的 `license` 是便于审查的 SPDX/常见名称提示，**不是法律意见**；
最终以镜像内 `/opt/toolchain-licenses/*.copyright` 的 Debian 包版权文件为准。GPL/BSD/
Info-ZIP/Artistic 等许可要求的分发说明随镜像保留，不因 `strip_installers` 删除 apt 而丢失。
