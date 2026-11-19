#!/usr/bin/env python3
"""离线规则候选报告：只读 attempts/lessons，不写数据库、不进入 Worker。

用法：
  python3 scripts/rules_report.py --db data/practice.db
  python3 scripts/rules_report.py --db data/practice.db --json --out rules.json

候选规则必须由人工审核；本脚本不会自动晋升，也不会把规则注入 prompt。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, List

FLAG_RE = re.compile(r"flag\{[^}]*\}", re.I)


def _redact(value: Any) -> str:
    return FLAG_RE.sub("flag{redacted}", str(value or ""))


def _loads(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def collect(conn: sqlite3.Connection, min_challenges: int = 2, limit: int = 20) -> Dict[str, Any]:
    groups: Dict[tuple, Dict[str, Any]] = defaultdict(lambda: {
        "challenges": set(), "attempts": 0, "reasons": Counter(),
        "actions": [], "next_actions": [], "evidence": set(), "last_seen": 0.0,
    })
    rows = conn.execute(
        "SELECT challenge_id, strategy_key, outcome, failure_reason, actions, "
        "next_best_action, evidence_ids, created_at FROM attempts ORDER BY created_at"
    )
    for row in rows:
        key = (str(row["strategy_key"] or ""), str(row["outcome"] or ""))
        item = groups[key]
        item["challenges"].add(str(row["challenge_id"] or ""))
        item["attempts"] += 1
        if row["failure_reason"]:
            item["reasons"][str(row["failure_reason"])] += 1
        item["actions"].extend(str(x) for x in _loads(row["actions"])[-3:])
        if row["next_best_action"]:
            item["next_actions"].append(str(row["next_best_action"]))
        item["evidence"].update(str(x) for x in _loads(row["evidence_ids"]))
        item["last_seen"] = max(item["last_seen"], float(row["created_at"] or 0))
    candidates = []
    for (strategy_key, outcome), item in groups.items():
        if len(item["challenges"]) < max(1, min_challenges):
            continue
        action = (item["next_actions"][-1] if item["next_actions"]
                  else (item["actions"][-1] if item["actions"] else ""))
        candidates.append({
            "strategy_key": strategy_key,
            "outcome": outcome,
            "challenges": sorted(x for x in item["challenges"] if x),
            "attempts": item["attempts"],
            "trigger": "strategy_key={}; outcome={}".format(strategy_key, outcome),
            "action": _redact(action),
            "evidence_count": len(item["evidence"]),
            "failure_reasons": [{"reason": _redact(k), "count": v}
                                for k, v in item["reasons"].most_common(5)],
            "last_seen": item["last_seen"],
            "status": "candidate",
        })
    candidates.sort(key=lambda x: (-x["attempts"], x["strategy_key"], x["outcome"]))
    lessons = conn.execute(
        "SELECT text, tags, superseded FROM lessons ORDER BY created_at DESC LIMIT 50"
    ).fetchall()
    return {
        "source": "attempts/lessons (read-only)",
        "generated_by": "scripts/rules_report.py",
        "injection": "none; candidates require human review",
        "candidates": candidates[:max(0, limit)],
        "lessons_seen": len(lessons),
        "superseded_lessons": sum(1 for row in lessons if int(row["superseded"] or 0)),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="离线规则候选报告（只读）")
    parser.add_argument("--db", default="data/practice.db")
    parser.add_argument("--min-challenges", type=int, default=2)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", default=None, help="可选：把报告写到文件")
    args = parser.parse_args(argv)
    path = pathlib.Path(args.db)
    if not path.is_file():
        print("database not found: {}".format(path), file=sys.stderr)
        return 1
    conn = sqlite3.connect("file:{}?mode=ro".format(path.resolve()), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        report = collect(conn, min_challenges=args.min_challenges, limit=args.limit)
    finally:
        conn.close()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        pathlib.Path(args.out).write_text(text + "\n", encoding="utf-8")
        print("wrote {} ({} candidates)".format(args.out, len(report["candidates"])))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
