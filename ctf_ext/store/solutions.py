"""解题思路记忆（平台确认解出 → 蒸馏整题思路 → 同题/同域检索注入）。

与 lessons（单次尝试微教训）互补：这里记录的是"整题怎么解出来的"可回放路径。
边界与 lessons 一致：FLAG 与候选答案一律脱敏；跨题共享只匹配同一领域，
避免泄露其他题结论。写入只在平台回执 ACCEPTED 后发生，检索 fail-open。
"""
from __future__ import annotations

import json
import posixpath
import re
import time
from typing import Any, Dict, List, Optional

from ..domain.models import new_id
from .lessons import sanitize_lesson_text

_MAX_APPROACH_CHARS = 2000
_MAX_ROWS = 500

# 平台/题面分类名归一（与 skills.loader 保持一致的别名口径，避免跨模块私用）。
_DOMAIN_ALIASES = {
    "reversing": "reverse",
    "rev": "reverse",
    "binary": "reverse",
    "stego": "forensics",
    "steganography": "forensics",
    "pwnning": "pwn",
    "pwnable": "pwn",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _json_list(value: Any) -> List[str]:
    """TEXT 列 JSON 数组安全解析；异常返回空列表。"""
    if isinstance(value, list):
        return [str(x) for x in value]
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return [str(x) for x in parsed] if isinstance(parsed, list) else []


def _terms(text: str) -> List[str]:
    """题名/描述 → 小写检索词（≥2 字符），保持出现顺序去重。"""
    seen: List[str] = []
    for token in _TOKEN_RE.findall((text or "").casefold()):
        if len(token) >= 2 and token not in seen:
            seen.append(token)
    return seen[:24]


def _command_basename(argv_raw: str) -> str:
    """argv JSON → 首元素 basename；解析失败返回空串。"""
    try:
        argv = json.loads(argv_raw) if isinstance(argv_raw, str) else (argv_raw or [])
    except (TypeError, ValueError):
        return ""
    if not isinstance(argv, list) or not argv:
        return ""
    return posixpath.basename(str(argv[0]).strip()) if str(argv[0]).strip() else ""


def _winning_attempt(db, challenge_id: str, source_submission: str):
    """优先取与接受提交幂等键对应的尝试，否则取最近一次 success 尝试。"""
    row = db.execute(
        "SELECT * FROM attempts WHERE challenge_id=? AND idempotency_key=?"
        " ORDER BY created_at DESC LIMIT 1", (challenge_id, source_submission)).fetchone()
    if row is not None:
        return row
    return db.execute(
        "SELECT * FROM attempts WHERE challenge_id=? AND outcome='success'"
        " ORDER BY created_at DESC LIMIT 1", (challenge_id,)).fetchone()


def _key_commands(db, challenge_id: str, evidence_ids: List[str]) -> List[str]:
    """提交引用证据对应的完整成功命令 basename 链；缺失时回退本题最近命令。"""
    commands: List[str] = []
    quoted_ids = [str(e) for e in evidence_ids[:20] if str(e)]
    if quoted_ids:
        placeholders = ",".join("?" * len(quoted_ids))
        rows = db.execute(
            "SELECT argv FROM command_log WHERE challenge_id=? AND complete=1"
            " AND evidence_id IN ({}) ORDER BY created_at".format(placeholders),
            (challenge_id, *quoted_ids)).fetchall()
        commands = [_command_basename(r["argv"]) for r in rows]
    if not any(commands):
        rows = db.execute(
            "SELECT argv FROM command_log WHERE challenge_id=? AND complete=1"
            " ORDER BY created_at DESC LIMIT 12", (challenge_id,)).fetchall()
        commands = [_command_basename(r["argv"]) for r in reversed(rows)]
    ordered: List[str] = []
    for name in commands:
        if name and name not in ordered:
            ordered.append(name)
    return ordered[:8]


def distill_solution(db, challenge_id: str, submission: Dict[str, Any]) -> Dict[str, Any]:
    """从挑战行 + 获胜尝试 + 命令链 + 本题教训蒸馏一份可回放思路。"""
    challenge = db.execute(
        "SELECT name, category, description FROM challenges WHERE id=?",
        (challenge_id,)).fetchone()
    name = str(challenge["name"]) if challenge else ""
    domain_raw = str(challenge["category"]) if challenge else "unknown"
    domain = _DOMAIN_ALIASES.get(domain_raw.strip().casefold(), domain_raw.strip().casefold()) or "unknown"

    source_submission = str(submission.get("idempotency_key") or "")
    attempt = _winning_attempt(db, challenge_id, source_submission)

    parts: List[str] = []
    title = ""
    strategy = str(attempt["strategy_key"]) if attempt else ""
    if attempt is not None:
        title = strategy.split(".")[-1] if strategy else ""
        if strategy:
            parts.append("路线：" + sanitize_lesson_text(strategy, 80))
        preconditions = _json_list(attempt["preconditions"])
        if preconditions:
            parts.append("成立条件：" + "、".join(
                sanitize_lesson_text(item, 60) for item in preconditions[:4]))
        actions = _json_list(attempt["actions"])
        if actions:
            parts.append("步骤：" + " → ".join(
                sanitize_lesson_text(item, 60) for item in actions[:6]))
        ruled_out = _json_list(attempt["ruled_out"])
        if ruled_out:
            parts.append("已排除：" + "、".join(
                sanitize_lesson_text(item, 60) for item in ruled_out[:4]))
    commands = _key_commands(db, challenge_id, _json_list(submission.get("evidence_ids")))
    if commands:
        parts.append("关键命令：" + " → ".join(commands))
    lesson_rows = db.execute(
        "SELECT text FROM lessons WHERE challenge_id=? AND superseded=0"
        " ORDER BY created_at DESC LIMIT 3", (challenge_id,)).fetchall()
    if lesson_rows:
        parts.append("教训：" + "；".join(
            sanitize_lesson_text(r["text"], 120) for r in lesson_rows))
    approach = sanitize_lesson_text("；".join(parts), _MAX_APPROACH_CHARS)
    if not approach:
        approach = "平台确认解出；本轮无详细路径记录，参考内置技能与工具白名单从头分析。"
    if not title:
        title = domain

    keywords = _terms(" ".join([name, str(challenge["description"]) if challenge else "",
                                domain_raw, strategy, " ".join(commands)]))
    return {
        "id": new_id("solu"),
        "challenge_id": challenge_id,
        "challenge_name": sanitize_lesson_text(name, 120),
        "domain": domain,
        "title": sanitize_lesson_text(title, 80) or "solved",
        "approach": approach,
        "key_commands": commands,
        "keywords": keywords,
        "source_submission": source_submission,
    }


def record_solution(db, challenge_id: str, submission: Dict[str, Any], *,
                    commit: bool = True) -> Optional[str]:
    """幂等记录：同一接受提交只写一次；同题重解则更新思路保留复用计数。"""
    if not isinstance(submission, dict):
        return None
    source_submission = str(submission.get("idempotency_key") or "")
    if not challenge_id or not source_submission:
        return None
    row = db.execute("SELECT id FROM solutions WHERE source_submission=?",
                     (source_submission,)).fetchone()
    if row is not None:
        return row["id"]
    distilled = distill_solution(db, challenge_id, submission)
    now = time.time()
    existing = db.execute("SELECT id FROM solutions WHERE challenge_id=?",
                          (challenge_id,)).fetchone()
    if existing is not None:
        db.execute(
            "UPDATE solutions SET challenge_name=?, domain=?, title=?, approach=?,"
            " key_commands=?, keywords=?, source_submission=? WHERE id=?",
            (distilled["challenge_name"], distilled["domain"], distilled["title"],
             distilled["approach"], json.dumps(distilled["key_commands"], ensure_ascii=False),
             json.dumps(distilled["keywords"], ensure_ascii=False),
             source_submission, existing["id"]))
        solution_id = existing["id"]
    else:
        solution_id = distilled["id"]
        db.execute(
            "INSERT INTO solutions(id, challenge_id, challenge_name, domain, title,"
            " approach, key_commands, keywords, source_submission, reuse_count,"
            " last_used_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,0,NULL,?)",
            (solution_id, challenge_id, distilled["challenge_name"], distilled["domain"],
             distilled["title"], distilled["approach"],
             json.dumps(distilled["key_commands"], ensure_ascii=False),
             json.dumps(distilled["keywords"], ensure_ascii=False),
             source_submission, now))
    if commit and not db.in_transaction:
        db.commit()
    return solution_id


def search_solutions(db, domain: str, query: str, challenge_id: Optional[str] = None,
                     limit: int = 2) -> List[Dict[str, Any]]:
    """同题精确命中优先，其次同域 + 题名/关键词打分；跨域不返回。"""
    limit = max(0, min(int(limit), 10))
    if not limit:
        return []
    domain_text = _DOMAIN_ALIASES.get((domain or "").strip().casefold(),
                                      (domain or "").strip().casefold())
    terms = _terms(query)
    rows = db.execute(
        "SELECT * FROM solutions ORDER BY created_at DESC LIMIT ?", (_MAX_ROWS,)).fetchall()
    scored: List[tuple] = []
    for row in rows:
        same_challenge = bool(challenge_id and row["challenge_id"] == challenge_id)
        row_domain = str(row["domain"] or "").casefold()
        # 跨域且非同题：宁可空缺也不注入无关思路（关键词命中也不放宽，
        # 与 lessons"跨题共享只匹配同一领域"口径一致）。
        if not same_challenge and (not domain_text or row_domain != domain_text):
            continue
        score = 0.0
        if same_challenge:
            score += 10.0
        else:
            score += 6.0
        keywords = " ".join(_json_list(row["keywords"])).casefold()
        title = str(row["title"] or "").casefold()
        for term in terms:
            if term in title or term in str(row["challenge_name"] or "").casefold():
                score += 3.0
            elif term in keywords:
                score += 2.0
        scored.append((-score, -int(row["reuse_count"] or 0), row))
    scored.sort(key=lambda item: (item[0], item[1], str(item[2]["id"])))
    return [dict(row) for _, _, row in scored[:limit]]


def solutions_context(db, domain: str, query: str, challenge_id: Optional[str] = None,
                      limit: int = 2, *, bump: bool = True) -> List[Dict[str, Any]]:
    """检索并格式化为 prompt 注入条目；命中时复用计数 +1（失败不影响返回）。"""
    rows = search_solutions(db, domain, query, challenge_id=challenge_id, limit=limit)
    entries: List[Dict[str, Any]] = []
    for row in rows:
        entries.append({
            "title": row["title"],
            "domain": row["domain"],
            "approach": row["approach"],
            "key_commands": _json_list(row["key_commands"]),
            "reuse_count": int(row["reuse_count"] or 0),
        })
    if bump and entries:
        try:
            placeholders = ",".join("?" * len(rows))
            db.execute(
                "UPDATE solutions SET reuse_count=reuse_count+1, last_used_at=?"
                " WHERE id IN ({})".format(placeholders),
                (time.time(), *[r["id"] for r in rows]))
            if not db.in_transaction:
                db.commit()
        except Exception:
            pass  # 计数失败不影响注入
    return entries
