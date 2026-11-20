"""python_exec：把模型提交的解题代码经 stdin 执行，stdout 回传（离线长尾兜底工具）。

固定 argv（模型不给 shell）：/usr/bin/python3 python_exec.py；代码体走 stdin
（≤20KB，ToolRegistry 的 stdin_argument 管道限额）。资源限额：
CPU 110s（< 注册表 timeout 120s）、内存 512MB、进程 32、单文件 20MB。
容器本身无公网路由、子进程 env 只有 PATH/LC_ALL（无凭据），cwd=题目工作区
（workspace 文件可直接 open/import）。异常打印 traceback 到 stdout 供模型自修。
"""
from __future__ import annotations

import resource
import sys
import traceback

CPU_SECONDS = 110
MEM_BYTES = 512 * 1024 * 1024
NPROC = 32
FSIZE_BYTES = 20 * 1024 * 1024


def _apply_limits() -> None:
    """设置子进程资源限额（CPU/内存/进程数/文件大小）；单项失败尽力跳过。"""
    # 尽力而为：个别平台/内核不允许某个限额（如 macOS 无 RLIMIT_AS 生效值、
    # 或容器硬上限更低），跳过该单项而不是整个工具失败；超时仍由注册表兜底。
    for rlimit, value in ((resource.RLIMIT_CPU, CPU_SECONDS),
                          (resource.RLIMIT_AS, MEM_BYTES),
                          (resource.RLIMIT_NPROC, NPROC),
                          (resource.RLIMIT_FSIZE, FSIZE_BYTES)):
        try:
            resource.setrlimit(rlimit, (value, value))
        except (ValueError, OSError):
            pass


def main() -> int:
    """stdin 读代码 → 限额 → exec 执行；异常 traceback 打到 stdout 供模型自修。"""
    raw = sys.stdin.buffer.read()
    if len(raw) > 20_000:
        print("python_exec: code too large ({} bytes > 20000)".format(len(raw)))
        return 2
    code = raw.decode("utf-8", errors="replace")
    if not code.strip():
        print("python_exec: empty code")
        return 2
    _apply_limits()
    globs = {"__name__": "__main__"}
    try:
        exec(compile(code, "<python_exec>", "exec"), globs)
    except SystemExit as exc:
        return int(exc.code or 0) if isinstance(exc.code, int) else 0
    except BaseException:
        # 解题代码报错不是工具错误：把 traceback 给模型看，让它自己改。
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
