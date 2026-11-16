#!/usr/bin/env python3
"""从本地 SQLite 生成静态运行看板（§18）。"""
import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ctf_ext.runtime.dashboard import write_dashboard


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/practice.db")
    parser.add_argument("--out", default="build/dashboard.html")
    parser.add_argument("--bench", default="build/bench.json")
    args = parser.parse_args()
    if not os.path.exists(args.db):
        print("db not found: {}".format(args.db))
        return 2
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    path = write_dashboard(conn, args.out, bench_path=args.bench, db_path=args.db)
    conn.close()
    print("dashboard written: {} ({} bytes)".format(path, os.path.getsize(path)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
