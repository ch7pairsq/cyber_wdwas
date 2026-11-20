"""教训蒸馏与检索（§7「一次试错帮到所有同伴」）。

只沉淀可复用、可脱敏的经验：成立条件、失败原因、已排除方向、下一步。
FLAG 与候选答案不进入教训；跨题共享只匹配同一领域，避免泄露其他题结论。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional

from ..domain.models import Lesson, new_id

# 宁可过度脱敏：中文紧邻、前后带其他字符的 FLAG 也必须替换，不能因
# Unicode/ASCII 词边界漏掉。占位符本身可重复匹配，不影响幂等。
_FLAG_TEXT = re.compile(r'flag\s*\{[^{}\s]{1,200}\}', re.IGNORECASE)
_FLAG_BRACKET = re.compile(r'flag\s*\[[^\[\]\s]{1,200}\]', re.IGNORECASE)
_MAX_LESSON_CHARS = 600


def sanitize_lesson_text(text: str, limit: int = _MAX_LESSON_CHARS) -> str:
    """脱敏并压缩为单行；宁可少记，也不能把 FLAG 写进共享层。"""
    if not isinstance(text, str):
        return ""
    value = _FLAG_TEXT.sub("flag{redacted}", text)
    value = _FLAG_BRACKET.sub("flag[redacted]", value)
    value = "".join(ch if ch >= " " and ch != "\x7f" else " " for ch in value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def _json_list(value: Any) -> List[str]:
    """把 TEXT 列里的 JSON 数组安全解析为字符串列表；异常返回空列表。"""
    if isinstance(value, list):
        return [str(x) for x in value]
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return [str(x) for x in parsed] if isinstance(parsed, list) else []


def distill(attempt_row, domain: Optional[str] = None) -> Optional[Lesson]:
    """从一次尝试提炼一句可复用教训；无信息量时返回 None。"""
    outcome = attempt_row["outcome"]
    parts: List[str] = []
    preconditions = _json_list(attempt_row["preconditions"])
    ruled_out = _json_list(attempt_row["ruled_out"])
    if preconditions:
        parts.append("成立条件：{}".format("、".join(preconditions[:4])))
    if attempt_row["failure_reason"]:
        parts.append("失败原因：{}".format(attempt_row["failure_reason"]))
    if outcome in ("success", "no_signal") and ruled_out:
        parts.append("已排除：{}".format("、".join(ruled_out[:4])))
    if attempt_row["next_best_action"]:
        parts.append("下一步：{}".format(attempt_row["next_best_action"]))
    text = sanitize_lesson_text("；".join(parts))
    if not text:
        return None
    strategy = str(attempt_row["strategy_key"] or "")
    tags = []
    if domain:
        tags.append("domain:" + sanitize_lesson_text(domain, 40))
    if strategy:
        tags.append(sanitize_lesson_text(strategy.split(".")[0], 40))
    tags.append("outcome:" + sanitize_lesson_text(str(outcome), 40))
    return Lesson(
        id=new_id("lesson"),
        challenge_id=attempt_row["challenge_id"],
        text=text,
        tags=[tag for tag in tags if tag],
        source_attempt_id=attempt_row["id"],
    )


def add_lesson(db: sqlite3.Connection, lesson: Lesson, *, commit: bool = True) -> str:
    """幂等写入；同一来源尝试只保留第一条教训。"""
    if lesson.source_attempt_id:
        row = db.execute("SELECT id FROM lessons WHERE source_attempt_id=? LIMIT 1",
                         (lesson.source_attempt_id,)).fetchone()
        if row:
            return row["id"]
    db.execute(
        "INSERT INTO lessons(id, challenge_id, text, tags, source_attempt_id, superseded, created_at)"
        " VALUES (?,?,?,?,?,0,?)",
        (lesson.id, lesson.challenge_id, lesson.text, json.dumps(lesson.tags),
         lesson.source_attempt_id, time.time()),
    )
    if commit and not db.in_transaction:
        db.commit()
    return lesson.id


def record_attempt(db: sqlite3.Connection, attempt_id: str, domain: Optional[str] = None,
                   *, commit: bool = True) -> Optional[str]:
    """把一次尝试蒸馏入库；失败不影响主结果。"""
    row = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
    if row is None:
        return None
    lesson = distill(row, domain=domain)
    if lesson is None:
        return None
    return add_lesson(db, lesson, commit=commit)


def _domain_pattern(domain: str) -> str:
    """构造匹配 tags 列 domain:xxx 标签的 LIKE 模式（JSON 转义后带通配符）。"""
    return "%" + json.dumps("domain:" + sanitize_lesson_text(domain, 40))[1:-1] + "%"


def search(db: sqlite3.Connection, query: str = "", challenge_id: Optional[str] = None,
           domain: Optional[str] = None, limit: int = 10):
    """按题目/领域/关键词检索未失效教训；最多返回 100 条，按时间倒序。"""
    sql = "SELECT * FROM lessons WHERE superseded=0"
    args: List[Any] = []
    if challenge_id:
        sql += " AND challenge_id=?"
        args.append(challenge_id)
    if domain:
        sql += " AND tags LIKE ?"
        args.append(_domain_pattern(domain))
    if query:
        like = "%{}%".format(sanitize_lesson_text(query, 120))
        sql += " AND (text LIKE ? OR tags LIKE ?)"
        args.extend([like, like])
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(max(0, min(int(limit), 100)))
    return [dict(r) for r in db.execute(sql, args)]


def lesson_context(db: sqlite3.Connection, challenge_id: str, domain: Optional[str] = None,
                   limit: int = 6) -> List[Dict[str, Any]]:
    """本题教训 + 同领域其他题教训；不返回来源题号或候选答案。"""
    limit = max(0, min(int(limit), 20))
    if not limit:
        return []
    if domain:
        rows = db.execute(
            "SELECT text, tags FROM lessons WHERE superseded=0 AND (challenge_id=? OR tags LIKE ?)"
            " ORDER BY CASE WHEN challenge_id=? THEN 0 ELSE 1 END, created_at DESC LIMIT ?",
            (challenge_id, _domain_pattern(domain), challenge_id, limit),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT text, tags FROM lessons WHERE superseded=0 AND challenge_id=?"
            " ORDER BY created_at DESC LIMIT ?", (challenge_id, limit),
        ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        text = sanitize_lesson_text(row["text"])
        if not text:
            continue
        out.append({"text": text, "tags": _json_list(row["tags"])})
    return out


def supersede(db: sqlite3.Connection, lesson_id: str, reason: str = "") -> None:
    """与新证据冲突时标记失效，保留历史（D.4）。"""
    db.execute(
        "UPDATE lessons SET superseded=1, text=text || ' [superseded: ' || ? || ']' WHERE id=?",
        (sanitize_lesson_text(reason) or "conflict", lesson_id),
    )
    if not db.in_transaction:
        db.commit()
