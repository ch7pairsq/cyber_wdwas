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
    return "{}-{}".format(prefix, uuid.uuid4().hex[:12])


@dataclass
class Challenge:
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
    id: str
    challenge_id: str
    text: str
    tags: List[str] = field(default_factory=list)
    source_attempt_id: str = ""
    superseded: bool = False


def to_dict(obj: Any) -> Dict[str, Any]:
    return asdict(obj)
