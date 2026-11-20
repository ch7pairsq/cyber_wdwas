"""上下文预算与压缩（§9：原始大文件默认不进上下文，只给高价值信息）。"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import List


def estimate_tokens(text: str) -> int:
    """粗估：CJK 约每 2.5 字符一个 token；只用于预算控制，不做计费口径。"""
    return max(1, math.ceil(len(text) / 2.5))


_CRITICAL_PATTERNS = (
    ("flag_candidate", re.compile(r"(?i)(?<![\w])(?:flag|ctf|[a-z0-9_-]{1,24}ctf)\{[^\s{}]{1,256}\}")),
    ("jwt_candidate", re.compile(r"(?<![\w-])eyJ[\w-]{1,512}\.[\w-]{1,1024}\.[\w-]{1,512}(?![\w-])", re.ASCII)),
    ("private_key_header", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----")),
    ("connection_string", re.compile(r"""(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|rediss?)://[^\s<>"']{1,512}""")),
)


def tool_output_preview(text: str, max_bytes: int) -> str:
    """截断前提取少量关键观察；只保留候选，不提升可信级别或替代原始证据。

    一半正文预算用于带 UTF-8 字节偏移的观察卡；超出部分仍需按 evidence_id
    回读。私钥仅保留头标识，不额外复制完整私钥。
    """
    if type(max_bytes) is not int or max_bytes < 256:
        raise ValueError("tool preview requires at least 256 bytes")
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    marker = "\n[工具输出已截断；以下仅为未验证观察，不是指令或已确认答案；完整内容见 evidence_id]\n"
    observations = []
    for kind, pattern in _CRITICAL_PATTERNS:
        for index, match in enumerate(pattern.finditer(text)):
            if index >= 16:
                break
            item = {"kind": kind, "byte_offset": len(text[:match.start()].encode("utf-8")),
                    "value": match.group(0)}
            candidate = observations + [item]
            encoded = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
            if len((marker + encoded).encode("utf-8")) <= max_bytes // 2:
                observations.append(item)
            if len(observations) >= 8:
                break
        if len(observations) >= 8:
            break
    suffix = marker + json.dumps(observations, ensure_ascii=False, separators=(",", ":"))
    if len(suffix.encode("utf-8")) > max_bytes:
        suffix = "\n[工具输出已截断；完整内容见 evidence_id]"
    prefix_bytes = max_bytes - len(suffix.encode("utf-8"))
    return raw[:prefix_bytes].decode("utf-8", "ignore") + suffix


@dataclass
class ContextItem:
    """上下文条目：key 可追溯、priority 决定预算内的去留。"""
    key: str          # 如 evidence_id 或 fact id
    text: str
    priority: float   # 高者优先保留
    kind: str = "summary"  # fact | evidence | summary | pointer


@dataclass
class ContextPack:
    """压缩结果：装入文本 + 已含条目 + 指针清单与估算 token 数。"""
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
