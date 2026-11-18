"""Skill 索引先行、按需加载（§10：只取所需章节，不全量注入）。

索引只包含名称/领域/前置/大小/风险；正文仅在题目匹配时读取。任何索引或
章节损坏都降级为空技能包，不能阻断 Worker。
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_SKILLS_DIR = Path(__file__).resolve().parent

# 平台/题面常见分类名到技能域的归一；只做映射，不改变选择算法。
_DOMAIN_ALIASES = {
    "reversing": "reverse",
    "rev": "reverse",
    "binary": "reverse",
    "stego": "forensics",
    "steganography": "forensics",
    "pwnning": "pwn",
    "pwnable": "pwn",
}


class SkillNotFound(Exception):
    pass


class SkillLoader:
    def __init__(self, skills_dir: Optional[Path] = None):
        self._dir = Path(skills_dir) if skills_dir else _SKILLS_DIR
        self._lock = threading.Lock()
        with (self._dir / "index.json").open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            raise ValueError("skill index must be a list")
        self._index: List[Dict[str, object]] = []
        for item in data:
            if not isinstance(item, dict):
                raise ValueError("skill index entries must be objects")
            name = item.get("name")
            domain = item.get("domain")
            filename = item.get("file")
            if not all(isinstance(x, str) and x for x in (name, domain, filename)):
                raise ValueError("skill index entry missing name/domain/file")
            if Path(filename).name != filename or filename in (".", ".."):
                raise ValueError("skill file must stay inside sections/")
            self._index.append(dict(item))
        self._cache: Dict[str, str] = {}

    def list_index(self) -> List[Dict[str, object]]:
        """索引给规划器看：名称/领域/前置/预估大小/风险，不含正文。"""
        return [dict(name=s["name"], domain=s["domain"], prerequisites=s["prerequisites"],
                     tokens_hint=s["tokens_hint"], risk=s["risk"]) for s in self._index]

    def find(self, name: str) -> Dict[str, object]:
        for s in self._index:
            if s["name"] == name:
                return s
        raise SkillNotFound(name)

    def select(self, domain: Optional[str] = None, query: str = "",
               limit: int = 2) -> List[Dict[str, object]]:
        """按题目领域和关键词打分，只返回最相关的少量技能。

        领域不匹配且名称无关键词命中时返回空，避免把无关技巧塞进 prompt。
        """
        limit = max(0, int(limit))
        if not limit:
            return []
        domain_text = (domain or "").strip().casefold()
        domain_text = _DOMAIN_ALIASES.get(domain_text, domain_text)
        terms = [t for t in re.findall(r"[a-z0-9]+", (query or "").casefold()) if len(t) >= 2]
        scored: List[Tuple[float, int, Dict[str, object]]] = []
        for meta in self._index:
            name = str(meta["name"]).casefold()
            skill_domain = str(meta["domain"]).casefold()
            keywords = " ".join(str(k) for k in (meta.get("keywords") or [])).casefold()
            score = 0.0
            if domain_text and skill_domain == domain_text:
                score += 6.0
            elif domain_text and domain_text in name:
                score += 2.0
            for term in terms:
                if term in name or term in skill_domain:
                    score += 2.0
                elif keywords and term in keywords:
                    score += 3.0
            if score <= 0:
                continue
            # 同等匹配优先更小、更低风险的章节，减少上下文占用。
            hint = int(meta.get("tokens_hint") or 0)
            scored.append((-score, hint, meta))
        scored.sort(key=lambda item: (item[0], item[1], str(item[2]["name"])))
        return [dict(meta) for _, _, meta in scored[:limit]]

    def load_section(self, name: str, max_bytes: int = 20000) -> str:
        """按需加载单节正文；超限报错而非静默截断；重复读取走缓存。"""
        meta = self.find(name)
        with self._lock:
            cached = self._cache.get(name)
        if cached is not None:
            # 缓存也必须重新受调用方字节上限约束，不能让第一次宽松加载
            # 让后续更小预算绕过限制。
            if len(cached.encode("utf-8")) > max_bytes:
                raise ValueError("skill {} cached size exceeds cap {}".format(name, max_bytes))
            return cached
        path = (self._dir / "sections" / str(meta["file"])).resolve()
        sections = (self._dir / "sections").resolve()
        if sections not in path.parents:
            raise ValueError("skill file escaped sections directory")
        data = path.read_bytes()
        if len(data) > max_bytes:
            raise ValueError("skill {} is {} bytes > cap {}".format(name, len(data), max_bytes))
        text = data.decode("utf-8")
        with self._lock:
            self._cache[name] = text
        return text

    def load_for(self, domain: Optional[str] = None, query: str = "", limit: int = 2,
                 max_bytes: int = 20000, total_bytes: int = 40000) -> Tuple[List[Dict[str, object]], List[Dict[str, str]]]:
        """返回 (已加载技能, 跳过原因)；总字节预算由调用方控制。"""
        loaded: List[Dict[str, object]] = []
        skipped: List[Dict[str, str]] = []
        total = 0
        for meta in self.select(domain=domain, query=query, limit=limit):
            try:
                text = self.load_section(str(meta["name"]), max_bytes=max_bytes)
            except (OSError, UnicodeError, ValueError) as exc:
                skipped.append({"name": str(meta["name"]), "reason": type(exc).__name__})
                continue
            size = len(text.encode("utf-8"))
            if total + size > total_bytes:
                skipped.append({"name": str(meta["name"]), "reason": "total_budget"})
                continue
            loaded.append({"name": meta["name"], "domain": meta["domain"],
                           "risk": meta.get("risk"), "text": text})
            total += size
        return loaded, skipped
