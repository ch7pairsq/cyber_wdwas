#!/usr/bin/env python3
"""外部进程 supervisor：心跳/推进卡死则杀进程组并有限重启。

只负责进程生命周期，不修改 SQLite、证据或提交状态；重启后的恢复由
Orchestrator/看门狗完成。示例：
  python3 scripts/supervise.py --heartbeat data/heartbeat --stale-seconds 180 \
      --progress data/progress --progress-stale-seconds 600 \
      --max-restarts 3 -- python3 -m ctf_ext run --mode llm --heartbeat-file data/heartbeat

两级信号：
- 心跳（heartbeat）：进程存活，由主 pump 维持——空转时也在刷；
- 推进（progress）：真实解题活动（模型调用/工具完成/结果落库），只在
  有前进动作时刷新。2026-11-21 第 19 题正式赛：worker 卡在 fork 阻塞，
  心跳活着但 35 分钟无模型调用（claim_timeout 30 分钟才打断一次，
  重开后又卡）——progress 停更可提前发现这种“活着但不推进”的卡死。

正式赛一次性窗口：加 --deadline-seconds 13800，重启时自动把子进程
--seconds 收缩为剩余时间，总墙钟不超过窗口。
"""
import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def heartbeat_age(path, now=None):
    if not path:
        return None
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return None
    return max(0.0, (now if now is not None else time.time()) - mtime)


def should_restart(returncode, stale, restarts, max_restarts):
    if stale:
        return restarts < max_restarts
    if returncode in (None, 0):
        return False
    return restarts < max_restarts


def terminate(proc, grace=5.0):
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def set_seconds(command, value):
    """把子进程命令里的 --seconds 收缩为 value（无则追加）。"""
    out = list(command)
    for idx, arg in enumerate(out):
        if arg == '--seconds' and idx + 1 < len(out):
            out[idx + 1] = str(value)
            return out
    return out + ['--seconds', str(value)]


def _tee_streams(proc, log_fh):
    """后台线程把子进程 stdout/stderr 透传的同时落盘（容器退出后可查）。

    平台只收集容器 stdout/stderr，pod 回收后日志即失；agent 每轮收尾/重启
    的关键输出（run loop finished / supervisor: restart / traceback）必须
    有独立副本。读线程随进程结束自然退出，fail-open 不影响主流程。
    """
    import threading

    def _pump(stream, forward):
        try:
            for line in iter(stream.readline, b""):
                forward(line)
                log_fh.write(line)
                log_fh.flush()
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    t_out = threading.Thread(target=_pump, args=(proc.stdout, sys.stdout.buffer.write), daemon=True)
    t_err = threading.Thread(target=_pump, args=(proc.stderr, sys.stderr.buffer.write), daemon=True)
    t_out.start()
    t_err.start()
    return t_out, t_err


def run(command, heartbeat=None, stale_seconds=180.0, max_restarts=3, backoff=2.0,
        deadline_seconds=None, min_remaining=90.0,
        progress=None, progress_stale_seconds=600.0, log_file=None):
    # 正式赛一次性窗口：deadline_seconds 从 supervisor 启动起算的总墙钟。
    # 每次启动/重启都把子进程 --seconds 收缩为剩余时间；剩余不足
    # min_remaining 时不再重启（不够完成一次有意义的恢复）。
    t0 = time.monotonic()
    restarts = 0
    while True:
        cmd = list(command)
        if deadline_seconds is not None:
            remaining = deadline_seconds - (time.monotonic() - t0)
            if remaining <= 0:
                return 0  # 总窗口耗尽：不再启动新进程
            cmd = set_seconds(cmd, remaining)
        if log_file:
            try:
                with open(log_file, "a", encoding="utf-8") as fh:
                    fh.write("\n===== supervisor launch {} =====\n".format(
                        time.strftime("%Y-%m-%d %H:%M:%S")))
            except OSError:
                pass
        started = time.monotonic()
        popen_kw = {"start_new_session": True}
        log_fh = None
        if log_file:
            try:
                log_fh = open(log_file, "ab")
                popen_kw.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except OSError:
                log_fh = None  # 落盘失败不阻断：保持平台直采路径
        proc = subprocess.Popen(cmd, **popen_kw)
        if log_fh is not None:
            _tee_streams(proc, log_fh)
        stale = False
        stale_reason = "stale heartbeat"
        while True:
            returncode = proc.poll()
            if returncode is not None:
                break
            if heartbeat and time.monotonic() - started >= stale_seconds:
                age = heartbeat_age(heartbeat)
                if age is None or age > stale_seconds:
                    stale = True
                    terminate(proc)
                    break
            # 推进卡死：progress 文件已存在（有过真实活动）但停更超过
            # progress_stale_seconds——进程活着但解题无前进。文件缺失视为
            # 未启用/尚未产生首个推进，不判（启动阶段无文件属正常）。
            if progress and progress_stale_seconds > 0:
                age = heartbeat_age(progress)
                if age is not None and age > progress_stale_seconds:
                    stale = True
                    stale_reason = "stale progress ({}s, threshold {}s)".format(
                        int(age), int(progress_stale_seconds))
                    terminate(proc)
                    break
            time.sleep(min(0.5, max(0.05, stale_seconds / 10)))
        returncode = proc.poll()
        if returncode == 0 and not stale:
            return 0
        if deadline_seconds is not None:
            remaining = deadline_seconds - (time.monotonic() - t0)
            if remaining <= min_remaining:
                return returncode if returncode is not None else 1
        if not should_restart(returncode, stale, restarts, max_restarts):
            return returncode if returncode is not None else 1
        restarts += 1
        print("supervisor: restart {}/{} after {}".format(
            restarts, max_restarts, stale_reason if stale else "exit " + str(returncode)),
            file=sys.stderr)
        time.sleep(max(0.0, backoff))


def main(argv=None):
    parser = argparse.ArgumentParser(description="心跳卡死时有限重启 ctf_ext")
    parser.add_argument("--heartbeat", default=None)
    parser.add_argument("--progress", default=None,
                        help="推进心跳文件路径（模型调用/工具完成时刷新）；"
                             "停更超过 --progress-stale-seconds 视为解题卡死")
    parser.add_argument("--log-file", default=None,
                        help="子进程 stdout/stderr 透传的同时追加写入该文件"
                             "（容器/pod 回收后平台日志丢失时的独立副本）")
    parser.add_argument("--progress-stale-seconds", type=float, default=600.0,
                        help="推进停更阈值；需大于单次模型超时+单次工具超时之和")
    parser.add_argument("--stale-seconds", type=float, default=180.0)
    parser.add_argument("--max-restarts", type=int, default=3)
    parser.add_argument("--backoff", type=float, default=2.0)
    parser.add_argument("--deadline-seconds", type=float, default=None,
                        help="总墙钟预算（从 supervisor 启动起算）；重启时把子进程 "
                             "--seconds 收缩为剩余时间，到点后不再重启")
    parser.add_argument("--min-remaining", type=float, default=90.0,
                        help="剩余时间低于该值时不再重启")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("a command is required after --")
    if args.stale_seconds <= 0:
        parser.error("--stale-seconds must be positive")
    if args.progress_stale_seconds < 0:
        parser.error("--progress-stale-seconds must be non-negative")
    if args.deadline_seconds is not None and args.deadline_seconds <= 0:
        parser.error("--deadline-seconds must be positive")
    if args.min_remaining < 0:
        parser.error("--min-remaining must be non-negative")
    return run(command, heartbeat=args.heartbeat, stale_seconds=args.stale_seconds,
               max_restarts=max(0, args.max_restarts), backoff=args.backoff,
               deadline_seconds=args.deadline_seconds, min_remaining=args.min_remaining,
               progress=args.progress, progress_stale_seconds=args.progress_stale_seconds,
               log_file=args.log_file)


if __name__ == "__main__":
    raise SystemExit(main())
