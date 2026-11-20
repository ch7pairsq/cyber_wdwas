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

# 目录只放“方法名/触发词”，不放命令；这些关键词仍保留在索引里供 select 使用，
# 只是生成目录时过滤掉，避免把工具名误当成可执行动作。
_INDEX_TRIGGER_DENY = {
    "7z", "binwalk", "steghide", "zbar", "zbarimg", "unzip", "openssl",
    "gmpy2", "angr", "ghidra", "sage", "exiftool", "pngcheck", "pdftotext",
    "cstool", "objdump", "readelf", "nm", "ropgadget", "checksec",
}

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
    """请求的技能名不在索引中。"""
    pass


class SkillLoader:
    """技能库加载器：索引常驻内存，正文按需读取并缓存（线程安全）。"""
    def __init__(self, skills_dir: Optional[Path] = None):
        """加载并严格校验 index.json（字段齐全、文件名不得逃逸 sections/）。"""
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
        return [dict(name=s["name"], domain=s["domain"],
                     prerequisites=s.get("prerequisites") or [],
                     tokens_hint=s.get("tokens_hint") or 0,
                     risk=s.get("risk") or "unknown",
                     deprecated=bool(s.get("deprecated"))) for s in self._index]

    def index_text(self, max_bytes: int = 600) -> str:
        """生成给 Worker 看的紧凑目录；只读索引元数据，不读 section 正文。

        目录只回答“有哪些方法、何时值得打开”，不包含命令和判据。
        超长时按字节预算截断，始终 fail-open：返回空串而不是抛错。
        """
        try:
            limit = max(0, int(max_bytes))
        except (TypeError, ValueError):
            limit = 600
        if not limit:
            return ""
        grouped: Dict[str, List[str]] = {}
        for meta in self._index:
            if meta.get("deprecated") or str(meta.get("risk") or "") == "deprecated":
                continue
            name = str(meta.get("name") or "")
            domain = str(meta.get("domain") or "")
            if not name or not domain:
                continue
            short = name.split(".", 1)[1] if "." in name else name
            trigger = meta.get("trigger")
            if not isinstance(trigger, str) or not trigger.strip():
                keywords = meta.get("keywords") or []
                if not isinstance(keywords, (list, tuple)):
                    keywords = []
                words = [str(k).strip() for k in keywords if str(k).strip()]
                words = [w for w in words if w.casefold() not in _INDEX_TRIGGER_DENY]
                trigger = "/".join(words[:3])
            trigger = " ".join(str(trigger).split())[:24]
            entry = short + ("(" + trigger + ")" if trigger else "")
            grouped.setdefault(domain, []).append(entry)
        lines = ["{}: {}".format(domain, " | ".join(entries)) for domain, entries in grouped.items()]
        out: List[str] = []
        used = 0
        for line in lines:
            size = len(line.encode("utf-8")) + (1 if out else 0)
            if used + size <= limit:
                out.append(line)
                used += size
                continue
            # 只在一个域名放不下时保留截断标记，避免输出超过预算。
            if not out and limit >= 6:
                cut = line.encode("utf-8")[:limit - 3].decode("utf-8", "ignore")
                out.append(cut + "...")
            elif out and used + 4 <= limit:
                out.append("...")
            break
        return "\n".join(out)

    def find(self, name: str) -> Dict[str, object]:
        """按名称查索引条目；不存在抛 SkillNotFound。"""
        for s in self._index:
            if s["name"] == name:
                return s
        raise SkillNotFound(name)

    @staticmethod
    def _word_pattern(term: str) -> str:
        """词边界匹配：term 必须整词出现，不是更长词的子串。"""
        return r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])"

    def select(self, domain: Optional[str] = None, query: str = "",
               limit: int = 2) -> List[Dict[str, object]]:
        """按题目领域和关键词打分，只返回最相关的少量技能。

        打分规则（2026-11-20 收紧，正式赛 static_file_server 配 php 技能的
        教训）：
        - 词边界匹配：term 必须整词出现（"file" 不命中 zip**file**、
          "app.py" 的碎片 "py" 不命中含 py 的关键词）；
        - 查询词去重：同一词多次出现只计一次分（词频不等于相关性）；
        - 最短 term 长度 3：排除扩展名类碎片词（py、md、io）；
        - 入围门槛 5.0：domain 精确命中（+6）直接入围；泛分类（如 ctf）
          需要两个不同实词命中（3+3）或 name+keyword 同词命中（2+3=5）。
          单关键词弱命中不入围——宁可不出技能，也不塞无关技巧占 prompt。
        """
        limit = max(0, int(limit))
        if not limit:
            return []
        domain_text = (domain or "").strip().casefold()
        domain_text = _DOMAIN_ALIASES.get(domain_text, domain_text)
        terms = list(dict.fromkeys(
            t for t in re.findall(r"[a-z0-9]+", (query or "").casefold())
            if len(t) >= 3))
        scored: List[Tuple[float, int, Dict[str, object]]] = []
        for meta in self._index:
            name = str(meta["name"]).casefold()
            skill_domain = str(meta["domain"]).casefold()
            keywords = " ".join(str(k) for k in (meta.get("keywords") or [])).casefold()
            score = 0.0
            if domain_text and skill_domain == domain_text:
                score += 6.0
            elif domain_text and re.search(self._word_pattern(domain_text), name):
                score += 2.0
            for term in terms:
                if re.search(self._word_pattern(term), name) or \
                        re.search(self._word_pattern(term), skill_domain):
                    score += 2.0
                elif keywords and re.search(self._word_pattern(term), keywords):
                    score += 3.0
            if score < 5.0:
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
