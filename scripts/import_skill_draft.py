#!/usr/bin/env python3
"""把外部 Markdown 转成离线草稿；绝不写 sections/index，也不联网。

这是人工蒸馏的起点，不是自动导入器。输出只保留少量短句，去掉代码块、
命令、URL 和字面 flag；工具/命令映射必须由人重新写。
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
from typing import List

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "docs" / "skills-drafts"
COMMIT_RE = re.compile(r"[0-9a-fA-F]{7,40}")
FLAG_RE = re.compile(r"flag\{[^}\n]*\}", re.I)
RAW_RE = re.compile(
    r"(^\s*\$|\b(?:pip|pip3|git|curl|wget|nc|nmap|sqlmap|nuclei|apt|brew|gem|go)\b|"
    r"\bpython3?\s+-c\b|\x60[^\x60]*\x60)", re.I)
HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$")


def _clean(line: str) -> str:
    line = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", line)
    line = re.sub(r"https?://\S+", "", line)
    line = FLAG_RE.sub("flag{...}", line)
    line = line.replace("\x60", "").strip()
    line = re.sub(r"\s+", " ", line)
    return line[:180].strip()


def extract_pivot(text: str) -> List[str]:
    lines = text.splitlines()
    out: List[str] = []
    in_fence = False
    current = ""
    for raw in lines:
        line = raw.rstrip()
        if line.strip().startswith("\x60\x60\x60"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = HEADING_RE.match(line)
        if match:
            current = match.group(2).strip().casefold()
            continue
        if current not in ("when to pivot", "when to switch", "换路", "何时换路"):
            continue
        stripped = line.strip()
        if not stripped.startswith(("- ", "* ")):
            continue
        cleaned = _clean(stripped[2:])
        if not cleaned or RAW_RE.search(cleaned) or "flag{" in cleaned:
            continue
        if cleaned not in out:
            out.append(cleaned)
        if len(out) >= 12:
            break
    return out


def draft(text: str, source: pathlib.Path, repo: str, commit: str, license_name: str,
          rel_path: str) -> str:
    title = source.stem
    for line in text.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            title = match.group(1).strip()
            break
    pivot = extract_pivot(text)
    body = [
        "# DRAFT: " + title,
        "",
        "> 非运行时草稿。人工补齐并过质量闸后，才可移入 \x60ctf_ext/skills/sections/\x60。",
        "",
        "## 来源",
        "- repo: " + repo,
        "- commit: " + commit,
        "- path: " + rel_path,
        "- license: " + license_name,
        "- reviewed_at: 2026-11-19",
        "",
        "## 候选取材（需改写为工具调用）",
    ]
    if pivot:
        body.extend("- " + item for item in pivot)
    else:
        body.append("- 未发现 When to Pivot 段；人工从原文提取，不直接复制命令。")
    body.extend([
        "",
        "## 待人工补齐",
        "- [ ] \x60## 触发\x60：可观察的文件/工具输出信号",
        "- [ ] \x60## 最小动作\x60：只写 \x60tool=...\x60 或 \x60python_exec\x60 标准库",
        "- [ ] \x60## 命中判据\x60：唯一 flag 与 evidence_id",
        "- [ ] \x60## 何时换路\x60：负判据与下一张卡",
        "- [ ] \x60## 踩坑\x60 / \x60## 来源/边界\x60",
        "",
    ])
    return "\n".join(body)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="外部 Markdown → 离线技能草稿")
    parser.add_argument("--input", action="append", required=True, help="本地 Markdown；可重复")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--repo", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--license", dest="license_name", required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if not COMMIT_RE.fullmatch(args.commit):
        print("commit must be a pinned 7-40 hex SHA", file=sys.stderr)
        return 2
    out_dir = pathlib.Path(args.out).resolve()
    if "ctf_ext/skills/sections" in str(out_dir).replace("\\", "/"):
        print("refusing to write into runtime sections/", file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)
    for raw in args.input:
        source = pathlib.Path(raw)
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            print("cannot read {}: {}".format(source, exc), file=sys.stderr)
            return 2
        slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", source.stem).strip("-") or "draft"
        target = out_dir / (slug + ".draft.md")
        if target.exists() and not args.force:
            print("refusing to overwrite {} (use --force)".format(target), file=sys.stderr)
            return 2
        target.write_text(draft(text, source, args.repo, args.commit, args.license_name, raw),
                          encoding="utf-8")
        print("wrote", target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
