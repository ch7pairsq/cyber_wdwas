"""Golden 回放库：平台真实响应形状固化为 fixture，stub 与守门测试从这里加载。

原则：每届比赛的真字节（回执、start payload、环境快照）回填进本目录，
标注 source 与 confidence（observed=实测 / shape-inferred=形状推断）。
stub 回放真实形状，而不是理想化假设——8 题自测拦不住线上事故的根源
就是"自建测试验证自建假设"。
"""
from __future__ import annotations

import json
from pathlib import Path

GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "golden"


def load_golden(name: str) -> dict:
    """读取一条 golden fixture（含 source/confidence 元数据与 response 字段）。"""
    path = GOLDEN_DIR / "{}.json".format(name)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def golden_response(name: str) -> dict:
    """只取响应体（stub 直接回放用）。"""
    return load_golden(name)["response"]
