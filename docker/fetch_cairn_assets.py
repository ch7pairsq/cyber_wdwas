"""构建期一次性拉取 cairn 容器同款解题资产（构建期联网，运行期离线）。

产出与去向：
- /opt/tools/ysoserial.jar               Java 反序列化 payload 生成器（v0.0.6，MIT）
- /opt/tools/jdwp-shellifier/            JDWP 调试协议利用脚本（stdlib 实现，MIT）
- /opt/tools/jwt_tool/                   JWT 弱点利用工具（依赖 apt python3-jwt/requests/termcolor）
- /opt/knowledge/PayloadsAllTheThings/   本地 payload 知识库（python_exec 可 grep）
- /usr/local/bin/{ysoserial,jwt_tool,jdwp-shellifier}  PATH wrapper：
  toolchain.json manual 条目的 bins 用 shutil.which 探测这里，命令白名单三份 yaml 同步。

失败即构建失败（fail loudly）；运行期不访问任何 URL。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import tarfile
import urllib.request

TOOLS_DIR = "/opt/tools"
KNOWLEDGE_DIR = "/opt/knowledge"
BIN_DIR = "/usr/local/bin"
UA = {"User-Agent": "ctf-agent-toolchain/1.0 (build-stage fetch)"}

YSOSERIAL_URL = "https://github.com/frohoff/ysoserial/releases/download/v0.0.6/ysoserial-all.jar"
REPOS = (
    ("IOActive/jdwp-shellifier", TOOLS_DIR),
    ("ticarpi/jwt_tool", TOOLS_DIR),
    ("swisskyrepo/PayloadsAllTheThings", KNOWLEDGE_DIR),
)

# ysoserial 输出经 base64（stdout 文本通道友好）；jwt_tool/jdwp 固定系统解释器。
WRAPPERS = {
    "ysoserial": '#!/bin/sh\njava -jar /opt/tools/ysoserial.jar "$@" | base64 -w 0\n',
    "jwt_tool": '#!/bin/sh\nexec /usr/bin/python3 /opt/tools/jwt_tool/jwt_tool.py "$@"\n',
    "jdwp-shellifier": '#!/bin/sh\nexec /usr/bin/python3 /opt/tools/jdwp-shellifier/jdwp-shellifier.py "$@"\n',
}


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(request, timeout=180) as response:
        return response.read()


def _clone_via_tarball(repo: str, dest_parent: str) -> str:
    """不依赖 git 二进制：GitHub API 探测默认分支 → codeload tarball → 解包改名。"""
    api = json.loads(_fetch("https://api.github.com/repos/" + repo))
    branch = api.get("default_branch") or "master"
    blob = _fetch("https://codeload.github.com/{}/tar.gz/refs/heads/{}".format(repo, branch))
    name = repo.rsplit("/", 1)[-1]
    os.makedirs(dest_parent, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        try:
            archive.extractall(dest_parent, filter="data")
        except TypeError:  # 旧解释器无 filter 参数
            archive.extractall(dest_parent)
    extracted = os.path.join(dest_parent, "{}-{}".format(name, branch))
    dest = os.path.join(dest_parent, name)
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.replace(extracted, dest)
    return dest


def main() -> int:
    os.makedirs(TOOLS_DIR, exist_ok=True)
    os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
    os.makedirs(BIN_DIR, exist_ok=True)

    jar = os.path.join(TOOLS_DIR, "ysoserial.jar")
    with open(jar, "wb") as handle:
        handle.write(_fetch(YSOSERIAL_URL))
    print("[cairn-assets] ysoserial.jar {} bytes".format(os.path.getsize(jar)))

    for repo, parent in REPOS:
        dest = _clone_via_tarball(repo, parent)
        print("[cairn-assets] repo {} -> {}".format(repo, dest))

    for name, body in WRAPPERS.items():
        path = os.path.join(BIN_DIR, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.chmod(path, 0o755)
        print("[cairn-assets] wrapper {}".format(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
