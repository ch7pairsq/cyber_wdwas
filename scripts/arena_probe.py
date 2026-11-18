"""Arena 只读探针：GET help 与 challenges，验证地址/凭据可用，不做 start/submit。

用法：ARENA_BASE_URL=http://<attempt-address>:8090 ARENA_SOLVER_TOKEN=... \\
  python scripts/arena_probe.py [--full]

token 不回显；--full 才打印完整题目字段（仍不含凭据）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from ctf_ext.harness.arena import (DEFAULT_HELP_PATH, DEFAULT_LIST_PATH,  # noqa: E402
                                   ArenaClient, ArenaProtocolError, _redact)


def main() -> int:
    parser = argparse.ArgumentParser(prog="arena_probe")
    parser.add_argument("--full", action="store_true", help="打印完整响应字段")
    args = parser.parse_args()
    base_url = os.environ.get("ARENA_BASE_URL") or ""
    token = os.environ.get("ARENA_SOLVER_TOKEN") or ""
    if not base_url:
        print("missing ARENA_BASE_URL (e.g. http://<attempt-address>:8090)", file=sys.stderr)
        return 2
    if not token:
        print("missing ARENA_SOLVER_TOKEN", file=sys.stderr)
        return 2
    client = ArenaClient(base_url, token, proxy=os.environ.get("ARENA_PROXY") or "")
    try:
        help_resp = client.request("GET", DEFAULT_HELP_PATH)
        list_resp = client.request("GET", DEFAULT_LIST_PATH)
    except ArenaProtocolError as exc:
        print("probe failed: {}".format(_redact(str(exc))), file=sys.stderr)
        return 1
    print("== help ==")
    print(_redact(json.dumps(help_resp, ensure_ascii=False, indent=1)))
    items = list_resp if isinstance(list_resp, list) else (list_resp.get("challenges")
                                                           or list_resp.get("items") or [])
    print("== challenges: {} items ==".format(len(items) if isinstance(items, list) else "?"))
    for item in (items if isinstance(items, list) else []):
        if args.full:
            print(_redact(json.dumps(item, ensure_ascii=False)))
        else:
            print("- {} | {} | {}".format(item.get("unique_code") or item.get("id"),
                                          item.get("name") or item.get("title"),
                                          item.get("category") or item.get("type")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
