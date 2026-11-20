"""竞赛领域对象与确定性键（设计文档 §7、附录 B.2、D.3）。"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

# Attempt 结果枚举（附录 B.2 Worker 输出契约）
OUTCOME_SUCCESS = "success"
OUTCOME_NO_SIGNAL = "no_signal"
OUTCOME_ERROR = "error"
OUTCOME_TIMEOUT = "timeout"


def canonical_params(params: Dict[str, Any]) -> str:
    """参数规范化：排序、紧凑分隔、统一编码（D.3 要求，否则会漏去重）。"""
    return json.dumps(params or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def build_attempt_key(challenge_id: str, strategy_key: str, params: Dict[str, Any], tool_version: str = "") -> str:
    """attempt_key = sha256(challenge_id | strategy_key | canonical(params) | tool_version)。"""
    raw = "|".join([challenge_id, strategy_key, canonical_params(params), tool_version])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_id(prefix: str) -> str:
    """生成带前缀的短随机 ID（前缀-12位hex），用于意图/尝试等实体标识。"""
    return "{}-{}".format(prefix, uuid.uuid4().hex[:12])


@dataclass
class Challenge:
    """题目领域对象：来自 Harness 的标准化输入（id/分类/描述/附件/难度等）。"""
    id: str
    name: str
    category: str = "unknown"
    description: str = ""
    attachments: tuple = ()
    points_hint: float = 0.0
    difficulty_level: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Attempt:
    """一次尝试的完整记录：策略、前提、动作、结果与证据引用，落库前由契约校验。"""
    id: str
    challenge_id: str
    strategy_key: str
    attempt_key: str
    preconditions: List[str] = field(default_factory=list)
    actions: List[str] = field(default_factory=list)
    outcome: str = OUTCOME_NO_SIGNAL
    failure_reason: str = ""
    ruled_out: List[str] = field(default_factory=list)
    next_best_action: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    idempotency_key: str = ""
    created_at: float = 0.0


@dataclass
class Lesson:
    """经验教训条目：从已完成的尝试中提炼，可被后续题目的上下文检索复用。"""
    id: str
    challenge_id: str
    text: str
    tags: List[str] = field(default_factory=list)
    source_attempt_id: str = ""
    superseded: bool = False


def to_dict(obj: Any) -> Dict[str, Any]:
    """dataclass → dict 的统一序列化入口（落库/交接包用）。"""
    return asdict(obj)
