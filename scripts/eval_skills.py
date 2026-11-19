#!/usr/bin/env python3
"""离线技能选择评测：不调用模型、不联网、不执行工具。

用法：
  python3 scripts/eval_skills.py [--cases tests/fixtures/skill_selection_cases.jsonl]
                                 [--strict] [--json]

用例格式（JSONL）：
  {"id":"crypto_dh","domain":"crypto","query":"diffie hellman shared secret zero",
   "expect":["crypto.dh.degenerate"],"forbid":["crypto.rsa.small_e"]}
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Dict, List

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ctf_ext.skills.loader import SkillLoader  # noqa: E402


def load_cases(path: pathlib.Path) -> List[Dict[str, object]]:
    cases: List[Dict[str, object]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            item = json.loads(line)
        except ValueError as exc:
            raise ValueError("{}:{}: invalid JSON: {}".format(path, lineno, exc)) from None
        if not isinstance(item, dict):
            raise ValueError("{}:{}: case must be an object".format(path, lineno))
        if not isinstance(item.get("id"), str) or not item["id"].strip():
            raise ValueError("{}:{}: missing id".format(path, lineno))
        if not isinstance(item.get("domain"), str):
            raise ValueError("{}:{}: domain must be a string".format(path, lineno))
        if not isinstance(item.get("query"), str):
            raise ValueError("{}:{}: query must be a string".format(path, lineno))
        for key in ("expect", "forbid"):
            value = item.get(key, [])
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                raise ValueError("{}:{}: {} must be a string list".format(path, lineno, key))
        cases.append(item)
    return cases


def evaluate(loader: SkillLoader, cases: List[Dict[str, object]], limit: int = 2) -> Dict[str, object]:
    results = []
    top1 = top2 = forbidden = empty = 0
    for item in cases:
        selected = [str(meta["name"]) for meta in loader.select(
            domain=str(item.get("domain") or ""), query=str(item.get("query") or ""),
            limit=limit)]
        expect = [str(x) for x in item.get("expect") or []]
        forbid = [str(x) for x in item.get("forbid") or []]
        first_ok = bool(expect) and selected[:1] == expect[:1]
        any_ok = bool(expect) and bool(set(expect) & set(selected))
        hit_forbidden = [name for name in selected if name in forbid]
        if first_ok:
            top1 += 1
        if any_ok:
            top2 += 1
        if hit_forbidden:
            forbidden += 1
        if not selected:
            empty += 1
        results.append({
            "id": item["id"], "selected": selected, "expect": expect,
            "forbid": forbid, "top1": first_ok, "top2": any_ok,
            "forbidden_hits": hit_forbidden,
        })
    failed = [row for row in results if not row["top2"] or row["forbidden_hits"]]
    return {
        "ok": not failed,
        "cases": len(cases),
        "top1": top1,
        "top2": top2,
        "forbidden": forbidden,
        "empty": empty,
        "failed": failed,
        "results": results,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="离线技能选择评测")
    parser.add_argument("--cases", default=str(ROOT / "tests" / "fixtures" / "skill_selection_cases.jsonl"))
    parser.add_argument("--limit", type=int, default=2)
    parser.add_argument("--strict", action="store_true", help="有失败用例时返回 1")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.limit < 1:
        print("limit must be >= 1", file=sys.stderr)
        return 2
    try:
        cases = load_cases(pathlib.Path(args.cases))
        report = evaluate(SkillLoader(), cases, limit=args.limit)
    except (OSError, ValueError, RuntimeError) as exc:
        print("cannot evaluate skills: {}".format(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for row in report["results"]:
            print("{} {} selected={} expect={}".format(
                "PASS" if row["top2"] and not row["forbidden_hits"] else "FAIL",
                row["id"], row["selected"], row["expect"]))
        print("skills-eval: cases={} top1={} top2={} forbidden={} empty={}".format(
            report["cases"], report["top1"], report["top2"], report["forbidden"], report["empty"]))
    return 1 if args.strict and not report["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
