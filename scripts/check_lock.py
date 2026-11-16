#!/usr/bin/env python3
"""校验 requirements-lock.txt 与 pyproject 下限一致；CI 前置门禁。

只做确定性的版本下限比较，不联网、不安装。
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEP_RE = re.compile(r'"([A-Za-z0-9_.-]+)>=\s*([0-9]+(?:\.[0-9]+)*)"')
PIN_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([0-9]+(?:\.[0-9]+)*)$")


def version_tuple(text):
    return tuple(int(part) for part in text.split("."))


def parse_requirements(path):
    items = {}
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        match = PIN_RE.match(line)
        if not match:
            continue
        name, version = match.group(1).lower(), match.group(2)
        if name in items:
            raise SystemExit("duplicate pin in {}: {}".format(path, name))
        items[name] = version
    return items


def main() -> int:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lower = {name.lower(): version for name, version in DEP_RE.findall(pyproject)}
    lock = parse_requirements(ROOT / "requirements-lock.txt")
    problems = []
    for name, minimum in lower.items():
        if name not in lock:
            problems.append("missing in lock: {} (pyproject >= {})".format(name, minimum))
            continue
        if version_tuple(lock[name]) < version_tuple(minimum):
            problems.append("lock {} {} < pyproject >={}".format(name, lock[name], minimum))
    if problems:
        print("LOCK MISMATCH")
        for problem in problems:
            print(" - " + problem)
        return 1
    print("lock ok: {}".format(", ".join("{}=={}".format(k, v) for k, v in sorted(lock.items()))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
