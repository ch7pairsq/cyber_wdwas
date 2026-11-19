"""git_history：只读读取工作区 Git 仓库历史（Forensics「Git 历史恢复」支撑工具）。

固定三模式（catalog._git_history_argv 构造 argv，模型不给 shell）：
- log  ：git log --all --name-status，列全分支提交与每提交的文件变动；
- fsck ：git fsck --full --no-reflogs，列悬空/不可达对象（amend/reset 残留，
         纯只读——不用 --lost-found，它会把对象写进磁盘）；
- show ：git show <rev>，rev 支持 commit:path 语法取回被删文件内容。

安全边界：rev 由 argv_builder 白名单正则校验（首字符不得是 "-"，禁空白与
shell 元字符）；git_dir 必须是真实仓库；git 自身无网络行为（clone/fetch
不在暴露面），env 清空凭据类变量；超时由脚本内 50s 与注册表 60s 双保险。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

TIMEOUT_SECONDS = 50
REV_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._~^:/-")

_LOG_ARGV = ["log", "--all", "--date=iso", "--pretty=medium", "--name-status"]
_FSCK_ARGV = ["fsck", "--full", "--no-reflogs"]


def _repo_root(raw: str) -> Path:
    """校验仓库路径：含 .git 的工作树、裸 .git 目录或 bare 仓库均可。"""
    path = Path(raw)
    if not path.is_dir():
        print("git_history: not a directory: {}".format(raw))
        raise SystemExit(2)
    if (path / ".git").is_dir() or (path / "HEAD").is_file() or (path / "objects").is_dir():
        return path
    print("git_history: no git repository detected under {}".format(raw))
    raise SystemExit(2)


def _validated_rev(rev: str) -> str:
    """rev 白名单：字符集限定 + 首字符非 "-"；show 的 rev 由模型提供，必须拦截选项注入。"""
    if not rev or rev[0] == "-" or any(ch not in REV_CHARS for ch in rev):
        print("git_history: invalid rev")
        raise SystemExit(2)
    return rev


def main() -> int:
    """只读 git 历史检查器：log/show/fsck 三模式，固定 argv 与受控环境。"""
    parser = argparse.ArgumentParser(description="read-only git history inspector")
    parser.add_argument("--git-dir", required=True)
    parser.add_argument("--mode", required=True, choices=("log", "show", "fsck"))
    parser.add_argument("--rev", default="HEAD")
    args = parser.parse_args()

    root = _repo_root(args.git_dir)
    if args.mode == "log":
        argv = ["git", "--no-pager", "-C", str(root), *_LOG_ARGV]
    elif args.mode == "fsck":
        argv = ["git", "-C", str(root), *_FSCK_ARGV]
    else:
        argv = ["git", "--no-pager", "-C", str(root), "show", _validated_rev(args.rev)]

    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C",
           "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_PAGER": "cat", "HOME": "/tmp"}
    try:
        proc = subprocess.run(argv, env=env, cwd=str(root), timeout=TIMEOUT_SECONDS,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.TimeoutExpired:
        print("git_history: git command timed out ({}s)".format(TIMEOUT_SECONDS))
        return 3
    except FileNotFoundError:
        print("git_history: git binary not available in this image")
        return 3
    if proc.stdout:
        sys.stdout.buffer.write(proc.stdout)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        print("git_history: git exited {}: {}".format(proc.returncode, detail[:500]))
        return 1
    # fsck 等命令把报告写 stderr（rc=0 也是有效结果）：成功时同样回传，不丢证据。
    if proc.stderr:
        sys.stdout.buffer.write(b"# stderr\n")
        sys.stdout.buffer.write(proc.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
