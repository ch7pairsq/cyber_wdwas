"""验证容器内 Cairn 的 Mock 队列，不调用模型或官方平台。"""
import argparse
import json
import time
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="http://cairn-server:8000")
    parser.add_argument("--timeout", type=float, default=45)
    args = parser.parse_args()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(path, payload=None):
        body = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(args.server.rstrip("/") + path, data=body,
                                     headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=5) as response:
            return json.load(response)

    request("/settings")
    created = request("/projects", {"title": "Docker Mock 冒烟", "origin": "容器运行验证",
                                   "goal": "验证 Mock 调度与落库", "bootstrap_enabled": True})
    project_id = created["project"]["id"]
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        project = request("/projects/" + project_id)
        if project["project"]["status"] == "completed":
            assert project.get("intents"), "任务完成但没有 Intent 记录"
            print(json.dumps({"passed": True, "project": project_id,
                              "status": "completed", "intents": len(project["intents"])}, ensure_ascii=False))
            return
        time.sleep(0.25)
    raise RuntimeError("Mock 调度超时，保留项目以供检查：" + project_id)


if __name__ == "__main__":
    main()
