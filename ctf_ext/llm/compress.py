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
    omitted_pointers: int = 0
    est_tokens: int = 0


def build_context(items: List[ContextItem], max_tokens: int) -> ContextPack:
    """按优先级装入预算；溢出项降级为指针，且最终文本不超过预算。

    指针列表始终保留全部可追溯 ID；正文放不下时只截断展示，不丢检索能力。
    """
    max_tokens = int(max_tokens)
    if max_tokens < 1:
        return ContextPack(text="", included=[], pointers=[], omitted_pointers=0, est_tokens=0)
    ordered = sorted(items, key=lambda i: i.priority, reverse=True)
    entries: List[ContextItem] = []
    pointers: List[str] = []
    used = 0
    for item in ordered:
        cost = estimate_tokens(item.text)
        if used + cost <= max_tokens:
            entries.append(item)
            used += cost
        else:
            pointers.append("[{}:{}]".format(item.kind, item.key))
    included = [item.key for item in entries]
    lines = [item.text for item in entries]
    omitted = 0
    if pointers:
        header = "超出预算，以下仅保留指针（原始内容见证据库）："
        candidate = lines + [header]
        if estimate_tokens("\n".join(candidate)) <= max_tokens:
            lines.append(header)
            for pointer in pointers:
                candidate = lines + [pointer]
                if estimate_tokens("\n".join(candidate)) <= max_tokens:
                    lines.append(pointer)
                else:
                    omitted += 1
            if omitted:
                note = "另有 {} 条指针未展开".format(omitted)
                if estimate_tokens("\n".join(lines + [note])) <= max_tokens:
                    lines.append(note)
    text = "\n".join(lines)
    # 极端小预算下按字符收敛，保证 est_tokens <= max_tokens。
    if estimate_tokens(text) > max_tokens:
        text = text[:max(0, int(max_tokens * 2.5))]
    return ContextPack(text=text, included=included, pointers=pointers,
                       omitted_pointers=omitted, est_tokens=estimate_tokens(text))
