#!/usr/bin/env python3
"""技能卡质量闸：默认只报告，--strict 才作为 CI 门禁。

检查项：索引字段、section 是否存在、四段结构、工具名是否在 default_specs、
禁用命令/未安装库、字面 flag、长度预算。不联网，不执行工具。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Dict, List

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REQUIRED_HEADINGS = ("触发", "最小动作", "命中判据", "踩坑")
HEADING_RE = {name: re.compile(r"^##\s*" + re.escape(name) + r"\s*$", re.M) for name in REQUIRED_HEADINGS}
TOOL_RE = re.compile(r"tool=([A-Za-z_][A-Za-z0-9_]*)")
FLAG_RE = re.compile(r"flag\{([^}]*)\}", re.I)
DENY_RE = re.compile(
    r"\b(?:unzip|openssl|gdb|gmpy2|angr|ghidra|sage|ROPgadget|objdump|readelf|nm|"
    r"binwalk|exiftool|pngcheck|zbarimg|steghide|pdftotext|7z|cstool)\b",
    re.I,
)
# 卡片只描述静态分析/受控计算；出现这些调用说明绕过了 ToolRegistry 或沙箱。
CODE_DENY_RE = re.compile(
    r"\b(?:subprocess|os\.system|socket\.|requests\.|urllib\.|http\.client|eval\s*\(|exec\s*\()",
    re.I,
)
INJECTION_RE = re.compile(
    r"(ignore\s+(all\s+)?previous|system\s+prompt|you\s+are\s+now|忽略(?:之前|以上)|输出系统提示)",
    re.I,
)
SOURCE_FIELDS = ("repo", "path", "commit", "license", "reviewed_at")
COMMIT_RE = re.compile(r"[0-9a-fA-F]{7,40}")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _load_tools() -> set:
    from ctf_ext.gateway.catalog import default_specs
    return {spec.name for spec in default_specs()}


def check_card(meta: Dict[str, object], text: str, tools: set, root: pathlib.Path = ROOT) -> Dict[str, List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    name = str(meta.get("name") or "<unnamed>")
    filename = str(meta.get("file") or "")
    if not filename:
        errors.append("{}: missing file".format(name))
        return {"errors": errors, "warnings": warnings}
    path = root / "ctf_ext" / "skills" / "sections" / filename
    if not path.is_file():
        errors.append("{}: section not found: {}".format(name, filename))
        return {"errors": errors, "warnings": warnings}
    raw = path.read_bytes()
    size = len(raw)
    if size > 2048:
        errors.append("{}: {} bytes > hard cap 2048".format(name, size))
    elif size > 1200:
        warnings.append("{}: {} bytes > target 1200".format(name, size))
    body = text
    for heading in REQUIRED_HEADINGS:
        if not HEADING_RE[heading].search(body):
            errors.append("{}: missing heading ## {}".format(name, heading))
    used_tools = TOOL_RE.findall(body)
    if not used_tools:
        errors.append("{}: no tool= line in 最小动作".format(name))
    for tool in used_tools:
        if tool not in tools:
            errors.append("{}: unknown tool={} (not in default_specs)".format(name, tool))
    for match in DENY_RE.finditer(body):
        errors.append("{}: raw command/library '{}' is not a registered tool".format(name, match.group(0)))
    for match in FLAG_RE.finditer(body):
        content = match.group(1).strip()
        if content and content not in ("...", "…", "redacted"):
            errors.append("{}: literal flag-like text is not allowed".format(name))
    if not str(meta.get("domain") or "").strip():
        errors.append("{}: missing domain".format(name))
    _check_source(meta, name, errors)
    _check_pivot(meta, name, errors)
    for match in CODE_DENY_RE.finditer(body):
        errors.append("{}: unsafe code call '{}' in card".format(name, match.group(0)))
    for match in INJECTION_RE.finditer(body):
        errors.append("{}: prompt-injection pattern '{}'".format(name, match.group(0)))
    for prereq in meta.get("prerequisites") or []:
        if not isinstance(prereq, str) or not prereq.strip():
            errors.append("{}: invalid prerequisite {!r}".format(name, prereq))
        elif prereq not in tools and prereq != "python3":
            errors.append("{}: prerequisite '{}' is not a registered tool".format(name, prereq))
    return {"errors": errors, "warnings": warnings}


def _check_source(meta: Dict[str, object], name: str, errors: List[str]) -> None:
    source = meta.get("source")
    if source is None:
        return
    if not isinstance(source, dict):
        errors.append("{}: source must be an object".format(name))
        return
    kind = source.get("type")
    if kind not in ("external", "internal", "derived"):
        errors.append("{}: source.type must be external/internal/derived".format(name))
        return
    if kind == "external":
        for field in SOURCE_FIELDS:
            value = source.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append("{}: external source missing {}".format(name, field))
        commit = str(source.get("commit") or "")
        if commit and not COMMIT_RE.fullmatch(commit):
            errors.append("{}: source.commit must be a pinned hex commit".format(name))
        reviewed = str(source.get("reviewed_at") or "")
        if reviewed and not DATE_RE.fullmatch(reviewed):
            errors.append("{}: source.reviewed_at must be YYYY-MM-DD".format(name))
        repo = str(source.get("repo") or "")
        if repo and not repo.startswith("https://"):
            errors.append("{}: source.repo must be an https URL".format(name))


def _check_pivot(meta: Dict[str, object], name: str, errors: List[str]) -> None:
    pivot = meta.get("pivot")
    if pivot is None:
        return
    if not isinstance(pivot, list) or not all(isinstance(item, str) and item.strip() for item in pivot):
        errors.append("{}: pivot must be a list of skill names".format(name))
        return
    if len(set(pivot)) != len(pivot):
        errors.append("{}: pivot contains duplicates".format(name))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="技能卡质量闸（默认 report）")
    parser.add_argument("--root", default=str(ROOT), help="仓库根目录")
    parser.add_argument("--strict", action="store_true", help="有 error 时返回 1；用于 CI")
    parser.add_argument("--warnings-as-errors", action="store_true", help="warning 也视为失败")
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    args = parser.parse_args(argv)
    root = pathlib.Path(args.root).resolve()
    index_path = root / "ctf_ext" / "skills" / "index.json"
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print("cannot read index: {}".format(exc), file=sys.stderr)
        return 1
    if not isinstance(data, list):
        print("skill index must be a list", file=sys.stderr)
        return 1
    try:
        tools = _load_tools()
    except Exception as exc:
        print("cannot load default_specs: {}".format(exc), file=sys.stderr)
        return 1
    names = set()
    pivots = []
    report = {"ok": True, "errors": [], "warnings": []}
    for item in data:
        if not isinstance(item, dict):
            report["errors"].append("index entry is not an object")
            continue
        name = str(item.get("name") or "")
        if not name:
            report["errors"].append("index entry missing name")
            continue
        if name in names:
            report["errors"].append("{}: duplicate name".format(name))
            continue
        names.add(name)
        if isinstance(item.get("pivot"), list):
            pivots.extend((name, str(target)) for target in item["pivot"])
        if item.get("deprecated") is True:
            continue
        path = root / "ctf_ext" / "skills" / "sections" / str(item.get("file") or "")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            report["errors"].append("{}: cannot read section: {}".format(name, exc))
            continue
        result = check_card(item, text, tools, root)
        report["errors"].extend(result["errors"])
        report["warnings"].extend(result["warnings"])
    for name, target in pivots:
        if target not in names:
            report["errors"].append("{}: pivot target not found: {}".format(name, target))
    report["ok"] = not report["errors"] and (not args.warnings_as_errors or not report["warnings"])
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for item in report["warnings"]:
            print("WARN " + item)
        for item in report["errors"]:
            print("ERROR " + item)
        print("skills: {} checked, {} errors, {} warnings".format(
            len(names), len(report["errors"]), len(report["warnings"])))
    if args.strict and not report["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
