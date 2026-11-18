"""验证 Mock 调试会话的断点与继续执行；容器退出码须另用 Docker 核验。"""
import argparse
import json
import socket
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="ctf-debug")
    parser.add_argument("--port", type=int, default=5678)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1] / "ctf_ext" / "__main__.py")
    args = parser.parse_args()
    source = args.source.read_text().splitlines()
    breakpoint_line = next(i for i, line in enumerate(source, 1) if line.strip().startswith("orch = build_runtime(args.db"))
    deadline = time.monotonic() + 60
    while True:
        try:
            connection = socket.create_connection((args.host, args.port), timeout=5)
            break
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)
    with connection:
        connection.settimeout(30)
        reader = connection.makefile("rb")
        sequence = 0

        def send(command, arguments):
            nonlocal sequence
            sequence += 1
            body = json.dumps({"seq": sequence, "type": "request", "command": command,
                               "arguments": arguments}).encode()
            connection.sendall(("Content-Length: %d\r\n\r\n" % len(body)).encode() + body)
            return sequence

        def wait(predicate):
            while time.monotonic() < deadline:
                length = None
                while True:
                    line = reader.readline()
                    if not line:
                        raise RuntimeError("调试连接提前关闭")
                    if line == b"\r\n":
                        break
                    key, value = line.decode().split(":", 1)
                    if key.lower() == "content-length":
                        length = int(value)
                assert length is not None and 0 < length <= 1024 * 1024
                message = json.loads(reader.read(length))
                if message.get("type") == "response" and not message.get("success"):
                    raise RuntimeError(str(message))
                if predicate(message):
                    return message
            raise TimeoutError("调试协议验证超时")

        def response(number):
            return wait(lambda m: m.get("type") == "response" and m.get("request_seq") == number)

        response(send("initialize", {"clientID": "cyber-wdwas-smoke", "adapterID": "python",
                                     "pathFormat": "path", "linesStartAt1": True,
                                     "columnsStartAt1": True, "supportsRunInTerminalRequest": False}))
        send("attach", {"justMyCode": False, "subProcess": False})
        wait(lambda m: m.get("event") == "initialized")
        result = response(send("setBreakpoints", {"source": {"path": "/app/ctf_ext/__main__.py"},
                                                   "breakpoints": [{"line": breakpoint_line}]}))
        assert result["body"]["breakpoints"][0]["verified"], result
        send("configurationDone", {})
        stopped = wait(lambda m: m.get("event") == "stopped")
        assert stopped["body"]["reason"] == "breakpoint", stopped
        thread_id = stopped["body"]["threadId"]
        stack = response(send("stackTrace", {"threadId": thread_id}))
        frame = stack["body"]["stackFrames"][0]
        assert frame["source"]["path"] == "/app/ctf_ext/__main__.py" and frame["line"] == breakpoint_line, frame
        print("断点命中：/app/ctf_ext/__main__.py:{}".format(breakpoint_line), flush=True)
        send("continue", {"threadId": thread_id})
        # attach 会话在部分 debugpy 版本中既不发送 exited 也不发送 terminated，
        # 且连接会保持到客户端断开；这里只验证继续执行被接受，退出码由外部
        # `docker wait` 核验（脚本注释和输出都明确这一点）。
        connection.settimeout(5)
        try:
            ended = wait(lambda m: m.get("event") in ("exited", "terminated"))
        except TimeoutError:
            ended = {"event": "none", "body": {}}
        exit_code = ended.get("body", {}).get("exitCode")
        if ended["event"] == "exited":
            assert exit_code == 0, ended
        print(json.dumps({"dap_passed": True, "breakpoint": breakpoint_line,
                          "termination_event": ended["event"], "exit_code": exit_code,
                          "container_exit_check_required": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
