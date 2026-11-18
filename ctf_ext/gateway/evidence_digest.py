"""evidence_digest：按证据 id 汇总原始内容，供模型做跨文件证据关联复核（只读）。

用法：python3 evidence_digest.py --root <evidence_root> [--max-bytes N] <sha256>...
输出每个证据的定界块（id + 长度 + 内容节选）；id 只接受 64 位十六进制
（sha256），并校验文件哈希一致，防路径逃逸。单证据超出配额时取头尾节选。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys


def _safe_read(path: str, limit: int):
    with open(path, "rb") as fh:
        head = fh.read(limit)
        rest = fh.read()
    return head, len(head) + len(rest)


def main() -> int:
    ap = argparse.ArgumentParser(prog="evidence_digest")
    ap.add_argument("--root", required=True, help="evidence store 根目录")
    ap.add_argument("--max-bytes", type=int, default=120000)
    ap.add_argument("ids", nargs="*", help="证据 sha256 列表")
    args = ap.parse_args()
    ids = [i for i in args.ids if len(i) == 64 and all(c in "0123456789abcdef" for c in i.lower())]
    if not ids:
        print("no valid evidence ids")
        return 2
    per = max(2000, args.max_bytes // len(ids))
    printed = 0
    for eid in ids:
        if printed >= args.max_bytes:
            print("=== TRUNCATED: byte budget exhausted ===")
            break
        path = os.path.join(args.root, eid[:2], eid)
        if not os.path.isfile(path):
            print("=== {} MISSING ===".format(eid))
            continue
        with open(path, "rb") as fh:
            data = fh.read()
        if hashlib.sha256(data).hexdigest() != eid:
            print("=== {} HASH MISMATCH (skipped) ===".format(eid))
            continue
        budget = min(per, args.max_bytes - printed)
        if len(data) <= budget:
            body, shown = data, len(data)
        else:
            head = data[:budget // 2]
            tail = data[-(budget - len(head) - 40):] if budget - len(head) > 60 else b""
            body, shown = head + b"\n...[SNIP]...\n" + tail, len(data)
        printed += len(body)
        sys.stdout.write("=== evidence {} ({} bytes{}) ===\n".format(
            eid, shown, "" if shown == len(data) else ", truncated"))
        sys.stdout.flush()
        sys.stdout.buffer.write(body if isinstance(body, bytes) else body.encode())
        sys.stdout.buffer.write(b"\n")
        sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
