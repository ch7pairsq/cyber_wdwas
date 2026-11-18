#!/usr/bin/env python3
"""外部进程 supervisor：心跳卡死则杀进程组并有限重启。

只负责进程生命周期，不修改 SQLite、证据或提交状态；重启后的恢复由
Orchestrator/看门狗完成。示例：
  python3 scripts/supervise.py --heartbeat data/heartbeat --stale-seconds 180 \
      --max-restarts 3 -- python3 -m ctf_ext run --mode llm --heartbeat-file data/heartbeat
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


def run(command, heartbeat=None, stale_seconds=180.0, max_restarts=3, backoff=2.0):
    restarts = 0
    while True:
        started = time.monotonic()
        proc = subprocess.Popen(command, start_new_session=True)
        stale = False
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
            time.sleep(min(0.5, max(0.05, stale_seconds / 10)))
        returncode = proc.poll()
        if returncode == 0 and not stale:
            return 0
        if not should_restart(returncode, stale, restarts, max_restarts):
            return returncode if returncode is not None else 1
        restarts += 1
        print("supervisor: restart {}/{} after {}".format(
            restarts, max_restarts, "stale heartbeat" if stale else "exit " + str(returncode)),
            file=sys.stderr)
        time.sleep(max(0.0, backoff))


def main(argv=None):
    parser = argparse.ArgumentParser(description="心跳卡死时有限重启 ctf_ext")
    parser.add_argument("--heartbeat", default=None)
    parser.add_argument("--stale-seconds", type=float, default=180.0)
    parser.add_argument("--max-restarts", type=int, default=3)
    parser.add_argument("--backoff", type=float, default=2.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("a command is required after --")
    if args.stale_seconds <= 0:
        parser.error("--stale-seconds must be positive")
    return run(command, heartbeat=args.heartbeat, stale_seconds=args.stale_seconds,
               max_restarts=max(0, args.max_restarts), backoff=args.backoff)


if __name__ == "__main__":
    raise SystemExit(main())
