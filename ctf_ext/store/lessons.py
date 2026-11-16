"""教训蒸馏与检索（§7「一次试错帮到所有同伴」）。"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import List, Optional

from ..domain.models import Lesson, new_id


def distill(attempt_row) -> Optional[Lesson]:
    """从一次尝试提炼一句可复用教训；无信息量时返回 None。"""
    parts: List[str] = []
    if attempt_row["outcome"] in ("success", "no_signal") and attempt_row["ruled_out"]:
        parts.append("已排除：{}".format("、".join(json.loads(attempt_row["ruled_out"]))))
    if attempt_row["failure_reason"]:
        parts.append("失败原因：{}".format(attempt_row["failure_reason"]))
    if attempt_row["next_best_action"]:
        parts.append("下一步最值得尝试：{}".format(attempt_row["next_best_action"]))
    if not parts:
        return None
    return Lesson(
        id=new_id("lesson"),
        challenge_id=attempt_row["challenge_id"],
        text="；".join(parts),
        tags=[attempt_row["strategy_key"].split(".")[0]] if attempt_row["strategy_key"] else [],
        source_attempt_id=attempt_row["id"],
    )


def add_lesson(db: sqlite3.Connection, lesson: Lesson) -> str:
    db.execute(
        "INSERT INTO lessons(id, challenge_id, text, tags, source_attempt_id, superseded, created_at)"
        " VALUES (?,?,?,?,?,0,?)",
        (lesson.id, lesson.challenge_id, lesson.text, json.dumps(lesson.tags),
         lesson.source_attempt_id, time.time()),
    )
    db.commit()
    return lesson.id


def search(db: sqlite3.Connection, query: str, challenge_id: Optional[str] = None, limit: int = 10):
    sql = "SELECT * FROM lessons WHERE superseded=0 AND (text LIKE ? OR tags LIKE ?)"
    like = "%{}%".format(query)
    args: List[str] = [like, like]
    if challenge_id:
        sql += " AND challenge_id=?"
        args.append(challenge_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(str(limit))
    return [dict(r) for r in db.execute(sql, args)]


def supersede(db: sqlite3.Connection, lesson_id: str, reason: str = "") -> None:
    """与新证据冲突时标记失效，保留历史（D.4）。"""
    db.execute(
        "UPDATE lessons SET superseded=1, text=text || ' [superseded: ' || ? || ']' WHERE id=?",
        (reason or "conflict", lesson_id),
    )
    db.commit()
