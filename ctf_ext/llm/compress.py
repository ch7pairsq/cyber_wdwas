"""上下文预算与压缩（§9：原始大文件默认不进上下文，只给高价值信息）。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List


def estimate_tokens(text: str) -> int:
    """粗估：CJK 约每 2.5 字符一个 token；只用于预算控制，不做计费口径。"""
    return max(1, math.ceil(len(text) / 2.5))


@dataclass
class ContextItem:
    key: str          # 如 evidence_id 或 fact id
    text: str
    priority: float   # 高者优先保留
    kind: str = "summary"  # fact | evidence | summary | pointer


@dataclass
class ContextPack:
    text: str
    included: List[str] = field(default_factory=list)
    pointers: List[str] = field(default_factory=list)
    est_tokens: int = 0


def build_context(items: List[ContextItem], max_tokens: int) -> ContextPack:
    """按优先级装入预算；溢出项降级为一行指针，保留可追溯性。"""
    ordered = sorted(items, key=lambda i: i.priority, reverse=True)
    lines: List[str] = []
    included: List[str] = []
    pointers: List[str] = []
    used = 0
    for item in ordered:
        cost = estimate_tokens(item.text)
        if used + cost <= max_tokens:
            lines.append(item.text)
            included.append(item.key)
            used += cost
        else:
            pointers.append("[{}:{}]".format(item.kind, item.key))
            used += estimate_tokens(pointers[-1])
    if pointers:
        lines.append("超出预算，以下仅保留指针（原始内容见证据库）：")
        lines.extend(pointers)
    text = "\n".join(lines)
    return ContextPack(text=text, included=included, pointers=pointers,
                       est_tokens=estimate_tokens(text))
